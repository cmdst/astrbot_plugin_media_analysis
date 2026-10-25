"""v0.4.2 配置迁移器 `tools/migrate_config_v042.py`（SPEC-v0.4.2 §3.5 / §13 契约）。

与 v0.3 那支脚本的**性质相同**（只补默认值、不搬值、不删键），只有增量不同：

- **1 个**新键（`background.mode` = `"plugin"`），不是 10 个；
- `KEY_ORDER` 是 **22 键**，且必须**恰好**是 `settings.KEY_MAP` 的顺序；
- `tools/migrate_config_v03.py` **冻结为历史快照**（仍 21 键）⇒ 本文件同时钉住
  "v03 是本表的 21 键前缀"这条关系（另一个视角见 `tests/test_config_map.py`）。

断言面（12 组）：键表三方同集同序 / 类型标签 / 新键默认值三处逐字 / 干跑不写盘 /
`--apply` 只补 1 键 + 保权限 + 0600 逐字节备份 + 原子 + 幂等 / `--report` 22 行不打印值 /
`--rollback` 逐字节还原 / 失败面（缺文件、坏 JSON、BOM、缺 root）/ 不 import 插件 / 不删键 /
顺序纪律写在脚本自己的文档里。
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

from astrbot_plugin_media_analysis import settings

_PLUGIN_DIR = Path(__file__).resolve().parents[1]
_SCRIPT = _PLUGIN_DIR / "tools" / "migrate_config_v042.py"
_SCRIPT_V03 = _PLUGIN_DIR / "tools" / "migrate_config_v03.py"
_SCHEMA = _PLUGIN_DIR / "_conf_schema.json"

_CFG_REL = Path("data/config/astrbot_plugin_media_analysis_config.json")

#: 一份 v0.4.1 形态的用户配置（21 键里的 6 个），用来验证"只补那一个新键、其余一个不动"
_V041_CONFIG: dict = {
    "provider_settings": {
        "stt_provider_id": "old_stt_id",
        "vision_provider_id": "vision-1",
    },
    "prompts": {"audio": "自定义转写提示词{focus_clause}"},
    "limits": {"max_media_seconds": 120, "max_frames": 6},
    "cache": {"ttl_seconds": 0},
}
_PRESENT_KEYS = 6

#: v0.4.2 的**唯一**新键
NEW_KEYS = {"background.mode"}


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


migrator = _load(_SCRIPT, "migrate_config_v042_under_test")
migrator_v03 = _load(_SCRIPT_V03, "migrate_config_v03_frozen_under_test")


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


def _write_root(tmp_path: Path, *, config: object = _V041_CONFIG) -> Path:
    root = tmp_path / "astrbot_root"
    config_path = root / _CFG_REL
    config_path.parent.mkdir(parents=True, exist_ok=True)
    text = (
        config
        if isinstance(config, str)
        else json.dumps(config, ensure_ascii=False, indent=2)
    )
    config_path.write_text(text, encoding="utf-8")
    return root


def _config_path(root: Path) -> Path:
    return root / _CFG_REL


def _backups(root: Path) -> list[Path]:
    return sorted(_config_path(root).parent.glob(_config_path(root).name + ".bak-*"))


def _run(capsys, *argv: str) -> tuple[int, str, str]:
    code = migrator.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --------------------------------------------------------------------------- #
# 1. KEY_ORDER / NEW_KEY_DEFAULTS：与插件侧的对偶
# --------------------------------------------------------------------------- #
def test_key_order_is_the_twenty_two_keys_in_schema_order() -> None:
    """22 键、集合 == `settings.KEY_MAP`、顺序 == schema 书写顺序 == `KEY_MAP` 顺序。"""
    keys = [key for key, _type in migrator.KEY_ORDER]
    assert len(keys) == 22
    assert set(keys) == set(settings.KEY_MAP)
    assert keys == list(_schema_leaves())
    assert keys == list(settings.KEY_MAP)
    assert keys[-1] == "background.mode", "新组必须追加在**最末尾**（旧 21 键相对顺序不动）"


def test_key_order_type_labels_match_the_schema_types() -> None:
    """展示用类型（`str/bool/int/text`）与 schema 的 `type`（`string/bool/int/text`）同义。"""
    leaves = _schema_leaves()
    same = {"str": "string", "string": "string", "int": "int", "bool": "bool", "text": "text"}
    for key, type_name in migrator.KEY_ORDER:
        assert same[type_name] == leaves[key]["type"], f"{key} 的类型标签与 schema 不一致"


def test_new_key_defaults_are_exactly_the_background_mode() -> None:
    """`NEW_KEY_DEFAULTS` **恰好 1 条**：多一个 = 脚本替用户造了旧键的值。"""
    assert set(migrator.NEW_KEY_DEFAULTS) == NEW_KEYS
    assert set(settings.KEY_MAP) - set(migrator.NEW_KEY_DEFAULTS) == (
        set(settings.KEY_MAP) - NEW_KEYS
    )
    assert len(set(settings.KEY_MAP) - NEW_KEYS) == 21, "旧 21 键必须原样留着"


def test_new_key_default_matches_settings_and_schema_verbatim() -> None:
    """三处逐字一致：迁移器 ⇄ `settings.DEFAULT_BACKGROUND_MODE` ⇄ schema 的 `default`。

    脚本是独立文件（不 import 插件模块）⇒ 这是**手工对偶**。漂了就会出现"WebUI 上看到
    的默认值" ≠ "运行期真的会用的值"。
    """
    leaf = _schema_leaves()["background.mode"]
    assert migrator.NEW_KEY_DEFAULTS["background.mode"] == settings.DEFAULT_BACKGROUND_MODE
    assert leaf["default"] == settings.DEFAULT_BACKGROUND_MODE
    assert settings.Settings().background_mode == settings.DEFAULT_BACKGROUND_MODE
    assert settings.DEFAULT_BACKGROUND_MODE == settings.MODE_PLUGIN == "plugin"
    assert tuple(leaf["options"]) == settings.BACKGROUND_MODES
    assert len(leaf["labels"]) == len(leaf["options"])


def test_v03_migrator_is_the_frozen_twenty_one_key_snapshot(monkeypatch=None) -> None:
    """v0.3 脚本**冻结**：仍 21 键、仍是 v0.4.2 的 21 键前缀、`NEW_KEY_DEFAULTS` 仍 10 条。

    为什么钉它：v0.3 脚本是 v0.2→v0.3 的**回滚锚点**（`VERIFY-v0.3.md` 与旧派单都引用过
    它的 21 行报告）；改它会让那些证据变成假证据。
    """
    keys = [key for key, _type in migrator_v03.KEY_ORDER]
    assert len(keys) == 21
    assert keys == [key for key, _type in migrator.KEY_ORDER][:21]
    assert "background.mode" not in keys
    assert set(migrator_v03.NEW_KEY_DEFAULTS) == {
        "provider_settings.video_provider_id",
        "video.probe_enabled",
        "video.probe_ttl_seconds",
        "video.max_direct_bytes",
        "video.native_timeout_seconds",
        "video.native_max_tokens",
        "video.compress_enabled",
        "video.compress_target_bytes",
        "video.compress_timeout_seconds",
        "prompts.video_native",
    }, "v0.3 的 10 键增量不许被 v0.4.2 顺手改掉"


# --------------------------------------------------------------------------- #
# 2. 默认干跑：不写盘、不建目录、不产生备份
# --------------------------------------------------------------------------- #
def test_dry_run_is_the_default_and_touches_nothing(tmp_path: Path, capsys) -> None:
    root = _write_root(tmp_path)
    before = _config_path(root).read_bytes()

    code, out, _err = _run(capsys, "--root", str(root))

    assert code == 0
    assert _config_path(root).read_bytes() == before, "干跑改了文件！"
    assert _backups(root) == [], "干跑产生了备份"
    assert "summary: changed=1" in out
    assert "干跑不写盘" in out, "干跑必须明确提示没有落盘"


def test_dry_run_reports_all_twenty_two_keys_with_type_and_length(
    tmp_path: Path, capsys
) -> None:
    """逐键报告：22 行，每条都有 `action` / `type` / `len`；只有 1 条 `add`。"""
    root = _write_root(tmp_path)
    _code, out, _err = _run(capsys, "--root", str(root))

    lines = [line for line in out.splitlines() if "action=" in line]
    assert len(lines) == 22, out
    for line in lines:
        assert " type=" in line and " len=" in line, line
    added = [line for line in lines if "action=add" in line]
    assert added == ["background.mode action=add type=str len=6"], added
    assert len([line for line in lines if "action=keep" in line]) == _PRESENT_KEYS
    assert len([line for line in lines if "action=skip" in line]) == 22 - _PRESENT_KEYS - 1


# --------------------------------------------------------------------------- #
# 3. --apply：只补那一个新键 + 保留权限位 + 0600 备份 + 原子写
# --------------------------------------------------------------------------- #
def test_apply_adds_only_the_new_key_and_keeps_existing_values(
    tmp_path: Path, capsys
) -> None:
    root = _write_root(tmp_path)
    code, out, _err = _run(capsys, "--root", str(root), "--apply")

    assert code == 0, out
    written = json.loads(_config_path(root).read_text(encoding="utf-8"))
    assert written["background"] == {"mode": "plugin"}, "新组只该有这一个键"
    # 用户已有值一个都没动（连"看起来像默认值"的 max_frames=6 也不动）
    assert written["provider_settings"]["vision_provider_id"] == "vision-1"
    assert written["provider_settings"]["stt_provider_id"] == "old_stt_id", "旧键不许被删/清空"
    assert written["prompts"]["audio"] == "自定义转写提示词{focus_clause}"
    assert written["limits"] == {"max_media_seconds": 120, "max_frames": 6}
    assert written["cache"] == {"ttl_seconds": 0}
    assert "video" not in written, "脚本不替用户造旧键的值（核心会在下次加载补默认）"


def test_apply_preserves_the_existing_file_mode(tmp_path: Path, capsys) -> None:
    """`--apply` 不许放宽配置权限（生产是 0600；`os.replace` 不复制 mode 会掉成 0644）。"""
    root = _write_root(tmp_path)
    config_path = _config_path(root)
    os.chmod(config_path, 0o600)

    _run(capsys, "--root", str(root), "--apply")

    mode = stat.S_IMODE(config_path.stat().st_mode)
    assert mode == 0o600, f"--apply 把配置权限改成了 {oct(mode)}"


@pytest.mark.parametrize("mode", [0o640, 0o444])
def test_apply_copies_the_exact_original_mode(tmp_path: Path, capsys, mode: int) -> None:
    """证明是"复制原 mode"而不是"写死 0600"（两个非 600 的值各验一次）。"""
    root = _write_root(tmp_path)
    os.chmod(_config_path(root), mode)
    _run(capsys, "--root", str(root), "--apply")
    assert stat.S_IMODE(_config_path(root).stat().st_mode) == mode


def test_apply_backup_is_byte_exact_and_0600(tmp_path: Path, capsys) -> None:
    root = _write_root(tmp_path)
    original = _config_path(root).read_bytes()

    _run(capsys, "--root", str(root), "--apply")

    backups = _backups(root)
    assert len(backups) == 1, backups
    assert backups[0].read_bytes() == original, "备份必须逐字节等于原文件"
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600


def test_apply_is_atomic_and_leaves_no_temp_files(tmp_path: Path, capsys) -> None:
    root = _write_root(tmp_path)
    _run(capsys, "--root", str(root), "--apply")
    strays = [
        path.name
        for path in _config_path(root).parent.iterdir()
        if path.name.startswith(".tmp-")
    ]
    assert strays == [], f"原子写的临时文件没清：{strays}"


def test_second_apply_is_idempotent(tmp_path: Path, capsys) -> None:
    root = _write_root(tmp_path)
    _first_code, first_out, _ = _run(capsys, "--root", str(root), "--apply")
    assert "changed=1" in first_out
    after_first = _config_path(root).read_bytes()

    second_code, second_out, _ = _run(capsys, "--root", str(root), "--apply")

    assert second_code == 0
    assert "changed=0" in second_out, second_out
    assert _config_path(root).read_bytes() == after_first, "幂等运行改了文件"
    assert len(_backups(root)) == 1, "幂等运行又造了备份"


# --------------------------------------------------------------------------- #
# 4. --report：22 行逐键 + 不进正文
# --------------------------------------------------------------------------- #
def test_report_lists_twenty_two_keys_and_never_prints_values(
    tmp_path: Path, capsys
) -> None:
    marker = "这是一段不该出现在日志里的自定义提示词"
    config = json.loads(json.dumps(_V041_CONFIG))
    config["prompts"]["audio"] = marker + "{focus_clause}"
    root = _write_root(tmp_path, config=config)

    code, out, _err = _run(capsys, "--root", str(root), "--report")

    assert code == 0
    assert "summary:" in out and "keys=22" in out
    assert "background.mode present=false" in out
    assert marker not in out, "stdout 里出现了提示词正文"
    assert "old_stt_id" not in out, "stdout 里出现了 provider id 字面量"
    assert "vision-1" not in out
    assert len([line for line in out.splitlines() if "present=" in line]) == 22, out


# --------------------------------------------------------------------------- #
# 5. --rollback：逐字节重建 + 成对回滚提醒
# --------------------------------------------------------------------------- #
def test_rollback_restores_bytes_from_backup(tmp_path: Path, capsys) -> None:
    root = _write_root(tmp_path)
    original = _config_path(root).read_bytes()
    _run(capsys, "--root", str(root), "--apply")
    assert _config_path(root).read_bytes() != original

    code, out, _err = _run(capsys, "--rollback", str(_backups(root)[0]))

    assert code == 0, out
    assert _config_path(root).read_bytes() == original
    assert "成对" in out, "必须提醒「配置回滚与代码回滚成对」"


def test_rollback_rejects_missing_and_unparsable_backups(tmp_path: Path, capsys) -> None:
    code, _out, err = _run(
        capsys, "--rollback", str(tmp_path / "nope.json.bak-20260101T000000Z")
    )
    assert code == 1 and "不存在" in err

    target = tmp_path / "cfg.json"
    target.write_text("{}", encoding="utf-8")
    backup = tmp_path / "cfg.json.bak-20260101T000000Z"
    backup.write_text("not json", encoding="utf-8")
    code, _out, err = _run(capsys, "--rollback", str(backup))
    assert code == 1 and "不可解析" in err
    assert target.read_text(encoding="utf-8") == "{}", "目标文件不该被动"


# --------------------------------------------------------------------------- #
# 6. 失败面 + 顺序纪律
# --------------------------------------------------------------------------- #
def test_missing_config_is_an_error(tmp_path: Path, capsys) -> None:
    root = tmp_path / "empty_root"
    root.mkdir()
    code, _out, err = _run(capsys, "--root", str(root))
    assert code == 1
    assert "不存在" in err


def test_broken_json_is_an_error_without_touching_the_file(tmp_path: Path, capsys) -> None:
    root = _write_root(tmp_path, config="{ this is not json")
    before = _config_path(root).read_bytes()
    code, _out, err = _run(capsys, "--root", str(root), "--apply")
    assert code == 1
    assert "读不出来" in err
    assert _config_path(root).read_bytes() == before
    assert _backups(root) == []


def test_bom_prefixed_config_is_readable(tmp_path: Path, capsys) -> None:
    """生产配置带 BOM（核心读 `utf-8-sig`）⇒ 迁移器必须同口径，否则第一次跑就报错。"""
    root = _write_root(tmp_path)
    payload = b"\xef\xbb\xbf" + json.dumps(_V041_CONFIG, ensure_ascii=False).encode("utf-8")
    _config_path(root).write_bytes(payload)

    code, out, _err = _run(capsys, "--root", str(root), "--apply")

    assert code == 0, out
    written = json.loads(_config_path(root).read_text(encoding="utf-8-sig"))
    assert written["background"]["mode"] == "plugin"


def test_bom_config_apply_keeps_every_value_and_rollback_restores_the_bytes(
    tmp_path: Path, capsys
) -> None:
    """BOM 配置的**值**保证：干跑不写、`--apply` 只多一个键、`--rollback` 逐字节还原。

    BOM 本身不是契约的一部分（核心读 `utf-8-sig`；v0.2 遗留 F3 登记过"`--apply` 会丢
    BOM"）。真正必须守住的是**值**：用户已有的 21 个键一个都不许被改写 —— 这里用
    "删掉新键后与原 dict 全等"来钉（强于抽查几个键），再用备份的逐字节比对把回滚锚点
    钉住（备份里连 BOM 都原样保留）。
    """
    original = b"\xef\xbb\xbf" + json.dumps(
        _V041_CONFIG, ensure_ascii=False, indent=2
    ).encode("utf-8")
    root = tmp_path / "astrbot_root"
    path = root / _CFG_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(original)
    before = json.loads(original.decode("utf-8-sig"))

    code, out, _err = _run(capsys, "--root", str(root))  # 干跑
    assert code == 0 and "changed=1" in out
    assert path.read_bytes() == original, "干跑必须一个字节都不写"

    code, out, _err = _run(capsys, "--root", str(root), "--apply")
    assert code == 0 and "changed=1" in out
    assert "background" not in before, "这份 v0.4.1 形态的夹具本来就没有新组"
    after = json.loads(path.read_text(encoding="utf-8-sig"))
    assert after["background"] == {"mode": "plugin"}, "新键必须补上默认值"
    after.pop("background")
    assert after == before, "既有 21 键的值被改写了"

    backups = _backups(root)
    assert len(backups) == 1, backups
    assert backups[0].read_bytes() == original, "备份必须是逐字节原文（含 BOM）"

    code, out, _err = _run(capsys, "--rollback", str(backups[0]))
    assert code == 0, out
    assert path.read_bytes() == original, "回滚必须逐字节还原（含 BOM）"


def test_missing_root_and_env_is_an_error(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("ASTRBOT_ROOT", raising=False)
    code, _out, err = _run(capsys)
    assert code == 1
    assert "缺少 --root" in err


def test_root_falls_back_to_astrbot_root_env(tmp_path: Path, monkeypatch, capsys) -> None:
    root = _write_root(tmp_path)
    monkeypatch.setenv("ASTRBOT_ROOT", str(root))
    code, out, _err = _run(capsys)
    assert code == 0, out
    assert "background.mode action=add" in out


def test_plan_does_not_mutate_the_input_config() -> None:
    config = json.loads(json.dumps(_V041_CONFIG))
    snapshot = json.loads(json.dumps(config))
    migrator.plan(config)
    assert config == snapshot


def test_script_never_imports_the_plugin_and_never_deletes_keys() -> None:
    """两条静态纪律：不 import 插件模块（运维侧独立）/ 不出现删键动作（核心会当 stale 落盘）。"""
    source = _SCRIPT.read_text(encoding="utf-8")
    assert "astrbot_plugin_media_analysis" not in source.replace(
        "astrbot_plugin_media_analysis_config.json", ""
    ), "迁移器 import 了插件模块（它必须能在插件树之外独立跑）"
    assert "del config[" not in source and ".pop(" not in source, "迁移器出现了删键动作"


def test_docs_state_the_order_rule() -> None:
    """顺序纪律必须写在脚本自己的文档里（超集 ⇒ **代码先行**，反序会被旧 schema 删键）。"""
    source = _SCRIPT.read_text(encoding="utf-8")
    assert "代码先行" in source
    assert "超集" in source
    assert "background.mode" in source
