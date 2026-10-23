"""配置迁移器 `tools/migrate_config_v02.py`（SPEC-v0.2 §5.5 契约）。

v0.1 → v0.2 的 11 键迁移是**唯一**会碰生产配置的动作（执行者是 ops/fix，不是 dev），所以它
的每条承诺都必须有测试兜住：

1. **默认干跑**：不写盘、不建目录、不产生备份；
2. **落盘**：先 0600 逐字节备份、再同目录 `.tmp-*` + `os.replace` 原子写；
3. **搬值带能力校验**：旧值语义上是 **STTProvider id**，而 `audio_provider_id` 必须是能做
   `text_chat(audio_urls=…)` 的多模态 chat provider —— 不可搬时**原样留在旧键**并退出码 2；
4. **幂等**：第二次运行 `changed=0`，不再产生备份；
5. **回滚**：从备份逐字节重建，并提醒"配置回滚必须与代码回滚成对"；
6. **不打印配置值**：只打点号键 / 动作 / 类型 / 长度。

另外两条**跨模块**守卫放在这里，因为它们描述的都是"同一个键集合"：
- 脚本的 `KEY_ORDER` 必须覆盖 `settings.KEY_MAP` 的全部 11 个键（少一个就没报告）；
- 旧键 `stt_provider_id` 在两边都**保留**（删键 = 核心 `check_config_integrity()` 静默删值）。
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

from astrbot_plugin_media_analysis import settings

_TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
_SCRIPT = _TOOLS_DIR / "migrate_config_v02.py"


def _load_migrator():
    spec = importlib.util.spec_from_file_location("migrate_config_v02_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


migrator = _load_migrator()

_CFG_REL = Path("data/config/astrbot_plugin_media_analysis_config.json")

#: 一份 v0.1 形态的用户配置（4 键）+ 一个"看起来能用"的旧 STT 值
_V01_CONFIG: dict = {
    "provider_settings": {"stt_provider_id": "old_stt_id", "vision_provider_id": "vision-1"},
    "limits": {"max_media_seconds": 60, "max_frames": 6},
}


def _cmd_config(*providers: dict) -> dict:
    return {"provider": list(providers)}


_AUDIO_CAPABLE = {
    "id": "old_stt_id",
    "provider_type": None,
    "type": "openai_chat_completion",
    "modalities": ["text", "audio"],
}


def _write_root(tmp_path: Path, *, config: object = _V01_CONFIG, cmd_config: object = None) -> Path:
    root = tmp_path / "astrbot_root"
    config_path = root / _CFG_REL
    config_path.parent.mkdir(parents=True, exist_ok=True)
    text = config if isinstance(config, str) else json.dumps(config, ensure_ascii=False, indent=2)
    config_path.write_text(text, encoding="utf-8")
    if cmd_config is not None:
        (root / "data" / "cmd_config.json").write_text(
            json.dumps(cmd_config, ensure_ascii=False), encoding="utf-8"
        )
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
# 1. 干跑：默认什么都不改
# --------------------------------------------------------------------------- #
def test_dry_run_does_not_touch_the_file_or_create_backups(tmp_path: Path, capsys) -> None:
    root = _write_root(tmp_path, cmd_config=_cmd_config(_AUDIO_CAPABLE))
    before = _config_path(root).read_bytes()

    code, out, _err = _run(capsys, "--root", str(root))

    assert code == 0
    assert _config_path(root).read_bytes() == before, "干跑改了文件！"
    assert _backups(root) == [], "干跑产生了备份"
    # 旧配置里没有音频键 ⇒ 动作是 `add`（有键但为空才是 `move`，见下一条用例）
    assert "provider_settings.audio_provider_id action=add" in out, out
    assert "summary: changed=1" in out, out


def test_dry_run_reports_move_when_the_audio_key_already_exists(
    tmp_path: Path, capsys
) -> None:
    """`audio_provider_id` 已存在但为空 ⇒ 动作是 `move`（键在、值不在）。"""
    config = json.loads(json.dumps(_V01_CONFIG))
    config["provider_settings"]["audio_provider_id"] = ""
    root = _write_root(tmp_path, config=config, cmd_config=_cmd_config(_AUDIO_CAPABLE))

    _code, out, _err = _run(capsys, "--root", str(root))
    assert "provider_settings.audio_provider_id action=move" in out, out


def test_dry_run_reports_every_key_with_type_and_length(tmp_path: Path, capsys) -> None:
    """逐键报告：11 条、每条都有 `action` / `type` / `len`（`len` 是长度不是内容）。"""
    root = _write_root(tmp_path, cmd_config=_cmd_config(_AUDIO_CAPABLE))
    _code, out, _err = _run(capsys, "--root", str(root))

    lines = [line for line in out.splitlines() if "action=" in line]
    assert len(lines) >= 11, out
    for line in lines:
        assert " type=" in line and " len=" in line, line
    # 旧键那条必须带原因码之外的"想搬但搬不了"信号（这里能搬，所以不带 reason）
    legacy_line = next(line for line in lines if "stt_provider_id" in line)
    assert "action=keep" in legacy_line or "action=clear" in legacy_line, legacy_line


# --------------------------------------------------------------------------- #
# 2. 落盘：搬值 + 备份 0600 + 原子写
# --------------------------------------------------------------------------- #
def test_apply_moves_legacy_value_to_audio_and_keeps_the_legacy_key(
    tmp_path: Path, capsys
) -> None:
    """v0.2 的核心搬值：旧值搬到 `audio_provider_id`，**旧键不删**（只保留原值）。"""
    root = _write_root(tmp_path, cmd_config=_cmd_config(_AUDIO_CAPABLE))
    code, out, _err = _run(capsys, "--root", str(root), "--apply")

    assert code == 0, out
    written = json.loads(_config_path(root).read_text(encoding="utf-8"))
    assert written["provider_settings"]["audio_provider_id"] == "old_stt_id"
    assert written["provider_settings"]["stt_provider_id"] == "old_stt_id", "旧键不许被删/清空"
    assert written["provider_settings"]["vision_provider_id"] == "vision-1"
    assert written["limits"] == {"max_media_seconds": 60, "max_frames": 6}, "不该动与迁移无关的键"

    backups = _backups(root)
    assert len(backups) == 1, backups
    mode = stat.S_IMODE(backups[0].stat().st_mode)
    assert mode == 0o600, f"备份权限必须是 0600（含 provider id 与提示词），实际 {oct(mode)}"


def test_apply_backup_is_a_byte_exact_copy_of_the_original(tmp_path: Path, capsys) -> None:
    root = _write_root(tmp_path, cmd_config=_cmd_config(_AUDIO_CAPABLE))
    original = _config_path(root).read_bytes()
    _run(capsys, "--root", str(root), "--apply")
    assert _backups(root)[0].read_bytes() == original


def test_apply_is_atomic_and_leaves_no_temp_files(tmp_path: Path, capsys) -> None:
    root = _write_root(tmp_path, cmd_config=_cmd_config(_AUDIO_CAPABLE))
    _run(capsys, "--root", str(root), "--apply")
    strays = [p.name for p in _config_path(root).parent.iterdir() if p.name.startswith(".tmp-")]
    assert strays == [], f"原子写的临时文件没清：{strays}"


@pytest.mark.parametrize("mode", [0o600, 0o640, 0o444])
def test_apply_preserves_the_existing_file_mode(tmp_path: Path, capsys, mode: int) -> None:
    """t5-F2：`--apply` **不许**放宽配置权限（生产是 `cmd_config` 同款 0600）。

    `os.replace` 换的是 inode：不显式复制 mode，新文件的权限就回落到默认 0644
    —— 把 600 的配置（含 provider id 与提示词）降级成同机可读。这里用 600 / 640 / 444
    三个值验证是"复制原 mode"而不是"写死 0600"。
    """
    root = _write_root(tmp_path, cmd_config=_cmd_config(_AUDIO_CAPABLE))
    config_path = _config_path(root)
    os.chmod(config_path, mode)

    code, out, _err = _run(capsys, "--root", str(root), "--apply")

    assert code == 0, out
    assert "summary: changed=1" in out, out
    after = stat.S_IMODE(config_path.stat().st_mode)
    assert after == mode, f"--apply 改了配置权限：{oct(mode)} → {oct(after)}"


def test_atomic_write_defaults_to_0600_when_the_target_is_absent(tmp_path: Path) -> None:
    """t5-F2 的另一半：目标文件不存在（首次创建）⇒ 按 0600 落盘，而不是 0644。"""
    target = tmp_path / "brand_new_config.json"
    assert not target.exists()

    migrator._atomic_write(str(target), b'{"ok": true}\n')

    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600, f"新建配置必须是 0600，实际 {oct(mode)}"


def test_apply_clear_legacy_empties_but_never_deletes_the_key(tmp_path: Path, capsys) -> None:
    """`--clear-legacy` 只在"值确实已经在音频位上"时置空；键永远保留。"""
    root = _write_root(tmp_path, cmd_config=_cmd_config(_AUDIO_CAPABLE))
    code, out, _err = _run(capsys, "--root", str(root), "--apply", "--clear-legacy")

    assert code == 0, out
    written = json.loads(_config_path(root).read_text(encoding="utf-8"))
    assert written["provider_settings"]["audio_provider_id"] == "old_stt_id"
    assert "stt_provider_id" in written["provider_settings"], "旧键被删了（核心会当 stale 落盘）"
    assert written["provider_settings"]["stt_provider_id"] == ""


def test_apply_keeps_existing_new_keys_untouched(tmp_path: Path, capsys) -> None:
    """新键已经存在（用户手填过）⇒ `keep`，不许被默认值覆盖。"""
    config = json.loads(json.dumps(_V01_CONFIG))
    config["prompts"] = {"audio": "自定义转写提示词{focus_clause}"}
    config["cache"] = {"ttl_seconds": 0}
    root = _write_root(tmp_path, config=config, cmd_config=_cmd_config(_AUDIO_CAPABLE))

    _run(capsys, "--root", str(root), "--apply")
    written = json.loads(_config_path(root).read_text(encoding="utf-8"))
    assert written["prompts"]["audio"] == "自定义转写提示词{focus_clause}"
    assert written["cache"]["ttl_seconds"] == 0


def test_apply_does_not_invent_missing_new_keys(tmp_path: Path, capsys) -> None:
    """脚本**不造新键**：缺失的键由核心 `check_config_integrity()` 在下次加载时补默认。

    这条防的是"以为不跑脚本新键就不出现"以及反过来的"脚本写死了默认值"（两者都会让
    WebUI 显示值与真实行为脱节）。
    """
    root = _write_root(tmp_path, cmd_config=_cmd_config(_AUDIO_CAPABLE))
    _run(capsys, "--root", str(root), "--apply")
    written = json.loads(_config_path(root).read_text(encoding="utf-8"))
    assert "max_media_bytes" not in written["limits"]
    assert "prompts" not in written
    assert "cache" not in written


# --------------------------------------------------------------------------- #
# 3. 不可搬：退出码 2 + 原样留在旧键（三种原因码）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("provider", "reason"),
    [
        ({"id": "old_stt_id", "provider_type": "speech_to_text", "modalities": ["audio"]}, "not_chat"),
        ({"id": "old_stt_id", "type": "mimo_stt_api", "modalities": ["audio"]}, "not_chat"),
        ({"id": "old_stt_id", "provider_type": None, "modalities": []}, "no_audio"),
        ({"id": "old_stt_id", "provider_type": None}, "no_audio"),
        ({"id": "someone_else", "provider_type": None, "modalities": ["text", "audio"]}, "not_found"),
    ],
)
def test_apply_refuses_to_move_an_incompatible_provider(
    tmp_path: Path, capsys, provider: dict, reason: str
) -> None:
    """搬一个不具备音频能力的 id ⇒ 工具每次调用都会 `PROVIDER_CAPABILITY_MISMATCH`，
    而且看起来像"模型不支持音频"。所以宁可退出码 2 让人工选，也不搬。"""
    root = _write_root(tmp_path, cmd_config=_cmd_config(provider))
    code, out, _err = _run(capsys, "--root", str(root), "--apply")

    assert code == 2, out
    assert f"reason={reason}" in out, out
    assert "needs_attention=1" in out
    written = json.loads(_config_path(root).read_text(encoding="utf-8"))
    assert "audio_provider_id" not in written["provider_settings"], "不可搬却写进去了"
    assert written["provider_settings"]["stt_provider_id"] == "old_stt_id", "旧值必须原样留着"


def test_apply_without_cmd_config_cannot_verify_and_does_not_move(
    tmp_path: Path, capsys
) -> None:
    """读不到 `cmd_config.json` ⇒ 能力校验按 `not_found` 处理 ⇒ 不搬、退出码 2。"""
    root = _write_root(tmp_path)  # 不写 cmd_config
    code, out, _err = _run(capsys, "--root", str(root), "--apply")
    assert code == 2
    assert "reason=not_found" in out


def test_apply_with_empty_legacy_value_is_a_no_op(tmp_path: Path, capsys) -> None:
    config = json.loads(json.dumps(_V01_CONFIG))
    config["provider_settings"]["stt_provider_id"] = ""
    root = _write_root(tmp_path, config=config, cmd_config=_cmd_config(_AUDIO_CAPABLE))

    code, out, _err = _run(capsys, "--root", str(root), "--apply")
    assert code == 0
    assert "changed=0" in out
    assert _backups(root) == [], "无变化就不该产生备份"
    assert json.loads(_config_path(root).read_text(encoding="utf-8")) == config


# --------------------------------------------------------------------------- #
# 4. 幂等：第二次运行 changed=0、不再备份
# --------------------------------------------------------------------------- #
def test_second_apply_is_idempotent(tmp_path: Path, capsys) -> None:
    root = _write_root(tmp_path, cmd_config=_cmd_config(_AUDIO_CAPABLE))
    first_code, first_out, _ = _run(capsys, "--root", str(root), "--apply")
    assert first_code == 0 and "changed=1" in first_out
    after_first = _config_path(root).read_bytes()
    assert len(_backups(root)) == 1

    second_code, second_out, _ = _run(capsys, "--root", str(root), "--apply")
    assert second_code == 0
    assert "changed=0" in second_out, second_out
    assert _config_path(root).read_bytes() == after_first, "幂等运行改了文件"
    assert len(_backups(root)) == 1, "幂等运行又造了备份"


def test_second_apply_after_clear_legacy_is_idempotent(tmp_path: Path, capsys) -> None:
    """`--apply` 之后紧接着 `--apply --clear-legacy`：备份名撞同一秒要靠随机后缀避开。"""
    root = _write_root(tmp_path, cmd_config=_cmd_config(_AUDIO_CAPABLE))
    _run(capsys, "--root", str(root), "--apply")
    code, out, _err = _run(capsys, "--root", str(root), "--apply", "--clear-legacy")

    assert code == 0, out
    assert "changed=1" in out
    assert len(_backups(root)) == 2, "第二次迁移必须另存一份备份（不许覆盖）"
    written = json.loads(_config_path(root).read_text(encoding="utf-8"))
    assert written["provider_settings"]["stt_provider_id"] == ""


# --------------------------------------------------------------------------- #
# 5. 回滚：从备份逐字节重建
# --------------------------------------------------------------------------- #
def test_rollback_restores_bytes_from_backup(tmp_path: Path, capsys) -> None:
    root = _write_root(tmp_path, cmd_config=_cmd_config(_AUDIO_CAPABLE))
    original = _config_path(root).read_bytes()
    _run(capsys, "--root", str(root), "--apply")
    assert _config_path(root).read_bytes() != original

    backup = _backups(root)[0]
    code, out, _err = _run(capsys, "--rollback", str(backup))
    assert code == 0, out
    assert _config_path(root).read_bytes() == original
    assert "成对" in out, "必须提醒「配置回滚与代码回滚成对」"


def test_rollback_rejects_a_missing_backup(capsys, tmp_path: Path) -> None:
    code, _out, err = _run(capsys, "--rollback", str(tmp_path / "nope.json.bak-20260101T000000Z"))
    assert code == 1
    assert "不存在" in err


def test_rollback_rejects_an_unparsable_backup(tmp_path: Path, capsys) -> None:
    target = tmp_path / "cfg.json"
    target.write_text("{}", encoding="utf-8")
    backup = tmp_path / "cfg.json.bak-20260101T000000Z"
    backup.write_text("not json", encoding="utf-8")

    code, _out, err = _run(capsys, "--rollback", str(backup))
    assert code == 1
    assert "不可解析" in err
    assert target.read_text(encoding="utf-8") == "{}", "目标文件不该被动"


# --------------------------------------------------------------------------- #
# 6. 不打印配置值（provider id / 提示词都不许出现在 stdout）
# --------------------------------------------------------------------------- #
def test_report_legacy_prints_flags_only(tmp_path: Path, capsys) -> None:
    secret_ish = "old_stt_id"
    root = _write_root(tmp_path, cmd_config=_cmd_config(_AUDIO_CAPABLE))
    code, out, _err = _run(capsys, "--root", str(root), "--report-legacy")

    assert code == 0
    assert f"legacy_key={migrator.LEGACY_KEY}" in out
    assert "movable=True" in out
    assert secret_ish not in out, f"stdout 里出现了 provider id 字面量：{out}"


def test_apply_never_prints_prompt_text(tmp_path: Path, capsys) -> None:
    """提示词是用户配置内容（可能含私有措辞）⇒ 只打长度，绝不打值。"""
    marker = "这是一段不该出现在日志里的自定义提示词"
    config = json.loads(json.dumps(_V01_CONFIG))
    config["prompts"] = {"audio": marker + "{focus_clause}"}
    root = _write_root(tmp_path, config=config, cmd_config=_cmd_config(_AUDIO_CAPABLE))

    _code, out, _err = _run(capsys, "--root", str(root), "--apply")
    assert marker not in out
    assert "prompts.audio" in out and "len=" in out


# --------------------------------------------------------------------------- #
# 7. 失败面：缺文件 / 坏 JSON / 非对象 / 缺 root
# --------------------------------------------------------------------------- #
def test_missing_config_is_an_error(tmp_path: Path, capsys) -> None:
    root = tmp_path / "empty_root"
    root.mkdir()
    code, _out, err = _run(capsys, "--root", str(root))
    assert code == 1
    assert "不存在" in err


def test_broken_json_is_an_error_without_touching_the_file(tmp_path: Path, capsys) -> None:
    root = _write_root(tmp_path, config="{ this is not json", cmd_config=_cmd_config(_AUDIO_CAPABLE))
    before = _config_path(root).read_bytes()
    code, _out, err = _run(capsys, "--root", str(root), "--apply")
    assert code == 1
    assert "读不出来" in err
    assert _config_path(root).read_bytes() == before
    assert _backups(root) == []


def test_non_object_config_is_an_error(tmp_path: Path, capsys) -> None:
    root = _write_root(tmp_path, config="[1, 2, 3]")
    code, _out, err = _run(capsys, "--root", str(root), "--apply")
    assert code == 1
    assert "不是 JSON 对象" in err


def test_bom_prefixed_config_is_readable(tmp_path: Path, capsys) -> None:
    """`cmd_config.json` 带 BOM（实测 `json.load` 会抛）⇒ 必须用 `utf-8-sig` 读。"""
    root = _write_root(tmp_path, cmd_config=_cmd_config(_AUDIO_CAPABLE))
    payload = b"\xef\xbb\xbf" + json.dumps(_V01_CONFIG).encode("utf-8")
    _config_path(root).write_bytes(payload)

    code, out, _err = _run(capsys, "--root", str(root), "--apply")
    assert code == 0, out
    written = json.loads(_config_path(root).read_text(encoding="utf-8-sig"))
    assert written["provider_settings"]["audio_provider_id"] == "old_stt_id"


def test_missing_root_and_env_is_an_error(monkeypatch, capsys) -> None:
    monkeypatch.delenv("ASTRBOT_ROOT", raising=False)
    code, _out, err = _run(capsys)
    assert code == 1
    assert "缺少 --root" in err


def test_root_falls_back_to_astrbot_root_env(tmp_path: Path, monkeypatch, capsys) -> None:
    root = _write_root(tmp_path, cmd_config=_cmd_config(_AUDIO_CAPABLE))
    monkeypatch.setenv("ASTRBOT_ROOT", str(root))
    code, out, _err = _run(capsys)
    assert code == 0, out
    assert "provider_settings.audio_provider_id action=add" in out, out


# --------------------------------------------------------------------------- #
# 8. 纯函数 + 跨模块守卫
# --------------------------------------------------------------------------- #
def test_plan_does_not_mutate_the_input_config() -> None:
    config = json.loads(json.dumps(_V01_CONFIG))
    snapshot = json.loads(json.dumps(config))
    migrator.plan(config, _cmd_config(_AUDIO_CAPABLE))
    assert config == snapshot


def test_v02_key_order_is_the_frozen_eleven_keys() -> None:
    """v0.2 迁移器报告的 **11** 个键是**冻结**的（它承诺的是 v0.1→v0.2，不跟着新版本长）。

    v0.3 的 21 键由 `tools/migrate_config_v03.py:KEY_ORDER` 负责（守卫在
    `tests/test_migrate_config_v03.py` 与 `tests/test_config_map.py` 的四处一致里）。
    这里只需要两条：① 11 条逐字不动；② 它们**都是**当前 21 键的子集（v0.3 没删没改名）。
    """
    frozen_v02 = {
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
    }
    script_keys = {key for key, _type in migrator.KEY_ORDER}
    assert script_keys == frozen_v02, script_keys ^ frozen_v02
    assert script_keys <= set(settings.KEY_MAP), "v0.2 的键在 v0.3 里被删/改名了"


def test_legacy_key_is_present_and_never_removed() -> None:
    assert migrator.LEGACY_KEY == settings.KEY_LEGACY_STT_PROVIDER_ID
    assert migrator.LEGACY_KEY in set(settings.KEY_MAP)
    source = _SCRIPT.read_text(encoding="utf-8")
    # 迁移器只许"置空"，不许删键：源码里不该出现对被迁移字典的 delete 语句
    assert "del config[" not in source and "pop(" not in source, "迁移器出现了删键动作"


def test_audio_compatible_reason_codes() -> None:
    assert migrator.audio_compatible(None) == migrator.REASON_NOT_FOUND
    assert migrator.audio_compatible({"modalities": ["text", "audio"]}) == migrator.REASON_OK
    assert migrator.audio_compatible({"modalities": ["text"]}) == migrator.REASON_NO_AUDIO
    assert migrator.audio_compatible({"modalities": []}) == migrator.REASON_NO_AUDIO
    assert migrator.audio_compatible({"provider_type": "embedding"}) == migrator.REASON_NOT_CHAT
    assert migrator.audio_compatible({"type": "vllm_rerank"}) == migrator.REASON_NOT_CHAT
    assert (
        migrator.audio_compatible({"enable": False, "modalities": ["audio"]})
        == migrator.REASON_NOT_CHAT
    )


def test_needs_attention_only_for_an_unmovable_legacy_value() -> None:
    _new, actions = migrator.plan(
        _V01_CONFIG, _cmd_config({"id": "old_stt_id", "provider_type": "speech_to_text"})
    )
    assert migrator.needs_attention(actions) is True

    _new2, ok_actions = migrator.plan(_V01_CONFIG, _cmd_config(_AUDIO_CAPABLE))
    assert migrator.needs_attention(ok_actions) is False


def test_history_already_moved_value_is_not_moved_twice() -> None:
    """历史上已经搬过（audio 与 legacy 逐字相等）⇒ 不动、不重复写。"""
    config = json.loads(json.dumps(_V01_CONFIG))
    config["provider_settings"]["audio_provider_id"] = "old_stt_id"
    new_config, actions = migrator.plan(config, _cmd_config(_AUDIO_CAPABLE))

    assert new_config["provider_settings"]["audio_provider_id"] == "old_stt_id"
    assert migrator.changed(config, new_config) is False
    audio_action = next(item for item in actions if item.key == migrator.AUDIO_KEY)
    assert audio_action.action == "keep"
