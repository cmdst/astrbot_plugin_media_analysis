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
   `_conf_schema.json` ↔ 迁移器 `migrate_config_v05.py:KEY_ORDER` ↔ 这里四方一致）。
   漏一个键 ＝ 生产链路上**静默丢弃**设置（SPEC-v0.5.1 §10）。
5. **加载期一致性告警**（`_config_check`，SPEC-v0.5.1 §8.1）与**静默读 provider**
   （`_silent_provider`，§8.2）都在本文件：前者只 WARN 不改配置，后者替代
   `context.get_provider_by_id()` 以消掉冷启动的 `Provider … was not found` 噪声。
"""

from __future__ import annotations

import asyncio
import re
import time
import traceback

from astrbot.api import AstrBotConfig, logger as LOG
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from . import analyze, background, cache, core_mode, envelope, media, settings, video_native

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


def _native_timeout_s() -> float:
    """原生视频单次调用的**生效上限**（该配置键已在 v0.5.1 删除，值进 `analyze` 常量）。

    唯一事实源是运行面的 `analyze.NATIVE_TIMEOUT_S`（SPEC-v0.5.1 §2.2-B）；`settings`
    里那份是**配置面的镜像**（`tests/test_config_map.py` 钉相等），只在这份常量尚未
    落地时兜底 —— 一个 `ready` 行字段**不许**让插件加载失败。
    """
    value = getattr(analyze, "NATIVE_TIMEOUT_S", None)
    return float(settings.NATIVE_TIMEOUT_S if value is None else value)


def _silent_provider(context, provider_id: str):
    """只读注册表取 provider，**不触发核心告警**（SPEC-v0.5.1 §8.2，修启动期噪声）。

    为什么不能用 `context.get_provider_by_id()`：它在目标缺失时**自己**打一条
    `logger.warning("Provider <id> was not found. …")`（`star/context.py:466`），而
    冷启动时插件先于 provider 注册 ⇒ 每次冷启动一条噪声（生产实测 02:03:05 / 02:38:03；
    重载那次 0 条）。加载期**禁止**调用任何"目标缺失就打 WARN"的核心 API，改为静默读注册表；
    取不到就 `None`（fail-open：允许把检查顺延到首次调用）。

    任何异常都吞掉 ⇒ 本函数**永不抛**（它在一个日志字段的路径上）。
    """
    try:
        manager = getattr(context, "provider_manager", None)
        inst_map = getattr(manager, "inst_map", None)
        if not isinstance(inst_map, dict):
            return None
        return inst_map.get(str(provider_id))
    except BaseException:  # noqa: BLE001 —— 读注册表失败绝不影响加载
        return None


def _elapsed_ms(started: float) -> int:
    """受理期耗时的毫秒数（只给 `stage=accept` 的 `route=` 行用；实测 0–2 ms）。"""
    return int((time.monotonic() - started) * 1000)


def _provider_model(provider) -> str:
    """从 provider 实例上取模型名（`provider_config["model"]` → `get_model()` → `""`）。"""
    try:
        config = getattr(provider, "provider_config", None)
        if isinstance(config, dict):
            model = str(config.get("model") or "")
            if model:
                return model
        getter = getattr(provider, "get_model", None)
        return str(getter()) if callable(getter) else ""
    except BaseException:  # noqa: BLE001
        return ""


#: 加载期一致性告警的**去重集合**（SPEC-v0.5.1 §8.1：每个 id 每进程**至多**一条 WARNING）。
#: 测试可以 `main._CONFIG_CHECK_DONE.clear()` 复现（本模块不提供别的重置入口）。
_CONFIG_CHECK_DONE: set[str] = set()


def _warn_config_once(check_id: str, template: str, *args: object) -> None:
    """打一条 `media_analysis: config-check warn=<id> …`（WARNING，每 id 每进程至多一次）。

    **只告警，不改用户配置**（SPEC-v0.5.1 §8）：配置是运营方的资产，插件只报告怀疑。
    """
    if check_id in _CONFIG_CHECK_DONE:
        return
    _CONFIG_CHECK_DONE.add(check_id)
    LOG.warning("media_analysis: config-check warn=%s " + template, check_id, *args)


class MediaAnalysisPlugin(Star):
    """`media_analyze` 工具的唯一实现体。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)  # Star.__init__ 不保存 config（star/base.py:26-29）
        self.config = config or {}
        # 缓存实例在 `initialize()` 里建（TTL 来自配置）；这里留空，工具被提前调用时
        # 由 `_cache_store()` 兜一个同配置的实例（懒建，绝不因为生命周期顺序而抛）。
        self._cache: cache.DiskCache | None = None
        self._settings_cache: settings.Settings | None = None
        # 引擎激活态（**内存级、进程级**；v0.4.2 新增，SPEC-v0.4.2 §4 / §5.1）。
        # 默认就是 `plugin`（A2）⇒ 没跑过 `initialize()` 时（含测试台 `_rig`）行为与
        # v0.4.1 **逐字一致** —— 这就是"默认不变"的机械证据。
        self._bg: core_mode.Activation = core_mode.INACTIVE
        # 首次调用的一次性复检开关（仅 `effective == core` 时置 True，SPEC-v0.4.2 §4.5）
        self._bg_recheck_pending = False

    async def initialize(self) -> None:
        """生命周期钩子（`star/base.py:141-145`）：装配配置摘要 + 建缓存 + **决定引擎**。

        摘要行只打 **provider id 与数字**（provider id 是配置值不是凭据）；
        弃用键 `stt_provider_id` 只"是否非空"，**不打值**。

        v0.5.1 起 `ready:` 行打**有效值**（SPEC-v0.5.1 §7）：三个字节键改 MB 名，
        两个已删键打**代码常量**，并给出两个"能被兑现的上界"（`native_timeout_cap_s` =
        `DEADLINE_S − NATIVE_RESERVE_S`、`audio_timeout_cap_s` = `AUDIO_HONOR_MAX_S`）。
        仍然是**单行**、前缀不变、可 grep。

        最后一步 `core_mode.activate()` 是 v0.4.2 加的（SPEC-v0.4.2 §8.1/§8.2）：`FuncTool`
        对象随每次 import **重建** ⇒ flag 必须**每次加载都重放**，探测也随之重跑。
        引擎判据走**独立一行**：`plugin` ⇒ DEBUG（生产零噪音）、`core` ⇒ INFO、被拒 ⇒ **ERROR**。
        """
        self._settings_cache = self._settings()
        current = self._settings_cache
        self._cache = cache.DiskCache(
            plugin_name=settings.PLUGIN_NAME, ttl_seconds=current.cache_ttl_seconds
        )
        LOG.info(
            "media_analysis ready: audio_provider_id=%r video_provider_id=%r "
            "vision_provider_id=%r max_media_seconds=%d max_frames=%d "
            "max_media_mb=%d video.max_direct_mb=%d video.compress_target_mb=%d "
            "native_timeout_s=%d native_timeout_cap_s=%d native_max_tokens=%d "
            "compress=%s probe=%s probe_ttl=%d audio_timeout_s=%d "
            "audio_timeout_cap_s=%d cache_ttl_seconds=%d cache_dir=%s",
            current.audio_provider_id,
            current.video_provider_id,
            current.vision_provider_id,
            current.max_media_seconds,
            current.max_frames,
            current.max_media_mb,
            current.video_max_direct_mb,
            current.video_compress_target_mb,
            int(_native_timeout_s()),
            int(analyze.DEADLINE_S - analyze.NATIVE_RESERVE_S),
            int(video_native.NATIVE_MAX_TOKENS),
            "on" if current.video_compress_enabled else "off",
            "on" if current.video_probe_enabled else "off",
            current.video_probe_ttl_seconds,
            current.audio_timeout_seconds,
            int(settings.AUDIO_HONOR_MAX_S),
            current.cache_ttl_seconds,
            self._cache.directory or "(disabled)",
        )
        # 加载期一致性告警（SPEC-v0.5.1 §8.1）：只 WARN、不改配置；三条各有固定 id
        self._config_check(current)
        if current.legacy_stt_provider_id:
            LOG.warning(
                "media_analysis: 配置项 provider_settings.stt_provider_id 已弃用且不生效"
                "（检测到旧值非空，本行不打值）；请改用 provider_settings.audio_provider_id，"
                "旧值可用 tools/migrate_config_v02.py 迁移"
            )
        # 引擎决定的**唯一**入口（同步、幂等、永不抛；失败只回落、绝不改配置）
        self._bg = core_mode.activate(
            self._tool_manager(), requested=current.background_mode
        )
        self._bg_recheck_pending = self._bg.effective == core_mode.MODE_CORE

    def _tool_manager(self):
        """安全取 `get_llm_tool_manager()`（`context is None` / 桩环境 ⇒ `None`，**不抛**）。

        为什么必须容错：既有 `test_ready_line…` 与出厂路径都用 `context=None` 调
        `initialize()`，任何一处抛异常都会把"加载期探测"变成"加载失败"。
        """
        getter = getattr(self.context, "get_llm_tool_manager", None)
        if not callable(getter):
            return None
        try:
            return getter()
        except BaseException as exc:  # noqa: BLE001 —— 取不到就当没有（fail-closed）
            LOG.debug(
                "media_analysis: get_llm_tool_manager 取不到（%s）", type(exc).__name__
            )
            return None

    def _config_check(self, current: settings.Settings) -> None:
        """加载期一致性告警三条（SPEC-v0.5.1 §8.1）：**只 WARN，绝不改用户配置**。

        | id | 判据 | 为什么这么判 |
        | --- | --- | --- |
        | `timeout_over_budget` | **raw** `audio_timeout_seconds` > `MAX_AUDIO_TIMEOUT_SECONDS`(95) | 钳位后恒 ≤ 95，拿钳位值比就是死代码；越界只可能来自"手改 JSON" |
        | `compress_target_ge_075_direct` | `4 × compress_target_mb > 3 × max_direct_mb`（整数比较） | 预算贴着门槛 = 压完仍会被直送闸拒；用整数乘避浮点 |
        | `max_direct_over_suggested` | `max_direct_mb × 1048576 > suggest_direct_bytes(model)` | 门槛高于该模型的延迟舒服值 ⇒ 周期性超时 |

        前两条不需要 provider ⇒ 加载期必打；第三条要 provider（见 `_config_check_provider`）。
        """
        if current.raw_audio_timeout > settings.MAX_AUDIO_TIMEOUT_SECONDS:
            _warn_config_once(
                "timeout_over_budget",
                "key=%s configured_s=%d honorable_s=%d",
                settings.KEY_AUDIO_TIMEOUT_SECONDS,
                current.raw_audio_timeout,
                int(settings.AUDIO_HONOR_MAX_S),
            )
        if 4 * int(current.video_compress_target_mb) > 3 * int(
            current.video_max_direct_mb
        ):
            _warn_config_once(
                "compress_target_ge_075_direct",
                "compress_target_mb=%d max_direct_mb=%d",
                current.video_compress_target_mb,
                current.video_max_direct_mb,
            )
        self._config_check_provider(current)

    def _config_check_provider(self, current: settings.Settings) -> None:
        """告警③ `max_direct_over_suggested`（唯一的 provider 相关检查）。

        **静默读注册表**（`_silent_provider`，§8.2）：冷启动时 provider 还没注册 ⇒
        退化为 DEBUG `skip=… reason=provider_unresolved`（**WARNING 数为 0**），并把
        这条检查顺延到首次 `analyze()`（`media_analyze` 入口再调一次本方法；去重集合
        保证每个 id 每进程至多一条 WARNING）。⇒ 冷启动**既不丢告警、也不污染日志**。
        """
        if "max_direct_over_suggested" in _CONFIG_CHECK_DONE:
            return
        provider_id = current.video_provider_id
        if not provider_id:
            # 明确没配视频位 ⇒ 原生视频链路不会跑，门槛值无意义（不是坏配，不打告警）
            LOG.debug(
                "media_analysis: config-check skip=max_direct_over_suggested "
                "reason=no_video_provider"
            )
            return
        provider = _silent_provider(self.context, provider_id)
        if provider is None:
            LOG.debug(
                "media_analysis: config-check skip=max_direct_over_suggested "
                "reason=provider_unresolved"
            )
            return
        suggested = settings.suggest_direct_bytes(_provider_model(provider))
        if current.video_max_direct_bytes > suggested:
            _warn_config_once(
                "max_direct_over_suggested",
                "max_direct_mb=%d suggested_mb=%d",
                current.video_max_direct_mb,
                settings.bytes_to_mb(suggested),
            )
        else:
            # 已在建议值内 ⇒ 记录"查过了"，后续调用不再重复读注册表
            _CONFIG_CHECK_DONE.add("max_direct_over_suggested")

    async def terminate(self) -> None:
        """卸载：取消全部后台任务 → 丢掉配置快照 → 关闭缓存（条目按 TTL 自然过期）。

        顺序有讲究：先取消任务再关缓存 —— 反过来的话后台任务会在已关闭的缓存上继续跑
        一段（`DiskCache` 的写盘是幂等的，但"先断粮再收工"更干净）。

        取消也是**双保险**：`background.cancel_all()` 对任务表 `cancel()` 并等它们收尾，
        因此插件卸载后不留"持有 event/context 的孤儿 task"（SPEC-v0.4 §2.6）。
        `terminate` 由核心在卸载/重载时调用（`core/star/star_manager.py:1965`），
        且必须是**本类自身** `__dict__` 里的方法（核心只在 `star_cls_type.__dict__` 里找）。

        v0.4.2 追加一步 `core_mode.deactivate()`：把本插件工具上的 `is_background_task`
        置回 `False`（幂等、永不抛）。C 模式下**在飞**的任务归核心 registry 管，这里
        **不取消**（§5.3：任务跨重载继续、唤醒轮仍能交付；"重载即止血"在 C 模式下失效，
        止血口径是"配置改回 `plugin` + 重载"，即 SPEC-v0.4.2 §13.3 的 R1）。
        """
        await background.cancel_all()
        self._bg = core_mode.INACTIVE
        self._bg_recheck_pending = False
        core_mode.deactivate(self._tool_manager())
        self._settings_cache = None
        store, self._cache = self._cache, None
        if store is not None:
            await store.close()

    def _settings(self) -> settings.Settings:
        """唯一配置读取点：21 个键逐条显式读（SPEC-v0.5.1 §2.3 键表 / §10 四处互钉），
        禁止散落到别处。

        漏一个键 ＝ 生产链路上**静默丢弃**设置（父手册 §3 第 8 条）；
        `tests/test_config_map.py` 用四方一致守卫钉住这里。
        布尔走 `ConfigView.flag()`（非 `bool` ⇒ 告警回默认），整数走 `bounded_int`（钳位）。

        v0.5.1 的两处细节：
        - 三个字节键**读的是 MB**，钳位也在 MB 域（`[lo_mb, hi_mb]`），字节只在 `Settings`
          的派生 property 里出现（§2.1「禁止先乘后钳」）；
        - `audio_timeout_seconds` 读**两次**：钳位值（运行期/`ready` 用）+ 原值
          `raw_audio_timeout`（加载期告警①用；钳位后恒 ≤ 95，拿钳位值判越界是死代码）。
        """
        view = settings.ConfigView(self.config)
        return settings.Settings(
            # v0.5.1 第 1 键（组序置顶）：枚举归一（坏值回 plugin + 一条 WARNING）
            background_mode=settings.normalize_background_mode(
                view.get(
                    settings.KEY_BACKGROUND_MODE, settings.DEFAULT_BACKGROUND_MODE
                )
            ),
            audio_provider_id=view.text(settings.KEY_AUDIO_PROVIDER_ID),
            video_provider_id=view.text(settings.KEY_VIDEO_PROVIDER_ID),
            vision_provider_id=view.text(settings.KEY_VISION_PROVIDER_ID),
            legacy_stt_provider_id=view.text(settings.KEY_LEGACY_STT_PROVIDER_ID),
            video_max_direct_mb=view.bounded_int(
                settings.KEY_VIDEO_MAX_DIRECT_MB,
                default=settings.DEFAULT_VIDEO_MAX_DIRECT_MB,
                lo=settings.MIN_VIDEO_MAX_DIRECT_MB,
                hi=settings.MAX_VIDEO_MAX_DIRECT_MB,
            ),
            video_compress_enabled=view.flag(
                settings.KEY_VIDEO_COMPRESS_ENABLED,
                default=settings.DEFAULT_VIDEO_COMPRESS_ENABLED,
            ),
            video_compress_target_mb=view.bounded_int(
                settings.KEY_VIDEO_COMPRESS_TARGET_MB,
                default=settings.DEFAULT_VIDEO_COMPRESS_TARGET_MB,
                lo=settings.MIN_VIDEO_COMPRESS_TARGET_MB,
                hi=settings.MAX_VIDEO_COMPRESS_TARGET_MB,
            ),
            video_compress_timeout_seconds=view.bounded_int(
                settings.KEY_VIDEO_COMPRESS_TIMEOUT_SECONDS,
                default=settings.DEFAULT_VIDEO_COMPRESS_TIMEOUT_SECONDS,
                lo=settings.MIN_VIDEO_COMPRESS_TIMEOUT_SECONDS,
                hi=settings.MAX_VIDEO_COMPRESS_TIMEOUT_SECONDS,
            ),
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
            max_media_mb=view.bounded_int(
                settings.KEY_MAX_MEDIA_MB,
                default=settings.DEFAULT_MAX_MEDIA_MB,
                lo=settings.MIN_MAX_MEDIA_MB,
                hi=settings.MAX_MAX_MEDIA_MB,
            ),
            audio_timeout_seconds=view.bounded_int(
                settings.KEY_AUDIO_TIMEOUT_SECONDS,
                default=settings.DEFAULT_AUDIO_TIMEOUT_SECONDS,
                lo=settings.MIN_AUDIO_TIMEOUT_SECONDS,
                hi=settings.MAX_AUDIO_TIMEOUT_SECONDS,
            ),
            cache_ttl_seconds=view.bounded_int(
                settings.KEY_CACHE_TTL_SECONDS,
                default=settings.DEFAULT_CACHE_TTL_SECONDS,
                lo=settings.MIN_CACHE_TTL_SECONDS,
                hi=settings.MAX_CACHE_TTL_SECONDS,
            ),
            prompt_audio=view.text(settings.KEY_PROMPTS_AUDIO),
            prompt_video=view.text(settings.KEY_PROMPTS_VIDEO),
            prompt_frame=view.text(settings.KEY_PROMPTS_FRAME),
            prompt_summary=view.text(settings.KEY_PROMPTS_SUMMARY),
            prompt_video_native=view.text(settings.KEY_PROMPTS_VIDEO_NATIVE),
            # 非键字段：同一配置键（limits.audio_timeout_seconds）的**原值**，只服务告警①
            raw_audio_timeout=view.raw_int(
                settings.KEY_AUDIO_TIMEOUT_SECONDS,
                default=settings.DEFAULT_AUDIO_TIMEOUT_SECONDS,
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
        """分析当前消息或引用消息里的语音/视频/GIF 动图（视频默认整段直送多模态模型）。

        当用户发来语音、视频、GIF 动图，或引用了一条语音/视频/GIF，并且想知道"里面说了
        什么 / 画面发生了什么"时调用本工具。**群里以「文件」形式发来的视频/语音/GIF 也用
        current 或 quoted 取，不要传文件路径**。普通图片（JPEG/PNG/WebP/APNG）不在本工具
        范围内。

        **本工具立即返回，分析在后台进行**：（若 AstrBot 已把本工具作为**原生后台任务**
        派发，你会收到框架的受理回执而不是下面的 JSON；语义相同：结果稍后另行发送。）
        否则返回的要么是受理回执
        {"ok":true,"state":"accepted","task_id":"…","hint":"…"}（同一条消息同一组参数重复
        调用时 state 为 "running"，task_id 不变），要么是失败信封
        {"ok":false,"error":"…","hint":"…"}。**分析结果不会回到你手里**——它会在完成时
        自动发送给用户（一条中文消息，含摘要与"转写 N 字 / 耗时 N 秒"等页脚）。
        因此你只需要回一句"已受理，稍后把结果发来"；不要编造分析内容，不要复述 task_id，
        也不要在后续回合重复调用本工具。

        视频默认走"原生视频"（一次调用同时看画面和听声音）。只有在视频模型不可用/视频太大
        且压不下去/调用失败时，插件才按 include_frames 决定是否抽帧回退。GIF 会先归一成
        mp4 再走同一条视频链，失败时报 GIF_CONVERT_FAILED。

        Args:
            target (string): 分析哪条消息。current＝当前消息（默认），quoted＝被引用的那条消息。
            focus (string): 用户想了解的重点问题（如「他在说什么事」）。留空表示要一份通篇摘要。
            include_frames (string): 三态。auto（默认）＝要画面，插件自己选路（原生视频优先，失败时若配了视觉模型就抽帧）；true＝明确要画面，原生失败必须抽帧回退；false＝明确不要画面，原生失败时绝不抽帧（只交音轨转写）。

        Returns:
            str: 受理信封（4 键）或失败信封（3 键）的 JSON 文本；分析结果由后台任务
            单独发送给用户，不经由本返回值。
        """
        # 加载期没能解析到 provider 的告警③在这里顺延（SPEC-v0.5.1 §8.2）：
        # 冷启动时 provider 尚未注册，首次调用时再去注册表静默看一眼（每 id 至多一条 WARNING）。
        self._config_check_provider(self._settings_cache or self._settings())
        mode = self._bg.effective  # ① 入口快照：一次调用只认一个引擎（§5.2）
        if mode == core_mode.MODE_CORE:  # ② 单值分支；C 分支**必然 return**
            if self._bg_recheck_pending:  # ③ 一次性复检（§4.5）：只影响**后续**调用
                self._bg_recheck_pending = False
                self._bg = core_mode.activate(
                    self._tool_manager(), requested=core_mode.MODE_CORE
                )
            return await self._run_core(
                event=event,
                target=target,
                focus=focus,
                include_frames=include_frames,
            )
        # —— 以下逐字沿用 v0.4.1 的 A2 门面（try/except 三出口不变）——
        # 注意：`mode` 这个名字在这里被**有意复用**（下面两行是 v0.4.1 原文）—— A2 分支
        # 用的是 `include_frames` 的三态；引擎快照已在上面那个 if 里用完（C 分支必然 return）。
        accept_started = time.monotonic()
        try:
            mode = _as_mode(include_frames)
            # D2 唤醒轮短路（SPEC-v0.5.1 §5.2）：位置在 `mode` 归一之后、`find_media` 之前。
            # 合成事件（核心 `CronMessageEvent` / 后台任务结果轮）**本来就不该**带媒体 ⇒
            # 立刻回一条**专用 hint** 的失败信封，**不**进 `background.submit()`
            # （⇒ 不产生 `task_id`，也不会有"唤醒轮里又跑一次分析"）。
            # `is_wake_round` 永不抛（fail-open）；`chain_has_media` 只做类型判定
            # （0 下载 / 0 ffmpeg / 0 provider），链里**真有**媒体 ⇒ 走正常路径（退路）。
            if analyze.is_wake_round(event) and not await analyze.chain_has_media(
                event, target
            ):
                analyze.log_route(
                    route=analyze.ROUTE_NONE,
                    stage=analyze.STAGE_ACCEPT,
                    engine=analyze.ENGINE_PLUGIN,
                    error=envelope.ERR_MEDIA_NOT_FOUND,
                    elapsed_ms=_elapsed_ms(accept_started),
                    wake_round=True,
                )
                return envelope.dumps(envelope.fail_envelope_wake())
            # 受理路径：类型判定 + 至多 6 字节魔数读（0 下载、0 转换、0 ffmpeg、0 provider）
            await media.find_media(event, target)
            payload = await background.submit(
                context=self.context,
                event=event,
                target=target,
                focus=focus,
                mode=mode,
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
            # 受理期拒绝 = `stage=accept` 那一行（SPEC-v0.5.1 §6.2/§6.3）：BAD_TARGET /
            # MEDIA_NOT_FOUND / TOO_MANY_TASKS 都从这里出去。链路没开跑 ⇒ kind=`-`、
            # fallback=`-`、cached=0、duration_s=0.00、frames=0、transcript_chars=0。
            analyze.log_route(
                route=analyze.ROUTE_NONE,
                stage=analyze.STAGE_ACCEPT,
                engine=analyze.ENGINE_PLUGIN,
                error=exc.code,
                elapsed_ms=_elapsed_ms(accept_started),
            )
            return envelope.dumps(envelope.fail_envelope(exc.code))
        except BaseException as exc:  # noqa: BLE001 —— 工具永不把异常抛给模型
            LOG.error(
                "media_analyze internal error: %s\n%s",
                redact_sensitive_text(str(exc))[:200],
                redact_sensitive_text(traceback.format_exc())[:4000],
            )
            analyze.log_route(
                route=analyze.ROUTE_NONE,
                stage=analyze.STAGE_ACCEPT,
                engine=analyze.ENGINE_PLUGIN,
                error=envelope.ERR_INTERNAL_ERROR,
                elapsed_ms=_elapsed_ms(accept_started),
            )
            return envelope.dumps(envelope.fail_envelope(envelope.ERR_INTERNAL_ERROR))

    async def _run_core(
        self,
        *,
        event,
        target: str,
        focus: str,
        include_frames: object,
    ) -> str:
        """C 模式（核心原生后台）的唯一入口：取快照 → 交给 `core_mode.run`。

        只做两件事（SPEC-v0.4.2 §6.1）：取一次 `settings` / `cache` 的**快照**，再调
        `core_mode.run`。**不** try/except —— 异常口径全在 `core_mode.run` 里（只有
        `asyncio.CancelledError` 上抛）。

        为什么必须"按值取好再往下传"：C 模式的任务可能在插件**重载之后**才跑完，那时旧
        实例的 `_settings_cache` 已被 `terminate()` 置 `None`（§8.3）；在 `run` 里反复
        `self._settings()` 会读到 `None`，这里一次取好就没有这个窗口。
        """
        current = self._settings_cache or self._settings()
        return await core_mode.run(
            context=self.context,
            event=event,
            target=target,
            focus=focus,
            include_frames=include_frames,
            settings=current,
            cache=self._cache_store(),
        )
