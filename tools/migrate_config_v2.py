#!/usr/bin/env python3
"""任务单里写的名字（`migrate_config_v2.py`）—— 转发到 SPEC-v0.2 的冻结名实现。

SPEC-v0.2 §5.5 / §10 / §14 与 `tests/test_migrate_config.py` 指的都是
`tools/migrate_config_v02.py`（"v02" 带前导零）；派单文字里出现过 `migrate_config_v2.py`。
两个名字指向**同一份实现**，本文件只是 runpy 转发，避免按派单执行时 404。

用法与退出码完全同 `migrate_config_v02.py`：

    python3 tools/migrate_config_v2.py --root <AstrBot 根>            # 干跑
    python3 tools/migrate_config_v2.py --root <AstrBot 根> --apply
"""

from __future__ import annotations

import os
import runpy

_TARGET = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "migrate_config_v02.py"
)

if __name__ == "__main__":
    runpy.run_path(_TARGET, run_name="__main__")
