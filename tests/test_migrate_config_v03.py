"""v0.3 配置迁移器 `tools/migrate_config_v03.py`（SPEC-v0.3 §8.2 / §14.2 契约）。

v0.2 → v0.3 与 v0.1 → v0.2 的**性质不同**，测试面也不同：

- v0.2 的脚本要**搬值**（`stt_provider_id` → `audio_provider_id`，带能力校验、可拒绝）；
- v0.3 的脚本**不搬任何值**：新 schema 是旧 schema 的**超集**，核心 `AstrBotConfig`
  构造期就会补默认值。脚本的职责只有两条：① 把 10 个 v0.3 新键**显式落盘**（审计 +
  回滚锚点）；② `--report` 逐键报告（21 行）。**旧键一个都不删**。

所以这里断言的是 v0.3 自己的承诺：

1. `KEY_ORDER` 是 21 键、顺序 == schema 书写顺序；**v0.4.2 起它是 22 键表的 21 键前缀**
   （本脚本冻结为历史快照，当前迁移器是 `tools/migrate_config_v042.py`）；
2. `NEW_KEY_DEFAULTS` 恰好是那 10 个新键，且默认值与 `settings.py` / `_config_schema.json`
   **逐字**相同（脚本不 import 插件模块 ⇒ 这份对偶只能靠测试对齐）；
3. 默认干跑（不写盘、不建目录、不产生备份）；
4. `--apply` 只**补缺失键**、保留原权限位（600→600）、备份 0600 逐字节、原子写；
5. 幂等（第二次 `changed=0`，不再产生备份）；
6. `--rollback` 逐字节重建并提醒成对回滚；
7. 任何模式都**不打印配置值**（只打键名/动作/类型/长度）。
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
_SCRIPT = _PLUGIN_DIR / "tools" / "migrate_config_v03.py"
_SCHEMA = _PLUGIN_DIR / "_conf_schema.json"

_CFG_REL = Path("data/config/astrbot_plugin_media_analysis_config.json")

#: 一份 v0.2 形态的用户配置（11 键里的 6 个 + 一个旧 STT 值），用来验证"只补不改"
_V02_CONFIG: dict = {
    "provider_settings": {
        "stt_provider_id": "old_stt_id",
        "vision_provider_id": "vision-1",
    },
    "prompts": {"audio": "自定义转写提示词{focus_clause}"},
    "limits": {"max_media_seconds": 120, "max_frames": 6},
    "cache": {"ttl_seconds": 0},
}

#: v0.3 的 10 个新键（其余 11 个是 v0.2 的，逐字不动）
NEW_KEYS = {
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
}


def _load_migrator():
    spec = importlib.util.spec_from_file_location(
        "migrate_config_v03_under_test", _SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


migrator = _load_migrator()


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


def _write_root(tmp_path: Path, *, config: object = _V02_CONFIG) -> Path:
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
def test_key_order_is_the_twenty_one_keys_in_schema_order() -> None:
    """21 键、**是 v0.4.2 键表的 21 键前缀**（SPEC-v0.4.2 §3.2）。

    **v0.4.2 起本脚本冻结**（v0.2→v0.3 的历史快照）：当前键表是 22 键、第四处换成了
    `tools/migrate_config_v042.py`，所以这里的判据从"全等"降级为"**前缀**"——集合 ==
    全集 − `{"background.mode"}`、顺序 == 前 21 位。这几个字是 v0.4.2 唯一改动本文件的地方。
    """
    keys = [key for key, _type in migrator.KEY_ORDER]
    current = list(settings.KEY_MAP)
    assert len(keys) == 21
    assert set(keys) == set(settings.KEY_MAP) - {"background.mode"}
    assert keys == list(_schema_leaves())[:21]
    assert keys == current[:21], "v0.3 必须是 v0.4.2 的**21 键前缀**"
    assert current[21:] == ["background.mode"], "新键只许追加在末尾"


def test_key_order_type_labels_match_the_schema_types() -> None:
    """展示用类型（`str/bool/int/text`）与 schema 的 `type`（`string/bool/int/text`）同义。

    迁移器刻意用短标签（它不 import 插件模块、也不读 schema），所以这里做一次**映射**后
    比对 —— 目的不是逼两边字面相同，而是防"标成 str 的键其实是 bool"这种会误导运维的漂移。
    """
    leaves = _schema_leaves()
    same = {"str": "string", "string": "string", "int": "int", "bool": "bool", "text": "text"}
    for key, type_name in migrator.KEY_ORDER:
        assert same[type_name] == leaves[key]["type"], f"{key} 的类型标签与 schema 不一致"


def test_new_key_defaults_are_exactly_the_ten_new_keys() -> None:
    """`NEW_KEY_DEFAULTS` 不能多也不能少：多一个 = 脚本替用户造了旧键的值。"""
    assert set(migrator.NEW_KEY_DEFAULTS) == NEW_KEYS
    # v0.4.2：全集是 22 键 ⇒ 右边多一个 `background.mode`（12 = 11 个 v0.2 键 + 1 个 v0.4.2 键）
    assert set(settings.KEY_MAP) - NEW_KEYS == {
        "provider_settings.audio_provider_id",
        "provider_settings.vision_provider_id",
        "provider_settings.stt_provider_id",
        "prompts.audio",
        "prompts.video",
        "prompts.frame",
        "prompts.summary",
        "limits.max_media_seconds",
        "limits.max_frames",
        "limits.max_media_bytes",
        "cache.ttl_seconds",
        "background.mode",
    }, "v0.2 的 11 键 + v0.4.2 的 1 键集合漂了"


def test_new_key_defaults_match_settings_and_schema_verbatim() -> None:
    """三处默认值逐字一致：迁移器 ⇄ `settings.py` ⇄ `_conf_schema.json`。

    脚本是独立文件（不 import 插件模块），所以它是**手工对偶**。三处只要有一处漂了，
    "跑完脚本以后 WebUI 上看到的默认值"就不等于"运行期真的会用的值"。
    """
    leaves = _schema_leaves()
    expected = {
        "provider_settings.video_provider_id": settings.DEFAULT_PROMPTS and "",
        "video.probe_enabled": settings.DEFAULT_VIDEO_PROBE_ENABLED,
        "video.probe_ttl_seconds": settings.DEFAULT_VIDEO_PROBE_TTL_SECONDS,
        "video.max_direct_bytes": settings.DEFAULT_VIDEO_MAX_DIRECT_BYTES,
        "video.native_timeout_seconds": settings.DEFAULT_VIDEO_NATIVE_TIMEOUT_SECONDS,
        "video.native_max_tokens": settings.DEFAULT_VIDEO_NATIVE_MAX_TOKENS,
        "video.compress_enabled": settings.DEFAULT_VIDEO_COMPRESS_ENABLED,
        "video.compress_target_bytes": settings.DEFAULT_VIDEO_COMPRESS_TARGET_BYTES,
        "video.compress_timeout_seconds": settings.DEFAULT_VIDEO_COMPRESS_TIMEOUT_SECONDS,
        "prompts.video_native": settings.DEFAULT_PROMPTS["video_native"],
    }
    assert migrator.NEW_KEY_DEFAULTS == expected
    for key, value in migrator.NEW_KEY_DEFAULTS.items():
        assert leaves[key]["default"] == value, f"{key} 的 schema 默认值与迁移器漂了"


def test_new_key_defaults_equal_settings_dataclass_defaults() -> None:
    """再对齐一次 `Settings()`：字段默认值 == 迁移器写的值（WebUI 与实际行为同一份）。"""
    snapshot = settings.Settings()
    assert migrator.NEW_KEY_DEFAULTS["provider_settings.video_provider_id"] == snapshot.video_provider_id
    assert migrator.NEW_KEY_DEFAULTS["video.probe_enabled"] == snapshot.video_probe_enabled
    assert migrator.NEW_KEY_DEFAULTS["video.probe_ttl_seconds"] == snapshot.video_probe_ttl_seconds
    assert migrator.NEW_KEY_DEFAULTS["video.max_direct_bytes"] == snapshot.video_max_direct_bytes
    assert (
        migrator.NEW_KEY_DEFAULTS["video.native_timeout_seconds"]
        == snapshot.video_native_timeout_seconds
    )
    assert (
        migrator.NEW_KEY_DEFAULTS["video.native_max_tokens"]
        == snapshot.video_native_max_tokens
    )
    assert migrator.NEW_KEY_DEFAULTS["video.compress_enabled"] == snapshot.video_compress_enabled
    assert (
        migrator.NEW_KEY_DEFAULTS["video.compress_target_bytes"]
        == snapshot.video_compress_target_bytes
    )
    assert (
        migrator.NEW_KEY_DEFAULTS["video.compress_timeout_seconds"]
        == snapshot.video_compress_timeout_seconds
    )
    assert migrator.NEW_KEY_DEFAULTS["prompts.video_native"] == settings.DEFAULT_PROMPTS["video_native"]


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


def test_dry_run_reports_all_twenty_one_keys_with_type_and_length(
    tmp_path: Path, capsys
) -> None:
    """逐键报告：21 行，每条都有 `action` / `type` / `len`（`len` 是长度不是内容）。"""
    root = _write_root(tmp_path)
    _code, out, _err = _run(capsys, "--root", str(root))

    lines = [line for line in out.splitlines() if "action=" in line]
    assert len(lines) == 21, out
    for line in lines:
        assert " type=" in line and " len=" in line, line
    added = [line for line in lines if "action=add" in line]
    assert len(added) == 10, "v0.3 的 10 个新键都该是 add"
    kept = [line for line in lines if "action=keep" in line]
    assert len(kept) == 6, "配置里已有的键都该是 keep"
    # v0.2 有、配置里没有的键：skip（核心补默认，脚本不替用户造值）
    skipped = [line for line in lines if "action=skip" in line]
    assert len(skipped) == 5, (skipped, lines)


# --------------------------------------------------------------------------- #
# 3. --apply：只补缺失键 + 保留权限位 + 0600 备份 + 原子写
# --------------------------------------------------------------------------- #
def test_apply_adds_new_keys_and_keeps_existing_values(tmp_path: Path, capsys) -> None:
    root = _write_root(tmp_path)
    code, out, _err = _run(capsys, "--root", str(root), "--apply")

    assert code == 0, out
    written = json.loads(_config_path(root).read_text(encoding="utf-8"))
    # 新键逐个到位（含新分组 `video`）
    assert written["provider_settings"]["video_provider_id"] == ""
    assert written["video"] == {
        "probe_enabled": True,
        "probe_ttl_seconds": 604800,
        "max_direct_bytes": 8388608,
        "native_timeout_seconds": 60,
        "native_max_tokens": 2048,  # 评审 F5：默认 1024 → 2048
        "compress_enabled": True,
        "compress_target_bytes": 8388608,
        "compress_timeout_seconds": 30,
    }
    assert written["prompts"]["video_native"] == settings.DEFAULT_PROMPTS["video_native"]
    # 用户已有值一个都没动（连"看起来像默认值"的 max_frames=6 也不动）
    assert written["provider_settings"]["vision_provider_id"] == "vision-1"
    assert written["provider_settings"]["stt_provider_id"] == "old_stt_id", "旧键不许被删/清空"
    assert written["prompts"]["audio"] == "自定义转写提示词{focus_clause}"
    assert written["limits"] == {"max_media_seconds": 120, "max_frames": 6}
    assert written["cache"] == {"ttl_seconds": 0}


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
# 4. --report：21 行逐键 + 不进正文
# --------------------------------------------------------------------------- #
def test_report_lists_twenty_one_keys_and_never_prints_values(
    tmp_path: Path, capsys
) -> None:
    marker = "这是一段不该出现在日志里的自定义提示词"
    config = json.loads(json.dumps(_V02_CONFIG))
    config["prompts"]["audio"] = marker + "{focus_clause}"
    root = _write_root(tmp_path, config=config)

    code, out, _err = _run(capsys, "--root", str(root), "--report")

    assert code == 0
    lines = [line for line in out.splitlines() if line.startswith("provider_settings.") or "." in line]
    assert "summary:" in out and "keys=21" in out
    assert "prompts.audio" in out
    assert marker not in out, "stdout 里出现了提示词正文"
    assert "vision-1" not in out or "vision_provider_id present=true" in out
    assert "old_stt_id" not in out, "stdout 里出现了 provider id 字面量"
    assert len([line for line in out.splitlines() if "present=" in line]) == 21, out


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
    code, _out, err = _run(capsys, "--rollback", str(tmp_path / "nope.json.bak-20260101T000000Z"))
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
    payload = b"\xef\xbb\xbf" + json.dumps(_V02_CONFIG, ensure_ascii=False).encode("utf-8")
    _config_path(root).write_bytes(payload)

    code, out, _err = _run(capsys, "--root", str(root), "--apply")

    assert code == 0, out
    written = json.loads(_config_path(root).read_text(encoding="utf-8-sig"))
    assert written["video"]["max_direct_bytes"] == 8388608


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
    assert "provider_settings.video_provider_id action=add" in out


def test_plan_does_not_mutate_the_input_config() -> None:
    config = json.loads(json.dumps(_V02_CONFIG))
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
