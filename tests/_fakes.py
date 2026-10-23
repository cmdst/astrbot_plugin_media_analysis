"""假 provider / 假 event / 假 context —— 全部**零网络、零真实 provider、零真实群媒体**。

设计口径（每条都对应 SPEC 的一处冻结契约）：

- **假 provider 继承真 ABC**：`FakeChatProvider(Provider)` / `FakeSTTProvider(STTProvider)`
  继承的是 `astrbot.core.provider.provider` 里那两个类（经 `astrbot.api.provider` 再导出）。
  这样插件里的 `isinstance(prov, Provider)`（v0.2：音频位与视觉位都要求是**对话** provider）
  在测试里走的是**同一套类**，而不是"因为桩长得像所以过"。
  `FakeSTTProvider` 在 v0.2 里只用于**反例**：把 STT provider 填进音频位必须判
  `PROVIDER_CAPABILITY_MISMATCH`（`STTProvider` 不是 `Provider`）。
- **`provider.provider_config` 是能力判定的唯一读点**：`supports_modality()` 读
  `provider.provider_config.get("modalities")`（SPEC §2.2）。所以假 provider 的构造参数
  就是 `modalities`，三态（缺省 / `[]` / `["text"]`）都能表达。
- **假 event 一律 duck-typed**：SPEC §9-4 明确「测试用 fake event **没有**
  `track_temporary_local_file`」，因此代码必须用 `getattr(..., None)` 兜底；为了能断言
  「产物登记走了另一条路」，这里另给 `TrackingFakeEvent` 子类，**默认不给**方法。
- **假 context 的 `get_provider_by_id` 是同步的**（`star/context.py:447-470`）。它返回普通
  对象，所以插件若误写成 `await context.get_provider_by_id(...)` 会直接 `TypeError`
  —— 那个最贵的坑（同名方法在 `ProviderManager` 上是异步、`manager.py:221-223`）由这个
  形状自动兜住，不需要额外断言。
- 所有假对象记录 `.calls`（`Call(name, args, kwargs)`），断言"调了什么、传了什么"。

用法：

    from _fakes import FakeContext, audio_chat, make_event, make_record

    ctx = FakeContext(by_id={"fake-audio": audio_chat()})
    event = make_event(current=[make_record(path)])
"""

from __future__ import annotations

import asyncio
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api.message_components import File, Plain, Record, Reply, Video
from astrbot.api.provider import Provider, STTProvider

try:  # 真核心（默认路径）：返回真的 LLMResponse，插件读的就是 `.completion_text`
    from astrbot.core.provider.entities import LLMResponse as _LLMResponse

    def _llm_response(text: str) -> Any:
        return _LLMResponse(role="assistant", completion_text=text)

except Exception:  # pragma: no cover - 只有桩模式下才会走到

    def _llm_response(text: str) -> Any:
        return types.SimpleNamespace(role="assistant", completion_text=text)


__all__ = [
    "Call",
    "FakeChatProvider",
    "FakeContext",
    "FakeEvent",
    "FakeSTTProvider",
    "NotAProvider",
    "RecordingLogger",
    "TrackingFakeEvent",
    "UnresolvableRecord",
    "UnresolvableVideo",
    "audio_chat",
    "make_event",
    "make_file",
    "make_record",
    "make_video",
]


# --------------------------------------------------------------------------- #
# 调用记录
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Call:
    name: str
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - 断言失败时的可读性
        rendered = ", ".join([*(repr(a) for a in self.args), *(f"{k}={v!r}" for k, v in self.kwargs.items())])
        return f"{self.name}({rendered})"


class _CallLog:
    def __init__(self) -> None:
        self.calls: list[Call] = []

    def record(self, name: str, *args: Any, **kwargs: Any) -> Call:
        call = Call(name, args, dict(kwargs))
        self.calls.append(call)
        return call

    def named(self, name: str) -> list[Call]:
        return [c for c in self.calls if c.name == name]

    def only(self, name: str) -> Call:
        found = self.named(name)
        assert len(found) == 1, f"期望恰好一次 {name}，实际 {len(found)} 次：{self.calls}"
        return found[0]

    def clear(self) -> None:
        self.calls.clear()


# --------------------------------------------------------------------------- #
# 假 provider
# --------------------------------------------------------------------------- #
_UNSET: Any = object()


class FakeChatProvider(Provider):
    """对话 / 视觉 provider。

    Args:
        text: `text_chat` 返回的正文。
        modalities: 三态 —— 省略＝`["text", "image"]`（默认"能读图"）；
            `None`＝`provider_config` 里**不放** `modalities` 键（SPEC §2.2 严格口径下
            应判**不支持**图像）；给了列表＝逐字用该列表（`[]` 是"迁移遗留的空列表"，
            按核心同口径算**支持**）。
        provider_config: 直接给整份 `provider_config`（与 `modalities` 互斥时以本参数为准）。
        fail_with: 非空则 `text_chat` 抛这个异常（测 `VISION_FAILED` / 降级）。
        delay: `text_chat` 前 `await asyncio.sleep(delay)`（测超时/预算，别用真网络）。
        model_name: `get_models()` 的返回内容。
    """

    def __init__(
        self,
        *,
        text: str = "画面里是一个标准测试图样（testsrc）。",
        modalities: Any = _UNSET,
        provider_config: dict[str, Any] | None = None,
        fail_with: BaseException | None = None,
        delay: float = 0.0,
        model_name: str = "fake-chat",
    ) -> None:
        if provider_config is None:
            provider_config = {"type": "openai_chat_completion", "id": model_name}
            if modalities is _UNSET:
                provider_config["modalities"] = ["text", "image"]
            elif modalities is not None:
                provider_config["modalities"] = list(modalities)
        super().__init__(provider_config, {})
        self.model_name = model_name
        self.text = text
        self.fail_with = fail_with
        self.delay = delay
        self.calls = _CallLog()

    # -- 抽象方法（真 Provider 是 ABC，少一个都实例化不了） ------------------ #
    async def text_chat(
        self,
        prompt: str | None = None,
        session_id: str | None = None,
        image_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
        **kwargs: Any,
    ) -> Any:
        self.calls.record(
            "text_chat",
            prompt=prompt,
            session_id=session_id,
            image_urls=list(image_urls or []),
            audio_urls=list(audio_urls or []),
            **kwargs,
        )
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail_with is not None:
            raise self.fail_with
        return _llm_response(self.text)

    async def get_models(self) -> list[str]:
        return [self.model_name]

    def set_key(self, key: str) -> None:
        self._key = key

    def get_current_key(self) -> str:
        return getattr(self, "_key", "")

    # -- 断言辅助 ----------------------------------------------------------- #
    def images_seen(self) -> list[str]:
        """每次 `text_chat` 的 `image_urls` 摊平（抽帧阶段应逐帧看到本地 jpg 路径）。"""
        return [img for call in self.calls.named("text_chat") for img in call.kwargs.get("image_urls", [])]

    def audios_seen(self) -> list[str]:
        """每次 `text_chat` 的 `audio_urls` 摊平（v0.2 音轨阶段只放 1 个本地 wav 路径）。"""
        return [url for call in self.calls.named("text_chat") for url in call.kwargs.get("audio_urls", [])]

    def prompts(self) -> list[str]:
        return [call.kwargs.get("prompt") or "" for call in self.calls.named("text_chat")]


def audio_chat(
    *,
    text: str = "这是一段合成测试音轨的转写结果。",
    delay: float = 0.0,
    fail_with: BaseException | None = None,
    model_name: str = "fake-audio",
) -> FakeChatProvider:
    """v0.2 的"音频 provider"：**多模态对话模型**（`modalities` 含 `audio`）。

    v0.2 删掉了 STT 链路（`resolve_stt_provider` / `STTProvider`）⇒ 音频阶段的假身必须是一个
    `Provider` 且 `modalities` 明确含 `audio`，否则严格口径下会被判 `AUDIO_UNAVAILABLE`。
    `FakeSTTProvider` 仍保留在 `_fakes` 里：它现在只用于**反例**（把 STT provider
    填进音频位 ⇒ `PROVIDER_CAPABILITY_MISMATCH`，见 `tests/test_providers.py`）。
    """
    return FakeChatProvider(
        text=text,
        delay=delay,
        fail_with=fail_with,
        modalities=["text", "audio"],
        model_name=model_name,
    )


class FakeSTTProvider(STTProvider):
    """语音识别 provider：唯一入口 `get_text(audio_url: str) -> str`（SPEC §2.3）。

    Args:
        text: 返回的整段转写（`""` 用于测 `stt_empty`）。
        fail_with: 非空则抛该异常（测 `STT_FAILED`）。
        delay: 前置 `asyncio.sleep`（测 `STT_TIMEOUT_S`）。
        require_existing_file: 为 True 时要求 `audio_url` 是**已存在的本地路径** ——
            这正是 SPEC §2.3 说的"名字骗人：实收本地路径"，把它变成测试自动兜住的断言。
    """

    def __init__(
        self,
        *,
        text: str = "这是一段合成测试音轨的转写结果。",
        fail_with: BaseException | None = None,
        delay: float = 0.0,
        require_existing_file: bool = False,
        provider_id: str = "fake_stt",
    ) -> None:
        super().__init__({"type": "speech_to_text", "id": provider_id, "modalities": ["audio"]}, {})
        self.provider_config = {"type": "speech_to_text", "id": provider_id, "modalities": ["audio"]}
        self.text = text
        self.fail_with = fail_with
        self.delay = delay
        self.require_existing_file = require_existing_file
        self.calls = _CallLog()

    async def get_text(self, audio_url: str) -> str:
        self.calls.record("get_text", audio_url=audio_url)
        assert isinstance(audio_url, str), f"get_text 只收字符串路径，收到 {type(audio_url)}"
        if self.require_existing_file:
            assert Path(audio_url).is_file(), f"get_text 应收到本地文件路径，收到 {audio_url!r}"
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail_with is not None:
            raise self.fail_with
        return self.text

    def paths_seen(self) -> list[str]:
        return [call.kwargs["audio_url"] for call in self.calls.named("get_text")]


class NotAProvider:
    """有 `provider_config` 但**不是** `Provider`/`STTProvider` —— 测能力不匹配。"""

    def __init__(self, modalities: list[str] | None = None) -> None:
        if modalities is not None:
            self.provider_config = {"modalities": list(modalities)}

    async def text_chat(self, *args: Any, **kwargs: Any):  # pragma: no cover
        raise AssertionError("NotAProvider 不该被调用")


# --------------------------------------------------------------------------- #
# 消息组件
# --------------------------------------------------------------------------- #
def make_record(path: str | Path, *, file: str | None = None) -> Record:
    """造一条 `Record`（真核心走 `fromFileSystem`，桩模式退回同形构造）。"""
    try:
        comp = Record.fromFileSystem(str(path))
    except AttributeError:  # pragma: no cover - 桩模式
        comp = Record(file=f"file://{path}")
        comp.path = str(path)
    if file is not None:
        comp.file = file
    return comp


def make_video(path: str | Path | None = None, *, file: str | None = None) -> Video:
    """造一条 `Video`。

    `path` 给了 ⇒ 本地文件组件（`path` 非空、`convert_to_file_path()` 零拷贝）；
    只给 `file`（如 `http://…`）⇒ `path` 为**假值**（真核心给 `""`，`Record` 给 `None`），
    测"必须 convert 兜底"的那条路 —— 所以判据一律写 `if not comp.path`，不写 `is None`。
    """
    if path is not None:
        try:
            comp = Video.fromFileSystem(str(path))
        except AttributeError:  # pragma: no cover - 桩模式
            comp = Video(file=f"file://{path}")
            comp.path = str(path)
        if file is not None:
            comp.file = file
        return comp
    assert file is not None, "make_video 需要 path 或 file 之一"
    return Video(file=file)


def make_file(name: str, *, file: str = "", url: str = "") -> File:
    """造一条 `File`（群文件投递）。

    核心 `File.__init__(name, file="", url="")` 的 `file` 走**属性 setter**：
    `http(s)://` 开头的写进 `url`，其余写进 `file_`。所以：
    - `make_file("a.mp4", file=str(path))` ⇒ `file_` 是本地路径（零 IO 取件那条）；
    - `make_file("a.mp4", url="http://…")` ⇒ `file_=""` + `url` 非空（有界下载那条）；
    - `make_file("a.pdf")` ⇒ 两者都空（`MEDIA_UNRESOLVABLE` 那条）。
    """
    return File(name=name, file=file, url=url)


class _UnresolvableMixin:
    """`path` 为空 + `convert_to_file_path()` 抛错 ⇒ `MEDIA_UNRESOLVABLE` 的那条路。

    刻意**不**用真 URL：`Video.convert_to_file_path()` 遇到 http(s) 会真下载
    （`components.py:348-359`），测试里一旦有网就会慢/不可重复。
    """

    async def convert_to_file_path(self) -> str:  # type: ignore[override]
        raise Exception(f"not a valid file: {getattr(self, 'file', None)}")


class UnresolvableVideo(_UnresolvableMixin, Video):
    pass


class UnresolvableRecord(_UnresolvableMixin, Record):
    pass


# --------------------------------------------------------------------------- #
# 假 event
# --------------------------------------------------------------------------- #
class FakeEvent:
    """最小事件：只有插件真正会碰的面。

    刻意**没有** `track_temporary_local_file`（SPEC §9-4：测试用 fake event 没有该方法，
    所以代码必须 `getattr(..., None)` 兜底）。要断言"产物登记"就用 `TrackingFakeEvent`。
    """

    def __init__(
        self,
        *,
        chain: list[Any] | None = None,
        quoted: list[Any] | None = None,
        umo: str = "aiocqhttp:GroupMessage:1000",
        sender_id: str = "10001",
        group_id: str = "1000",
        message_str: str = "",
    ) -> None:
        self._chain: list[Any] = list(chain or [])
        if quoted is not None:
            # 引用的链是**一层**（SPEC §2.1 事实 C：嵌套回复 get_reply=False）
            self._chain.append(Reply(id="fake-quoted", chain=list(quoted)))
        self.unified_msg_origin = umo
        self.sender_id = sender_id
        self.group_id = group_id
        self.message_str = message_str
        self.message_obj = types.SimpleNamespace(message=self._chain)

    def get_messages(self) -> list[Any]:
        return list(self._chain)

    def get_sender_id(self) -> str:
        return self.sender_id

    def get_group_id(self) -> str:
        return self.group_id

    def get_platform_name(self) -> str:
        return "aiocqhttp"

    def get_message_str(self) -> str:
        return self.message_str

    def plain(self, text: str) -> "FakeEvent":
        """追加一条 `Plain`，用于"消息里有文字但没媒体"的用例。"""
        self._chain.append(Plain(text))
        return self


class TrackingFakeEvent(FakeEvent):
    """带 `track_temporary_local_file` 的变体（SPEC §9-4 的另一条路）。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.tracked_files: list[str] = []

    def track_temporary_local_file(self, path: str) -> None:
        self.tracked_files.append(str(path))


def make_event(
    *,
    current: list[Any] | None = None,
    quoted: list[Any] | None = None,
    tracking: bool = False,
    **kwargs: Any,
) -> FakeEvent:
    """一行造事件：`current` 是当前消息链，`quoted` 是引用消息链。"""
    cls = TrackingFakeEvent if tracking else FakeEvent
    return cls(chain=current, quoted=quoted, **kwargs)


# --------------------------------------------------------------------------- #
# 假 context（provider 解析阶梯）
# --------------------------------------------------------------------------- #
class FakeContext:
    """`Context` 的假身，只实现 SPEC §2.2 阶梯用到的五个面。

    Args:
        by_id: `get_provider_by_id` 的表（id → provider；缺键返回 `None`）。
        using_provider: `get_using_provider_async` 的返回（配了 vision id 时不会被调用）。
        using_stt: `get_using_stt_provider_async` 的返回（`enable=False` 时真核心返回 None）。
        all_stt: `get_all_stt_providers` 的返回（阶梯第 ④ 步）。
        curr_stt: `provider_manager.curr_stt_provider_inst`（阶梯第 ③ 步，默认"属性不存在"）。
        has_provider_manager: False ⇒ `provider_manager` 为 `None`（测第 ③ 步落空）。
    """

    def __init__(
        self,
        *,
        by_id: dict[str, Any] | None = None,
        using_provider: Any = None,
        using_stt: Any = None,
        all_stt: list[Any] | None = None,
        curr_stt: Any = _UNSET,
        has_provider_manager: bool = True,
    ) -> None:
        self._by_id = dict(by_id or {})
        self._using_provider = using_provider
        self._using_stt = using_stt
        self._all_stt = list(all_stt or [])
        self.calls = _CallLog()
        if not has_provider_manager:
            self.provider_manager = None
        else:
            manager = types.SimpleNamespace()
            if curr_stt is not _UNSET:
                manager.curr_stt_provider_inst = curr_stt
            self.provider_manager = manager

    # -- 同步！插件若 await 它就会 TypeError（同名方法在 ProviderManager 上是异步） -- #
    def get_provider_by_id(self, provider_id: str) -> Any:
        self.calls.record("get_provider_by_id", provider_id)
        return self._by_id.get(provider_id)

    async def get_using_provider_async(self, umo: str | None = None, *, locale: str = "zh-CN") -> Any:
        self.calls.record("get_using_provider_async", umo, locale=locale)
        return self._using_provider

    async def get_using_stt_provider_async(self, umo: str | None = None) -> Any:
        self.calls.record("get_using_stt_provider_async", umo)
        return self._using_stt

    def get_all_stt_providers(self) -> list[Any]:
        self.calls.record("get_all_stt_providers")
        return list(self._all_stt)

    # -- 断言辅助 ----------------------------------------------------------- #
    def provider_ids_asked(self) -> list[str]:
        return [call.args[0] for call in self.calls.named("get_provider_by_id")]


# --------------------------------------------------------------------------- #
# 假 logger（断言"日志里没有正文"用）
# --------------------------------------------------------------------------- #
class RecordingLogger:
    """记录 `(level, message)`；配合 `monkeypatch.setattr(module, "LOG", logger)`。

    SPEC §9-1：插件**只**打错误码/耗时/帧数/provider id，不打转写正文与帧描述。
    断言写法：

        log = RecordingLogger()
        monkeypatch.setattr(analyze, "LOG", log, raising=False)
        ...
        assert "这是一段合成测试音轨" not in log.text()
    """

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def _record(self, level: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        if args:
            template, *rest = args
            try:
                message = str(template) % tuple(rest) if rest else str(template)
            except (TypeError, ValueError):
                message = " ".join(str(a) for a in args)
        else:
            message = str(kwargs)
        self.records.append((level, message))

    def debug(self, *args: Any, **kwargs: Any) -> None:
        self._record("DEBUG", args, kwargs)

    def info(self, *args: Any, **kwargs: Any) -> None:
        self._record("INFO", args, kwargs)

    def warning(self, *args: Any, **kwargs: Any) -> None:
        self._record("WARNING", args, kwargs)

    warn = warning

    def error(self, *args: Any, **kwargs: Any) -> None:
        self._record("ERROR", args, kwargs)

    def critical(self, *args: Any, **kwargs: Any) -> None:
        self._record("CRITICAL", args, kwargs)

    def exception(self, *args: Any, **kwargs: Any) -> None:
        self._record("ERROR", args, kwargs)

    # -- 断言辅助 ----------------------------------------------------------- #
    def text(self) -> str:
        return "\n".join(message for _level, message in self.records)

    def levels(self) -> list[str]:
        return [level for level, _message in self.records]

    def contains(self, needle: str) -> bool:
        return needle in self.text()

    def clear(self) -> None:
        self.records.clear()
