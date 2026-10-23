#!/usr/bin/env python3
"""配置迁移器：v0.1（4 键）→ v0.2（11 键）。**默认干跑**，只打印，不写盘、不建目录。

执行者是 ops / fix，**不是 dev**（本 lane 不碰生产配置）。契约见 SPEC-v0.2 §5.5。

先记住三件事（否则会误以为"不跑脚本新键就不会出现"）：

1. **脚本不负责"造新键"**。新代码 + 新 schema 上线后，核心 `AstrBotConfig` 构造期就会跑
   `check_config_integrity(default_from_schema, 用户配置, schema)`（`astrbot_config.py:113-121`）
   并把 schema 里有、配置里没有的键**补上默认值**（`:213-221`）。所以脚本的职责只有三条：
   ① 备份、② 把旧 STT 值**安全地**搬到 `audio_provider_id`、③ 提供成对回滚的锚点。
2. **顺序不能反**：先上代码+schema → 重启/重载 → 再跑本脚本。反序时**旧 schema** 不认识新键，
   下次加载会把 `provider_settings.audio_provider_id` 当 stale 键**删掉并落盘**
   （`astrbot_config.py:252-266`）。
3. **旧键 `stt_provider_id` 不删**。它继续留在 v0.2 的 schema 里（否则值会被静默删除）；
   本脚本最多用 `--clear-legacy` 把它置空，**永不删键**。

搬值必须**带类型校验**（captain 口径 + SPEC §5.5 的搬值规则）：旧值语义上是 **STTProvider id**，
而 `audio_provider_id` 必须是能做 `text_chat(audio_urls=…)` 的多模态 chat provider。
只有"读得到该 id、是 chat 类、`modalities` **明确含 `audio`**"才搬；否则**不搬**，把原因码
（`not_chat` / `no_audio` / `not_found`）写进报告 —— 搬一个不具备音频能力的 id，只会让工具
每次调用都报 `PROVIDER_CAPABILITY_MISMATCH`，而且看起来像"模型不支持音频"。

**任何模式都不打印配置值**：只打印点号键、动作、类型、长度（`len` 是长度不是内容）。
stdout 里不出现 provider id 字面量，stderr 只打异常类名。

**落盘不改权限**：目标文件原来的权限位会被**原样保留**（核心自己写 `cmd_config.json` 用
0600，生产那份也是 0600）；`os.replace` 换的是 inode，不显式复制 mode 就会把 600 放宽成
默认的 644 —— 那是把配置权限降级。原文件不存在时才用 0600。

用法：

    python3 tools/migrate_config_v02.py --root <AstrBot 根>            # 干跑（默认）
    python3 tools/migrate_config_v02.py --root <AstrBot 根> --apply    # 备份 + 原子落盘
    python3 tools/migrate_config_v02.py --root <AstrBot 根> --apply --clear-legacy
    python3 tools/migrate_config_v02.py --root <AstrBot 根> --report-legacy
    python3 tools/migrate_config_v02.py --rollback <备份文件>

退出码：`0` 成功（含"无变化"）/ `1` 错误（读、写、JSON、BOM、路径不存在）/
`2` 需人工介入（`needs_attention`，例如旧值不可搬到音频位）。

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
CMD_CONFIG_REL = os.path.join("data", "cmd_config.json")

AUDIO_KEY = "provider_settings.audio_provider_id"
LEGACY_KEY = "provider_settings.stt_provider_id"

#: 目标文件不存在时的兜底权限（配置里有 provider id 与提示词，默认不给同机他人读）
DEFAULT_CONFIG_MODE = 0o600

# 11 个键的**书写顺序**（与 `_conf_schema.json` 一致）与展示用类型。
# 只有前两条是本脚本"拥有"的（会改值）；其余仅报告 keep/skip。
KEY_ORDER: tuple[tuple[str, str], ...] = (
    (AUDIO_KEY, "str"),
    ("provider_settings.vision_provider_id", "str"),
    (LEGACY_KEY, "str"),
    ("prompts.audio", "text"),
    ("prompts.video", "text"),
    ("prompts.frame", "text"),
    ("prompts.summary", "text"),
    ("limits.max_media_seconds", "int"),
    ("limits.max_frames", "int"),
    ("limits.max_media_bytes", "int"),
    ("cache.ttl_seconds", "int"),
)

REASON_OK = "ok"
REASON_NOT_FOUND = "not_found"
REASON_NOT_CHAT = "not_chat"
REASON_NO_AUDIO = "no_audio"

# 非 chat 类的 provider 类型标记（`cmd_config.json` 里两种字段都见过：
# `provider_type` ∈ {None(chat), embedding, rerank, speech_to_text}，
# `type` ∈ {None, mimo_stt_api, openai_embedding, vllm_rerank}）。
_NON_CHAT_PROVIDER_TYPES = frozenset(
    {"embedding", "rerank", "speech_to_text", "stt", "tts", "text_to_speech"}
)
_NON_CHAT_TYPES = frozenset({"mimo_stt_api", "openai_embedding", "vllm_rerank"})


class Action(NamedTuple):
    """一条逐键报告：键、动作、类型、长度（`None` ⇒ 打印 `-`）、原因码。

    刻意用 `NamedTuple` 而不是 `dataclass`：本文件是**独立脚本**，不保证调用方先把它
    注册进 `sys.modules`（`dataclass` 在那种形态下会去 `sys.modules[cls.__module__]`
    取命名空间而炸）。`NamedTuple` 没有这个隐含前提，被 `importlib` 当纯函数库加载也稳。
    """

    key: str
    action: str  # add | keep | move | clear | skip
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


# --------------------------------------------------------------------------- #
# 能力判定（只读 cmd_config）
# --------------------------------------------------------------------------- #
def find_provider(cmd_config, provider_id: str) -> dict | None:
    """在 `cmd_config.json` 的 `provider` 列表里按 id 找条目（找不到回 `None`）。"""
    if not isinstance(cmd_config, dict):
        return None
    providers = cmd_config.get("provider")
    if not isinstance(providers, list):
        return None
    for entry in providers:
        if isinstance(entry, dict) and entry.get("id") == provider_id:
            return entry
    return None


def audio_compatible(entry: dict | None) -> str:
    """该 provider 能否放到 `audio_provider_id`：回原因码（`ok` / `not_found` / …）。

    三条全部满足才算 `ok`：存在、是 chat 类、`modalities` **明确含 `audio`**。
    口径与插件运行期**严格一致**（`providers.supports_modality`：`None`/缺省 ⇒ 不支持），
    所以这里"不搬"的判断和工具上线后"能不能听"的判断不会打架。
    """
    if not isinstance(entry, dict):
        return REASON_NOT_FOUND
    provider_type = entry.get("provider_type")
    if isinstance(provider_type, str):
        if provider_type.strip().lower() in _NON_CHAT_PROVIDER_TYPES:
            return REASON_NOT_CHAT
    raw_type = entry.get("type")
    if isinstance(raw_type, str) and raw_type.strip().lower() in _NON_CHAT_TYPES:
        return REASON_NOT_CHAT
    modalities = entry.get("modalities")
    if not isinstance(modalities, list) or "audio" not in modalities:
        return REASON_NO_AUDIO
    provided = entry.get("enable", True)
    if provided is False:
        return REASON_NOT_CHAT  # 同一个原因码：不是"可用的 chat provider"
    return REASON_OK


# --------------------------------------------------------------------------- #
# 纯函数：plan
# --------------------------------------------------------------------------- #
def plan(config: dict, cmd_config, *, clear_legacy: bool = False):
    """算出目标配置与逐键动作，**不碰磁盘**（`tests/test_migrate_config.py` 直接调它）。

    返回 `(new_config, actions)`：`new_config` 是**新的 dict**（不改入参），
    `actions` 按 `KEY_ORDER` 顺序给出 11 条报告。
    """
    new_config = json.loads(json.dumps(config)) if isinstance(config, dict) else {}
    actions: list[Action] = []
    moved = False
    attempted_move = False
    legacy_reason = REASON_OK

    audio_present = get_path(config, AUDIO_KEY) is not None
    legacy_value = _text(get_path(config, LEGACY_KEY, ""))
    audio_value = _text(get_path(config, AUDIO_KEY, ""))

    if legacy_value and not audio_value:
        attempted_move = True
        legacy_reason = audio_compatible(find_provider(cmd_config, legacy_value))
        if legacy_reason == REASON_OK:
            set_path(new_config, AUDIO_KEY, legacy_value)
            moved = True
    # `--clear-legacy` 只在"值确实已经在音频位上"时才允许置空：本次搬成功，或历史上已搬过
    # （audio 的值与旧值逐字相等）。否则清空＝静默丢设置。
    clearable = bool(legacy_value) and (moved or audio_value == legacy_value)

    for key, type_name in KEY_ORDER:
        current = get_path(new_config, key)
        present = current is not None
        if key == AUDIO_KEY:
            if moved:
                action = "move" if audio_present else "add"
            elif present:
                action = "keep"
            else:
                action = "skip"
            actions.append(
                Action(key, action, type_name, _length(current) if present else None)
            )
            continue
        if key == LEGACY_KEY:
            reason = ""
            if legacy_value and clear_legacy and clearable:
                set_path(new_config, LEGACY_KEY, "")
                action, length = "clear", 0
            elif legacy_value and attempted_move and not moved:
                # 想搬但搬不了：值**原样留在旧键**，原因码进报告，退出码 2 等人工
                action, length, reason = "skip", len(legacy_value), legacy_reason
            elif present:
                action, length = "keep", _length(current)
            else:
                action, length = "skip", None
            actions.append(Action(key, action, type_name, length, reason))
            continue
        if present:
            actions.append(Action(key, "keep", type_name, _length(current)))
        else:
            # 缺失的新键由核心 `check_config_integrity` 在下次加载时补默认（脚本不造键）
            actions.append(Action(key, "skip", type_name, None, "core_default"))
    return new_config, actions


def needs_attention(actions: list[Action]) -> bool:
    """是否有需要人工介入的项（旧值不可搬 ⇒ 退出码 2）。"""
    return any(
        item.key == LEGACY_KEY and item.action == "skip" and item.reason
        for item in actions
    )


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
    """目标文件当前的权限位（`0o600` 这类）；不存在或取不到 ⇒ `None`。"""
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return None


def _atomic_write(path: str, data: bytes) -> None:
    """同目录 `.tmp-<pid>-<rand>` → `os.replace`；**原文件权限位原样保留**。

    任一步失败 ⇒ 目标文件保持原样。`os.replace` 换的是 inode，不显式 `chmod` 的话新文件
    会拿到默认 0644 ⇒ 把核心写的 0600 配置**降级**成同机可读（provider id 与提示词都在里面）。
    原文件不存在（首次创建）⇒ 用 `DEFAULT_CONFIG_MODE`（0600）。
    """
    directory = os.path.dirname(os.path.abspath(path))
    mode = _existing_mode(path)
    tmp_path = os.path.join(
        directory, f".tmp-{os.getpid()}-{secrets.token_hex(4)}"
    )
    try:
        # 先按 0600 建（`os.open` 的 mode 会被 umask 削，所以下面再 `chmod` 精确落位）
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
    """逐字节备份原文件，权限 **0600**（配置里有 provider id 与提示词，不给同机他人读）。

    文件名 = `<配置>.bak-<UTC yyyymmddThhmmssZ>`；同一秒内第二次迁移（例如 `--apply`
    之后紧接着 `--apply --clear-legacy`）会撞名，因此撞名时追加 `-<6 hex>` 重试，
    **绝不覆盖**已有备份（`O_EXCL`）。
    """
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
        description="media_analysis 配置迁移器（默认干跑；只打印键名/动作/类型/长度，不打印值）",
    )
    parser.add_argument("--root", default="", help="AstrBot 根目录（未给则取 $ASTRBOT_ROOT）")
    parser.add_argument("--apply", action="store_true", help="真正落盘（默认只干跑）")
    parser.add_argument("--clear-legacy", action="store_true", help="值已搬走后把旧键置空（不删键）")
    parser.add_argument("--rollback", default="", help="从备份文件回滚（与代码回滚成对进行）")
    parser.add_argument("--report-legacy", action="store_true", help="只读报告：旧键是否为空、能否搬到音频位")
    args = parser.parse_args(argv)

    try:
        if args.rollback:
            return _do_rollback(args.rollback)
        if not args.report_legacy:
            root = _resolve_root(args)
        else:
            root = args.root or os.environ.get("ASTRBOT_ROOT") or ""
            if not root:
                raise SystemExit(_fail("缺少 --root 且未设置 ASTRBOT_ROOT（不猜生产路径）"))
            root = os.path.abspath(os.path.expanduser(root))
    except SystemExit as exc:  # `_resolve_root` 的明确失败
        code = exc.code
        return code if isinstance(code, int) else 1

    config_path = os.path.join(root, CONFIG_REL)
    cmd_config_path = os.path.join(root, CMD_CONFIG_REL)
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

    cmd_config = None
    if os.path.isfile(cmd_config_path):
        try:
            cmd_config = _load_json(cmd_config_path)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            print(
                f"warn: cmd_config 读不出来（{type(exc).__name__}），能力校验将按 not_found 处理",
                file=sys.stderr,
            )
            cmd_config = None

    new_config, actions = plan(config, cmd_config, clear_legacy=args.clear_legacy)

    if args.report_legacy:
        legacy_value = _text(get_path(config, LEGACY_KEY, ""))
        audio_value = _text(get_path(config, AUDIO_KEY, ""))
        reason = (
            audio_compatible(find_provider(cmd_config, legacy_value))
            if legacy_value
            else REASON_OK
        )
        print(f"legacy_key={LEGACY_KEY} empty={not bool(legacy_value)}")
        print(f"audio_key={AUDIO_KEY} empty={not bool(audio_value)}")
        movable = bool(legacy_value) and not audio_value and reason == REASON_OK
        print(f"movable={movable} reason={reason}")
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
    summary = f"summary: changed={1 if dirty else 0} backup={shown_backup}"
    if needs_attention(actions):
        summary += " needs_attention=1"
    print(summary)
    if needs_attention(actions):
        print(
            "提示：旧值不是可用的多模态 chat provider，已原样留在 stt_provider_id（未搬）。"
            "请人工在插件配置里选一个模型能力勾了「音频」的对话模型。"
        )
        return 2
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
    print("提醒：回滚配置必须与代码回滚成对进行（先回代码、再回配置、最后重启；见 SPEC-v0.2 §14 / README「部署与迁移」）")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
