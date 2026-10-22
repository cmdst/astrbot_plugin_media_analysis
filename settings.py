"""配置读取层：11 个键 + 点号读取 + 钳位 + 提示词校验（SPEC-v0.2 §5.2 / §7.1）。

模块名**刻意不叫 `config`**：`Star.__init__` 的第二个形参就叫 `config`，加载器按
`star_cls_type(context=…, config=…)` 关键字调用（`star_manager.py:1229-1238`），
模块名撞形参名会让 `from . import config` 与形参在 `Star.__init__` 里打架。

`AstrBotConfig` 是 `dict` 子类，**没有**点号 `get`（`config/astrbot_config.py:33-60`），
所以点号能力由本模块的 `ConfigView` 提供（形状照屋内先例 `log_watcher/config_map.py`）。

本模块**不提供**第二个 `load_*` 入口：唯一的组装点是 `main._settings()`（SPEC §10），
三方一致守卫测试（`tests/test_config_map.py`）就是靠"只有一个读取点"才钉得住。

**11 个键里有一个是弃用键**（`provider_settings.stt_provider_id`，SPEC-v0.2 §5.3）：
它必须继续留在 schema 与 `KEY_MAP` 里 —— 从 schema 删掉的那一刻，核心
`check_config_integrity()` 会把用户配置里的旧值当 stale 键**静默删除并 `save_config()`**
（`astrbot_config.py:252-266` + `:113-121`）。代码只"读而不生效"，迁移脚本负责搬值。
"""

from __future__ import annotations

import hashlib
import string
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cached_property
from typing import Any

from astrbot.api import logger as LOG

PLUGIN_NAME = "astrbot_plugin_media_analysis"
"""插件目录名（== `metadata.yaml:name`，也 == 缓存目录名）。"""

PLUGIN_VERSION = "0.2.0"
"""与 `metadata.yaml:version` 去掉 `v` 前缀后逐字相等（缓存键成分，见 `cache.build_key`）。"""

# --------------------------------------------------------------------------- #
# 点号键（11 条，SPEC-v0.2 §5.2 键表）
# --------------------------------------------------------------------------- #
KEY_AUDIO_PROVIDER_ID = "provider_settings.audio_provider_id"
KEY_VISION_PROVIDER_ID = "provider_settings.vision_provider_id"
KEY_LEGACY_STT_PROVIDER_ID = "provider_settings.stt_provider_id"  # 已弃用，只读不生效
KEY_MAX_MEDIA_SECONDS = "limits.max_media_seconds"
KEY_MAX_FRAMES = "limits.max_frames"
KEY_MAX_MEDIA_BYTES = "limits.max_media_bytes"
KEY_PROMPTS_AUDIO = "prompts.audio"
KEY_PROMPTS_VIDEO = "prompts.video"
KEY_PROMPTS_FRAME = "prompts.frame"
KEY_PROMPTS_SUMMARY = "prompts.summary"
KEY_CACHE_TTL_SECONDS = "cache.ttl_seconds"

# 点号路径 → Settings 字段名。这是「schema ⇄ KEY_MAP ⇄ 读取点」三方守卫的事实源。
# 值集合 == Settings 的字段集合（11 条，一一对应；派生量走 properties，不算字段）。
KEY_MAP: dict[str, str] = {
    KEY_AUDIO_PROVIDER_ID: "audio_provider_id",
    KEY_VISION_PROVIDER_ID: "vision_provider_id",
    KEY_LEGACY_STT_PROVIDER_ID: "legacy_stt_provider_id",
    KEY_MAX_MEDIA_SECONDS: "max_media_seconds",
    KEY_MAX_FRAMES: "max_frames",
    KEY_MAX_MEDIA_BYTES: "max_media_bytes",
    KEY_PROMPTS_AUDIO: "prompt_audio",
    KEY_PROMPTS_VIDEO: "prompt_video",
    KEY_PROMPTS_FRAME: "prompt_frame",
    KEY_PROMPTS_SUMMARY: "prompt_summary",
    KEY_CACHE_TTL_SECONDS: "cache_ttl_seconds",
}

# --------------------------------------------------------------------------- #
# 默认值与钳位（SPEC-v0.2 §5.2「默认值依据」）
# --------------------------------------------------------------------------- #
DEFAULT_MAX_MEDIA_SECONDS = 60
DEFAULT_MAX_FRAMES = 6
DEFAULT_MAX_MEDIA_BYTES = 67108864  # 64 MiB：生产那条群视频 5.64 MB 的 11 倍余量
DEFAULT_CACHE_TTL_SECONDS = 86400  # 1 天

# 钳位区间：不是产品承诺，只防"配置手滑成 100000"。
MIN_MAX_MEDIA_SECONDS, MAX_MAX_MEDIA_SECONDS = 5, 300
MIN_MAX_FRAMES, MAX_MAX_FRAMES = 1, 12
MIN_MAX_MEDIA_BYTES, MAX_MAX_MEDIA_BYTES = 1048576, 536870912
MIN_CACHE_TTL_SECONDS, MAX_CACHE_TTL_SECONDS = 0, 2592000  # 0＝关闭，上限 30 天

PROMPT_MAX_CHARS = 2000  # 单份提示词模板上限（§7.3）
MATERIAL_MAX_CHARS = 8000  # 摘要材料上限（§7.3）

# --------------------------------------------------------------------------- #
# 提示词（4 份；默认值与 `_conf_schema.json` 的 `default` **逐字相同**，§5.1）
# --------------------------------------------------------------------------- #
PROMPT_KEYS: tuple[str, ...] = ("audio", "video", "frame", "summary")

DEFAULT_PROMPTS: dict[str, str] = {
    "audio": (
        "这是一段语音消息的录音。请把音频里说的话完整转写成文字：\n"
        "1) 保留原话，不要总结、不要改写、不要补充音频里没有的信息；\n"
        "2) 说话人变化或明显停顿处换行；\n"
        "3) 听不清的地方写 [听不清]，不要猜；\n"
        "4) 只输出转写结果本身，不要任何前言、标题或解释。\n"
        "如果整段音频里没有任何人声，只输出：[无语音]\n"
        "{focus_clause}"
    ),
    "video": (
        "这是一段视频的音轨（画面另有抽帧分析）。请把音轨里说的话完整转写成文字：\n"
        "1) 保留原话，不要总结、不要改写、不要补充音频里没有的信息；\n"
        "2) 如果是纯音乐、环境音或没有人声，只输出：[无语音]；\n"
        "3) 听不清的地方写 [听不清]，不要猜；\n"
        "4) 只输出转写结果本身，不要任何前言、标题或解释。\n"
        "{focus_clause}"
    ),
    "frame": (
        "这是一段视频里截取的一帧画面。请用一到两句话客观描述画面里能看到的内容"
        "（人物/动作/场景/画面上的文字）。不要猜测视频主题，不要评价，不要补充画面里没有的信息。\n"
        "{focus_clause}"
    ),
    "summary": (
        "下面的材料来自一段{kind_cn}的分析结果。{task}\n\n材料：\n{material}"
    ),
}

# 允许的占位符（超出即坏值 ⇒ 回退默认模板，§7.1）
ALLOWED_PLACEHOLDERS: dict[str, frozenset[str]] = {
    "audio": frozenset({"focus_clause"}),
    "video": frozenset({"focus_clause"}),
    "frame": frozenset({"focus_clause"}),
    "summary": frozenset({"kind_cn", "task", "material"}),
}

# 焦点从句（音频/视频用；frame 用 analyze.FRAME_FOCUS_CLAUSE，逐字沿用 v0.1）
FOCUS_CLAUSE = (
    "另外，用户关心的是「{focus}」：音频里如果提到与之相关的内容，"
    "请特别留意并原样转写出来（仍然只做转写，不要改成回答）。"
)

_WARNED: set[tuple[str, str]] = set()


def placeholder_names(template: str) -> set[str] | None:
    """解析模板里的占位符名；花括号本身坏了（`ValueError`）或非简单名字 ⇒ `None`。

    只接受 `{name}` 形式（`str.isidentifier()`）：`{a.b}` / `{a[0]}` / `{}` / `{0}`
    一律算坏值 —— 那些写法会把"用户配置写错"变成运行期的 `KeyError`/`IndexError`，
    而规格要求坏值**在装配期**就回退默认（§7.1）。
    """
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError:
        return None
    names: set[str] = set()
    for _literal, field_name, _spec, _conversion in parsed:
        if field_name is None:
            continue
        if not field_name.isidentifier():
            return None
        names.add(field_name)
    return names


def _renderable(template: str, allowed: frozenset[str]) -> bool:
    """模板能安全渲染且只用允许的占位符。"""
    names = placeholder_names(template)
    if names is None:
        return False
    if not names.issubset(allowed):
        return False
    try:
        template.format(**{name: "x" for name in allowed})
    except (KeyError, IndexError, ValueError, AttributeError):
        return False
    return True


def _warn_once(key: str, reason: str) -> None:
    """同一份提示词的同一类坏值只告警一次（**不打印模板内容**，配置值不进日志）。"""
    token = (key, reason)
    if token in _WARNED:
        return
    _WARNED.add(token)
    LOG.warning(
        "media_analysis: 提示词 prompts.%s 非法（%s），已回退内置默认值",
        key,
        reason,
    )


def validate_prompts(raw: Mapping[str, Any]) -> tuple[dict[str, str], frozenset[str]]:
    """把配置原文变成**生效模板**：空值→默认、非法→默认、超长→截断。

    返回 `(生效模板 dict, 被截断的键集合)`。校验先于截断：先确认模板可渲染且只用允许的
    占位符，再截断；若截断把占位符切成半句（尾部正好被切断）导致不可渲染，同样回退默认
    —— 宁可给模型一份完好的默认提示词，也不给它半句话（§7.1）。
    """
    effective: dict[str, str] = {}
    trimmed: set[str] = set()
    allowed_all = ALLOWED_PLACEHOLDERS
    for key in PROMPT_KEYS:
        value = raw.get(key, "")
        template = value.strip() if isinstance(value, str) else ""
        if not template:
            effective[key] = DEFAULT_PROMPTS[key]
            continue
        allowed = allowed_all[key]
        if not _renderable(template, allowed):
            _warn_once(key, "非法花括号或未知占位符")
            effective[key] = DEFAULT_PROMPTS[key]
            continue
        if len(template) > PROMPT_MAX_CHARS:
            cut = template[:PROMPT_MAX_CHARS]
            if not _renderable(cut, allowed):
                _warn_once(key, "截断后不可渲染")
                effective[key] = DEFAULT_PROMPTS[key]
                continue
            effective[key] = cut
            trimmed.add(key)
            continue
        effective[key] = template
    return effective, frozenset(trimmed)


@dataclass(frozen=True)
class Settings:
    """一次装配、整轮复用的配置快照（配置改动仍需重启，核心不在运行期热更）。

    字段 == `KEY_MAP` 的值集合（11 条，一一对应）。四个 `prompt_*` 字段存的是**配置原文**
    （`ConfigView.text` 去空白后），生效模板与派生量走下面的缓存属性：

    - `prompts`：4 元组，顺序固定 `(audio, video, frame, summary)`，已做"回退默认 + 截断"；
    - `prompts_trimmed`：被截断的键集合（调用时据此加 `prompt_trimmed:<key>` note）；
    - `prompts_fp`：4 份生效模板的指纹 `sha256("\\x00".join(...))[:16]`（缓存键成分）。
    """

    audio_provider_id: str = ""
    vision_provider_id: str = ""
    legacy_stt_provider_id: str = ""
    max_media_seconds: int = DEFAULT_MAX_MEDIA_SECONDS
    max_frames: int = DEFAULT_MAX_FRAMES
    max_media_bytes: int = DEFAULT_MAX_MEDIA_BYTES
    prompt_audio: str = ""
    prompt_video: str = ""
    prompt_frame: str = ""
    prompt_summary: str = ""
    cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS

    @cached_property
    def _effective_prompts(self) -> tuple[dict[str, str], frozenset[str]]:
        return validate_prompts(
            {
                "audio": self.prompt_audio,
                "video": self.prompt_video,
                "frame": self.prompt_frame,
                "summary": self.prompt_summary,
            }
        )

    @property
    def prompts(self) -> tuple[str, str, str, str]:
        """生效模板 4 元组（顺序 `audio, video, frame, summary`，一次装配多次复用）。"""
        effective = self._effective_prompts[0]
        return (
            effective["audio"],
            effective["video"],
            effective["frame"],
            effective["summary"],
        )

    @property
    def prompts_trimmed(self) -> frozenset[str]:
        """超 2000 字符被截断的提示词键集合。"""
        return self._effective_prompts[1]

    @cached_property
    def prompts_fp(self) -> str:
        """4 份生效模板的指纹（缓存键成分，§6.1）。"""
        joined = "\x00".join(self.prompts)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


class ConfigView:
    """点号读取视图；`raw` 可以是 `AstrBotConfig`、普通 dict 或 None。"""

    def __init__(self, raw: Mapping[str, Any] | None) -> None:
        self._raw: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}

    def get(self, path: str, default: Any = None) -> Any:
        """按点号路径取值；路径不存在或中间层不是 mapping 时回 `default`。

        空 dict / 空 list 都是**合法值**，不会被 default 顶掉（只有 `None` 会）。
        """
        node: Any = self._raw
        for part in str(path).split("."):
            if not isinstance(node, Mapping) or part not in node:
                return default
            node = node[part]
        return default if node is None else node

    def text(self, path: str) -> str:
        """取字符串配置；去首尾空白，非 `str` 一律 `""`。"""
        value = self.get(path, "")
        if isinstance(value, str):
            return value.strip()
        return ""

    def bounded_int(self, path: str, *, default: int, lo: int, hi: int) -> int:
        """取整数配置并钳位到 `[lo, hi]`；非法值回 `default`，越界钳位并告警。"""
        value = self.get(path, default)
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            LOG.warning(
                "media_analysis: 配置 %s 不是整数（%s），改用默认值 %d",
                path,
                type(value).__name__,
                default,
            )
            return default
        try:
            number = int(value)
        except (TypeError, ValueError):
            LOG.warning(
                "media_analysis: 配置 %s 无法转成整数（%s），改用默认值 %d",
                path,
                type(value).__name__,
                default,
            )
            return default
        if number < lo:
            LOG.warning(
                "media_analysis: 配置 %s=%d 小于下限 %d，已钳位", path, number, lo
            )
            return lo
        if number > hi:
            LOG.warning(
                "media_analysis: 配置 %s=%d 大于上限 %d，已钳位", path, number, hi
            )
            return hi
        return number
