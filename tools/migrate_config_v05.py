#!/usr/bin/env python3
"""配置迁移器：v0.4.2（**22 叶**）→ v0.5.1（**21 叶**）。**默认干跑**，只打印，不写盘。

执行者是 ops / fix，**不是 dev**（本 lane 不碰生产配置）。契约见 SPEC-v0.5.1 §2.2 / §3.2–§3.5。

**这一版与前面几支脚本的性质不同：它真的要改值**（前几支只补默认）。四类动作：

| 动作 | 键 | 说明 |
| --- | --- | --- |
| `rename` ×3 | `video.max_direct_bytes`→`_mb` / `video.compress_target_bytes`→`_mb` / `limits.max_media_bytes`→`_mb` | 字节→MB，**half-up** 取整后按新 slider 区间钳位 |
| `drop` ×2 | `video.native_max_tokens` / `video.native_timeout_seconds` | 值进**代码常量**（4096 / 60.0），配置面不再有这两项 |
| `add` ×1 | `limits.audio_timeout_seconds` | 补默认 60（= v0.2–v0.4.2 硬编码的 `AUDIO_TIMEOUT_S`） |
| `keep` ×17 | 其余（含 4 个**隐藏**键） | 原值原样保留；隐藏键只加面板级 `invisible`，**值照旧保号** |

三件事必须记住（否则会误以为"不跑脚本也能升级"）：

1. **顺序不能反：先迁移、再重载**（SPEC-v0.5.1 §3.1）。新 schema 把三个字节键当 **stale**
   ⇒ 若先重载后迁移，核心 `check_config_integrity()` 会**删掉旧键并落盘**（`astrbot_config.py:252-266`
   + `:113-121`），用户的 32 MB / 29 MB 一次性被默认值（8 / 6 / 64）顶掉，那时本脚本已经
   **无旧值可读**。
2. **键序必须按新 schema 写**（§3.1-2）：核心在加载期会把键序重排成 schema 序并**再存一次盘**
   （`astrbot_config.py:269-274`）⇒ 只有"落盘序 == schema 序"才可能满足「重载后 sha 不变」。
   本脚本的 `KEY_ORDER` 就是 schema 序（组序 `background` 置顶、`prompts` 置末）。
3. **落盘格式与核心逐字节同形**：`b"\\xef\\xbb\\xbf" + json.dumps(cfg, indent=2, ensure_ascii=False)`
   —— **带 BOM、2 空格缩进、不转义中文、无尾随换行**，并**原样保留原文件权限位**（生产是 0600）。
   ⚠ **不许继承** `migrate_config_v042.py` 的 `json.dumps(...).encode() + b"\\n"`（无 BOM + 多一个
   换行）：那份脚本从未真正写过盘（v0.4.2 部署时 `changed=0`），所以格式缺陷没暴露过。

**任何模式都不打印配置内容**：只打印点号键、动作、类型、长度（`len` 是长度不是内容）与
**字节换算审计字段**（`old_bytes` / `exact_mb` / `new_mb` / `new_bytes` / `delta` / `clamped`
—— 这是 SPEC-v0.5.1 §2.1 明确要求的迁移审计面）；`prompts.*` 的正文与 provider id **一律不打印**。

用法：

    python3 tools/migrate_config_v05.py --root <AstrBot 根>              # 干跑（默认；末行 summary: changed=… keys=21）
    python3 tools/migrate_config_v05.py --root <AstrBot 根> --report     # 只读逐键报告（同上，显式态）
    python3 tools/migrate_config_v05.py --root <AstrBot 根> --apply      # 0600 备份 + 原子落盘
    python3 tools/migrate_config_v05.py --rollback <备份文件>            # 逐字节写回（必须与代码回滚成对）

退出码：`0` 成功（含"无变化"）/ `1` 错误（读、写、JSON、BOM、路径不存在）。

本脚本**绝不做**：不 import 插件模块、不重启服务、不碰 `data/plugins/**`、不删"其它键"
（未知键原样保留）、不联网。
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

#: 1 MB ≡ 1 MiB（SPEC-v0.5.1 §2.1 写死：**二进制** MB，不是 10^6）
BYTES_PER_MB = 1048576

#: 21 个键的**书写顺序**（== `_conf_schema.json` == `settings.KEY_MAP` == 面板渲染序）与展示用类型。
#:
#: 与 v0.4.2 的 22 键相比：`background` **置顶**、`prompts` **置末**；`video` 组内可见三键
#: 在前、隐藏键在后（SPEC-v0.5.1 §2.3 的 J14）。三个 `*_mb` 仍是原来的逻辑槽位（值不搬家）。
KEY_ORDER: tuple[tuple[str, str], ...] = (
    ("background.mode", "str"),
    ("provider_settings.audio_provider_id", "str"),
    ("provider_settings.video_provider_id", "str"),
    ("provider_settings.vision_provider_id", "str"),
    ("provider_settings.stt_provider_id", "str"),
    ("video.max_direct_mb", "int"),
    ("video.compress_enabled", "bool"),
    ("video.compress_target_mb", "int"),
    ("video.compress_timeout_seconds", "int"),
    ("video.probe_enabled", "bool"),
    ("video.probe_ttl_seconds", "int"),
    ("limits.max_media_seconds", "int"),
    ("limits.max_frames", "int"),
    ("limits.max_media_mb", "int"),
    ("limits.audio_timeout_seconds", "int"),
    ("cache.ttl_seconds", "int"),
    ("prompts.audio", "text"),
    ("prompts.video", "text"),
    ("prompts.frame", "text"),
    ("prompts.summary", "text"),
    ("prompts.video_native", "text"),
)

#: 4 个**隐藏**键（item 级 `invisible: true`）：**键与值都保留**，只是面板不再渲染。
#:
#: 为什么"隐藏"而不是"删除"：从 schema 删掉的那一刻，核心会把用户的旧值当 stale
#: **删掉并落盘**（§1.3-2）—— 隐藏是零风险且**保号**（生产 `compress_timeout_seconds=60`
#: 正是"隐藏≠删除"的现场证据）。
HIDDEN_KEYS: frozenset[str] = frozenset(
    {
        "provider_settings.stt_provider_id",
        "video.probe_enabled",
        "video.probe_ttl_seconds",
        "video.compress_timeout_seconds",
    }
)


class Rename(NamedTuple):
    """一条字节→MB 的改名规则（含新 slider 区间 ⇒ 换算后要按它钳位）。"""

    old: str
    new: str
    lo_mb: int
    hi_mb: int


#: 3 条改名（SPEC-v0.5.1 §2.2-A）。区间与 `settings.MIN/MAX_*_MB`、
#: `_conf_schema.json` 的 `slider` 三处同值（守卫：`tests/test_migrate_config_v05.py`）。
RENAMES: tuple[Rename, ...] = (
    Rename("video.max_direct_bytes", "video.max_direct_mb", 1, 32),
    Rename("video.compress_target_bytes", "video.compress_target_mb", 1, 32),
    Rename("limits.max_media_bytes", "limits.max_media_mb", 1, 512),
)

#: 2 条删除（值进代码常量：`video_native.NATIVE_MAX_TOKENS=4096` / `analyze.NATIVE_TIMEOUT_S=60.0`）。
#: 带展示用类型：它们**不在** `KEY_ORDER` 里（新 schema 没有槽位），报告仍要给出 `type=`。
DROPS: tuple[tuple[str, str], ...] = (
    ("video.native_max_tokens", "int"),
    ("video.native_timeout_seconds", "int"),
)

#: 新增键的默认值（**只有 1 条**）。默认值必须与 `settings.DEFAULT_AUDIO_TIMEOUT_SECONDS`
#: 与 `_conf_schema.json` 的 `default` 逐字相同 —— 本脚本不 import 插件模块，所以这是一份
#: **受守卫的对偶**：`tests/test_migrate_config_v05.py` 会对齐三处。
NEW_KEY_DEFAULTS: dict[str, object] = {
    "limits.audio_timeout_seconds": 60,
}

#: 动作词表（SPEC-v0.5.1 §3.2 冻死）
ACTIONS: frozenset[str] = frozenset({"keep", "rename", "drop", "add", "skip"})


def mb_to_bytes(value: int) -> int:
    """MB → 字节（纯乘法）。与 `settings.mb_to_bytes()` **同值**（受守卫对偶）。"""
    return int(value) * BYTES_PER_MB


def bytes_to_mb(value: int) -> int:
    """字节 → MB，**half-up 四舍五入**（SPEC-v0.5.1 §2.1：`(bytes + 524288) // 1048576`）。

    与 `settings.bytes_to_mb()` 是**受守卫对偶**（本脚本不 import 插件模块）——
    `tests/test_migrate_config_v05.py` 对边界表 `1048575 / 1048576 / 524288 / 30000000 /
    33554432 / 67108864` 断言两侧同值，从而不引入第二事实源。
    """
    return (int(value) + BYTES_PER_MB // 2) // BYTES_PER_MB


class Action(NamedTuple):
    """一条逐键报告：键、动作、类型、长度（`None` ⇒ 打印 `-`）与迁移审计字段。

    刻意用 `NamedTuple` 而不是 `dataclass`：本文件是**独立脚本**，被 `importlib` 当纯函数
    库加载时（测试会这么做）`dataclass` 会去 `sys.modules[cls.__module__]` 取命名空间而炸。
    """

    key: str
    action: str  # keep | rename | drop | add | skip
    type_name: str
    length: int | None = None
    hidden: bool = False
    # —— 仅 rename ——
    old_bytes: int | None = None
    new_mb: int | None = None
    new_bytes: int | None = None
    clamped: bool = False
    # —— 仅 skip ——
    reason: str = ""

    @property
    def delta_bytes(self) -> int | None:
        """`new_bytes − old_bytes`（rename 才有；正数带 `+`）。"""
        if self.old_bytes is None or self.new_bytes is None:
            return None
        return int(self.new_bytes) - int(self.old_bytes)

    def render(self) -> str:
        length = "-" if self.length is None else str(self.length)
        line = f"{self.key} action={self.action} type={self.type_name} len={length}"
        if self.hidden:
            line += " hidden=1"
        if self.action == "rename":
            exact_mb = int(self.old_bytes) / BYTES_PER_MB
            delta = self.delta_bytes or 0
            # SPEC-v0.5.1 §2.1：审计面必须同时给出这 6 个量（`exact_mb` 4 位小数）
            line += (
                f" old_bytes={int(self.old_bytes)} exact_mb={exact_mb:.4f}"
                f" new_mb={int(self.new_mb)} new_bytes={int(self.new_bytes)}"
                f" delta={delta:+d} clamped={1 if self.clamped else 0}"
            )
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


def delete_path(config: dict, path: str) -> bool:
    """删掉点号路径（父节点不是 dict / 键不存在 ⇒ `False`）。"""
    parts = path.split(".")
    node = config
    for part in parts[:-1]:
        node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            return False
    if isinstance(node, dict) and parts[-1] in node:
        del node[parts[-1]]
        return True
    return False


def _text_length(value) -> int | None:
    return len(value) if isinstance(value, str) else None


def _reorder(config: dict) -> dict:
    """按 `KEY_ORDER` 重排（组序 + 组内序）；**未知键/未知组原样保留**（不删其它键）。

    组内：先按 `KEY_ORDER` 的叶序放，再把该组里不在 `KEY_ORDER` 的键按原相对顺序追加；
    组间的"未知组"按原顺序追加在末尾。⇒ 迁移只搬动它认识的那 22 个键的**位置**。
    """
    groups: list[str] = []
    for path, _type in KEY_ORDER:
        group = path.split(".", 1)[0]
        if group not in groups:
            groups.append(group)

    out: dict = {}
    for group in groups:
        if group not in config:
            continue
        node = config[group]
        if not isinstance(node, dict):
            out[group] = node
            continue
        leaves = [path.split(".", 1)[1] for path, _t in KEY_ORDER if path.split(".", 1)[0] == group]
        ordered: dict = {}
        for leaf in leaves:
            if leaf in node:
                ordered[leaf] = node[leaf]
        for leaf, value in node.items():
            if leaf not in ordered:
                ordered[leaf] = value
        out[group] = ordered
    for group, node in config.items():
        if group not in out:
            out[group] = node
    return out


def plan(config: dict):
    """算出目标配置与逐键动作，**不碰磁盘**（测试直接调它）。

    动作语义（SPEC-v0.5.1 §3.2）：`rename` = 删旧键写新键（MB 换算 + 钳位）；
    `drop` = 删旧键不写新键；`add` = 写新键默认值；`keep` = 原值原样；
    `skip` = 源配置没有该键（无值可搬，核心会在下次加载时补 schema 默认）。
    """
    new_config = json.loads(json.dumps(config)) if isinstance(config, dict) else {}
    actions: list[Action] = []

    # ① 改名：先算（它决定目标 dict 里新键的值），再统一按 KEY_ORDER 出报告
    renamed: dict[str, Action] = {}
    for rule in RENAMES:
        raw = get_path(new_config, rule.old)
        if raw is None:
            # 已经迁移过（旧键没了、新键在）⇒ keep，不是 skip（保证幂等：二跑全是 keep）
            existing = get_path(new_config, rule.new)
            if existing is not None:
                renamed[rule.new] = Action(
                    rule.new,
                    "keep",
                    _type_of(rule.new),
                    _text_length(existing),
                    rule.new in HIDDEN_KEYS,
                )
            else:
                renamed[rule.new] = Action(
                    rule.new, "skip", _type_of(rule.new), None, rule.new in HIDDEN_KEYS,
                    reason="no_source",
                )
            continue
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            renamed[rule.new] = Action(
                rule.new, "skip", _type_of(rule.new), None, rule.new in HIDDEN_KEYS,
                reason="not_int",
            )
            continue
        try:
            old_bytes = int(raw)
        except (TypeError, ValueError):
            renamed[rule.new] = Action(
                rule.new, "skip", _type_of(rule.new), None, rule.new in HIDDEN_KEYS,
                reason="not_int",
            )
            continue
        exact_mb = bytes_to_mb(old_bytes)
        new_mb = min(max(exact_mb, rule.lo_mb), rule.hi_mb)  # 先按 MB 钳（§2.1 钳位顺序）
        delete_path(new_config, rule.old)
        set_path(new_config, rule.new, new_mb)
        renamed[rule.new] = Action(
            rule.new,
            "rename",
            _type_of(rule.new),
            None,
            rule.new in HIDDEN_KEYS,
            old_bytes=old_bytes,
            new_mb=new_mb,
            new_bytes=mb_to_bytes(new_mb),
            clamped=new_mb != exact_mb,
        )

    # ② 删除（值进代码常量）
    dropped: dict[str, Action] = {}
    for key, type_name in DROPS:
        if get_path(new_config, key) is not None:
            delete_path(new_config, key)
            dropped[key] = Action(key, "drop", type_name, None, key in HIDDEN_KEYS)
        else:
            dropped[key] = Action(
                key, "skip", type_name, None, key in HIDDEN_KEYS, reason="no_source"
            )

    # ③ 按 KEY_ORDER 出报告（顺序 == schema 序 == 面板序）
    for key, type_name in KEY_ORDER:
        if key in renamed:
            actions.append(renamed[key])
            continue
        if key in dropped:
            actions.append(dropped[key])
            continue
        current = get_path(new_config, key)
        if current is not None:
            actions.append(
                Action(key, "keep", type_name, _text_length(current), key in HIDDEN_KEYS)
            )
            continue
        if key in NEW_KEY_DEFAULTS:
            set_path(new_config, key, NEW_KEY_DEFAULTS[key])
            actions.append(
                Action(
                    key,
                    "add",
                    type_name,
                    _text_length(NEW_KEY_DEFAULTS[key]),
                    key in HIDDEN_KEYS,
                )
            )
            continue
        actions.append(
            Action(
                key, "skip", type_name, None, key in HIDDEN_KEYS, reason="core_default"
            )
        )

    # ④ 删除动作的行**排在 21 行之后**：那两个旧键在新 schema 里已经没有槽位
    #    （放回 `video` 组里会被读成"这个组还有这一项"，反而误导审计）
    for key, _type in DROPS:
        actions.append(dropped[key])

    # ⑤ 按新 schema 序重排（未知键原样保留）
    return _reorder(new_config), actions


def _type_of(key: str) -> str:
    for path, type_name in KEY_ORDER:
        if path == key:
            return type_name
    return "?"


def counts(actions) -> dict[str, int]:
    """动作计数（生产 22 叶 → 21 叶 的期望：rename 3 / drop 2 / add 1 / **keep 17**）。

    17 = 旧 22 叶 − 3 改名 − 2 删除；新 21 叶 = 17 keep + 3 改名 + 1 新增（SPEC-v0.5.1 §2.2 校验式）。
    """
    result = {name: 0 for name in ACTIONS}
    for item in actions:
        result[item.action] = result.get(item.action, 0) + 1
    return result


def changed(config: dict, new_config: dict) -> bool:
    """幂等判据：目标 dict 与读到的 dict 是否不同（相等 ⇒ `changed=0`，不产生备份）。"""
    return config != new_config


# --------------------------------------------------------------------------- #
# 磁盘
# --------------------------------------------------------------------------- #
def _load_json(path: str):
    """读 JSON（`utf-8-sig`：核心写出来的配置带 BOM，实测 `json.load` 会抛）。"""
    with open(path, "rb") as handle:
        raw = handle.read()
    return json.loads(raw.decode("utf-8-sig"))


def _dump_like_core(config: dict) -> bytes:
    """**逐字节同核心** `_write_config_snapshot`：BOM + indent=2 + 不转义中文 + 无尾随换行。

    `astrbot/core/config/astrbot_config.py:358-359`：`os.fdopen(fd, "w", encoding="utf-8-sig")`
    + `json.dump(..., indent=2, ensure_ascii=False)`。实测（SPEC §1.2）：
    `b"\\xef\\xbb\\xbf" + json.dumps(cfg, indent=2, ensure_ascii=False).encode() == 原文件字节`。
    """
    return b"\xef\xbb\xbf" + json.dumps(
        config, indent=2, ensure_ascii=False
    ).encode("utf-8")


def _existing_mode(path: str) -> int | None:
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return None


def _atomic_write(path: str, data: bytes) -> None:
    """同目录 `.tmp-<pid>-<rand>` → `os.replace`；**原文件权限位原样保留**。

    `os.replace` 换的是 inode，不显式复制 mode 就会把 0600 放宽成默认 0644 ——
    那是把配置权限降级。原文件不存在时才用 0600。
    """
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
        description=(
            "media_analysis v0.5.1 配置迁移器（22 叶 → 21 叶；默认干跑；"
            "只打印键名/动作/类型/长度与字节换算审计字段）"
        ),
    )
    parser.add_argument("--root", default="", help="AstrBot 根目录（未给则取 $ASTRBOT_ROOT）")
    parser.add_argument("--apply", action="store_true", help="真正落盘（默认只干跑）")
    parser.add_argument("--report", action="store_true", help="只读逐键报告（21 行，一键一行）")
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
    for item in actions:
        print(item.render())

    dirty = changed(config, new_config)
    if args.report or not args.apply:
        # 只读两态（默认 / --report）：逐键报告 + `keys=21` 摘要，**不写盘、不建备份**
        if dirty:
            print("提示：干跑不写盘；确认后用 --apply 落盘（会先写 0600 备份，保留原权限位）。")
        print(f"summary: changed={1 if dirty else 0} keys={len(KEY_ORDER)}")
        return 0

    backup_path = "-"
    if dirty:
        try:
            backup_path = _write_backup(config_path, original_bytes)
        except OSError as exc:
            return _fail(f"备份失败（{type(exc).__name__}），目标文件未改动")
        try:
            _atomic_write(config_path, _dump_like_core(new_config))
        except OSError as exc:
            kept = os.path.basename(backup_path)
            return _fail(
                f"写入失败（{type(exc).__name__}），目标文件保持原样（备份：{kept}）"
            )
    print(f"summary: changed={1 if dirty else 0} backup={backup_path}")
    return 0


def _do_rollback(backup: str) -> int:
    """从备份回滚：校验可解析 ⇒ 逐字节写回（原子）⇒ 打印恢复路径与成对回滚提醒。

    目标路径从备份名反推（`<配置>.bak-<UTC 戳>`）。**必须与代码回滚成对**：只回配置
    不改代码是**无效动作** —— 新 schema 下一次加载就把字节键当 stale 删掉（SPEC-v0.5.1 §3.4）。
    """
    target = backup.split(".bak-")[0]
    if not os.path.isfile(backup):
        return _fail("备份文件不存在")
    if not target or target == backup:
        return _fail("无法从备份路径推断目标配置")
    try:
        with open(backup, "rb") as handle:
            data = handle.read()
        json.loads(data.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return _fail(f"备份不可解析（{type(exc).__name__}）")
    try:
        _atomic_write(target, data)
    except OSError as exc:
        return _fail(f"回滚写入失败（{type(exc).__name__}）")
    print(f"restored={os.path.basename(target)}")
    print(
        "提醒：回滚配置必须与代码回滚**成对且反向**进行（先回代码 → 再 --rollback → 最后一次重载）；"
        "只要代码是 v0.5.1，单独回配置无效（新 schema 会把字节键当 stale 删掉，见 SPEC-v0.5.1 §3.4）"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
