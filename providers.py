"""provider 解析阶梯 + 能力判定（SPEC-v0.2 §2.4 / §2.5）。

两个同名 `get_provider_by_id` 的坑（本模块的注释就是为此写的）：
`Context.get_provider_by_id` 是**同步**（`star/context.py:447-470`，插件只拿得到它），
`ProviderManager.get_provider_by_id` 是**异步**（`provider/manager.py:221-223`）。
规格统一用 `Context` 的那个，**不要 `await`**。

v0.2 的两处变更（SPEC-v0.2 §2.5）：
1. **删掉 STT 链路**（`resolve_stt_provider` + 五级阶梯 + `STTProvider` 依赖）——
   音频改走多模态对话模型 `text_chat(audio_urls=[…])`，新增 `resolve_audio_provider`。
2. **"配置错"与"缺配置"分成两个码**：显式配了但不符合 ⇒ `PROVIDER_CAPABILITY_MISMATCH`；
   没配且回落到的核心当前对话 provider 也不符合 ⇒ `AUDIO_UNAVAILABLE` / `VISION_UNAVAILABLE`。
   （v0.1 把后者也判 `PROVIDER_CAPABILITY_MISMATCH`，hint 却让人去勾复选框 —— 真实病因是
   "你根本没指定"。这是对 `SPEC.md` §5 两行判据的**覆盖**。）
"""

from __future__ import annotations

from astrbot.api import logger as LOG
from astrbot.api.provider import Provider

from . import envelope

# `modalities` 的可选值由核心定义：["text","image","audio","tool_use"]
# （`config/default.py:2246-2255`）。
MODALITY_IMAGE = "image"
MODALITY_AUDIO = "audio"


def supports_modality(provider: object, modality: str) -> bool:
    """严格口径（SPEC-v0.2 §2.4 裁决：继续严格）：`modalities is None` ⇒ 不支持。

    与核心 `astr_main_agent.py:1412-1417` 的 `_provider_supports_modality` 同口径：
    迁移遗留的**空列表**按"未配置"处理（算支持），`None`/缺省算不支持。
    已知核心内部不一致：`agent/runners/tool_loop_agent_runner.py:1109-1113` 用宽松口径
    （空/None 都算支持）；本插件取严格一侧。

    为什么必须严格：`_resolve_audio_part` 在取件失败时**静默丢音频**
    （`openai_source.py:247-268` / `gemini_source.py:1092-1112`：`except Exception` 后
    `return None`，请求照发、模型照答）⇒ 跟着宽松走会把"音频根本没发出去"记成 `ok:true`。
    """
    config = getattr(provider, "provider_config", None)
    if not isinstance(config, dict):
        return False
    modalities = config.get("modalities", None)
    if isinstance(modalities, list) and not modalities:
        return True
    return isinstance(modalities, list) and modality in modalities


def provider_id_of(provider: object) -> str:
    """取 provider 的配置 id，用于 `notes` 溯源；取不到就回 `"unknown"`。

    provider id 是**配置值不是凭据**（核心自己也这么打日志，`context.py:464-470`）。
    """
    config = getattr(provider, "provider_config", None)
    if isinstance(config, dict):
        value = config.get("id")
        if isinstance(value, str) and value:
            return value
    return "unknown"


async def resolve_audio_provider(context, provider_id: str, umo: str) -> Provider:
    """音频（多模态）provider 解析（SPEC-v0.2 §2.5 冻结阶梯）。

    ```
    ① audio_provider_id 非空：
         get_provider_by_id → None                      ⇒ PROVIDER_NOT_FOUND
         不是 Provider（例如把 STT provider 填进来）      ⇒ PROVIDER_CAPABILITY_MISMATCH
         modalities 不含 audio                          ⇒ PROVIDER_CAPABILITY_MISMATCH
    ② 空 ⇒ 回落核心当前对话 provider：
         拿不到 / 不是 Provider / 不含 audio            ⇒ AUDIO_UNAVAILABLE
    ```
    """
    if provider_id:
        prov = context.get_provider_by_id(provider_id)  # 同步，别 await
        if prov is None:
            raise envelope.ToolError(
                envelope.ERR_PROVIDER_NOT_FOUND,
                log_detail=f"audio_provider_id={provider_id}",
            )
        if not isinstance(prov, Provider):
            raise envelope.ToolError(
                envelope.ERR_PROVIDER_CAPABILITY_MISMATCH,
                log_detail=(
                    f"audio_provider_id={provider_id} type={type(prov).__name__}"
                ),
            )
        if not supports_modality(prov, MODALITY_AUDIO):
            raise envelope.ToolError(
                envelope.ERR_PROVIDER_CAPABILITY_MISMATCH,
                log_detail=(
                    f"audio_provider_id={provider_id} modalities 不含 audio"
                ),
            )
        return prov

    prov = await _using_chat_provider(context, umo)
    if not isinstance(prov, Provider) or not supports_modality(prov, MODALITY_AUDIO):
        raise envelope.ToolError(
            envelope.ERR_AUDIO_UNAVAILABLE,
            log_detail=(
                "no audio_provider_id and the current chat provider "
                f"({'missing' if not isinstance(prov, Provider) else 'no audio modality'})"
            ),
        )
    return prov


async def resolve_chat_provider(
    context,
    provider_id: str,
    umo: str,
    *,
    require_image: bool,
) -> Provider:
    """对话 provider 解析：配了 id 就用它，空串回落核心当前对话 provider。

    `require_image=True` 时追加严格的能力判定（`modalities` 必须含 `image`），
    否则只要求是 `Provider`（摘要阶段是纯文本调用，不该被图像能力挡住）。

    ★ 与 v0.1 的差别：**回落**到的 provider 不合格时判 `*_UNAVAILABLE`（缺配置），
    只有**显式配了**却不符合才判 `PROVIDER_CAPABILITY_MISMATCH`（配错了）。
    """
    if provider_id:
        prov = context.get_provider_by_id(provider_id)  # 同步，别 await
        if prov is None:
            raise envelope.ToolError(
                envelope.ERR_PROVIDER_NOT_FOUND,
                log_detail=f"vision_provider_id={provider_id}",
            )
        if not isinstance(prov, Provider):
            raise envelope.ToolError(
                envelope.ERR_PROVIDER_CAPABILITY_MISMATCH,
                log_detail=(
                    f"vision_provider_id={provider_id} type={type(prov).__name__}"
                ),
            )
        if require_image and not supports_modality(prov, MODALITY_IMAGE):
            raise envelope.ToolError(
                envelope.ERR_PROVIDER_CAPABILITY_MISMATCH,
                log_detail=f"vision_provider_id={provider_id} modalities 不含 image",
            )
        return prov

    prov = await _using_chat_provider(context, umo)
    if not isinstance(prov, Provider):
        raise envelope.ToolError(envelope.ERR_VISION_UNAVAILABLE)
    if require_image and not supports_modality(prov, MODALITY_IMAGE):
        raise envelope.ToolError(
            envelope.ERR_VISION_UNAVAILABLE,
            log_detail=f"provider={provider_id_of(prov)} modalities 不含 image",
        )
    return prov


async def resolve_vision_provider(context, provider_id: str, umo: str) -> Provider:
    """读图专用入口 = `resolve_chat_provider(..., require_image=True)`。"""
    return await resolve_chat_provider(
        context, provider_id, umo, require_image=True
    )


async def _using_chat_provider(context, umo: str) -> object:
    """`context.get_using_provider_async(umo)`，类型不符的 `ValueError` 吞成 `None`。

    核心在"解析出的 provider 不是文本生成类型"时会抛 `ValueError`
    （`provider/manager.py:245-258`）—— 那是"没有可用对话模型"，不是内部错误。
    """
    try:
        return await context.get_using_provider_async(umo)
    except ValueError as exc:
        LOG.warning(
            "media_analysis: get_using_provider_async 类型不符（%s）",
            type(exc).__name__,
        )
        return None
    except Exception as exc:  # noqa: BLE001 —— 阶梯不许把异常漏给模型
        LOG.warning(
            "media_analysis: get_using_provider_async 失败（%s）",
            type(exc).__name__,
        )
        return None
