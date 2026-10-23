"""插件门面：只有三件事 —— 拿配置、调 `analyze`、把异常收成信封。

加载面（SPEC.md §2.7）：核心按 `data.plugins.<目录名>.main` 把本目录当**包**导入
（`star_manager.py:461/1124-1126`）⇒ 包内模块之间只能相对导入。

四个容易踩的点（都写在 SPEC 的要点里）：
1. `__init__` 的第二个形参**必须叫 `config`**：加载器按关键字调用
   `star_cls_type(context=…, config=…)`（`star_manager.py:1229-1238`），名字不对会被
   `except TypeError` 兜成"无配置实例化"，**静默丢配置**。
2. `Star.__init__` **不保存** `config`（`star/base.py:26-29`）⇒ 自己存。
3. 工具方法**只挂** `@filter.llm_tool`：叠加 `@filter.permission_type` 会让它被当成
   消息处理器，权限不符就丢用户消息（AGENTS §3 装饰器纪律）。
4. `_settings()` 是**唯一配置读取点**，21 个键逐条显式读（`settings.KEY_MAP` ↔
   `_conf_schema.json` ↔ 迁移器 `KEY_ORDER` ↔ 这里四方一致）。漏一个键 ＝ 生产链路上
   **静默丢弃**设置。
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
# 参数归一（v0.3：`bool` → **三态字符串** `auto|true|false`，SPEC-v0.3 §13.1）
#
# 为什么还要防类型：核心**不校验**工具参数类型，模型完全可能发 `"include_frames": "false"`。
# v0.3 的参数是**字符串枚举**（schema 类型从 boolean 变 string），归一逻辑的**唯一实现**
# 在 `analyze.normalize_mode`（纯函数，测试与工具门面共用一份）：
#
# | 输入 | 输出 |
# | --- | --- |
# | `True/False` | `true/false`（旧客户端兼容） |
# | `"auto"/"true"/"false"`（大小写不敏感） | 原样 |
# | `"1"/"yes"/"on"` · `"0"/"no"/"off"/""` | `true` · `false` |
# | `None` | `auto` |
# | 其它（`2.5`、`{}`、未知串…） | **`auto`** + 一条 WARNING |
#
# 与 v0.2 相反的一处：非法值不再默认 `false`。v0.2 那样做是为了省 token，而 v0.3 的
# 主路径本身就是"看视频"—— 默认 `auto` 只多一次原生调用，默认 `false` 会把"其实没看画面"
# 悄悄记成成功。
# --------------------------------------------------------------------------- #
def _as_mode(value: object) -> str:
    """把模型给的 `include_frames` 归一成 `"auto" | "true" | "false"`（门面入口）。

    归一本身在 `analyze.normalize_mode`（纯函数）；**告警在这里**：唯一的生产入口是工具
    门面，非法值一条 WARNING（带类型名、不带值），与 v0.2 的 `_as_bool` 同口径。
    """
    mode = analyze.normalize_mode(value)
    if not analyze.is_valid_mode_value(value):
        LOG.warning(
            "media_analyze: 参数 include_frames 收到非法值（类型 %s），按 %s 处理",
            type(value).__name__,
            mode,
        )
    return mode


class MediaAnalysisPlugin(Star):
    """`media_analyze` 工具的唯一实现体。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)  # Star.__init__ 不保存 config（star/base.py:26-29）
        self.config = config or {}
        # 缓存实例在 `initialize()` 里建（TTL 来自配置）；这里留空，工具被提前调用时
        # 由 `_cache_store()` 兜一个同配置的实例（懒建，绝不因为生命周期顺序而抛）。
        self._cache: cache.DiskCache | None = None
        self._settings_cache: settings.Settings | None = None

    async def initialize(self) -> None:
        """生命周期钩子（`star/base.py:141-145`）：装配配置摘要 + 建缓存。

        摘要行只打 **provider id 与数字**（provider id 是配置值不是凭据）；
        弃用键 `stt_provider_id` 只"是否非空"，**不打值**。
        """
        self._settings_cache = self._settings()
        current = self._settings_cache
        self._cache = cache.DiskCache(
            plugin_name=settings.PLUGIN_NAME, ttl_seconds=current.cache_ttl_seconds
        )
        LOG.info(
            "media_analysis ready: audio_provider_id=%r video_provider_id=%r "
            "vision_provider_id=%r max_media_seconds=%d max_frames=%d "
            "max_media_bytes=%d video.max_direct_bytes=%d native_timeout_s=%d "
            "native_max_tokens=%d compress=%s target=%d probe=%s ttl=%d "
            "cache_ttl_seconds=%d cache_dir=%s",
            current.audio_provider_id,
            current.video_provider_id,
            current.vision_provider_id,
            current.max_media_seconds,
            current.max_frames,
            current.max_media_bytes,
            current.video_max_direct_bytes,
            current.video_native_timeout_seconds,
            current.video_native_max_tokens,
            "on" if current.video_compress_enabled else "off",
            current.video_compress_target_bytes,
            "on" if current.video_probe_enabled else "off",
            current.video_probe_ttl_seconds,
            current.cache_ttl_seconds,
            self._cache.directory or "(disabled)",
        )
        self._log_direct_bytes_hint(current)
        if current.legacy_stt_provider_id:
            LOG.warning(
                "media_analysis: 配置项 provider_settings.stt_provider_id 已弃用且不生效"
                "（检测到旧值非空，本行不打值）；请改用 provider_settings.audio_provider_id，"
                "旧值可用 tools/migrate_config_v02.py 迁移"
            )

    def _log_direct_bytes_hint(self, current: settings.Settings) -> None:
        """按 §4 的建议表提示一句（**只打数字，不覆盖配置**）。

        为什么要有：直送门槛是"按延迟"定的，配得比模型实测的舒服值高就会周期性超时；
        但**不能**自动改（否决项 §16.2-6：第二事实源）。所以这里只打一行 INFO，
        且整段 try/except —— 拿不到 provider/model 时静默跳过，绝不因为一句提示拒载。
        """
        try:
            provider_id = current.video_provider_id
            if not provider_id:
                return
            provider = self.context.get_provider_by_id(provider_id)
            model = ""
            config = getattr(provider, "provider_config", None)
            if isinstance(config, dict):
                model = str(config.get("model") or "")
            if not model:
                getter = getattr(provider, "get_model", None)
                model = str(getter()) if callable(getter) else ""
            suggested = settings.suggest_direct_bytes(model)
            if current.video_max_direct_bytes > suggested:
                LOG.info(
                    "media_analysis: 提示 —— video.max_direct_bytes=%d 高于该模型的延迟建议值 %d"
                    "（实测口径，见 README「视频能力探测」）；若经常超时请下调，或先用"
                    "tools/probe_video_capability.py 校准",
                    current.video_max_direct_bytes,
                    suggested,
                )
        except Exception as exc:  # noqa: BLE001 —— 一句提示不许影响加载
            LOG.debug("media_analysis: 直送门槛提示跳过（%s）", type(exc).__name__)

    async def terminate(self) -> None:
        """卸载：丢掉配置快照，关闭缓存（磁盘条目按 TTL 自然过期，可整目录删）。"""
        self._settings_cache = None
        store, self._cache = self._cache, None
        if store is not None:
            await store.close()

    def _settings(self) -> settings.Settings:
        """唯一配置读取点：21 个键逐条显式读（SPEC-v0.3 §8.1 键表），禁止散落到别处。

        漏一个键 ＝ 生产链路上**静默丢弃**设置（父手册 §3 第 8 条）；
        `tests/test_config_map.py` 用四方一致守卫钉住这里。
        布尔走 `ConfigView.flag()`（非 `bool` ⇒ 告警回默认），整数走 `bounded_int`（钳位）。
        """
        view = settings.ConfigView(self.config)
        return settings.Settings(
            audio_provider_id=view.text(settings.KEY_AUDIO_PROVIDER_ID),
            video_provider_id=view.text(settings.KEY_VIDEO_PROVIDER_ID),
            vision_provider_id=view.text(settings.KEY_VISION_PROVIDER_ID),
            legacy_stt_provider_id=view.text(settings.KEY_LEGACY_STT_PROVIDER_ID),
            video_probe_enabled=view.flag(
                settings.KEY_VIDEO_PROBE_ENABLED,
                default=settings.DEFAULT_VIDEO_PROBE_ENABLED,
            ),
            video_probe_ttl_seconds=view.bounded_int(
                settings.KEY_VIDEO_PROBE_TTL_SECONDS,
                default=settings.DEFAULT_VIDEO_PROBE_TTL_SECONDS,
                lo=settings.MIN_VIDEO_PROBE_TTL_SECONDS,
                hi=settings.MAX_VIDEO_PROBE_TTL_SECONDS,
            ),
            video_max_direct_bytes=view.bounded_int(
                settings.KEY_VIDEO_MAX_DIRECT_BYTES,
                default=settings.DEFAULT_VIDEO_MAX_DIRECT_BYTES,
                lo=settings.MIN_VIDEO_MAX_DIRECT_BYTES,
                hi=settings.MAX_VIDEO_MAX_DIRECT_BYTES,
            ),
            video_native_timeout_seconds=view.bounded_int(
                settings.KEY_VIDEO_NATIVE_TIMEOUT_SECONDS,
                default=settings.DEFAULT_VIDEO_NATIVE_TIMEOUT_SECONDS,
                lo=settings.MIN_VIDEO_NATIVE_TIMEOUT_SECONDS,
                hi=settings.MAX_VIDEO_NATIVE_TIMEOUT_SECONDS,
            ),
            video_native_max_tokens=view.bounded_int(
                settings.KEY_VIDEO_NATIVE_MAX_TOKENS,
                default=settings.DEFAULT_VIDEO_NATIVE_MAX_TOKENS,
                lo=settings.MIN_VIDEO_NATIVE_MAX_TOKENS,
                hi=settings.MAX_VIDEO_NATIVE_MAX_TOKENS,
            ),
            video_compress_enabled=view.flag(
                settings.KEY_VIDEO_COMPRESS_ENABLED,
                default=settings.DEFAULT_VIDEO_COMPRESS_ENABLED,
            ),
            video_compress_target_bytes=view.bounded_int(
                settings.KEY_VIDEO_COMPRESS_TARGET_BYTES,
                default=settings.DEFAULT_VIDEO_COMPRESS_TARGET_BYTES,
                lo=settings.MIN_VIDEO_COMPRESS_TARGET_BYTES,
                hi=settings.MAX_VIDEO_COMPRESS_TARGET_BYTES,
            ),
            video_compress_timeout_seconds=view.bounded_int(
                settings.KEY_VIDEO_COMPRESS_TIMEOUT_SECONDS,
                default=settings.DEFAULT_VIDEO_COMPRESS_TIMEOUT_SECONDS,
                lo=settings.MIN_VIDEO_COMPRESS_TIMEOUT_SECONDS,
                hi=settings.MAX_VIDEO_COMPRESS_TIMEOUT_SECONDS,
            ),
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
            max_media_bytes=view.bounded_int(
                settings.KEY_MAX_MEDIA_BYTES,
                default=settings.DEFAULT_MAX_MEDIA_BYTES,
                lo=settings.MIN_MAX_MEDIA_BYTES,
                hi=settings.MAX_MAX_MEDIA_BYTES,
            ),
            prompt_audio=view.text(settings.KEY_PROMPTS_AUDIO),
            prompt_video=view.text(settings.KEY_PROMPTS_VIDEO),
            prompt_frame=view.text(settings.KEY_PROMPTS_FRAME),
            prompt_summary=view.text(settings.KEY_PROMPTS_SUMMARY),
            prompt_video_native=view.text(settings.KEY_PROMPTS_VIDEO_NATIVE),
            cache_ttl_seconds=view.bounded_int(
                settings.KEY_CACHE_TTL_SECONDS,
                default=settings.DEFAULT_CACHE_TTL_SECONDS,
                lo=settings.MIN_CACHE_TTL_SECONDS,
                hi=settings.MAX_CACHE_TTL_SECONDS,
            ),
        )

    def _cache_store(self) -> cache.DiskCache:
        """取缓存实例；`initialize()` 还没跑过时按当前配置懒建一个（幂等）。"""
        if self._cache is None:
            current = self._settings_cache or self._settings()
            self._cache = cache.DiskCache(
                plugin_name=settings.PLUGIN_NAME,
                ttl_seconds=current.cache_ttl_seconds,
            )
        return self._cache

    @filter.llm_tool(name="media_analyze")  # 只挂这一个装饰器（AGENTS §3 装饰器纪律）
    async def media_analyze(
        self,
        event: AstrMessageEvent,
        target: str = "current",
        focus: str = "",
        include_frames: str = "auto",
    ) -> str:
        """分析当前消息或引用消息里的语音/视频（视频默认整段直送多模态模型，含画面与声音）。

        当用户发来语音、视频，或引用了一条语音/视频，并且想知道"里面说了什么 / 画面发生了
        什么"时调用本工具。**群里以「文件」形式发来的视频/语音也用 current 或 quoted 取，
        不要传文件路径**。返回 JSON：转写文本（transcript）、可选的分帧画面描述（frames）、
        摘要（summary），失败时返回 {ok:false, error, hint}。

        视频默认走"原生视频"（一次调用同时看画面和听声音，返回在 summary 里）。只有在视频
        模型不可用/视频太大且压不下去/调用失败时，插件才按 include_frames 决定是否抽帧回退。

        Args:
            target (string): 分析哪条消息。current＝当前消息（默认），quoted＝被引用的那条消息。
            focus (string): 用户想了解的重点问题（如「他在说什么事」）。留空表示要一份通篇摘要。
            include_frames (string): 三态。auto（默认）＝要画面，插件自己选路（原生视频优先，失败时若配了视觉模型就抽帧）；true＝明确要画面，原生失败必须抽帧回退；false＝明确不要画面，原生失败时绝不抽帧（只交音轨转写）。
        """
        started = time.monotonic()
        try:
            mode = _as_mode(include_frames)
            payload = await analyze.analyze(
                context=self.context,
                event=event,
                target=target,
                focus=focus,
                include_frames=mode,
                settings=self._settings_cache or self._settings(),
                cache=self._cache_store(),
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
