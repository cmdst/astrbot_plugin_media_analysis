"""v0.5.1 配置迁移器 `tools/migrate_config_v05.py`（SPEC-v0.5.1 §2.1 / §3.2–§3.5 契约）。

这一支脚本与前面几支**性质不同**：v0.2/v0.3/v0.4.2 那三支只"补默认值"（`changed=0` 是常态），
本支**真的改值**（字节 → MB 换算、删两个键、加一个键）⇒ 它必须自带"可审计 + 可回滚 +
幂等"三件套，而且落盘格式必须与核心**逐字节同形**（否则核心下次加载会重写文件，
"重载后 sha 不变"那条判据直接红）。

断言面（G3 六格 + 附加）：

1. **换算**：`bytes_to_mb` / `mb_to_bytes` 的边界表，并与 `settings` 的同名函数**逐值相等**
   （脚本不 import 插件模块 ⇒ 这是一份**受守卫对偶**，不是第二事实源）；
2. **动作**：生产 22 叶 → `rename 3 / drop 2 / add 1 / keep 17`（`skip 0`），
   `hidden=1` 恰 4 条；受 NAME 表（`RENAMES` / `DROPS` / `NEW_KEY_DEFAULTS` / `HIDDEN_KEYS`）
   与 schema / settings 三方对齐；
3. **换算值**：`33554432 → 32`（delta 0）、`30000000 → 29`（30408704，delta +408704）、
   `67108864 → 64`（delta 0）；隐藏键 `compress_timeout_seconds=60` **保号**；
4. **落盘**：BOM + `indent=2` + `ensure_ascii=False` + **无尾随换行** + 键序 = schema 序 +
   原权限位（0600）保留 + 原子（先备份再写）；
5. **三态**：`--report` 只读（不写盘、不建备份）、`--apply` 先备份后落盘、`--rollback`
   逐字节还原（sha 相同）；
6. **失败面**：缺 `--root`、文件不存在、坏 JSON、备份不存在/不可解析 ⇒ exit 1 且目标不动。

口径：动作词表 `keep/rename/drop/add/skip` 由 SPEC §3.2 冻死；`keep` 的期望值是 **17**
（= §2.2 的"13 不动 + 4 隐藏"；§3.2 的 `keep` 行同样写 **17**，两处一致）。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

from astrbot_plugin_media_analysis import settings

_PLUGIN_DIR = Path(__file__).resolve().parents[1]
_SCRIPT = _PLUGIN_DIR / "tools" / "migrate_config_v05.py"
_SCHEMA = _PLUGIN_DIR / "_conf_schema.json"

_CFG_REL = Path("data/config/astrbot_plugin_media_analysis_config.json")

#: 生产实测的 22 叶（SPEC-v0.5.1 §1.1 的现场值；`background.mode='core'` 按 U2 **不动**）
PRODUCTION_CONFIG: dict = {
    "provider_settings": {
        "audio_provider_id": "ocg-high/mimo-v2.6-flash",
        "video_provider_id": "cpa_Gemini/gemini-3.8-flash-high",
        "vision_provider_id": "cpa_Gemini/gemini-3.5-flash-lite",
        "stt_provider_id": "",
    },
    "video": {
        "probe_enabled": True,
        "probe_ttl_seconds": 604800,
        "max_direct_bytes": 33554432,
        "native_timeout_seconds": 110,
        "native_max_tokens": 2048,
        "compress_enabled": True,
        "compress_target_bytes": 30000000,
        "compress_timeout_seconds": 60,
    },
    "prompts": {
        "audio": "自定义转写提示词{focus_clause}",
        "video": "自定义音轨提示词{focus_clause}",
        "frame": "自定义单帧提示词{focus_clause}",
        "summary": "摘要{kind_cn}{task}{material}",
        "video_native": "自定义原生提示词{focus_clause}",
    },
    "limits": {"max_media_seconds": 300, "max_frames": 6, "max_media_bytes": 67108864},
    "cache": {"ttl_seconds": 86400},
    "background": {"mode": "core"},
}


def _load():
    assert _SCRIPT.is_file(), f"迁移器不见了：{_SCRIPT}"
    spec = importlib.util.spec_from_file_location("migrate_config_v05_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


migrator = _load()


def _schema_leaves() -> dict[str, dict]:
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    leaves: dict[str, dict] = {}

    def walk(node: dict, prefix: str = "") -> None:
        for key, spec in node.items():
            path = f"{prefix}{key}"
            items = spec.get("items") if isinstance(spec, dict) else None
            if isinstance(items, dict) and items:
                walk(items, path + ".")
            else:
                leaves[path] = spec

    walk(schema)
    return leaves


# --------------------------------------------------------------------------- #
# 文件工具：造一份"生产形态"的 AstrBot 根（0600 + BOM + 无尾随换行）
# --------------------------------------------------------------------------- #
def _core_bytes(config: dict) -> bytes:
    """核心 `_write_config_snapshot` 的同形字节（SPEC-v0.5.1 §1.2 实测公式）。"""
    return b"\xef\xbb\xbf" + json.dumps(config, indent=2, ensure_ascii=False).encode("utf-8")


def _config_path(root: Path) -> Path:
    return root / _CFG_REL


def _write_root(tmp_path: Path, config: dict | None = None, *, mode: int = 0o600) -> Path:
    root = tmp_path / "astrbot"
    target = _config_path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_core_bytes(PRODUCTION_CONFIG if config is None else config))
    os.chmod(target, mode)
    return root


def _backups(root: Path) -> list[Path]:
    return sorted(_config_path(root).parent.glob("*.bak-*"))


def _run(capsys, *argv: str) -> tuple[int, str, str]:
    code = migrator.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --------------------------------------------------------------------------- #
# 1. 换算：与 settings **逐值相等**（受守卫对偶），边界表写死
# --------------------------------------------------------------------------- #
def test_bytes_to_mb_is_half_up_and_matches_settings() -> None:
    """`bytes_to_mb`：`(bytes + 524288) // 1048576`（§2.1 写死）。

    边界表是**独立的第二份数据**（不是从实现反推）：每个数都对应一次真实的四舍五入决策。
    """
    table = {
        0: 0,
        1: 0,
        524287: 0,
        524288: 1,
        1048575: 1,
        1048576: 1,
        1572864: 2,
        30000000: 29,
        30408704: 29,
        33554432: 32,
        67108864: 64,
        104857600: 100,
    }
    for raw, expected in table.items():
        assert migrator.bytes_to_mb(raw) == expected, f"{raw} 字节 ≠ {expected} MB"
        assert migrator.bytes_to_mb(raw) == settings.bytes_to_mb(raw), "两侧必须同值"


def test_mb_to_bytes_matches_settings() -> None:
    """`mb_to_bytes` 是纯乘法；两侧同值（否则迁移值与运行期值会差一个数量级）。"""
    for mb in (0, 1, 6, 8, 29, 32, 64, 100, 512):
        assert migrator.mb_to_bytes(mb) == settings.mb_to_bytes(mb) == mb * 1048576


def test_mb_constant_matches() -> None:
    """`BYTES_PER_MB` 只有这一个值（`1 MB ≡ 1 MiB`，J2）。"""
    assert migrator.BYTES_PER_MB == settings.BYTES_PER_MB == 1048576


# --------------------------------------------------------------------------- #
# 2. 表对齐：KEY_ORDER / RENAMES / DROPS / NEW_KEY_DEFAULTS / HIDDEN_KEYS
# --------------------------------------------------------------------------- #
def test_key_order_is_the_twenty_one_leaves_in_schema_order() -> None:
    """21 键，顺序 == schema 书写序 == `settings.KEY_MAP`（面板序 = 报告序）。"""
    keys = [key for key, _type in migrator.KEY_ORDER]
    assert len(keys) == 21
    assert keys == list(_schema_leaves())
    assert keys == list(settings.KEY_MAP)


def test_key_order_type_labels_match_the_schema_types() -> None:
    """展示用类型（`str/bool/int/text`）与 schema 的 `type`（`string/bool/int/text`）同义。"""
    leaves = _schema_leaves()
    same = {"str": "string", "string": "string", "int": "int", "bool": "bool", "text": "text"}
    for key, type_name in migrator.KEY_ORDER:
        assert same[type_name] == leaves[key]["type"], f"{key} 的类型标签与 schema 不一致"


def test_renames_are_the_three_byte_to_mb_keys() -> None:
    """3 条改名 = 三个字节键 → MB；钳位区间与 `settings` 常量、schema `slider` 三方同值。"""
    assert [(rule.old, rule.new) for rule in migrator.RENAMES] == [
        ("video.max_direct_bytes", "video.max_direct_mb"),
        ("video.compress_target_bytes", "video.compress_target_mb"),
        ("limits.max_media_bytes", "limits.max_media_mb"),
    ]
    leaves = _schema_leaves()
    expected_bounds = {
        "video.max_direct_mb": (
            settings.MIN_VIDEO_MAX_DIRECT_MB,
            settings.MAX_VIDEO_MAX_DIRECT_MB,
        ),
        "video.compress_target_mb": (
            settings.MIN_VIDEO_COMPRESS_TARGET_MB,
            settings.MAX_VIDEO_COMPRESS_TARGET_MB,
        ),
        "limits.max_media_mb": (settings.MIN_MAX_MEDIA_MB, settings.MAX_MAX_MEDIA_MB),
    }
    for rule in migrator.RENAMES:
        assert (rule.lo_mb, rule.hi_mb) == expected_bounds[rule.new]
        slider = leaves[rule.new]["slider"]
        assert (rule.lo_mb, rule.hi_mb) == (slider["min"], slider["max"])
        assert rule.new in settings.KEY_MAP and rule.old not in settings.KEY_MAP


def test_drops_are_the_two_native_keys() -> None:
    """2 条删除 = 两个 `native_*` 键（值进代码常量：4096 / 60.0，§2.2-B）。"""
    assert dict(migrator.DROPS) == {
        "video.native_max_tokens": "int",
        "video.native_timeout_seconds": "int",
    }
    for key, _type in migrator.DROPS:
        assert key not in settings.KEY_MAP
        assert key not in _schema_leaves()


def test_new_key_defaults_are_exactly_the_audio_timeout() -> None:
    """`NEW_KEY_DEFAULTS` **恰好 1 条**，且三处逐字一致（脚本 ⇄ settings ⇄ schema）。"""
    assert set(migrator.NEW_KEY_DEFAULTS) == {"limits.audio_timeout_seconds"}
    value = migrator.NEW_KEY_DEFAULTS["limits.audio_timeout_seconds"]
    assert value == settings.DEFAULT_AUDIO_TIMEOUT_SECONDS
    assert value == _schema_leaves()["limits.audio_timeout_seconds"]["default"]
    # 它确实是**新**键（v0.4.2 的 22 叶里没有）
    assert "limits.audio_timeout_seconds" not in PRODUCTION_CONFIG["limits"]


def test_hidden_keys_match_the_schema_invisible_set() -> None:
    """`hidden=1` 的 4 条 = schema 里 item 级 `invisible: true` 的那 4 叶。"""
    invisible = {
        path for path, spec in _schema_leaves().items() if spec.get("invisible") is True
    }
    assert set(migrator.HIDDEN_KEYS) == invisible
    assert len(migrator.HIDDEN_KEYS) == 4
    for key in migrator.HIDDEN_KEYS:
        assert key in settings.KEY_MAP, "隐藏键必须仍在键表里（隐藏 ≠ 删除）"


# --------------------------------------------------------------------------- #
# 3. plan()：动作计数 / 换算值 / 键序 / 幂等 / 钳位 / 未知键
# --------------------------------------------------------------------------- #
def _actions_by_key(actions) -> dict:
    return {item.key: item for item in actions}


def test_plan_action_counts_on_the_production_shape() -> None:
    """生产 22 叶 → `<21 叶 + 2 条 drop 行>`：`rename 3 / drop 2 / add 1 / keep 17`。

    计数口径按 SPEC §2.2 的校验式（旧 22 = 13 不动 + 4 隐藏 + 3 改名 + 2 删除 ⇒ keep = 17），
    与 SPEC §3.2 的动作表（`keep` = **17**）逐值一致。
    """
    _new, actions = migrator.plan(PRODUCTION_CONFIG)
    counts = migrator.counts(actions)
    assert counts["rename"] == 3
    assert counts["drop"] == 2
    assert counts["add"] == 1
    assert counts["keep"] == 17
    assert counts["skip"] == 0
    hidden = [item.key for item in actions if item.hidden]
    assert len(hidden) == 4, hidden
    assert set(hidden) == set(migrator.HIDDEN_KEYS), "hidden=1 只许出现在那 4 个隐藏键上"


def test_plan_converts_the_production_bytes_with_the_frozen_rounding() -> None:
    """换算值逐项断言：32→32（delta 0）/ 30000000→29（delta +408704）/ 67108864→64（delta 0）。"""
    new, actions = migrator.plan(PRODUCTION_CONFIG)
    by_key = _actions_by_key(actions)

    direct = by_key["video.max_direct_mb"]
    assert (direct.old_bytes, direct.new_mb, direct.new_bytes) == (33554432, 32, 33554432)
    assert direct.delta_bytes == 0 and direct.clamped is False

    target = by_key["video.compress_target_mb"]
    assert (target.old_bytes, target.new_mb, target.new_bytes) == (30000000, 29, 30408704)
    assert target.delta_bytes == 408704 and target.clamped is False
    assert abs(target.delta_bytes) <= 524288, "E2：半个 MB 内的差算等价"

    media = by_key["limits.max_media_mb"]
    assert (media.old_bytes, media.new_mb, media.new_bytes) == (67108864, 64, 67108864)
    assert media.delta_bytes == 0

    # 落盘后的**语义**：显式钳位后乘回去，等于核心会用到的字节
    assert new["video"]["max_direct_mb"] == 32
    assert new["video"]["compress_target_mb"] == 29
    assert new["limits"]["max_media_mb"] == 64
    assert settings.mb_to_bytes(new["video"]["compress_target_mb"]) == 30408704


def test_plan_keeps_the_hidden_values_and_drops_the_native_keys() -> None:
    """隐藏键**保号**（`compress_timeout_seconds` 仍是生产值 60）；两个 native 键消失。"""
    new, _actions = migrator.plan(PRODUCTION_CONFIG)
    assert new["video"]["compress_timeout_seconds"] == 60, "隐藏≠删除：值必须原样留着"
    assert new["video"]["probe_ttl_seconds"] == 604800
    assert new["video"]["probe_enabled"] is True
    assert new["provider_settings"]["stt_provider_id"] == ""
    assert "native_max_tokens" not in new["video"]
    assert "native_timeout_seconds" not in new["video"]
    assert "max_direct_bytes" not in new["video"]
    assert "compress_target_bytes" not in new["video"]
    assert "max_media_bytes" not in new["limits"]
    assert new["limits"]["audio_timeout_seconds"] == 60
    # U2：引擎开关**一个字节都不动**
    assert new["background"]["mode"] == "core"


def test_plan_writes_the_schema_key_order() -> None:
    """落盘键序 = schema 序（组序 `background` 置顶 + 组内 `KEY_ORDER`），否则核心会重写文件。"""
    new, _actions = migrator.plan(PRODUCTION_CONFIG)
    assert list(new) == ["background", "provider_settings", "video", "limits", "cache", "prompts"]
    assert list(new["video"]) == [
        "max_direct_mb",
        "compress_enabled",
        "compress_target_mb",
        "compress_timeout_seconds",
        "probe_enabled",
        "probe_ttl_seconds",
    ]
    assert list(new["limits"]) == [
        "max_media_seconds",
        "max_frames",
        "max_media_mb",
        "audio_timeout_seconds",
    ]
    assert list(new["provider_settings"]) == [
        "audio_provider_id",
        "video_provider_id",
        "vision_provider_id",
        "stt_provider_id",
    ]
    assert list(new["prompts"]) == [
        "audio",
        "video",
        "frame",
        "summary",
        "video_native",
    ]
    # 改名键的**相对次序**保留（max_direct 在 compress_target 之前）
    video_keys = list(new["video"])
    assert video_keys.index("max_direct_mb") < video_keys.index("compress_target_mb")


def test_plan_is_idempotent() -> None:
    """`plan(plan(x))` 不再产生任何变化（`changed=0`，全部 `keep`，不产备份）。"""
    once, _actions = migrator.plan(PRODUCTION_CONFIG)
    twice, second_actions = migrator.plan(once)
    assert twice == once
    assert migrator.changed(once, twice) is False
    assert migrator.counts(second_actions) == {
        "keep": 21,
        "rename": 0,
        "drop": 0,
        "add": 0,
        # 两个 native 键在上一趟已被删掉 ⇒ 这一趟是 `skip reason=no_source`（**不是** drop）
        "skip": 2,
    }


def test_plan_clamps_into_the_new_mb_window() -> None:
    """钳位：0.5 MiB 的老值抬到 1 MB（`clamped=1`，D-4 的直接后果）；超上界压回 32 / 512。"""
    config = {
        "provider_settings": {},
        "video": {"compress_target_bytes": 400000, "max_direct_bytes": 2 * 1024**3},
        "prompts": {},
        "limits": {"max_media_bytes": 2 * 1024**3},
        "cache": {},
        "background": {"mode": "plugin"},
    }
    _new, actions = migrator.plan(config)
    by_key = _actions_by_key(actions)
    # 0.38 MB 取整成 0 ⇒ 钳到下限 1（`clamped=1`）
    assert by_key["video.compress_target_mb"].new_mb == 1
    assert by_key["video.compress_target_mb"].clamped is True
    assert by_key["video.max_direct_mb"].new_mb == 32
    assert by_key["video.max_direct_mb"].clamped is True
    assert by_key["limits.max_media_mb"].new_mb == 512
    assert by_key["limits.max_media_mb"].clamped is True
    # 被钳位的行必须能在**报告文本**里看见 rolled 标记
    rendered = [item.render() for item in actions if item.action == "rename"]
    assert sum("clamped=1" in line for line in rendered) == 3, rendered

    # 对照：524288 B（0.5 MiB）靠 **half-up 取整** 就落到 1 MB，不需要钳位（D-4 的边界）
    _new2, actions2 = migrator.plan(
        {"video": {"compress_target_bytes": 524288}, "background": {"mode": "plugin"}}
    )
    half = _actions_by_key(actions2)["video.compress_target_mb"]
    assert (half.old_bytes, half.new_mb, half.clamped) == (524288, 1, False)


def test_plan_preserves_unknown_keys() -> None:
    """**不删其它键**：未知键（含未知组、组内未知叶）原样保留，只挪位置。"""
    config = json.loads(json.dumps(PRODUCTION_CONFIG))
    config["video"]["some_extra_key"] = {"deep": 1}
    config["my_own_group"] = {"flag": True}
    new, _actions = migrator.plan(config)
    assert new["video"]["some_extra_key"] == {"deep": 1}
    assert new["my_own_group"] == {"flag": True}
    # 未知叶排在**已认识的叶之后**（不抢面板位置），未知组排在最后
    assert list(new["video"])[-1] == "some_extra_key"
    assert list(new)[-1] == "my_own_group"


def test_plan_skips_a_missing_rename_source() -> None:
    """老键缺失（半配置）⇒ `action=skip reason=no_source`，脚本不替用户造值。"""
    config = {"background": {"mode": "plugin"}, "video": {"compress_enabled": True}}
    new, actions = migrator.plan(config)
    by_key = _actions_by_key(actions)
    for rule in migrator.RENAMES:
        assert by_key[rule.new].action == "skip"
        assert by_key[rule.new].reason == "no_source"
    assert "max_direct_mb" not in new["video"], "没旧值就写默认值（核心会补 schema 默认）"


# --------------------------------------------------------------------------- #
# 4. 落盘格式：BOM + indent=2 + 无尾随换行 + 权限位 + 原子
# --------------------------------------------------------------------------- #
def test_dump_is_byte_identical_to_the_core_writer() -> None:
    """`_dump_like_core()` 与核心 `_write_config_snapshot` 同形（§1.2 实测公式）。"""
    new, _actions = migrator.plan(PRODUCTION_CONFIG)
    data = migrator._dump_like_core(new)
    assert data[:3] == b"\xef\xbb\xbf", "必须带 BOM"
    assert not data.endswith(b"\n"), "不许有尾随换行（核心没有）"
    assert data[3:] == json.dumps(new, indent=2, ensure_ascii=False).encode("utf-8")
    # 中文不转义（提示词里有中文；转义后核心一写回就换 sha）
    assert "自定义" in data[3:].decode("utf-8")


# --------------------------------------------------------------------------- #
# 5. CLI 三态
# --------------------------------------------------------------------------- #
def test_report_is_read_only(tmp_path: Path, capsys) -> None:
    """`--report`：逐键报告 + `summary: changed=1 keys=21`，**不写盘、不建备份**。"""
    root = _write_root(tmp_path)
    before = _config_path(root).read_bytes()

    code, out, err = _run(capsys, "--root", str(root), "--report")

    assert code == 0, err
    assert _config_path(root).read_bytes() == before, "只读模式改了文件！"
    assert _backups(root) == [], "只读模式产生了备份"
    lines = out.splitlines()
    assert lines[-1] == "summary: changed=1 keys=21"
    action_lines = [line for line in lines if " action=" in line]
    assert len(action_lines) == 23, "21 个新叶 + 2 条 drop 行"
    assert sum("action=rename" in line for line in action_lines) == 3
    assert sum("action=drop" in line for line in action_lines) == 2
    assert sum("action=add" in line for line in action_lines) == 1
    assert sum("action=keep" in line for line in action_lines) == 17
    assert sum("hidden=1" in line for line in action_lines) == 4


def test_default_mode_is_a_dry_run(tmp_path: Path, capsys) -> None:
    """不带 `--apply` 就是干跑：文件不动、无备份、末行仍是 `keys=21` 摘要。"""
    root = _write_root(tmp_path)
    before = _config_path(root).read_bytes()

    code, out, _err = _run(capsys, "--root", str(root))

    assert code == 0
    assert _config_path(root).read_bytes() == before, "干跑改了文件！"
    assert _backups(root) == []
    assert out.splitlines()[-1] == "summary: changed=1 keys=21"
    assert "干跑不写盘" in out


def test_report_prints_no_config_values(tmp_path: Path, capsys) -> None:
    """报告**不打印内容**：provider id、提示词正文一个字符都不进 stdout（只打 len）。"""
    root = _write_root(tmp_path)
    _code, out, _err = _run(capsys, "--root", str(root), "--report")
    for secret in ("ocg-high", "cpa_Gemini", "自定义转写提示词", "自定义原生提示词"):
        assert secret not in out, f"报告泄露了配置内容：{secret}"
    # 长度是**允许**的（`len` 是长度不是内容）
    assert "prompts.audio action=keep type=text len=" in out


def test_apply_backs_up_first_then_writes_the_core_format(tmp_path: Path, capsys) -> None:
    """`--apply`：0600 逐字节备份 → 原子落盘；新文件 BOM/键序/无尾随换行/权限位全对。"""
    root = _write_root(tmp_path)
    original = _config_path(root).read_bytes()
    os.chmod(_config_path(root), 0o600)

    code, out, err = _run(capsys, "--root", str(root), "--apply")

    assert code == 0, err
    backups = _backups(root)
    assert len(backups) == 1, backups
    backup = backups[0]
    assert backup.read_bytes() == original, "备份必须逐字节等于迁移前文件"
    assert stat.S_IMODE(os.stat(backup).st_mode) == 0o600, "备份必须 0600"
    assert out.splitlines()[-1] == f"summary: changed=1 backup={backup}"

    raw = _config_path(root).read_bytes()
    assert raw[:3] == b"\xef\xbb\xbf", "落盘少了 BOM"
    assert not raw.endswith(b"\n"), "落盘多了尾随换行"
    config = json.loads(raw.decode("utf-8-sig"))
    assert raw[3:] == json.dumps(config, indent=2, ensure_ascii=False).encode("utf-8")
    assert list(config) == ["background", "provider_settings", "video", "limits", "cache", "prompts"]
    assert config["video"]["max_direct_mb"] == 32
    assert config["video"]["compress_target_mb"] == 29
    assert config["limits"]["max_media_mb"] == 64
    assert config["limits"]["audio_timeout_seconds"] == 60
    assert config["video"]["compress_timeout_seconds"] == 60, "隐藏键保号"
    assert stat.S_IMODE(os.stat(_config_path(root)).st_mode) == 0o600, "权限位被放宽了"
    # 提示词与 provider id 逐字保留（迁移不碰值）
    assert config["prompts"]["audio"] == PRODUCTION_CONFIG["prompts"]["audio"]
    assert (
        config["provider_settings"]["video_provider_id"]
        == PRODUCTION_CONFIG["provider_settings"]["video_provider_id"]
    )


def test_apply_keeps_a_non_default_mode(tmp_path: Path, capsys) -> None:
    """原文件是 0644（手滑）也原样保留：写盘不动权限位（只补 BOM 等格式）。"""
    root = _write_root(tmp_path, mode=0o644)
    _code, _out, err = _run(capsys, "--root", str(root), "--apply")
    assert err == ""
    assert stat.S_IMODE(os.stat(_config_path(root)).st_mode) == 0o644


def test_apply_is_idempotent_and_creates_no_second_backup(tmp_path: Path, capsys) -> None:
    """迁移完成后再跑 `--apply` ⇒ `changed=0`，不产新备份、文件字节不变。"""
    root = _write_root(tmp_path)
    assert _run(capsys, "--root", str(root), "--apply")[0] == 0
    after_first = _config_path(root).read_bytes()
    first_backups = _backups(root)

    code, out, _err = _run(capsys, "--root", str(root), "--apply")

    assert code == 0
    assert out.splitlines()[-1] == "summary: changed=0 backup=-"
    assert _config_path(root).read_bytes() == after_first
    assert _backups(root) == first_backups


def test_rollback_restores_the_exact_bytes(tmp_path: Path, capsys) -> None:
    """`--rollback <备份>`：逐字节还原（sha 相同）+ 权限位 0600 + 成对回滚提醒。"""
    import hashlib

    root = _write_root(tmp_path)
    original = _config_path(root).read_bytes()
    _run(capsys, "--root", str(root), "--apply")
    backup = _backups(root)[0]
    assert _config_path(root).read_bytes() != original

    code, out, err = _run(capsys, "--rollback", str(backup))

    assert code == 0, err
    restored = _config_path(root).read_bytes()
    assert hashlib.sha256(restored).hexdigest() == hashlib.sha256(original).hexdigest()
    assert stat.S_IMODE(os.stat(_config_path(root)).st_mode) == 0o600
    assert f"restored={_config_path(root).name}" in out
    assert "成对" in out, "回滚必须提示与代码回滚成对（§3.4：独立回配置无效）"


# --------------------------------------------------------------------------- #
# 6. 失败面（全部 exit 1，且目标文件不动）
# --------------------------------------------------------------------------- #
def test_missing_root_fails_without_touching_anything(tmp_path: Path, capsys, monkeypatch) -> None:
    """没有 `--root` 且没有 `$ASTRBOT_ROOT` ⇒ exit 1（不猜生产路径）。"""
    monkeypatch.delenv("ASTRBOT_ROOT", raising=False)
    code, _out, err = _run(capsys)
    assert code == 1
    assert "缺少 --root" in err


def test_missing_config_fails(tmp_path: Path, capsys) -> None:
    root = tmp_path / "empty"
    (root / "data" / "config").mkdir(parents=True)
    code, _out, err = _run(capsys, "--root", str(root), "--report")
    assert code == 1
    assert "目标配置不存在" in err


def test_broken_json_fails_and_leaves_the_file_alone(tmp_path: Path, capsys) -> None:
    root = tmp_path / "astrbot"
    target = _config_path(root)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"\xef\xbb\xbf{not json")
    before = target.read_bytes()

    code, _out, err = _run(capsys, "--root", str(root), "--apply")

    assert code == 1
    assert "读不出来" in err
    assert target.read_bytes() == before
    assert _backups(root) == []


def test_rollback_rejects_missing_or_broken_backup(tmp_path: Path, capsys) -> None:
    code, _out, err = _run(capsys, "--rollback", str(tmp_path / "nope.bak-20260101T000000Z"))
    assert code == 1
    assert "备份文件不存在" in err

    broken = tmp_path / "astrbot" / "data" / "config" / "x.json.bak-20260101T000000Z"
    broken.parent.mkdir(parents=True)
    broken.write_text("{nope", encoding="utf-8")
    code, _out, err = _run(capsys, "--rollback", str(broken))
    assert code == 1
    assert "备份不可解析" in err


# --------------------------------------------------------------------------- #
# 7. 内容冻结：不 import 插件模块（独立脚本）
# --------------------------------------------------------------------------- #
def test_migrator_does_not_import_the_plugin_package() -> None:
    """脚本必须能在**插件树之外**跑：不许 import `astrbot*` 或插件包（AST 级判据）。"""
    tree = ast.parse(_SCRIPT.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    offenders = [name for name in imported if name.split(".")[0] in {"astrbot", "astrbot_plugin_media_analysis"}]
    assert not offenders, f"迁移器 import 了插件/核心模块：{offenders}"
    # 也不许出现相对导入（独立脚本没有包上下文）
    assert "from ." not in _SCRIPT.read_text(encoding="utf-8")


def test_action_vocabulary_is_the_frozen_five() -> None:
    """动作词表 `keep/rename/drop/add/skip` 由 SPEC §3.2 冻死（多一个词就多一种运维误读）。"""
    assert {item.action for item in migrator.plan(PRODUCTION_CONFIG)[1]} <= migrator.ACTIONS
    assert migrator.ACTIONS == {"keep", "rename", "drop", "add", "skip"}
    assert issubclass(migrator.Action, tuple), "报告行必须是 NamedTuple（独立脚本可被 importlib 加载）"
