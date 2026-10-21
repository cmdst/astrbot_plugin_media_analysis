"""配置读取层：4 个键 + 点号读取 + 钳位（SPEC §3.2 / §4.3）。

模块名**刻意不叫 `config`**：`Star.__init__` 的第二个形参就叫 `config`，加载器按
`star_cls_type(context=…, config=…)` 关键字调用（`star_manager.py:1229-1238`），
模块名撞形参名会让 `from . import config` 与形参在 `Star.__init__` 里打架。

`AstrBotConfig` 是 `dict` 子类，**没有**点号 `get`（`config/astrbot_config.py:33-60`），
所以点号能力由本模块的 `ConfigView` 提供（形状照屋内先例 `log_watcher/config_map.py`）。

本模块**不提供**第二个 `load_*` 入口：唯一的组装点是 `main._settings()`（SPEC §4.3），
三方一致守卫测试（`tests/test_config_map.py`）就是靠"只有一个读取点"才钉得住。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger as LOG

KEY_STT_PROVIDER_ID = "provider_settings.stt_provider_id"
KEY_VISION_PROVIDER_ID = "provider_settings.vision_provider_id"
KEY_MAX_MEDIA_SECONDS = "limits.max_media_seconds"
KEY_MAX_FRAMES = "limits.max_frames"

# 点号路径 → Settings 字段名。这是「schema ⇄ KEY_MAP ⇄ 读取点」三方守卫的事实源。
KEY_MAP: dict[str, str] = {
    KEY_STT_PROVIDER_ID: "stt_provider_id",
    KEY_VISION_PROVIDER_ID: "vision_provider_id",
    KEY_MAX_MEDIA_SECONDS: "max_media_seconds",
    KEY_MAX_FRAMES: "max_frames",
}

# 默认值 = SPEC §4.2 的实测依据值（60 s / 6 帧），由 verifier 复量后回写 SPEC/AGENTS。
DEFAULT_MAX_MEDIA_SECONDS = 60
DEFAULT_MAX_FRAMES = 6
# 钳位区间：不是产品承诺，只防"配置手滑成 100000"（SPEC §13-6）。
MIN_MAX_MEDIA_SECONDS, MAX_MAX_MEDIA_SECONDS = 5, 300
MIN_MAX_FRAMES, MAX_MAX_FRAMES = 1, 12


@dataclass(frozen=True)
class Settings:
    """一次装配、整轮复用的配置快照（配置改动仍需重启，核心不在运行期热更）。"""

    stt_provider_id: str = ""
    vision_provider_id: str = ""
    max_media_seconds: int = DEFAULT_MAX_MEDIA_SECONDS
    max_frames: int = DEFAULT_MAX_FRAMES


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
