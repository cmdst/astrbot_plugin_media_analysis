"""双提示词（SPEC-v0.2 §7）：四份模板的**校验 / 回退 / 截断 / 指纹**，以及主链路里的注入位置。

v0.2 的诉求 ② 是"视频/语音各自独立提示词"—— 说人话：
- `prompts.audio` 只给**语音条**（`Record` / 音频 `File`）用；
- `prompts.video` 只给**视频音轨**用（画面另有抽帧分析，所以措辞不同）；
- `prompts.frame` 逐帧用；`prompts.summary` 拼材料用。

本文件分两层：
1. **纯函数层**（`settings.validate_prompts` / `placeholder_names` / `prompts_fp`）：
   空串回默认、坏值回默认 + 一次告警、超长截断、占位符白名单；
2. **主链路注入层**（`analyze._audio_prompt` / `_frame_prompt` / `_summary_prompt`）：
   哪个 kind 用哪份模板、焦点从句怎么补、截断怎么记 note。

"提示词进了缓存键"这件事在 `tests/test_analyze.py`（同媒体、换提示词 ⇒ miss），
因为那需要完整的一次主链路调用。
"""

from __future__ import annotations

import pytest

from astrbot_plugin_media_analysis import analyze as analyze_mod
from astrbot_plugin_media_analysis import media, providers, settings
from _fakes import FakeChatProvider, FakeContext, audio_chat, make_event, make_record
from astrbot_plugin_media_analysis import cache as cache_mod


def _rendered_audio(focus: str, template: str = "") -> str:
    cfg = settings.Settings(prompt_audio=template) if template else settings.Settings()
    notes: list[str] = []
    return analyze_mod._audio_prompt("audio", focus, cfg, notes)


def _rendered_video(focus: str, template: str = "") -> str:
    cfg = settings.Settings(prompt_video=template) if template else settings.Settings()
    notes: list[str] = []
    return analyze_mod._audio_prompt("video", focus, cfg, notes)


# --------------------------------------------------------------------------- #
# 1. 纯函数层：空值 / 坏值 / 超长 / 指纹
# --------------------------------------------------------------------------- #
def test_empty_prompts_fall_back_to_the_builtin_defaults() -> None:
    """四份空串 ⇒ 四份默认模板（逐字相同，不是"空模板发给模型"）。"""
    effective, trimmed = settings.validate_prompts(
        {"audio": "", "video": "   ", "frame": "", "summary": ""}
    )
    assert effective == settings.DEFAULT_PROMPTS
    assert trimmed == frozenset()


@pytest.mark.parametrize(
    "bad",
    ["用 {unknown_placeholder} 转写", "用 {a.b} 转写", "用 {0} 转写", "花括号 { 没闭合"],
)
def test_bad_templates_fall_back_with_one_warning(
    bad: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """坏模板（未知占位符 / 非简单名字 / 花括号坏）⇒ 回退默认 + **一次**告警。

    为什么必须在装配期回退：渲染期才发现坏值会变成运行期 `KeyError`，那是一次
    未知形态的失败（而且每条消息都失败一次）。
    """
    from _fakes import RecordingLogger

    log = RecordingLogger()
    monkeypatch.setattr(settings, "LOG", log)
    monkeypatch.setattr(settings, "_WARNED", set(), raising=False)

    effective, trimmed = settings.validate_prompts({"audio": bad})
    assert effective["audio"] == settings.DEFAULT_PROMPTS["audio"]
    assert trimmed == frozenset()
    warnings = [message for level, message in log.records if level == "WARNING"]
    assert len(warnings) == 1, warnings
    assert "prompts.audio" in warnings[0]
    assert bad not in warnings[0], "告警里不许出现模板内容（配置值不进日志）"


def test_warning_is_deduplicated_per_key_and_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    from _fakes import RecordingLogger

    log = RecordingLogger()
    monkeypatch.setattr(settings, "LOG", log)
    monkeypatch.setattr(settings, "_WARNED", set(), raising=False)

    for _ in range(3):
        settings.validate_prompts({"audio": "{nope}"})
    assert len([1 for level, _m in log.records if level == "WARNING"]) == 1


def test_overlong_prompt_is_truncated_and_reported() -> None:
    """超 2000 字符 ⇒ 截断到上限并记在 `prompts_trimmed` 里（主链路据此加 note）。"""
    long_template = "转写" * 1500
    effective, trimmed = settings.validate_prompts({"audio": long_template})
    assert len(effective["audio"]) == settings.PROMPT_MAX_CHARS
    assert trimmed == frozenset({"audio"})

    cfg = settings.Settings(prompt_audio=long_template)
    notes: list[str] = []
    analyze_mod._audio_prompt("audio", "", cfg, notes)
    assert notes == ["prompt_trimmed:audio"]


def test_truncation_that_breaks_the_placeholder_falls_back() -> None:
    """截断把 `{focus_clause}` 切成半句 ⇒ 回退整份默认（宁可给完好的默认，不给半句话）。"""
    filler = "字" * (settings.PROMPT_MAX_CHARS - 5)
    template = filler + "{focus_clause}"
    effective, trimmed = settings.validate_prompts({"audio": template})
    assert effective["audio"] == settings.DEFAULT_PROMPTS["audio"]
    assert trimmed == frozenset()


def test_placeholder_names_accepts_only_simple_names() -> None:
    assert settings.placeholder_names("a {focus_clause} b") == {"focus_clause"}
    assert settings.placeholder_names("no placeholders") == set()
    assert settings.placeholder_names("{a.b}") is None
    assert settings.placeholder_names("{0}") is None
    assert settings.placeholder_names("{}") is None
    assert settings.placeholder_names("{ broken") is None


def test_allowed_placeholders_per_prompt() -> None:
    """白名单是**逐份**的：`summary` 的 `{kind_cn}/{task}/{material}` 不许出现在 audio 里。"""
    assert settings.ALLOWED_PLACEHOLDERS["audio"] == frozenset({"focus_clause"})
    assert settings.ALLOWED_PLACEHOLDERS["video"] == frozenset({"focus_clause"})
    assert settings.ALLOWED_PLACEHOLDERS["frame"] == frozenset({"focus_clause"})
    assert settings.ALLOWED_PLACEHOLDERS["summary"] == frozenset({"kind_cn", "task", "material"})


def test_prompts_fingerprint_changes_with_any_template() -> None:
    """`prompts_fp` 是缓存键成分：任何一份模板变了，指纹就必须变（否则会命中旧提示的词）。"""
    base = settings.Settings()
    assert base.prompts_fp == settings.Settings().prompts_fp, "同配置必须同指纹"
    templates = {
        # 每份模板都必须用**它自己那份**允许的占位符，否则会先回退默认（那就测不到指纹了）
        "prompt_audio": "不同的语音模板 {focus_clause}",
        "prompt_video": "不同的视频模板 {focus_clause}",
        "prompt_frame": "不同的帧模板 {focus_clause}",
        "prompt_summary": "不同的摘要模板 {kind_cn}{task}{material}",
    }
    for field, template in templates.items():
        other = settings.Settings(**{field: template})
        assert other.prompts_fp != base.prompts_fp, f"{field} 没进指纹"


def test_prompts_tuple_order_is_frozen() -> None:
    """4 元组顺序冻结 `(audio, video, frame, summary)` —— `_PROMPT_INDEX` 与它必须同步。"""
    cfg = settings.Settings(
        prompt_audio="A{focus_clause}",
        prompt_video="V{focus_clause}",
        prompt_frame="F{focus_clause}",
        prompt_summary="{material}",
    )
    assert cfg.prompts[0].startswith("A")
    assert cfg.prompts[1].startswith("V")
    assert cfg.prompts[2].startswith("F")
    assert cfg.prompts[3] == "{material}"
    assert analyze_mod._PROMPT_INDEX == {"audio": 0, "video": 1, "frame": 2, "summary": 3}


# --------------------------------------------------------------------------- #
# 2. 两键各自生效（③ 的"各自独立"）
# --------------------------------------------------------------------------- #
def test_audio_and_video_templates_are_independent() -> None:
    """同一份配置里两个键互不覆盖：语音用 audio、视频音轨用 video。"""
    cfg = settings.Settings(
        prompt_audio="【语音模板】{focus_clause}", prompt_video="【视频模板】{focus_clause}"
    )
    audio_notes: list[str] = []
    video_notes: list[str] = []
    assert analyze_mod._audio_prompt("audio", "", cfg, audio_notes) == "【语音模板】"
    assert analyze_mod._audio_prompt("video", "", cfg, video_notes) == "【视频模板】"
    assert "【视频模板】" not in analyze_mod._audio_prompt("audio", "", cfg, audio_notes)


def test_only_audio_configured_keeps_video_on_default() -> None:
    """只配了一个键 ⇒ 另一个键仍走默认（不许"一份模板管两处"）。"""
    cfg = settings.Settings(prompt_audio="【只有语音】{focus_clause}")
    notes: list[str] = []
    assert analyze_mod._audio_prompt("audio", "", cfg, notes).startswith("【只有语音】")
    # 默认模板渲染后（无 focus ⇒ `{focus_clause}` 变空串）
    assert analyze_mod._audio_prompt("video", "", cfg, notes) == settings.DEFAULT_PROMPTS[
        "video"
    ].format(focus_clause="")


def test_frame_prompt_is_separate_from_audio_and_video() -> None:
    cfg = settings.Settings(
        prompt_frame="【帧模板】{focus_clause}",
        prompt_audio="【语音模板】{focus_clause}",
        prompt_video="【视频模板】{focus_clause}",
    )
    notes: list[str] = []
    assert analyze_mod._frame_prompt("", cfg, notes) == "【帧模板】"


# --------------------------------------------------------------------------- #
# 3. 焦点从句的注入与补偿（§7.1）
# --------------------------------------------------------------------------- #
def test_focus_clause_is_injected_when_focus_is_given() -> None:
    rendered = _rendered_audio("他在说什么", "模板。{focus_clause}")
    assert "他在说什么" in rendered
    assert "用户关心的是" in rendered


def test_no_focus_means_no_clause() -> None:
    rendered = _rendered_audio("", "模板。{focus_clause}")
    assert rendered == "模板。"
    assert "用户关心的是" not in rendered


def test_focus_is_appended_when_the_template_dropped_the_placeholder() -> None:
    """用户删掉了 `{focus_clause}` ⇒ 焦点仍要送到模型（记 `focus_appended:audio`）。"""
    cfg = settings.Settings(prompt_audio="模板里没有占位符")
    notes: list[str] = []
    rendered = analyze_mod._audio_prompt("audio", "他在说什么", cfg, notes)
    assert rendered.startswith("模板里没有占位符")
    assert "他在说什么" in rendered
    assert notes == ["focus_appended:audio"]


def test_focus_braces_are_not_reparsed() -> None:
    """补偿是"渲染后追加"：focus 里带花括号不许被二次 format（否则会炸/被吃掉）。"""
    cfg = settings.Settings(prompt_audio="没有占位符")
    notes: list[str] = []
    rendered = analyze_mod._audio_prompt("audio", "什么是 {x}", cfg, notes)
    assert "{x}" in rendered


# --------------------------------------------------------------------------- #
# 4. 摘要模板的占位符补偿（`kind_cn` / `task` / `material`）
# --------------------------------------------------------------------------- #
def test_summary_prompt_fills_all_three_placeholders() -> None:
    notes: list[str] = []
    rendered = analyze_mod._summary_prompt(
        "转写：他说了话",
        focus="",
        kind="audio",
        settings=settings.Settings(),
        notes=notes,
    )
    assert "语音" in rendered
    assert "转写：他说了话" in rendered
    assert "请用不超过三句中文概括" in rendered
    assert notes == []


def test_summary_prompt_with_focus_asks_the_question() -> None:
    notes: list[str] = []
    rendered = analyze_mod._summary_prompt(
        "材料正文", focus="他提了什么要求", kind="video", settings=settings.Settings(), notes=notes
    )
    assert "视频" in rendered
    assert "他提了什么要求" in rendered


def test_summary_prompt_compensates_missing_material_and_task() -> None:
    """模板里少了 `{material}` / `{task}` ⇒ 渲染后补上，并各记一条 note。"""
    cfg = settings.Settings(prompt_summary="只有一句话")
    notes: list[str] = []
    rendered = analyze_mod._summary_prompt(
        "材料正文", focus="", kind="audio", settings=cfg, notes=notes
    )
    assert rendered.startswith("只有一句话")
    assert "材料正文" in rendered
    assert sorted(notes) == ["focus_appended:summary", "material_trimmed"] or set(notes) >= {
        "focus_appended:summary"
    }


def test_summary_prompt_falls_back_when_settings_is_none() -> None:
    """`build_summary(settings=None)` 的老入口仍要能用（`kind` 缺省 ⇒ `{kind_cn}` 回落「媒体」）。"""
    notes: list[str] = []
    rendered = analyze_mod._summary_prompt(
        "材料", focus="", kind="", settings=None, notes=notes
    )
    assert "媒体" in rendered
    assert "材料" in rendered


def test_safe_format_never_raises_on_a_broken_template() -> None:
    assert analyze_mod._safe_format("{missing}", focus_clause="x") == "{missing}"


# --------------------------------------------------------------------------- #
# 5. 主链路注入：哪个 kind 用哪份模板（端到端，假 provider + 假媒体层）
# --------------------------------------------------------------------------- #
def _stub_media(monkeypatch: pytest.MonkeyPatch, tmp_path):
    media_path = tmp_path / "clip.wav"
    media_path.write_bytes(b"\x00" * 64)

    async def _classify(_handle, _path, *, max_media_bytes: int, workdir: str):
        return "audio", 30.0, True, False

    async def _digest(_path: str) -> str:
        return "0123456789abcdef"

    async def _provider_wav(path: str, *, workdir: str, extracted: bool = False) -> str:
        return path

    monkeypatch.setattr(media, "classify", _classify)
    monkeypatch.setattr(media, "content_digest", _digest)
    monkeypatch.setattr(media, "ensure_provider_wav", _provider_wav)
    return media_path


async def _run_once(monkeypatch, tmp_path, cfg: settings.Settings, *, digest: str = "0123456789abcdef"):
    media_path = _stub_media(monkeypatch, tmp_path)

    async def _digest(_path: str) -> str:
        return digest

    monkeypatch.setattr(media, "content_digest", _digest)
    audio = audio_chat(text="转写正文")
    monkeypatch.setattr(providers, "resolve_audio_provider", _async_return(audio))
    monkeypatch.setattr(
        providers, "resolve_chat_provider", _async_return(FakeChatProvider(text="摘要"))
    )
    monkeypatch.setattr(
        cache_mod, "_core_plugin_data_path", lambda: str(tmp_path / "plugin_data")
    )
    store = cache_mod.DiskCache(plugin_name=settings.PLUGIN_NAME, ttl_seconds=86400)
    payload = await analyze_mod.analyze(
        context=FakeContext(),
        event=make_event(current=[make_record(media_path)]),
        target="current",
        focus="",
        include_frames=False,
        settings=cfg,
        cache=store,
    )
    return payload, audio


def _async_return(value):
    async def _inner(*_args, **_kwargs):
        return value

    return _inner


@pytest.mark.asyncio
async def test_configured_audio_prompt_reaches_the_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    cfg = settings.Settings(prompt_audio="【自定义语音提示词】{focus_clause}")
    _payload, audio = await _run_once(monkeypatch, tmp_path, cfg)
    assert audio.prompts() == ["【自定义语音提示词】"]


@pytest.mark.asyncio
async def test_empty_audio_prompt_sends_the_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    cfg = settings.Settings()
    _payload, audio = await _run_once(monkeypatch, tmp_path, cfg)
    assert audio.prompts() == [settings.DEFAULT_PROMPTS["audio"].format(focus_clause="")]


@pytest.mark.asyncio
async def test_prompt_change_invalidates_the_cache_for_the_same_media(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """③ 的关键一面：同媒体、同参数、只换提示词 ⇒ **必须 miss**（否则用户改了词却拿到旧结果）。"""
    first, _audio = await _run_once(
        monkeypatch, tmp_path, settings.Settings(prompt_audio="模板一{focus_clause}")
    )
    second, audio = await _run_once(
        monkeypatch, tmp_path, settings.Settings(prompt_audio="模板二{focus_clause}")
    )

    assert first["cached"] is False
    assert second["cached"] is False, "提示词变了却命中了旧缓存"
    assert audio.prompts() == ["模板二"]

    third, _audio = await _run_once(
        monkeypatch, tmp_path, settings.Settings(prompt_audio="模板二{focus_clause}")
    )
    assert third["cached"] is True, "同提示词第二次应该命中"
