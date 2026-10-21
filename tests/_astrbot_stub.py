"""最小 astrbot 桩 —— **只在真核心不可用时**由 `conftest.py` 调用。

口径（三条，缺一条就会把测试带歪）：

1. **真核心优先**。`/root/AstrBot/.venv/bin/python` 里 `astrbot` 就是部署树本身
   （SPEC §11.1 实测：`from astrbot.api.message_components import Record, Video` 通过），
   所以正常跑门禁**永远走真核心**，本模块不参与。插件需要 `Record.fromFileSystem` /
   `Video.fromFileSystem` / `isinstance(x, STTProvider)` 这些**真类型面**，桩替代不了。
2. 本模块只服务一个场景：拿**没有 astrbot 的解释器**（系统 `python3`）跑 `tests/` 时，
   让 `conftest` 与 `_fakes` 至少能 import、collection 不炸（这时别指望插件源码能跑：
   `media.py` 的 `astrbot.core.utils.media_utils` 在桩里只有同名占位）。
3. 桩**绝不覆盖已导入的真核心**：每个 `sys.modules[name]` 只在缺失时注册，
   父模块上的同名属性也只在缺失时赋值。这样再跑一次也不会把真核心换掉。

形状对齐（与 `astrbot_plugin_search_Groupmember_information/tests/test_tool.py` 的内联桩同构，
但把"装模块"抽成可复用函数，因为本插件要装的面更宽）：

- `astrbot.api`            → `logger`、`AstrBotConfig`
- `astrbot.api.event`      → `AstrMessageEvent`、`filter`
- `astrbot.api.event.filter` → `llm_tool` / `permission_type` / `command` / `on_llm_request`
- `astrbot.api.star`       → `Context`、`Star`、`register`
- `astrbot.api.message_components` → `BaseMessageComponent` / `Plain` / `Record` / `Video` / `Reply` / `Image` / `File`
- `astrbot.api.provider`   → `Provider` / `STTProvider`
- `astrbot.core.utils.media_utils` → `get_media_duration`（异步，恒 `None`）
- `astrbot.core.utils.error_redaction` → `redact_sensitive_text`（恒等）

`Record` / `Video` 的桩保留 `path` / `file` 两个字段与 `convert_to_file_path()`，
因为 SPEC §2.1 的取用口径就是围绕这两个字段写的（path 优先、convert 兜底）。
"""

from __future__ import annotations

import sys
import types
from typing import Any

__all__ = ["install", "is_installed"]

_INSTALLED = False


# --------------------------------------------------------------------------- #
# 注册器：缺什么补什么，已有的（真核心）一个都不动
# --------------------------------------------------------------------------- #
def _ensure(name: str) -> types.ModuleType:
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        sys.modules[name] = module
    return module


def _ensure_attr(module: types.ModuleType, attr: str, value: Any) -> None:
    if not hasattr(module, attr):
        setattr(module, attr, value)


# --------------------------------------------------------------------------- #
# logger
# --------------------------------------------------------------------------- #
class _StubLogger:
    """吞掉一切日志；`records` 留着给测试断言"没打正文"。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def _log(self, level: str, *args: Any, **kwargs: Any) -> None:
        message = " ".join(str(a) for a in args) if args else ""
        self.records.append((level, message))

    def debug(self, *args: Any, **kwargs: Any) -> None:
        self._log("DEBUG", *args, **kwargs)

    def info(self, *args: Any, **kwargs: Any) -> None:
        self._log("INFO", *args, **kwargs)

    def warning(self, *args: Any, **kwargs: Any) -> None:
        self._log("WARNING", *args, **kwargs)

    warn = warning

    def error(self, *args: Any, **kwargs: Any) -> None:
        self._log("ERROR", *args, **kwargs)

    def critical(self, *args: Any, **kwargs: Any) -> None:
        self._log("CRITICAL", *args, **kwargs)

    def exception(self, *args: Any, **kwargs: Any) -> None:
        self._log("ERROR", *args, **kwargs)


# --------------------------------------------------------------------------- #
# 消息组件
# --------------------------------------------------------------------------- #
class _StubBaseMessageComponent:
    type: str = "base"

    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)

    def __repr__(self) -> str:  # pragma: no cover - 只为排错时看得懂
        return f"<stub {type(self).__name__} {self.__dict__}>"


class _StubPlain(_StubBaseMessageComponent):
    type = "plain"

    def __init__(self, text: str = "", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.text = text


class _StubFileComponent(_StubBaseMessageComponent):
    """共同形状：`file` 可能是 `file://` / `http(s)://` / `base64://`，`path` 是本地路径。"""

    def __init__(self, file: str | None = None, path: str | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.file = file
        self.path = path if path is not None else _file_uri_to_path(file)

    @classmethod
    def fromFileSystem(cls, path: str, **kwargs: Any) -> "_StubFileComponent":
        return cls(file=f"file://{path}", path=str(path), **kwargs)

    @classmethod
    def fromURL(cls, url: str, **kwargs: Any) -> "_StubFileComponent":
        return cls(file=url, path=None, **kwargs)

    async def convert_to_file_path(self) -> str:
        if self.path:
            return self.path
        raise Exception(f"not a valid file: {self.file}")


def _file_uri_to_path(file: str | None) -> str | None:
    if not file:
        return None
    if file.startswith("file://"):
        return file[len("file://") :]
    if file.startswith("/"):
        return file
    return None


class _StubRecord(_StubFileComponent):
    type = "record"


class _StubVideo(_StubFileComponent):
    type = "video"


class _StubImage(_StubFileComponent):
    type = "image"


class _StubFile(_StubFileComponent):
    type = "file"


class _StubReply(_StubBaseMessageComponent):
    type = "reply"

    def __init__(self, id: str | int | None = None, chain: list | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.id = id
        self.chain = chain or []


# --------------------------------------------------------------------------- #
# provider
# --------------------------------------------------------------------------- #
class _StubProvider:
    def __init__(self, provider_config: dict | None = None, provider_settings: dict | None = None) -> None:
        self.provider_config = dict(provider_config or {})
        self.provider_settings = dict(provider_settings or {})
        self.model_name = ""

    async def text_chat(self, *args: Any, **kwargs: Any):  # pragma: no cover - 由 _fakes 覆盖
        raise NotImplementedError

    async def get_models(self) -> list[str]:  # pragma: no cover
        return []

    def set_key(self, key: str) -> None:  # pragma: no cover
        self._key = key

    def get_current_key(self) -> str:  # pragma: no cover
        return getattr(self, "_key", "")


class _StubSTTProvider(_StubProvider):
    async def get_text(self, audio_url: str) -> str:  # pragma: no cover - 由 _fakes 覆盖
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# star / event / filter
# --------------------------------------------------------------------------- #
class _StubContext:
    """真 `Context` 的极小替身；插件只用到 provider 查询面（见 SPEC §2.2）。"""

    def get_provider_by_id(self, provider_id: str):  # pragma: no cover
        return None

    async def get_using_provider_async(self, umo=None, *, locale: str = "zh-CN"):  # pragma: no cover
        return None

    async def get_using_stt_provider_async(self, umo=None):  # pragma: no cover
        return None

    def get_all_stt_providers(self) -> list:  # pragma: no cover
        return []


class _StubStar:
    def __init__(self, context: Any = None, config: dict | None = None) -> None:
        self.context = context
        self.config = config or {}


class _StubMessageEvent:
    def get_messages(self) -> list:  # pragma: no cover
        return []

    def get_sender_id(self) -> str:  # pragma: no cover
        return "0"


def _passthrough_decorator(*_args: Any, **_kwargs: Any):
    """`@filter.xxx(...)` 的桩：原样返回函数（不做注册）。"""

    def decorator(func):
        return func

    return decorator


def _register(*_args: Any, **_kwargs: Any):
    def decorator(cls):
        return cls

    return decorator


# --------------------------------------------------------------------------- #
# core 工具面
# --------------------------------------------------------------------------- #
async def _stub_get_media_duration(file_path: str) -> int | None:
    return None


def _stub_redact_sensitive_text(text: str) -> str:
    return text


# --------------------------------------------------------------------------- #
# install
# --------------------------------------------------------------------------- #
def install() -> bool:
    """把缺失的 astrbot 模块补成桩；返回 True 表示本次真的装了什么。"""
    global _INSTALLED

    astrbot = _ensure("astrbot")
    api = _ensure("astrbot.api")
    event = _ensure("astrbot.api.event")
    event_filter = _ensure("astrbot.api.event.filter")
    star = _ensure("astrbot.api.star")
    components = _ensure("astrbot.api.message_components")
    provider = _ensure("astrbot.api.provider")
    core = _ensure("astrbot.core")
    core_utils = _ensure("astrbot.core.utils")
    core_media_utils = _ensure("astrbot.core.utils.media_utils")
    core_error_redaction = _ensure("astrbot.core.utils.error_redaction")

    before = _snapshot()

    _ensure_attr(astrbot, "api", api)
    _ensure_attr(astrbot, "core", core)
    _ensure_attr(astrbot, "__version__", "0.0.0-stub")

    logger = getattr(api, "logger", None)
    if logger is None:
        logger = _StubLogger()
    _ensure_attr(api, "logger", logger)
    _ensure_attr(api, "AstrBotConfig", dict)

    _ensure_attr(api, "event", event)
    _ensure_attr(api, "star", star)
    _ensure_attr(api, "message_components", components)
    _ensure_attr(api, "provider", provider)

    _ensure_attr(event, "filter", event_filter)
    _ensure_attr(event, "AstrMessageEvent", _StubMessageEvent)
    for name in ("llm_tool", "permission_type", "command", "on_llm_request", "event_message_type"):
        _ensure_attr(event_filter, name, _passthrough_decorator)

    _ensure_attr(star, "Context", _StubContext)
    _ensure_attr(star, "Star", _StubStar)
    _ensure_attr(star, "register", _register)

    for name, obj in (
        ("BaseMessageComponent", _StubBaseMessageComponent),
        ("Plain", _StubPlain),
        ("Record", _StubRecord),
        ("Video", _StubVideo),
        ("Image", _StubImage),
        ("File", _StubFile),
        ("Reply", _StubReply),
    ):
        _ensure_attr(components, name, obj)

    _ensure_attr(provider, "Provider", _StubProvider)
    _ensure_attr(provider, "STTProvider", _StubSTTProvider)
    _ensure_attr(provider, "AbstractProvider", _StubProvider)

    _ensure_attr(core, "utils", core_utils)
    _ensure_attr(core_utils, "media_utils", core_media_utils)
    _ensure_attr(core_utils, "error_redaction", core_error_redaction)
    _ensure_attr(core_media_utils, "get_media_duration", _stub_get_media_duration)
    _ensure_attr(core_error_redaction, "redact_sensitive_text", _stub_redact_sensitive_text)

    _INSTALLED = True
    return before != _snapshot()


def _snapshot() -> set[tuple[str, str]]:
    """记录"哪些模块带哪些属性"，只用来判断本次 install 是否真的改动了什么。"""
    return {
        (name, attr)
        for name, module in list(sys.modules.items())
        if name == "astrbot" or name.startswith("astrbot.")
        for attr in vars(module)
    }


def is_installed() -> bool:
    return _INSTALLED
