"""配置读取层：21 个键 + 点号读取 + 钳位 + MB 换算 + 提示词校验
（SPEC-v0.2 §5.2 / SPEC-v0.3 §8 / SPEC-v0.4.2 §3 / SPEC-v0.5.1 §2）。

模块名**刻意不叫 `config`**：`Star.__init__` 的第二个形参就叫 `config`，加载器按
`star_cls_type(context=…, config=…)` 关键字调用（`star_manager.py:1229-1238`），
模块名撞形参名会让 `from . import config` 与形参在 `Star.__init__` 里打架。

`AstrBotConfig` 是 `dict` 子类，**没有**点号 `get`（`config/astrbot_config.py:33-60`），
所以点号能力由本模块的 `ConfigView` 提供（形状照屋内先例 `log_watcher/config_map.py`）。

本模块**不提供**第二个 `load_*` 入口：唯一的组装点是 `main._settings()`（SPEC §10），
四方一致守卫测试（`tests/test_config_map.py`）就是靠"只有一个读取点"才钉得住。

**v0.5.1 的配置面（SPEC-v0.5.1 §2）：22 叶 → 21 叶**，四类动作——

| 动作 | 键 | 落点 |
| --- | --- | --- |
| 改名 3（字节 → **MB**） | `video.max_direct_mb` / `video.compress_target_mb` / `limits.max_media_mb` | 本模块常量 + `Settings` 的 MB 字段 + 派生字节 property |
| 删除 2（→ **代码常量**） | `video` 组的 `native_max_tokens` / `native_timeout_seconds` 两键 | `video_native.NATIVE_MAX_TOKENS` / `analyze.NATIVE_TIMEOUT_S` |
| 新增 1 | `limits.audio_timeout_seconds` | 本模块 `DEFAULT_AUDIO_TIMEOUT_SECONDS` |
| 隐藏 4（item 级 `invisible`，**键与值都保留**） | `provider_settings.stt_provider_id` / `video.probe_enabled` / `video.probe_ttl_seconds` / `video.compress_timeout_seconds` | 键表照旧 |

**单位口径（§2.1 写死）**：`1 MB ≡ 1 MiB ≡ 1048576 字节`（二进制，**不是** 10^6）；
`mb(bytes)` 是 half-up 四舍五入。**钳位顺序**：先按 MB 钳到 `[lo_mb, hi_mb]`，**再**乘
`BYTES_PER_MB`（禁止"先乘后钳"：浮点/溢出）。换算**只发生在三处**：本模块的两个纯函数、
`Settings` 的三个派生 property、迁移器自己那份**受守卫对偶**（`tests/test_migrate_config_v05.py`
用边界表断言两侧同值）。

为什么存 MB、派生字节：消费方（`analyze` / `media` / `video_native`）读的仍是**字节**
（`Settings.video_max_direct_bytes` 等），所以这次改名对运行期是**零改动** ——
除了两个已删的键（`native_*`），它们的值搬进了代码常量。

`background.mode`（v0.4.2 新增，本版**只挪位置不改默认**）取值只有 `plugin`（默认，
插件自管后台 A2）/ `core`（核心原生后台，**实验**，需核心一行补丁 + 重启）。坏值一律
**回 `plugin`**（保守侧：少花钱、少不确定性），见 `normalize_background_mode`。

`provider_settings.stt_provider_id` 仍是**弃用键**（SPEC-v0.2 §5.3）：它必须继续留在
schema 与 `KEY_MAP` 里 —— 从 schema 删掉的那一刻，核心 `check_config_integrity()` 会把
用户配置里的旧值当 stale 键**静默删除并 `save_config()`**（`astrbot_config.py:252-266`
+ `:113-121`）。v0.5.1 把它**隐藏**（item 级 `invisible`）而不是删除：键与值都保号，
只是面板不再渲染它。
"""

from __future__ import annotations

import fnmatch
import hashlib
import string
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cached_property
from typing import Any

from astrbot.api import logger as LOG

PLUGIN_NAME = "astrbot_plugin_media_analysis"
"""插件目录名（== `metadata.yaml:name`，也 == 缓存 / probe 目录名）。"""

PLUGIN_VERSION = "0.5.1"
"""与 `metadata.yaml:version` 去掉 `v` 前缀后逐字相等（缓存键与 probe 键成分）。"""

# --------------------------------------------------------------------------- #
# 点号键（21 条，SPEC-v0.5.1 §2.3 键表；
# 顺序 == schema 顺序 == 迁移器 `migrate_config_v05.py:KEY_ORDER` 顺序）
#
# v0.5.1 的四处改动（§2.2 四类动作）：
#   · 三个字节键**改名**成 `*_mb`（值按 MB 换算）；
#   · 两个 `native_*` 键**删除**（值进代码常量）；
#   · 一个 `limits.audio_timeout_seconds` **新增**；
#   · 四个键**隐藏**（键与值都保留，只加 item 级 `invisible`）。
# 组序（§2.3 冻死）：`background → provider_settings → video → limits → cache → prompts`。
# --------------------------------------------------------------------------- #
KEY_BACKGROUND_MODE = "background.mode"  # v0.4.2 新增；v0.5.1 组序置顶

KEY_AUDIO_PROVIDER_ID = "provider_settings.audio_provider_id"
KEY_VIDEO_PROVIDER_ID = "provider_settings.video_provider_id"  # v0.3 新增
KEY_VISION_PROVIDER_ID = "provider_settings.vision_provider_id"
KEY_LEGACY_STT_PROVIDER_ID = "provider_settings.stt_provider_id"  # 已弃用；v0.5.1 隐藏

KEY_VIDEO_MAX_DIRECT_MB = "video.max_direct_mb"  # v0.5.1 改名（原 _bytes）
KEY_VIDEO_COMPRESS_ENABLED = "video.compress_enabled"
KEY_VIDEO_COMPRESS_TARGET_MB = "video.compress_target_mb"  # v0.5.1 改名（原 _bytes）
KEY_VIDEO_COMPRESS_TIMEOUT_SECONDS = "video.compress_timeout_seconds"  # v0.5.1 隐藏
KEY_VIDEO_PROBE_ENABLED = "video.probe_enabled"  # v0.3 新增；v0.5.1 隐藏
KEY_VIDEO_PROBE_TTL_SECONDS = "video.probe_ttl_seconds"  # v0.5.1 隐藏

KEY_MAX_MEDIA_SECONDS = "limits.max_media_seconds"
KEY_MAX_FRAMES = "limits.max_frames"
KEY_MAX_MEDIA_MB = "limits.max_media_mb"  # v0.5.1 改名（原 _bytes）
KEY_AUDIO_TIMEOUT_SECONDS = "limits.audio_timeout_seconds"  # v0.5.1 新增

KEY_CACHE_TTL_SECONDS = "cache.ttl_seconds"

KEY_PROMPTS_AUDIO = "prompts.audio"
KEY_PROMPTS_VIDEO = "prompts.video"
KEY_PROMPTS_FRAME = "prompts.frame"
KEY_PROMPTS_SUMMARY = "prompts.summary"
KEY_PROMPTS_VIDEO_NATIVE = "prompts.video_native"  # v0.3 新增

# 点号路径 → Settings 字段名。这是「schema ⇄ KEY_MAP ⇄ 读取点 ⇄ 迁移器 KEY_ORDER」
# 四处守卫的事实源。值集合 == Settings 的字段集合（21 条，一一对应；派生量走
# properties，不算字段；`raw_audio_timeout` 是同一配置键的**原值**，不是配置键）。
KEY_MAP: dict[str, str] = {
    KEY_BACKGROUND_MODE: "background_mode",
    KEY_AUDIO_PROVIDER_ID: "audio_provider_id",
    KEY_VIDEO_PROVIDER_ID: "video_provider_id",
    KEY_VISION_PROVIDER_ID: "vision_provider_id",
    KEY_LEGACY_STT_PROVIDER_ID: "legacy_stt_provider_id",
    KEY_VIDEO_MAX_DIRECT_MB: "video_max_direct_mb",
    KEY_VIDEO_COMPRESS_ENABLED: "video_compress_enabled",
    KEY_VIDEO_COMPRESS_TARGET_MB: "video_compress_target_mb",
    KEY_VIDEO_COMPRESS_TIMEOUT_SECONDS: "video_compress_timeout_seconds",
    KEY_VIDEO_PROBE_ENABLED: "video_probe_enabled",
    KEY_VIDEO_PROBE_TTL_SECONDS: "video_probe_ttl_seconds",
    KEY_MAX_MEDIA_SECONDS: "max_media_seconds",
    KEY_MAX_FRAMES: "max_frames",
    KEY_MAX_MEDIA_MB: "max_media_mb",
    KEY_AUDIO_TIMEOUT_SECONDS: "audio_timeout_seconds",
    KEY_CACHE_TTL_SECONDS: "cache_ttl_seconds",
    KEY_PROMPTS_AUDIO: "prompt_audio",
    KEY_PROMPTS_VIDEO: "prompt_video",
    KEY_PROMPTS_FRAME: "prompt_frame",
    KEY_PROMPTS_SUMMARY: "prompt_summary",
    KEY_PROMPTS_VIDEO_NATIVE: "prompt_video_native",
}

#: `Settings` 里**不对应配置键**的字段（白名单）：`raw_audio_timeout` 是
#: `limits.audio_timeout_seconds` 的**原值**（不钳位），只服务加载期告警①的越界判定。
NON_KEY_FIELDS: frozenset[str] = frozenset({"raw_audio_timeout"})

# --------------------------------------------------------------------------- #
# 默认值与钳位（SPEC-v0.2 §5.2「默认值依据」+ SPEC-v0.3 §8.1 + SPEC-v0.5.1 §2.3/§2.4）
# --------------------------------------------------------------------------- #
BYTES_PER_MB = 1048576
"""1 MB ≡ 1 MiB（SPEC-v0.5.1 §2.1）：**二进制** MB，不是 10^6。

【agent 判断 J2·可否决】现网三个字节键的每个"整值"都是 2 的幂（8388608 / 33554432 /
67108864），换成 10^6 会让"8 MB"变成 8.39 MB，面板与 README 的历史口径全部错位。
"""


def mb_to_bytes(value: int) -> int:
    """MB → 字节（纯乘法；**钳位在前**，见模块文档的「钳位顺序」）。"""
    return int(value) * BYTES_PER_MB


def bytes_to_mb(value: int) -> int:
    """字节 → MB，**half-up 四舍五入**（SPEC-v0.5.1 §2.1：`(bytes + 524288) // 1048576`）。

    与迁移器 `tools/migrate_config_v05.py:bytes_to_mb()` 是**受守卫对偶**
    （`tests/test_migrate_config_v05.py` 用边界表断言两侧同值）—— 迁移器不 import 插件
    模块，所以这份对偶只能靠测试钉住，不能靠 import 消掉。
    """
    return (int(value) + BYTES_PER_MB // 2) // BYTES_PER_MB


DEFAULT_MAX_MEDIA_SECONDS = 60
DEFAULT_MAX_FRAMES = 6
DEFAULT_CACHE_TTL_SECONDS = 86400  # 1 天

# v0.5.1：三个字节键的 MB 默认值（== `_conf_schema.json` 的 `default`，逐字相同）
DEFAULT_MAX_MEDIA_MB = 64  # 64 MiB：生产那条群视频 5.64 MB 的 11 倍余量
MIN_MAX_MEDIA_MB, MAX_MAX_MEDIA_MB = 1, 512

DEFAULT_VIDEO_MAX_DIRECT_MB = 8  # 按**延迟**定的直送门槛（非 provider 上限）
MIN_VIDEO_MAX_DIRECT_MB, MAX_VIDEO_MAX_DIRECT_MB = 1, 32  # 32 同时是内存硬上限

DEFAULT_VIDEO_COMPRESS_TARGET_MB = 6
"""压缩目标：v0.5.1 默认 **8 → 6**（【agent 判断 J1b·可否决】，D-3 已批准）。

理由：加载期告警②的判据是「`compress_target_mb ≥ 0.75 × max_direct_mb` 属坏配」，
默认对 (8, 8) 恰好落在坏配里 ⇒ 交付即告警 = 把告警变成噪声。6/8 = 0.75 且判据取
**严格大于**，默认对不触发。**只影响全新安装**（存量由迁移保号）。
"""
MIN_VIDEO_COMPRESS_TARGET_MB, MAX_VIDEO_COMPRESS_TARGET_MB = 1, 32
"""下限 v0.5.1 从 0.5 MiB 收窄到 **1 MiB**（【agent 判断 J1·可否决】，D-4 已批准）。

整数 MB + slider 无法表达 0.5；生产值是 29，无人命中旧下限。否决方案（若运营方要 0.5）：
把该叶改成 `float` + `slider{min:0.5,max:32,step:0.5}`，改动点仅 schema `type` 与本节 2 个常量。
"""

DEFAULT_AUDIO_TIMEOUT_SECONDS = 60
"""`limits.audio_timeout_seconds` 默认 60（== v0.2–v0.4.2 硬编码的 `AUDIO_TIMEOUT_S`）。"""

AUDIO_TIMEOUT_DEFAULT_S = float(DEFAULT_AUDIO_TIMEOUT_SECONDS)
"""单次多模态音频调用超时的**默认秒数**（同一事实的浮点形态；`analyze.transcribe` 的 `cap`）。"""

MIN_AUDIO_TIMEOUT_SECONDS = 10

AUDIO_HONOR_MAX_S = 95.0
"""配置值能被**兑现**的上界（SPEC-v0.5.1 §4.1）：`= analyze.DEADLINE_S(100) − 5.0`。

超时链路的预算是 `budgeted_timeout(remaining, cap, reserve)` ⇒ 任何大于
`DEADLINE_S − 5` 的 cap 都不可能被兑现（填 96 与填 120 等价）。滑块因此只给到这一档；
`tests/test_config_map.py` 用 `analyze.DEADLINE_S` 把这个派生关系钉住（不手写 95 的来历）。
"""

MAX_AUDIO_TIMEOUT_SECONDS = int(AUDIO_HONOR_MAX_S)
"""slider 上界 **95**：`AUDIO_HONOR_MAX_S` 的整数化（**派生，不手写**）。

【captain 裁决 2026-10-26】原本规格写 120，但 96–120 永远不可能生效 —— 与派单 C2 抱怨的
「填 110 与填 61 完全等价」是同一个病。滑块只给能生效的区间；
`config-check warn=timeout_over_budget` 保留，专兜「手改 JSON 写超了」那一情形。
"""

NATIVE_TIMEOUT_S = 60.0
"""原生视频**单次调用上限**（该配置键已在 v0.5.1 删除，值进本常量）。

生效值是 `min(NATIVE_TIMEOUT_S, remaining − NATIVE_RESERVE_S)`（`analyze` 侧常量承担，
SPEC-v0.5.1 §2.2-B）。本常量是**配置面的镜像**（受守卫对偶：`tests/test_config_map.py`
断言与 `analyze.NATIVE_TIMEOUT_S` 相等，落地前退回同值），唯一用途是 `ready` 行与
加载期不变式断言 —— 运行期判断一律读 `analyze` 侧那份。
"""

# v0.4.2 `background.mode`（枚举，不是布尔：见 SPEC-v0.4.2 §3.1「键的取舍」——布尔表达不了
# "默认 / 实验"的语义差别，将来出现第三种引擎还要再改类型＝破坏性变更）
MODE_PLUGIN = "plugin"
"""插件自管后台（A2）：受理即返回 + 插件自己 `send_message` **恰好一次**。**默认值**。"""

MODE_CORE = "core"
"""核心原生后台（路线 C，**实验**）：需核心补 `_PermissionGuardedTool` 一行镜像并重启。"""

BACKGROUND_MODES: tuple[str, str] = (MODE_PLUGIN, MODE_CORE)
"""合法取值（== `_conf_schema.json` 的 `options`，测试钉相等）。"""

DEFAULT_BACKGROUND_MODE = MODE_PLUGIN
"""默认值（== schema 的 `default` == `Settings.background_mode` 的默认值）。

v0.5.1 **不改**这个默认（【用户裁决 U2】：生产 `background.mode=core` 由配置决定，不回退）。
"""

# v0.3 `video.*` 的非字节默认值（== `_conf_schema.json` 的 `default`，逐字相同）
DEFAULT_VIDEO_PROBE_ENABLED = True
DEFAULT_VIDEO_PROBE_TTL_SECONDS = 604800  # 7 天
DEFAULT_VIDEO_COMPRESS_ENABLED = True
DEFAULT_VIDEO_COMPRESS_TIMEOUT_SECONDS = 30

# 钳位区间：不是产品承诺，只防"配置手滑成 100000"。
MIN_MAX_MEDIA_SECONDS, MAX_MAX_MEDIA_SECONDS = 5, 300
MIN_MAX_FRAMES, MAX_MAX_FRAMES = 1, 12
MIN_CACHE_TTL_SECONDS, MAX_CACHE_TTL_SECONDS = 0, 2592000  # 0＝关闭，上限 30 天

MIN_VIDEO_PROBE_TTL_SECONDS, MAX_VIDEO_PROBE_TTL_SECONDS = 0, 2592000  # 0＝关闭探测
MIN_VIDEO_COMPRESS_TIMEOUT_SECONDS, MAX_VIDEO_COMPRESS_TIMEOUT_SECONDS = 10, 60

#: 直送门槛的**建议表**（SPEC-v0.3 §4 冻结；`model` 匹配为大小写不敏感的通配）。
#:
#: **不参与运行期判定**：运行期只认 `video.max_direct_mb` 一个来源（§4 末段、否决项 §16.2-6
#: "自动按 provider 调门槛"被否决 —— 那会造出"表与配置打架"的第二事实源）。这张表只有两个
#: 用途：① 加载期告警③（`max_direct_over_suggested`，v0.5.1 起是 WARNING）；② 校准工具的建议值。
#: 证据：Gemini 9.4 MB⇒43.2 s / 18.6 MB⇒91.3 s；mimo 8 MiB⇒74.7 s 且抖动大（`MEASURE-v0.3`
#: §3/§4/§6）。用户口述的 mimo "<50 MB" **不进表**：t8 实测 36.5 MiB / base64 51.0 MB 仍 200，
#: 体积不是拒绝点，那条断言既未证伪也未坐实。
VIDEO_DIRECT_BYTES_SUGGESTIONS: tuple[tuple[str, int], ...] = (
    ("*gemini-3*flash*", 8 * BYTES_PER_MB),
    ("*mimo-v2.6*", 4 * BYTES_PER_MB),
    ("*mimo*", 4 * BYTES_PER_MB),
    ("*", 4 * BYTES_PER_MB),  # 未知 provider 的保守默认
)

PROMPT_MAX_CHARS = 2000  # 单份提示词模板上限（§7.3）
MATERIAL_MAX_CHARS = 8000  # 摘要材料上限（§7.3）

# --------------------------------------------------------------------------- #
# 提示词（5 份；默认值与 `_conf_schema.json` 的 `default` **逐字相同**）
#
# 元组顺序**沿用 v0.2 的前四位**（audio, video, frame, summary），v0.3 的新键
# `video_native` 追加在末位 ⇒ 老下标（0–3）不变，少一次"全表错位"的机会。
# --------------------------------------------------------------------------- #
PROMPT_KEYS: tuple[str, ...] = ("audio", "video", "frame", "summary", "video_native")

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
    "video_native": (
        "这是一段视频。请综合画面和声音回答下面的要求：\n"
        "1) 先客观描述画面里发生了什么（人物/动作/场景/画面上的文字）；\n"
        "2) 再简要复述声音里说的话（听不清就写 [听不清]）；\n"
        "3) 不要猜测画面或声音里没有的信息；\n"
        "4) 只输出结果本身，不要前言、标题或解释。\n"
        "如果画面和声音都没有可用内容，只输出：[无可用内容]\n"
        "{focus_clause}"
    ),
}

# 允许的占位符（超出即坏值 ⇒ 回退默认模板，§7.1）
ALLOWED_PLACEHOLDERS: dict[str, frozenset[str]] = {
    "audio": frozenset({"focus_clause"}),
    "video": frozenset({"focus_clause"}),
    "frame": frozenset({"focus_clause"}),
    "summary": frozenset({"kind_cn", "task", "material"}),
    "video_native": frozenset({"focus_clause"}),
}

# 焦点从句（音频/视频用；frame 用 analyze.FRAME_FOCUS_CLAUSE，逐字沿用 v0.1）
FOCUS_CLAUSE = (
    "另外，用户关心的是「{focus}」：音频里如果提到与之相关的内容，"
    "请特别留意并原样转写出来（仍然只做转写，不要改成回答）。"
)

_WARNED: set[tuple[str, str]] = set()


def suggest_direct_bytes(model: str) -> int:
    """按 §4 的建议表给出该模型的**建议**直送门槛（纯函数，永不抛）。

    只用于加载期告警③与校准工具；**调用方不得用它覆盖配置值**（§4 末段）。
    """
    text = str(model or "").strip().lower()
    for pattern, value in VIDEO_DIRECT_BYTES_SUGGESTIONS:
        if fnmatch.fnmatchcase(text or " ", pattern):
            return value
    return mb_to_bytes(DEFAULT_VIDEO_MAX_DIRECT_MB)


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


def normalize_background_mode(value: object) -> str:
    """把配置里的 `background.mode` 归一成 `"plugin" | "core"`（SPEC-v0.4.2 §3.3）。

    | 输入 | 输出 | 日志 |
    | --- | --- | --- |
    | `"plugin"` / `"core"`（`strip().lower()` 后） | 原样 | 无 |
    | `"CORE"` / `" core "` | `"core"` | 无（大小写与空白是可容忍的手滑） |
    | `""` / `None` / 缺键 | `"plugin"` | 无（缺键由核心 `check_config_integrity` 补默认） |
    | 其它（`"on"` / `"1"` / `True` / `{}` / `2.5`…） | `"plugin"` | **WARNING 一次**（不打值） |

    方向与 `analyze.normalize_mode`（工具参数非法值回 `auto`＝多花一次调用）**相反**：
    配置是运维写的，写错时应当站在**保守侧**（`plugin` 少花钱、少不确定性）。
    """
    if value is None:
        return DEFAULT_BACKGROUND_MODE
    if isinstance(value, str):
        text = value.strip().lower()
        if text in BACKGROUND_MODES:
            return text
        if not text:
            return DEFAULT_BACKGROUND_MODE
    _warn_bad_background_mode(type(value).__name__)
    return DEFAULT_BACKGROUND_MODE


def _warn_bad_background_mode(type_name: str) -> None:
    """非法 `background.mode` **只告警一次**，且**不打值**（配置值不进日志，AGENTS §8）。"""
    token = ("background.mode", type_name)
    if token in _WARNED:
        return
    _WARNED.add(token)
    LOG.warning(
        "media_analysis: 配置 background.mode 非法（类型 %s），按 plugin 处理",
        type_name,
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

    字段 == `KEY_MAP` 的值集合（21 条，一一对应）**加上** `NON_KEY_FIELDS` 里的
    `raw_audio_timeout`（同一配置键的原值，不是新键）。五个 `prompt_*` 字段存的是
    **配置原文**（`ConfigView.text` 去空白后），生效模板与派生量走下面的缓存属性：

    - `prompts`：5 元组，顺序固定 `(audio, video, frame, summary, video_native)`，
      已做"回退默认 + 截断"（前四位与 v0.2 一致，新键在末位）；
    - `prompts_trimmed`：被截断的键集合（调用时据此加 `prompt_trimmed:<key>` note）；
    - `prompts_fp`：5 份生效模板的指纹 `sha256("\\x00".join(...))[:16]`（缓存键成分）；
    - `video_fp`：影响视频产物的三项配置的指纹（§9.1 的 12 分量之一）；
    - `compress_budget_bytes`：`min(compress_target_bytes, max_direct_bytes)`（§6.1）。

    **MB 存储、字节派生（v0.5.1）**：`video_max_direct_mb` / `video_compress_target_mb` /
    `max_media_mb` 是配置面（MB 整数，已按 MB 钳位），三个同名 `*_bytes` property 是
    **运行期**读的量（纯乘法）。转换只在这里发生，消费方不需要知道 MB 存在。
    """

    # —— 引擎（组序第 1）——
    background_mode: str = DEFAULT_BACKGROUND_MODE
    """引擎选择（**已归一**）：`plugin`（默认，A2）/ `core`（实验，核心原生后台）。

    它**不进**缓存键（12 分量）：交付机制不影响分析结果，换引擎不该让老条目失效。
    """

    # —— provider 位 ——
    audio_provider_id: str = ""
    video_provider_id: str = ""
    vision_provider_id: str = ""
    legacy_stt_provider_id: str = ""

    # —— video（可见三键 + 隐藏三键）——
    video_max_direct_mb: int = DEFAULT_VIDEO_MAX_DIRECT_MB
    video_compress_enabled: bool = DEFAULT_VIDEO_COMPRESS_ENABLED
    video_compress_target_mb: int = DEFAULT_VIDEO_COMPRESS_TARGET_MB
    video_compress_timeout_seconds: int = DEFAULT_VIDEO_COMPRESS_TIMEOUT_SECONDS
    video_probe_enabled: bool = DEFAULT_VIDEO_PROBE_ENABLED
    video_probe_ttl_seconds: int = DEFAULT_VIDEO_PROBE_TTL_SECONDS

    # —— limits ——
    max_media_seconds: int = DEFAULT_MAX_MEDIA_SECONDS
    max_frames: int = DEFAULT_MAX_FRAMES
    max_media_mb: int = DEFAULT_MAX_MEDIA_MB
    audio_timeout_seconds: int = DEFAULT_AUDIO_TIMEOUT_SECONDS

    # —— cache ——
    cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS

    # —— prompts（原文；生效模板走 property）——
    prompt_audio: str = ""
    prompt_video: str = ""
    prompt_frame: str = ""
    prompt_summary: str = ""
    prompt_video_native: str = ""

    # —— 非键字段（同一配置键的原值，只服务加载期告警①）——
    raw_audio_timeout: int = DEFAULT_AUDIO_TIMEOUT_SECONDS
    """`limits.audio_timeout_seconds` 的**配置原值**（不钳位）。

    存在的理由（SPEC-v0.5.1 §8.1 告警①）：钳位后的值恒 ≤ `MAX_AUDIO_TIMEOUT_SECONDS`，
    拿它去比就是死代码；越界只可能来自"手改 JSON"。所以加载期留一份原值给告警，
    运行期与 `ready` 行一律用**钳位后**的 `audio_timeout_seconds`。
    """

    # —— 派生：MB → 字节（运行期唯一读点，转换只在这里发生）——
    @property
    def video_max_direct_bytes(self) -> int:
        """直送门槛（字节）= `video_max_direct_mb × 1048576`（§2.1 钳位在前）。"""
        return mb_to_bytes(self.video_max_direct_mb)

    @property
    def video_compress_target_bytes(self) -> int:
        """压缩目标（字节）= `video_compress_target_mb × 1048576`。"""
        return mb_to_bytes(self.video_compress_target_mb)

    @property
    def max_media_bytes(self) -> int:
        """取件硬闸（字节）= `max_media_mb × 1048576`。"""
        return mb_to_bytes(self.max_media_mb)

    @cached_property
    def _effective_prompts(self) -> tuple[dict[str, str], frozenset[str]]:
        return validate_prompts(
            {
                "audio": self.prompt_audio,
                "video": self.prompt_video,
                "frame": self.prompt_frame,
                "summary": self.prompt_summary,
                "video_native": self.prompt_video_native,
            }
        )

    @property
    def prompts(self) -> tuple[str, str, str, str, str]:
        """生效模板 5 元组（顺序 `audio, video, frame, summary, video_native`）。"""
        effective = self._effective_prompts[0]
        return (
            effective["audio"],
            effective["video"],
            effective["frame"],
            effective["summary"],
            effective["video_native"],
        )

    @property
    def prompts_trimmed(self) -> frozenset[str]:
        """超 2000 字符被截断的提示词键集合。"""
        return self._effective_prompts[1]

    @cached_property
    def prompts_fp(self) -> str:
        """5 份生效模板的指纹（缓存键成分，§9.1）。"""
        joined = "\x00".join(self.prompts)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]

    @cached_property
    def video_fp(self) -> str:
        """影响视频产物的三项配置的指纹（缓存键成分，§9.1 冻结公式，8 位）。

        公式**逐字未变**（v0.5.1 §2.4 冻死）：仍哈希**派生字节**
        `"{max_direct_bytes}|{compress_enabled}|{compress_target_bytes}"`
        ⇒ 与 v0.4.2 同源，缓存失效的唯一原因是 `PLUGIN_VERSION` bump，不是公式变了。
        """
        material = (
            f"{int(self.video_max_direct_bytes)}|"
            f"{bool(self.video_compress_enabled)}|"
            f"{int(self.video_compress_target_bytes)}"
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:8]

    @property
    def compress_budget_bytes(self) -> int:
        """压缩字节预算 = `min(compress_target_bytes, max_direct_bytes)`（§6.1）。

        配得比门槛还大时按**小者**算：压到比门槛还大等于没压。
        """
        return min(
            int(self.video_compress_target_bytes), int(self.video_max_direct_bytes)
        )


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

    def raw_int(self, path: str, *, default: int) -> int:
        """取整数配置的**原值**：不钳位、不告警（非法类型 ⇒ 默认值）。

        只给"越界本身就是判据"的场景用（SPEC-v0.5.1 §8.1 告警①）—— 它比的是配置面
        的原始输入，不是钳位结果。正常读取仍走 `bounded_int`。
        """
        value = self.get(path, default)
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def flag(self, path: str, *, default: bool) -> bool:
        """取**布尔**配置（SPEC-v0.3 §8.2 ③）：非 `bool` ⇒ 告警一次并回默认。

        刻意不收 `0/1` 与 `"true"`：JSON schema 的 `bool` 在 WebUI 上就是真布尔，
        出现其它类型说明配置被手改坏了 —— 猜一个方向（尤其猜成 `true`）会**多花钱**
        （自动探测 / 自动压缩），所以只认作者写进来的那个类型。
        """
        value = self.get(path, default)
        if isinstance(value, bool):
            return value
        LOG.warning(
            "media_analysis: 配置 %s 不是布尔值（%s），改用默认值 %s",
            path,
            type(value).__name__,
            default,
        )
        return default

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
