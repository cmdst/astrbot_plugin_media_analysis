"""插件门面：只有三件事 —— 拿配置、调 `analyze`、把异常收成信封。

加载面（SPEC §2.7）：核心按 `data.plugins.<目录名>.main` 把本目录当**包**导入
（`star_manager.py:461/1124-1126`）⇒ 包内模块之间只能相对导入。

三个容易踩的点（都写在 SPEC §3.3 的要点里）：
1. `__init__` 的第二个形参**必须叫 `config`**：加载器按关键字调用
   `star_cls_type(context=…, config=…)`（`star_manager.py:1229-1238`），名字不对会被
   `except TypeError` 兜成"无配置实例化"，**静默丢配置**。
2. `Star.__init__` **不保存** `config`（`star/base.py:26-29`）⇒ 自己存。
3. 工具方法**只挂** `@filter.llm_tool`：叠加 `@filter.permission_type` 会让它被当成
   消息处理器，权限不符就丢用户消息（AGENTS §3 装饰器纪律）。
"""

from __future__ import annotations

import asyncio
import re
import time
import traceback

from astrbot.api import AstrBotConfig, logger as LOG
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from . import analyze, cache, envelope, settings

# --------------------------------------------------------------------------- #
# 核心**内部面**加固（`error_redaction` 不在 `astrbot.api.*` 的公开导出里）。
# 为什么必须 try/except：它一旦在升级中搬家，`from … import` 会在**导入期**抛
# ImportError，插件就变成**静默拒载** —— 目录在、工具不注册、界面上像"没装"
# （父手册 §3.4 的最贵失败模式）。加固后最坏情况退化为
# 「本地兜底脱敏 + 插件照常加载」，而不是整插件消失。
# 正常路径**逐字等价**：`redact_sensitive_text` 就是核心那个函数，调用点不变。
# --------------------------------------------------------------------------- #
_TOKEN_SHAPED = re.compile(r"[A-Za-z0-9_\-]{24,}")


def _fallback_redact(text: str) -> str:
    """本地兜底脱敏：只把 token 形状的长串（≥24 位）替换成 `[REDACTED]`，其余原样保留。"""
    return _TOKEN_SHAPED.sub("[REDACTED]", text)


try:
    from astrbot.core.utils.error_redaction import redact_sensitive_text
except Exception:  # noqa: BLE001 —— 拿不到就退化为本地兜底，绝不因此拒载
    LOG.warning(
        "media_analysis: 核心内部面 astrbot.core.utils.error_redaction 不可用，"
        "已切换到本地兜底脱敏（插件仍加载）"
    )
    redact_sensitive_text = _fallback_redact


# --------------------------------------------------------------------------- #
# 参数类型防御（评审 F6）：核心**不校验**工具参数类型，模型完全可能发
# `"include_frames": "false"`（字符串）。字符串 `"false"` 是**真值** ⇒ 会静默多抽
# `max_frames` 帧 + 同样次数的视觉调用（真金白银，且用户看不出为什么）。
# 这里只把"真/假"归一，非法类型告警一次并按 schema 默认 `False`。
# --------------------------------------------------------------------------- #
_TRUE_LITERALS = frozenset({"1", "true", "yes", "on"})
_FALSE_LITERALS = frozenset({"", "0", "false", "no", "off"})


def _as_bool(value: object) -> bool:
    """把模型给的 `include_frames` 归一成 `bool`（纯函数，无副作用，只可能告警）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_LITERALS:
            return True
        if text in _FALSE_LITERALS:
            return False
    LOG.warning(
        "media_analyze: 参数 include_frames 收到非法值（类型 %s），按默认 False 处理",
        type(value).__name__,
    )
    return False


class MediaAnalysisPlugin(Star):
    """`media_analyze` 工具的唯一实现体。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)  # Star.__init__ 不保存 config（star/base.py:26-29）
        self.config = config or {}
        self._cache = cache.ResultCache(maxsize=32)
        self._settings_cache: settings.Settings | None = None

    async def initialize(self) -> None:
        """生命周期钩子（`star/base.py:141-145`）：装配配置摘要，只打 id 与上限。"""
        self._settings_cache = self._settings()
        LOG.info(
            "media_analysis ready: stt_provider_id=%r vision_provider_id=%r "
            "max_media_seconds=%d max_frames=%d",
            self._settings_cache.stt_provider_id,
            self._settings_cache.vision_provider_id,
            self._settings_cache.max_media_seconds,
            self._settings_cache.max_frames,
        )

    async def terminate(self) -> None:
        """卸载：丢掉配置快照与进程内缓存（缓存里有转写正文，只活在内存里）。"""
        self._settings_cache = None
        self._cache.clear()

    def _settings(self) -> settings.Settings:
        """唯一配置读取点：4 个键逐条显式读（见 SPEC §4.3），禁止散落到别处。

        漏一个键 ＝ 生产链路上**静默丢弃**设置（父手册 §3 第 8 条）；
        `tests/test_config_map.py` 用三方一致守卫钉住这里。
        """
        view = settings.ConfigView(self.config)
        return settings.Settings(
            stt_provider_id=view.text(settings.KEY_STT_PROVIDER_ID),
            vision_provider_id=view.text(settings.KEY_VISION_PROVIDER_ID),
            max_media_seconds=view.bounded_int(
                settings.KEY_MAX_MEDIA_SECONDS,
                default=settings.DEFAULT_MAX_MEDIA_SECONDS,
                lo=settings.MIN_MAX_MEDIA_SECONDS,
                hi=settings.MAX_MAX_MEDIA_SECONDS,
            ),
            max_frames=view.bounded_int(
                settings.KEY_MAX_FRAMES,
                default=settings.DEFAULT_MAX_FRAMES,
                lo=settings.MIN_MAX_FRAMES,
                hi=settings.MAX_MAX_FRAMES,
            ),
        )

    @filter.llm_tool(name="media_analyze")  # 只挂这一个装饰器（AGENTS §3 装饰器纪律）
    async def media_analyze(
        self,
        event: AstrMessageEvent,
        target: str = "current",
        focus: str = "",
        include_frames: bool = False,
    ) -> str:
        """分析当前消息或引用消息里的语音/视频（可只转写音轨，也可抽帧看画面）。

        当用户发来语音、或发来视频/引用一条语音视频，并且想知道"里面说了什么 / 画面发生了什么"
        时调用本工具。返回 JSON：转写文本（transcript）、可选的分帧画面描述（frames）、
        一句摘要（summary），失败时返回 {ok:false, error, hint}。

        Args:
            target (string): 分析哪条消息。current＝当前消息（默认），quoted＝被引用的那条消息。
            focus (string): 用户想了解的重点问题（如「他在说什么事」）。留空表示要一份通篇摘要。
            include_frames (boolean): 只对视频有意义。true＝抽帧并描述画面；false（默认）＝只做音轨转写。
        """
        started = time.monotonic()
        try:
            include_frames = _as_bool(include_frames)
            payload = await analyze.analyze(
                context=self.context,
                event=event,
                target=target,
                focus=focus,
                include_frames=include_frames,
                settings=self._settings_cache or self._settings(),
                cache=self._cache,
            )
            return envelope.dumps(payload)
        except asyncio.CancelledError:
            # 插件卸载 / 进程停止：**取消语义必须传下去**，不许被收成信封（评审 F5）
            raise
        except envelope.ToolError as exc:
            LOG.info(
                "media_analyze rejected: code=%s detail=%s",
                exc.code,
                redact_sensitive_text(exc.log_detail)[:200],
            )
            return envelope.dumps(envelope.fail_envelope(exc.code))
        except BaseException as exc:  # noqa: BLE001 —— 工具永不把异常抛给模型
            LOG.error(
                "media_analyze internal error: %s\n%s",
                redact_sensitive_text(str(exc))[:200],
                redact_sensitive_text(traceback.format_exc())[:4000],
            )
            return envelope.dumps(envelope.fail_envelope(envelope.ERR_INTERNAL_ERROR))
