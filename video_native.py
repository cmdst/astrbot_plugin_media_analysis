"""原生视频通道：内容块形状 + 三条护栏（SPEC-v0.3 §7.2 / §16.1-R1 / §10.2）。

**为什么必须碰核心内部面 `provider.client`**（这一段的证据在 SPEC-v0.3 §1.2，都是实测的）：
- `text_chat` 的形参只有 `image_urls` / `audio_urls`，**没有** `video_urls`
  （`astrbot/core/provider/provider.py:100-115`）；
- 核心 `ContentPart` 家族没有 video 变体（`astrbot/core/agent/message.py:19,80,112,128`）；
- `assemble_context` 对未知内容块**直接抛** `ValueError("不支持的额外内容块类型…")`
  （`openai_source.py:1424`，t8 实跑复现）；
- `modalities` 的枚举里**没有** `video`（`config/default.py:2246-2255`）⇒ 没有任何"勾选视频"
  的声明式判据，视频能力只能用"调用一次且答对"（`probe.py` 的 L1）来证。

于是唯一的入口是 provider 底层的 `AsyncOpenAI` 实例（`openai_source.py:382`）：
`client.chat.completions.create(...)` 不过 `assemble_context`，任意内容块都能直发。

**它是核心实现细节、不是 `astrbot.api` 公开 API**，所以三条护栏是硬要求而不是建议：

1. 全程 `getattr` 取，**不 import `openai` 的任何类型**（核心换 SDK 也炸不到导入期）；
2. 任何异常（`AttributeError` / `TypeError` / SDK 异常 / 连接错）一律收成
   `NativeOutcome(ok=False)`，由调用方映射 `video_native_failed` ⇒ **回退**，绝不外抛；
3. 缺失 / 形状不符 ⇒ `NativeOutcome(ok=False, note="video_native_unavailable")` ⇒ 回退。

落地成一句话：**这条链路坏了只能是"少一条路"，不能是"插件坏了"或"插件像没装"**
（v0.2 那次静默拒载的教训，`AGENTS.md` §3）。
"""

from __future__ import annotations

import asyncio
import base64
import time

from astrbot.api import logger as LOG

from . import media

#: 唯一验过的适配器类型（SPEC-v0.3 §2.2-④）：t8 的两条通道都是它。
ADAPTER_TYPE = "openai_chat_completion"

#: t8 唯一验过的内容块形状；`image_url` 只换 `type`/键名，不要混用（SPEC-v0.3 §7.2）。
VIDEO_BLOCK_TYPE = "video_url"
DATA_URL_PREFIX = "data:video/mp4;base64,"

#: `max_tokens` 的**生效值**（v0.5.1 起是代码常量：配置键 `video.native_max_tokens` 已删除）。
#:
#: 为什么不再让运维填：这个旋钮只有"只能调大"一个语义，而 mimo 的 4096 需求
#: （`SPEC-v0.3` §7.2 护栏①：8 MiB + 长提示词在 1024/2048 下会 200 但正文为空）是**唯一**
#: 被实测过的档位 ⇒ 留着等于把一个必然要调到 4096 的旋钮交给用户
#: （SPEC-v0.5.1 §2.2-B 的 D-1，已由 captain 确认不可否决）。
NATIVE_MAX_TOKENS = 4096

#: `max_tokens` 硬下限（§7.2 护栏①）：mimo 在 64 时 **200 但 `content` 空**
#: （全被 reasoning 吃掉，`MEASURE §2.2`）⇒ 只看状态码会把"没看见"记成成功。
#: v0.5.1 起它**只是下界断言**（`tokens = max(FLOOR, max_tokens)`），配置面不再暴露。
MAX_TOKENS_FLOOR = 1024

#: 直送的**内存**硬上限（§10.2-③）：与 `video.max_direct_mb` 的钳位上限同值（32 MB）。
#: 32 MiB 时 Python 侧 base64 峰值 ≈42.6 MiB、总峰值 ≤160 MiB。
HARD_MAX_DIRECT_BYTES = 32 * 1024 * 1024

NOTE_OK = "video_native"
NOTE_EMPTY = "video_native_empty"
NOTE_FAILED = "video_native_failed"
NOTE_UNAVAILABLE = "video_native_unavailable"


class NativeOutcome:
    """一次原生调用的结果（`ok=False` 时 `note` 决定回退链上记哪一条）。

    `detail` **只进日志**：不提路径、不提异常正文（`envelope` 的常量纪律同理，
    信封会被框架全量写进生产日志）。
    """

    __slots__ = ("ok", "text", "note", "detail", "elapsed_ms")

    def __init__(
        self,
        *,
        ok: bool,
        text: str = "",
        note: str = "",
        detail: str = "",
        elapsed_ms: int = 0,
    ) -> None:
        self.ok = bool(ok)
        self.text = text
        self.note = note
        self.detail = detail
        self.elapsed_ms = int(elapsed_ms)

    def __repr__(self) -> str:  # pragma: no cover - 只为日志/断言可读
        return (
            f"NativeOutcome(ok={self.ok}, chars={len(self.text)}, "
            f"note={self.note!r}, detail={self.detail!r}, elapsed_ms={self.elapsed_ms})"
        )


def _attr_of(obj: object, name: str) -> object:
    """`getattr` 的**兜底版**：属性访问本身抛错（property 里炸了）也只返回 `None`。

    为什么需要（评审 F10）：`_create_of` 原来只防 `None`，可 `provider.client` 完全可能是
    一个**会抛异常的 property**（核心内部面换实现 / 懒加载失败 / 适配器自定义）——
    那时异常会一路冒到工具门面，被收成 `INTERNAL_ERROR`，看起来像"插件坏了"。
    这类失败的真实语义只有一种：**这条通道这次用不了**（⇒ `video_native_unavailable`）。
    """
    try:
        return getattr(obj, name, None)
    except Exception as exc:  # noqa: BLE001 —— 属性访问失败＝没有这个能力
        LOG.debug(
            "media_analysis: 取 provider.%s 失败（%s），按无原生通道处理",
            name,
            type(exc).__name__,
        )
        return None


def channel_reason(provider: object) -> str:
    """这个 provider 能不能走原生视频通道：`""` 表示能。

    返回码（**都不是错误码**，只决定 note 与是否回退）：
    - `"adapter_not_openai"`：适配器类型不是 `openai_chat_completion`
      （用户在面板上选了别的适配器 —— 不是配置错误，回退即可，§2.2-④）；
    - `"no_client"`：实例上没有可用 `client.chat.completions.create`
      （核心内部面搬家 / 换了 SDK 形状，§2.2-⑤）。

    刻意**不读** `modalities`（核心没有 `video` 选项，读了恒 False，§16.2-2）。
    """
    config = _attr_of(provider, "provider_config")
    if not isinstance(config, dict):
        return "adapter_not_openai"
    adapter = config.get("type")
    if adapter != ADAPTER_TYPE:
        return "adapter_not_openai"
    if _create_of(provider) is None:
        return "no_client"
    return ""


def _create_of(provider: object):
    """`client.chat.completions.create`（拿不到回 `None`）；全程兜底 `getattr`，不 import SDK。

    评审 F10：链路上**任何一次属性访问都可能抛**（`provider.client` 是 property，
    `client.chat` / `.completions` / `.create` 也可能是），所以每一层都用 `_attr_of` 兜住
    —— 拿不到就是"没有原生通道"，绝不让它变成 `INTERNAL_ERROR`。
    """
    client = _attr_of(provider, "client")
    if client is None:
        return None
    completions = _attr_of(_attr_of(client, "chat"), "completions")
    create = _attr_of(completions, "create")
    return create if callable(create) else None


def model_of(provider: object) -> str:
    """模型名：先 `get_model()`，再 `provider_config["model"]`，最后 `"unknown"`。"""
    getter = _attr_of(provider, "get_model")
    if callable(getter):
        try:
            value = getter()
        except Exception:  # noqa: BLE001 —— 取模型名失败不该让整条链失败
            value = None
        if isinstance(value, str) and value:
            return value
    config = _attr_of(provider, "provider_config")
    if isinstance(config, dict):
        value = config.get("model")
        if isinstance(value, str) and value:
            return value
    return "unknown"


def build_messages(prompt: str, data_url: str) -> list[dict[str, object]]:
    """§7.2 冻结的 messages：一个 text 块 + 一个 `video_url` 块（只这两个）。"""
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": VIDEO_BLOCK_TYPE,
                    "video_url": {"url": data_url},
                },
            ],
        }
    ]


async def call_native(
    provider: object,
    *,
    prompt: str,
    video_path: str,
    max_tokens: int = NATIVE_MAX_TOKENS,
    timeout: float,
) -> NativeOutcome:
    """把一段本地 mp4 以内联 base64 直送多模态模型，返回 `NativeOutcome`。

    顺序（§10.2 内存护栏，全部在"读文件"之前）：
    `stat` 取 size → 超 `HARD_MAX_DIRECT_BYTES` 直接失败 → 读字节 → base64 → **立即
    `del raw`** → 组 messages → 一次 `client.chat.completions.create`。

    `timeout` 由调用方算好（`min(NATIVE_TIMEOUT_S, remaining - NATIVE_RESERVE_S)`）：
    **底层调用绕过了核心的超时与重试**（`retry_provider_request` 不在链上），
    所以这里必须自建 `asyncio.wait_for`（§7.2 护栏③）。
    """
    try:
        create = _create_of(provider)
    except Exception as exc:  # noqa: BLE001 —— 兜底（`_attr_of` 已挡，双保险）
        return NativeOutcome(
            ok=False, note=NOTE_UNAVAILABLE, detail=type(exc).__name__
        )
    if create is None:
        return NativeOutcome(ok=False, note=NOTE_UNAVAILABLE, detail="no client")

    started = time.monotonic()
    size = media._size_of(video_path)
    if not isinstance(size, int) or size <= 0:
        return NativeOutcome(ok=False, note=NOTE_FAILED, detail="stat")
    if size > HARD_MAX_DIRECT_BYTES:
        # 内存硬闸：调用方本应先按 `video.max_direct_mb`（×1048576）拦下（§5.1-②/§10.2-③）
        return NativeOutcome(
            ok=False, note=NOTE_FAILED, detail=f"too_large {size}"
        )

    try:
        raw = await asyncio.to_thread(media.read_bytes, video_path)
    except OSError as exc:
        return NativeOutcome(ok=False, note=NOTE_FAILED, detail=f"read {type(exc).__name__}")
    try:
        encoded = base64.b64encode(raw).decode("ascii")
    finally:
        del raw  # 峰值 ≈3.7N 而非 5N（§10.1，不保留第二份原始字节）
    data_url = DATA_URL_PREFIX + encoded
    del encoded
    messages = build_messages(prompt, data_url)

    tokens = max(MAX_TOKENS_FLOOR, int(max_tokens))
    budget = max(0.1, float(timeout))
    try:
        completion = await asyncio.wait_for(
            create(
                model=model_of(provider),
                messages=messages,
                max_tokens=tokens,
                stream=False,
            ),
            timeout=budget,
        )
    except (asyncio.TimeoutError, TimeoutError):
        return NativeOutcome(
            ok=False, note=NOTE_FAILED, detail=f"timeout {budget:.1f}s"
        )
    except Exception as exc:  # noqa: BLE001 —— SDK 侧什么都可能抛；一律回退
        LOG.warning(
            "media_analysis: 原生视频调用失败（%s），已回退", type(exc).__name__
        )
        return NativeOutcome(ok=False, note=NOTE_FAILED, detail=type(exc).__name__)

    elapsed_ms = int((time.monotonic() - started) * 1000)
    text = completion_text(completion)
    if not text:
        # 护栏②：200 但空文本 ≠ 看见（mimo 的 reasoning 陷阱，§1.1-5）
        reasoning = _reasoning_chars(completion)
        return NativeOutcome(
            ok=False,
            note=NOTE_EMPTY,
            detail=f"empty content reasoning_chars={reasoning}",
            elapsed_ms=elapsed_ms,
        )
    return NativeOutcome(
        ok=True, text=text, note=NOTE_OK, elapsed_ms=elapsed_ms
    )


def completion_text(completion: object) -> str:
    """从 completion 里取正文：`choices[0].message.content`（str 或 part 列表）。

    任何形状不符 → `""`（调用方据此判 `video_native_empty`）。
    `reasoning_content` **不进信封**（§7.2 护栏②：只用于判定"是否真失败"）。
    """
    choices = getattr(completion, "choices", None)
    if not isinstance(choices, (list, tuple)) or not choices:
        return ""
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, (list, tuple)):
        parts: list[str] = []
        for part in content:
            text = part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
            if isinstance(text, str):
                parts.append(text)
        return "".join(parts).strip()
    return ""


def _reasoning_chars(completion: object) -> int:
    """空文本时看一眼 reasoning 长度（**只进日志**，帮人区分"没看见"与"预算被推理吃掉"）。"""
    choices = getattr(completion, "choices", None)
    if not isinstance(choices, (list, tuple)) or not choices:
        return 0
    message = getattr(choices[0], "message", None)
    for name in ("reasoning_content", "reasoning"):
        value = getattr(message, name, None)
        if isinstance(value, str):
            return len(value)
    return 0
