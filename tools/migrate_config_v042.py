#!/usr/bin/env python3
"""配置迁移器：v0.4.x（21 键）→ v0.4.2（22 键）。**默认干跑**，只打印，不写盘、不建目录。

执行者是 ops / fix，**不是 dev**（本 lane 不碰生产配置）。契约见 SPEC-v0.4.2 §3.5 / §13。

先记住三件事（否则会误以为"不跑脚本新键就不会出现"）：

1. **脚本不负责"造新键"**。新代码 + 新 schema 上线后，核心 `AstrBotConfig` 构造期就会跑
   `check_config_integrity(default_from_schema, 用户配置, schema)`
   （`astrbot_config.py:113-121`）并把 schema 里有、配置里没有的键**补上默认值**
   （`:252-266`）。所以本脚本的职责只有两条：① 把那**一个** v0.4.2 新键
   （`background.mode`）**显式落盘**（便于审计与回滚锚点）、② 逐键报告（`--report`），
   **不搬任何值**。
2. **顺序不能反**：先上代码+schema → 重启/重载 → 再跑本脚本。反序时**旧 schema** 不认识
   `background.mode`，下次加载会把它当 stale 键**删掉并落盘**（`astrbot_config.py:252-266`）。
   v0.4.2 的新 schema 是 v0.4.1 的**超集**（只加一个键）⇒ **代码先行**。
3. **`tools/migrate_config_v03.py` 已冻结为 v0.2→v0.3 的历史快照**（仍报 21 键）。
   本脚本是 v0.4.2 的对偶：它的 `KEY_ORDER` 必须**恰好**是 `settings.KEY_MAP` 的顺序
   （22 键），而 v0.3 的 `KEY_ORDER` 必须**恰好**是本表的 **21 键前缀**（守卫：
   `tests/test_config_map.py::test_v03_migrator_is_the_frozen_21_key_prefix`）。

**任何模式都不打印配置值**：只打印点号键、动作、类型、长度（`len` 是长度不是内容）。
stdout 里不出现 provider id 字面量，stderr 只打异常类名。

**落盘不改权限**：目标文件原来的权限位会被**原样保留**（核心自己写 `cmd_config.json` 用
0600，生产那份也是 0600）；`os.replace` 换的是 inode，不显式复制 mode 就会把 600 放宽成
默认的 644 —— 那是把配置权限降级。原文件不存在时才用 0600。

用法：

    python3 tools/migrate_config_v042.py --root <AstrBot 根>              # 干跑（默认）
    python3 tools/migrate_config_v042.py --root <AstrBot 根> --apply      # 备份 + 原子落盘
    python3 tools/migrate_config_v042.py --root <AstrBot 根> --report     # 只读逐键报告
    python3 tools/migrate_config_v042.py --rollback <备份文件>

退出码：`0` 成功（含"无变化"）/ `1` 错误（读、写、JSON、BOM、路径不存在）。

本脚本**绝不做**：不 import 插件模块、不重启服务、不碰 `data/plugins/**`、不删键、不联网。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
import sys
import time
from typing import NamedTuple

CONFIG_REL = os.path.join(
    "data", "config", "astrbot_plugin_media_analysis_config.json"
)

#: 目标文件不存在时的兜底权限（配置里有 provider id 与提示词，默认不给同机他人读）
DEFAULT_CONFIG_MODE = 0o600

# 22 个键的**书写顺序**（== `_conf_schema.json` 与 SPEC-v0.4.2 §3.1 的顺序）与展示用类型。
#
# 关键不变式：**既有 21 键的相对顺序一个字都没动**（新组 `background` 追加在最末尾）
# ⇒ v0.3 迁移器的 `KEY_ORDER` 恰好是本表的 21 键前缀，两份脚本因此可以并存。
KEY_ORDER: tuple[tuple[str, str], ...] = (
    ("provider_settings.audio_provider_id", "str"),
    ("provider_settings.video_provider_id", "str"),
    ("provider_settings.vision_provider_id", "str"),
    ("provider_settings.stt_provider_id", "str"),
    ("video.probe_enabled", "bool"),
    ("video.probe_ttl_seconds", "int"),
    ("video.max_direct_bytes", "int"),
    ("video.native_timeout_seconds", "int"),
    ("video.native_max_tokens", "int"),
    ("video.compress_enabled", "bool"),
    ("video.compress_target_bytes", "int"),
    ("video.compress_timeout_seconds", "int"),
    ("prompts.audio", "text"),
    ("prompts.video", "text"),
    ("prompts.frame", "text"),
    ("prompts.summary", "text"),
    ("prompts.video_native", "text"),
    ("limits.max_media_seconds", "int"),
    ("limits.max_frames", "int"),
    ("limits.max_media_bytes", "int"),
    ("cache.ttl_seconds", "int"),
    ("background.mode", "str"),
)

#: v0.4.2 新增的 **1 个**键（**只补默认值，不搬值**）。多一条 = 脚本替用户造了旧键的值。
#: 默认值必须与 `settings.DEFAULT_BACKGROUND_MODE` 与 `_conf_schema.json` 的 `default`
#: 逐字相同（本脚本是独立文件，不能 import 插件模块，所以这里是一份**受守卫的对偶**：
#: `tests/test_migrate_config_v042.py` 会对齐三处）。
NEW_KEY_DEFAULTS: dict[str, object] = {
    "background.mode": "plugin",
}


class Action(NamedTuple):
    """一条逐键报告：键、动作、类型、长度（`None` ⇒ 打印 `-`）、原因码。

    刻意用 `NamedTuple` 而不是 `dataclass`：本文件是**独立脚本**，被 `importlib` 当纯函数
    库加载时（测试会这么做）`dataclass` 会去 `sys.modules[cls.__module__]` 取命名空间而炸。
    """

    key: str
    action: str  # keep | add | skip
    type_name: str
    length: int | None = None
    reason: str = ""

    def render(self) -> str:
        length = "-" if self.length is None else str(self.length)
        line = f"{self.key} action={self.action} type={self.type_name} len={length}"
        if self.reason:
            line += f" reason={self.reason}"
        return line


# --------------------------------------------------------------------------- #
# 点号读写（不依赖插件模块）
# --------------------------------------------------------------------------- #
def get_path(config: dict, path: str, default=None):
    node = config
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def set_path(config: dict, path: str, value) -> None:
    parts = path.split(".")
    node = config
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _length(value) -> int | None:
    return len(value) if isinstance(value, str) else None


def plan(config: dict):
    """算出目标配置与逐键动作，**不碰磁盘**（测试直接调它）。

    只补 `NEW_KEY_DEFAULTS` 里缺失的键；**已存在的键一律 keep**（v0.2/v0.3 的 21 个键
    名字、类型、默认值都不动 ⇒ 用户旧配置零迁移可用）。
    """
    new_config = json.loads(json.dumps(config)) if isinstance(config, dict) else {}
    actions: list[Action] = []
    for key, type_name in KEY_ORDER:
        current = get_path(new_config, key)
        if current is not None:
            actions.append(Action(key, "keep", type_name, _length(current)))
            continue
        if key in NEW_KEY_DEFAULTS:
            set_path(new_config, key, NEW_KEY_DEFAULTS[key])
            actions.append(Action(key, "add", type_name, _length(NEW_KEY_DEFAULTS[key])))
            continue
        # 旧键却缺失：不替用户造值（核心会在下次加载时补 schema 默认）
        actions.append(Action(key, "skip", type_name, None, "core_default"))
    return new_config, actions


def changed(config: dict, new_config: dict) -> bool:
    """幂等判据：目标 dict 与读到的 dict 是否不同（相等 ⇒ `changed=0`，不产生备份）。"""
    return config != new_config


# --------------------------------------------------------------------------- #
# 磁盘
# --------------------------------------------------------------------------- #
def _load_json(path: str):
    """读 JSON（`utf-8-sig`：`cmd_config.json` 带 BOM，实测 `json.load` 会抛）。"""
    with open(path, "rb") as handle:
        raw = handle.read()
    return json.loads(raw.decode("utf-8-sig"))


def _existing_mode(path: str) -> int | None:
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return None


def _atomic_write(path: str, data: bytes) -> None:
    """同目录 `.tmp-<pid>-<rand>` → `os.replace`；**原文件权限位原样保留**。"""
    directory = os.path.dirname(os.path.abspath(path))
    mode = _existing_mode(path)
    tmp_path = os.path.join(directory, f".tmp-{os.getpid()}-{secrets.token_hex(4)}")
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, DEFAULT_CONFIG_MODE)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, DEFAULT_CONFIG_MODE if mode is None else mode)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _write_backup(path: str, data: bytes) -> str:
    """逐字节备份原文件，权限 **0600**；撞名追加随机后缀，**绝不覆盖**已有备份。"""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    base = f"{path}.bak-{stamp}"
    for attempt in range(8):
        candidate = base if attempt == 0 else f"{base}-{secrets.token_hex(3)}"
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                os.unlink(candidate)
            except OSError:
                pass
            raise
        return candidate
    raise OSError("备份文件名连续撞名，已放弃（目标文件未改动）")


def _resolve_root(args) -> str:
    root = args.root or os.environ.get("ASTRBOT_ROOT") or ""
    if not root:
        raise SystemExit(_fail("缺少 --root 且未设置 ASTRBOT_ROOT（不猜生产路径）"))
    return os.path.abspath(os.path.expanduser(root))


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        add_help=True,
        description="media_analysis v0.4.2 配置迁移器（默认干跑；只打印键名/动作/类型/长度）",
    )
    parser.add_argument("--root", default="", help="AstrBot 根目录（未给则取 $ASTRBOT_ROOT）")
    parser.add_argument("--apply", action="store_true", help="真正落盘（默认只干跑）")
    parser.add_argument("--report", action="store_true", help="只读逐键报告（22 行，一键一行）")
    parser.add_argument("--rollback", default="", help="从备份文件回滚（与代码回滚成对进行）")
    args = parser.parse_args(argv)

    try:
        if args.rollback:
            return _do_rollback(args.rollback)
        root = _resolve_root(args)
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else 1

    config_path = os.path.join(root, CONFIG_REL)
    if not os.path.isfile(config_path):
        return _fail(f"目标配置不存在：{os.path.relpath(config_path, root)}")

    try:
        with open(config_path, "rb") as handle:
            original_bytes = handle.read()
        config = json.loads(original_bytes.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return _fail(f"目标配置读不出来（{type(exc).__name__}）")
    if not isinstance(config, dict):
        return _fail("目标配置不是 JSON 对象")

    new_config, actions = plan(config)

    if args.report:
        for item in actions:
            present = get_path(config, item.key) is not None
            value = get_path(config, item.key)
            print(
                f"{item.key} present={str(present).lower()} "
                f"type={item.type_name} len={'-' if _length(value) is None else _length(value)} "
                f"action={item.action}"
            )
        print(f"summary: changed={1 if changed(config, new_config) else 0} keys={len(KEY_ORDER)}")
        return 0

    for item in actions:
        print(item.render())

    dirty = changed(config, new_config)
    backup_path = "-"
    if dirty and args.apply:
        try:
            backup_path = _write_backup(config_path, original_bytes)
        except OSError as exc:
            return _fail(f"备份失败（{type(exc).__name__}），目标文件未改动")
        try:
            data = (
                json.dumps(new_config, ensure_ascii=False, indent=2).encode("utf-8")
                + b"\n"
            )
            _atomic_write(config_path, data)
        except OSError as exc:
            kept = os.path.basename(backup_path)
            return _fail(
                f"写入失败（{type(exc).__name__}），目标文件保持原样（备份：{kept}）"
            )

    shown_backup = backup_path if args.apply else "-"
    print(f"summary: changed={1 if dirty else 0} backup={shown_backup}")
    if dirty and not args.apply:
        print("提示：干跑不写盘；确认后用 --apply 落盘（会先写 0600 备份，保留原权限位）。")
    return 0


def _do_rollback(backup: str) -> int:
    """从备份回滚：校验可解析 ⇒ 逐字节写回（原子）⇒ 打印恢复路径与成对回滚提醒。"""
    target = backup.split(".bak-")[0]
    if not os.path.isfile(backup):
        return _fail("备份文件不存在")
    try:
        with open(backup, "rb") as handle:
            data = handle.read()
        json.loads(data.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return _fail(f"备份不可解析（{type(exc).__name__}）")
    if not target:
        return _fail("无法从备份路径推断目标配置")
    try:
        _atomic_write(target, data)
    except OSError as exc:
        return _fail(f"回滚写入失败（{type(exc).__name__}）")
    print(f"restored={os.path.basename(target)}")
    print(
        "提醒：回滚配置必须与代码回滚成对进行（先回代码、再回配置、最后重启；"
        "见 SPEC-v0.4.2 §13.3 / README「部署与迁移」）"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
