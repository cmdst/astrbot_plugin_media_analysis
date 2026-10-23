"""v0.3 视频主链路：原生直送 → 压缩 → 抽帧回退（SPEC-v0.3 §7.3 / §7.4 / §5 / §6 / §9）。

这是 v0.3 的**主路径**，也是"钱"最集中的地方。本文件按七跳的顺序守：

| 跳 | 守什么 |
| --- | --- |
| [1] 视频位 | 没有原生通道 ⇒ `video_native_unavailable`（回退，不是错误）；id 配错 ⇒ 两个既有码 |
| [2] L1 探测 | 未过/缓存未过 ⇒ 回退；`probe_enabled=false` / `ttl=0` ⇒ 信任显式声明 |
| [3] 直送闸 | `size > max_direct_bytes` ⇒ 先压缩；压缩被禁/失败/不可行 ⇒ 各一条 note |
| [4] 原生调用 | 成功 ⇒ 信封里 `transcript=[]` + `summary=<整段回答>`，**零视觉、零抽帧、零音轨调用** |
| [5] 回退 | 三态矩阵 36 组合逐一断言；**`mode=false` 任何组合都不得抽帧**（最硬的一条） |
| [6] 摘要 | 原生成功时**不再**调摘要（那一跳的钱也不花） |
| [7] 信封 | 9 键 / 恰好 3 键；notes ⊆ 40 条词表（F7 另加 1 条） |

以及两条跨跳的不变式：

- **每一跳只花该跳的钱**：跳过的跳不许有 provider 调用 / ffmpeg 进程（用调用计数断言）；
- **缓存键含 `mode` 与 `video_fp`**（SPEC-v0.3 §9.1）：同媒体 `auto` 与 `false` **不互相命中**。
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from astrbot_plugin_media_analysis import analyze as analyze_mod
from astrbot_plugin_media_analysis import envelope, media, probe, providers, video_native
from _fakes import FakeChatProvider, FakeContext, RecordingLogger, audio_chat, native_video
from _rig import build_rig
from _vocab import bad_notes, split_note

MiB = 1024 * 1024


# --------------------------------------------------------------------------- #
# 公共小工具
# --------------------------------------------------------------------------- #
def _bigger_source(rig, size: int = 2_000_000) -> None:
    """把测试媒体的字节数顶到 `size`（直送闸/压缩那一跳只认 `stat`，不认内容）。"""
    rig.media_path.write_bytes(b"\x00" * size)


def _compress_stub(monkeypatch: pytest.MonkeyPatch, *, ok: bool, size: int = 1024, reason: str = "ok", no_audio: bool = False):
    """把 `media.compress_video` 换成"写一份小产物"的假身；返回它收到的 kwargs 列表。"""
    calls: list[dict] = []

    async def _compress(source_path, **kwargs):
        calls.append({"source_path": source_path, **kwargs})
        if not ok:
            return media.CompressResult(False, None, None, kwargs["budget_bytes"] * 10, reason, 1, no_audio)
        out = Path(kwargs["workdir"]) / "compressed.mp4"
        out.write_bytes(b"\x00" * size)
        return media.CompressResult(True, str(out), size, kwargs["budget_bytes"] * 10, "ok", 1, no_audio)

    monkeypatch.setattr(media, "compress_video", _compress)
    return calls


# --------------------------------------------------------------------------- #
# [4] 原生成功：信封形状 + "零多余开销"
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_native_success_fills_summary_and_skips_every_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """原生成功 ⇒ `summary` 是整段回答、`transcript=[]`、**零视觉/零抽帧/零音轨调用**。

    `transcript=[]` 是显式取舍（SPEC-v0.3 §16.2-7）：不"原生 + 音轨"双跑 —— 那会把一次
    调用变成两次，且第二次的转写与原生回答重复。
    """
    native = native_video(text="画面里有一个数字 7319，声音里有人说“开始录制”。")
    rig = build_rig(
        monkeypatch, tmp_path, kind="video", native=native, frames=[(2.5, "unused")]
    )

    payload = await rig.run(include_frames="auto")

    assert list(payload) == list(envelope.OK_KEYS)
    assert payload["ok"] is True
    assert payload["kind"] == "video"
    assert payload["transcript"] == []
    assert payload["frames"] == []
    assert payload["summary"] == "画面里有一个数字 7319，声音里有人说“开始录制”。"
    assert payload["cached"] is False
    assert "video_native" in payload["notes"]
    assert f"video_provider:{native.provider_config['id']}" in payload["notes"]
    assert "video_asserted" in payload["notes"]
    assert not bad_notes(payload["notes"])

    # 每一跳只花该跳的钱
    assert len(native.creates()) == 1, "原生调用恰好一次"
    assert rig.prov_calls["vision"] == 0, "原生成功不许解析视觉位"
    assert rig.prov_calls["audio"] == 0, "原生成功不许转写音轨"
    assert rig.prov_calls["chat"] == 0, "原生成功不许再做摘要（summary 就是回答本身）"
    assert rig.media_calls["extract_frames"] == 0
    assert rig.media_calls["extract_wav"] == 0
    assert not rig.audio.calls.named("text_chat")
    assert not rig.chat.calls.named("text_chat")
    assert rig.probe_calls["ensure_l1"] == 1


@pytest.mark.asyncio
async def test_native_prompt_uses_the_configured_template_and_focus_clause(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """原生提示词 = `prompts.video_native` + 焦点从句（焦点永不被静默丢弃）。"""
    from astrbot_plugin_media_analysis import settings

    native = native_video(text="回答")
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        native=native,
        cfg=settings.Settings(
            vision_provider_id="fake-chat",
            prompt_video_native="【原生模板】{focus_clause}",
        ),
    )

    payload = await rig.run(focus="他在说什么", include_frames="auto")

    assert payload["ok"] is True
    prompt = native.prompt_texts()[0]
    assert prompt.startswith("【原生模板】")
    assert "他在说什么" in prompt
    assert "focus_appended:video_native" not in payload["notes"]


@pytest.mark.asyncio
async def test_native_prompt_appends_the_focus_clause_when_the_template_lacks_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """模板里没有 `{focus_clause}` ⇒ 从句补在末尾 + `focus_appended:video_native`。"""
    from astrbot_plugin_media_analysis import settings

    native = native_video(text="回答")
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        native=native,
        cfg=settings.Settings(
            vision_provider_id="fake-chat", prompt_video_native="【没有占位符】"
        ),
    )
    payload = await rig.run(focus="他在说什么", include_frames="auto")

    assert "他在说什么" in native.prompt_texts()[0]
    assert "focus_appended:video_native" in payload["notes"]


# --------------------------------------------------------------------------- #
# [4] 原生失败 / 空文本 / 哨兵 ⇒ 回退（不许记 ok）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_native_empty_text_falls_back_and_is_never_recorded_as_seen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """200 但空文本 ⇒ `video_native_empty` + 回退链（**不是** `video_native`）。

    这是 mimo 在 `max_tokens=64` 时的形态（t8 实测）：只判状态码会把"没看见"记成成功。
    """
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        native=native_video(text="   "),
        frames=[(2.5, "unused")],
    )

    payload = await rig.run(include_frames="auto")

    assert payload["ok"] is True, "回退链还有抽帧这一跳，所以整体仍可成功"
    assert "video_native_empty" in payload["notes"]
    assert "video_native" not in payload["notes"]
    assert len(payload["frames"]) == 1, "空文本后应回退抽帧"
    assert rig.media_calls["extract_frames"] == 1


@pytest.mark.asyncio
async def test_native_sentinel_text_is_treated_as_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """模型原样回哨兵（`[无可用内容]` / `[无语音]`）⇒ 等同空文本（§12.1）。"""
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        native=native_video(text=analyze_mod.NO_CONTENT_SENTINEL),
        frames=[(2.5, "unused")],
    )
    payload = await rig.run(include_frames="auto")

    assert "video_native_empty" in payload["notes"]
    assert "video_native" not in payload["notes"]
    assert payload["summary"] != analyze_mod.NO_CONTENT_SENTINEL


@pytest.mark.asyncio
async def test_native_failure_falls_back_with_the_failed_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        native=native_video(fail_with=RuntimeError("upstream 500")),
        frames=[(2.5, "unused")],
    )
    payload = await rig.run(include_frames="auto")

    assert payload["ok"] is True
    assert "video_native_failed" in payload["notes"]
    assert "video_native" not in payload["notes"]
    assert len(payload["frames"]) == 1
    assert not rig.native.calls.named("text_chat")


@pytest.mark.asyncio
async def test_native_failure_with_mode_false_still_delivers_the_transcript(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`mode=false` + 原生失败 + 有音轨 ⇒ `ok:true`（只交转写）+ `video_frames_forbidden`。"""
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        native=native_video(fail_with=RuntimeError("boom")),
    )
    payload = await rig.run(include_frames="false")

    assert payload["ok"] is True
    assert payload["transcript"], "有音轨就必须交转写"
    assert payload["frames"] == []
    assert "video_frames_forbidden" in payload["notes"]
    assert "video_native_failed" in payload["notes"]


# --------------------------------------------------------------------------- #
# `mode=false` 的成本约束：**绝不静默抽帧**（SPEC-v0.3 §7.1 最硬的一条）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize("native_state", ["failed", "unavailable"])
async def test_mode_false_never_extracts_frames_or_resolves_vision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, native_state: str
) -> None:
    """`mode=false` + 原生失败/不可用 ⇒ `extract_frames` 与 `resolve_vision_provider` **都是 0**。

    这条是"每一跳只花该跳的钱"的极端面：v0.2 的布尔 `False` 只表示"不要帧"，v0.3 把它升级成
    **显式成本约束** —— 用户说了不要画面，就不许在原生失败后偷偷抽 6 帧再花 6 次视觉调用。
    （评审要求的"用调用计数断言"，不是只看 `frames == []`。）
    """
    native = None if native_state == "unavailable" else native_video(fail_with=RuntimeError("x"))
    rig = build_rig(monkeypatch, tmp_path, kind="video", native=native, frames=[(2.5, "unused")])

    payload = await rig.run(include_frames="false")

    assert payload["ok"] is True
    assert payload["frames"] == []
    assert rig.media_calls["extract_frames"] == 0
    assert rig.prov_calls["vision"] == 0
    assert "video_fallback_frames" not in payload["notes"]
    assert "video_frames_forbidden" in payload["notes"]
    assert not rig.chat.images_seen()


# --------------------------------------------------------------------------- #
# [5] §7.4 终态码表（没有可用回退时按"为什么没直送"给码）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_no_audio_with_mode_false_is_video_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§7.4 第一行：无原生通道 + 无音轨 + `mode=false` ⇒ `VIDEO_UNAVAILABLE`。

    这条同时是"按表不按括注"的证据：§7.3 的括注还写着 `NO_AUDIO_TRACK` 的语义沿用，
    但 §7.4 的表把它换成了 `VIDEO_*`（最终裁决以表为准，captain 2026-10-24 指令）。
    """
    rig = build_rig(monkeypatch, tmp_path, kind="video", streams=(False, True))
    with pytest.raises(envelope.ToolError) as exc_info:
        await rig.run(include_frames="false")

    assert exc_info.value.code == envelope.ERR_VIDEO_UNAVAILABLE
    assert rig.media_calls["extract_frames"] == 0


@pytest.mark.asyncio
async def test_no_audio_with_native_failure_and_mode_false_is_video_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§7.4 第三行：原生调用失败 + 无音轨 + 无可用回退 ⇒ `VIDEO_FAILED`。"""
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        streams=(False, True),
        native=native_video(fail_with=RuntimeError("boom")),
    )
    with pytest.raises(envelope.ToolError) as exc_info:
        await rig.run(include_frames="false")

    assert exc_info.value.code == envelope.ERR_VIDEO_FAILED


@pytest.mark.asyncio
async def test_no_audio_with_mode_auto_and_no_vision_is_video_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§7.4 第二行：`mode=auto` 且 vision 未配 + 无音轨 ⇒ `VIDEO_UNAVAILABLE`。"""
    from astrbot_plugin_media_analysis import settings

    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        streams=(False, True),
        cfg=settings.Settings(vision_provider_id=""),
    )
    with pytest.raises(envelope.ToolError) as exc_info:
        await rig.run(include_frames="auto")

    assert exc_info.value.code == envelope.ERR_VIDEO_UNAVAILABLE


@pytest.mark.asyncio
async def test_no_audio_with_compression_unavailable_and_mode_false_is_video_too_large(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§7.4 第五行：超门槛 + 压缩失败 + `mode=false` + 无音轨 ⇒ `VIDEO_TOO_LARGE`。"""
    from astrbot_plugin_media_analysis import settings

    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        streams=(False, True),
        native=native_video(),
        cfg=settings.Settings(
            video_max_direct_bytes=MiB, video_compress_target_bytes=MiB
        ),
    )
    _bigger_source(rig)
    calls = _compress_stub(monkeypatch, ok=False, reason="budget_exceeded")

    with pytest.raises(envelope.ToolError) as exc_info:
        await rig.run(include_frames="false")

    assert exc_info.value.code == envelope.ERR_VIDEO_TOO_LARGE
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# [3] 直送闸 + 压缩
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_over_threshold_compresses_with_the_frozen_budget_then_sends(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """超门槛 ⇒ 压缩（预算 = `min(compress_target_bytes, max_direct_bytes)`）→ 用产物直送。

    断言四件事：① 压缩只调一次、参数正确；② note 是 `video_compressed:<from>-><to>`；
    ③ 直送的是**产物字节**（不是原文件）；④ 压缩之后不再抽帧。
    """
    from astrbot_plugin_media_analysis import settings

    cfg = settings.Settings(
        vision_provider_id="fake-chat",
        video_max_direct_bytes=MiB,
        video_compress_target_bytes=8 * MiB,
    )
    native = native_video(text="压缩后仍然看见了")
    rig = build_rig(monkeypatch, tmp_path, kind="video", native=native, cfg=cfg)
    _bigger_source(rig)
    calls = _compress_stub(monkeypatch, ok=True, size=1024)
    # 记下"读进来的到底是哪份文件"：超门槛时**原文件一次都不许被读**
    reads: list[str] = []
    real_read = media.read_bytes

    def _record_read(path: str) -> bytes:
        reads.append(path)
        return real_read(path)

    monkeypatch.setattr(media, "read_bytes", _record_read)

    payload = await rig.run(include_frames="auto")

    assert payload["ok"] is True
    assert payload["summary"] == "压缩后仍然看见了"
    assert calls and calls[0]["budget_bytes"] == MiB, "预算必须是 min(target, direct)"
    assert calls[0]["duration_s"] == 30.0
    assert calls[0]["has_audio"] is True
    assert calls[0]["timeout_s"] == float(cfg.video_compress_timeout_seconds)
    assert "video_compressed:2000000->1024" in payload["notes"]
    assert rig.prov_calls["vision"] == 0
    assert rig.media_calls["extract_frames"] == 0

    url = native.video_urls()[0]
    payload_bytes = base64.b64decode(url[len(video_native.DATA_URL_PREFIX) :])
    assert len(payload_bytes) == 1024, "直送的必须是压缩产物，不是原文件"
    # 内存护栏（T9 的同一条要求换到直送闸上）：原文件**一次都没被读进内存**
    assert str(rig.media_path) not in reads, reads
    product = str(Path(calls[0]["workdir"]) / "compressed.mp4")
    assert reads == [product], reads


@pytest.mark.asyncio
async def test_compress_budget_follows_the_smaller_of_target_and_direct(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from astrbot_plugin_media_analysis import settings

    cfg = settings.Settings(
        vision_provider_id="fake-chat",
        video_max_direct_bytes=MiB,
        video_compress_target_bytes=settings.MIN_VIDEO_COMPRESS_TARGET_BYTES,
    )
    rig = build_rig(monkeypatch, tmp_path, kind="video", native=native_video(), cfg=cfg)
    _bigger_source(rig)
    calls = _compress_stub(monkeypatch, ok=True)

    await rig.run(include_frames="auto")

    assert calls[0]["budget_bytes"] == settings.MIN_VIDEO_COMPRESS_TARGET_BYTES


@pytest.mark.asyncio
async def test_compress_disabled_falls_back_to_frames(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """压缩被关 ⇒ `video_compress_skipped`，**不调 ffmpeg**，直接走抽帧回退。"""
    from astrbot_plugin_media_analysis import settings

    cfg = settings.Settings(
        vision_provider_id="fake-chat",
        video_max_direct_bytes=MiB,
        video_compress_enabled=False,
    )
    native = native_video()
    rig = build_rig(
        monkeypatch, tmp_path, kind="video", native=native, cfg=cfg, frames=[(2.5, "unused")]
    )
    _bigger_source(rig)
    calls = _compress_stub(monkeypatch, ok=True)

    payload = await rig.run(include_frames="auto")

    assert payload["ok"] is True
    assert "video_compress_skipped" in payload["notes"]
    assert "video_fallback_frames" in payload["notes"]
    assert calls == [], "压缩关了就不许起 ffmpeg"
    assert not native.creates(), "超门槛且没压 ⇒ 不许直送原文件"


@pytest.mark.asyncio
async def test_compress_failure_falls_back_to_frames_with_mode_auto(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """压缩失败 + vision 可用 + auto ⇒ **不报错**，走抽帧（§7.4 末行）。"""
    from astrbot_plugin_media_analysis import settings

    cfg = settings.Settings(vision_provider_id="fake-chat", video_max_direct_bytes=MiB)
    native = native_video()
    rig = build_rig(
        monkeypatch, tmp_path, kind="video", native=native, cfg=cfg, frames=[(2.5, "unused")]
    )
    _bigger_source(rig)
    _compress_stub(monkeypatch, ok=False, reason="budget_exceeded")

    payload = await rig.run(include_frames="auto")

    assert payload["ok"] is True
    assert "video_compress_failed" in payload["notes"]
    assert "video_fallback_frames" in payload["notes"]
    assert len(payload["frames"]) == 1
    assert not native.creates()


@pytest.mark.asyncio
async def test_compress_infeasible_is_its_own_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from astrbot_plugin_media_analysis import settings

    cfg = settings.Settings(vision_provider_id="fake-chat", video_max_direct_bytes=MiB)
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        native=native_video(),
        cfg=cfg,
        frames=[(2.5, "unused")],
    )
    _bigger_source(rig)
    _compress_stub(monkeypatch, ok=False, reason="infeasible")

    payload = await rig.run(include_frames="auto")

    assert "video_compress_infeasible" in payload["notes"]
    assert "video_compress_failed" not in payload["notes"]


@pytest.mark.asyncio
async def test_compressed_without_audio_records_the_noaudio_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """源无音轨 ⇒ 压缩去音频参 + `video_compressed_noaudio`（否则 ffmpeg 会造空音轨）。"""
    from astrbot_plugin_media_analysis import settings

    cfg = settings.Settings(vision_provider_id="fake-chat", video_max_direct_bytes=MiB)
    native = native_video(text="无声视频也看见了")
    rig = build_rig(
        monkeypatch, tmp_path, kind="video", native=native, cfg=cfg, streams=(False, True)
    )
    _bigger_source(rig)
    _compress_stub(monkeypatch, ok=True, no_audio=True)

    payload = await rig.run(include_frames="auto")

    assert payload["ok"] is True
    assert "video_compressed_noaudio" in payload["notes"]
    assert "no_audio_track" not in payload["notes"], "原生成功了就不该走到音轨那一跳"


@pytest.mark.asyncio
async def test_short_budget_skips_both_compression_and_native(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """剩余预算 < `compress_timeout + native_timeout/2` ⇒ `video_skip_budget`，两跳都不花钱。"""
    from astrbot_plugin_media_analysis import settings

    cfg = settings.Settings(vision_provider_id="fake-chat", video_max_direct_bytes=MiB)
    native = native_video()
    rig = build_rig(monkeypatch, tmp_path, kind="video", native=native, cfg=cfg)
    _bigger_source(rig)
    calls = _compress_stub(monkeypatch, ok=True)
    # 30（压缩整块）+ 60*0.5 = 60 s 才"值得动手"；给 20 s ⇒ 直接放弃这一跳
    monkeypatch.setattr(analyze_mod, "DEADLINE_S", 20.0)

    payload = await rig.run(include_frames="auto")

    assert calls == [], "预算不够就不许起压缩"
    assert not native.creates()
    assert "video_skip_budget" in payload["notes"]
    assert payload["ok"] is True, "有音轨 ⇒ 只交转写"


# --------------------------------------------------------------------------- #
# [1]/[2] 视频位与 L1 探测
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_probe_disabled_trusts_the_declared_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`probe_enabled=false` ⇒ 信任显式声明、**不探测**（overt 成本开关，§3.4）。"""
    from astrbot_plugin_media_analysis import settings

    cfg = settings.Settings(
        vision_provider_id="fake-chat", video_probe_enabled=False
    )
    native = native_video(text="直送成功")
    rig = build_rig(monkeypatch, tmp_path, kind="video", native=native, cfg=cfg)

    payload = await rig.run(include_frames="auto")

    assert payload["summary"] == "直送成功"
    assert "video_probe_disabled" in payload["notes"]
    assert rig.probe_calls["ensure_l1"] == 0, "关了探测就不许跑 L1"
    assert "video_probe_l1_ok" not in payload["notes"]


@pytest.mark.asyncio
async def test_probe_ttl_zero_also_disables_probing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`ttl_seconds=0` ⇒ 与 `probe_enabled=false` 等价（既不读缓存也不探测）。"""
    from astrbot_plugin_media_analysis import settings

    cfg = settings.Settings(
        vision_provider_id="fake-chat",
        video_probe_enabled=True,
        video_probe_ttl_seconds=0,
    )
    rig = build_rig(monkeypatch, tmp_path, kind="video", native=native_video(), cfg=cfg)

    payload = await rig.run(include_frames="auto")

    assert "video_probe_disabled" in payload["notes"]
    assert rig.probe_calls["ensure_l1"] == 0


@pytest.mark.asyncio
async def test_probe_cached_success_does_not_claim_a_fresh_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """缓存命中且通过 ⇒ **只有** `video_probe_cached`（没有实跑，就不许说"本次 L1 通过"）。"""
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        native=native_video(text="好"),
        probe_source="cache",
        digest="aaaaaaaaaaaaaaa1",
    )
    payload = await rig.run(include_frames="auto")

    assert "video_probe_cached" in payload["notes"]
    assert "video_probe_l1_ok" not in payload["notes"]
    assert "video_probe_l1_failed" not in payload["notes"]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["cache", "live"])
async def test_probe_failure_falls_back_with_both_notes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str
) -> None:
    """L1 未过 ⇒ **不直送**、回退抽帧；缓存未过时两条 note 都在（§2.2-⑥）。"""
    native = native_video()
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        native=native,
        frames=[(2.5, "unused")],
        probe_ok=False,
        probe_source=source,
    )

    payload = await rig.run(include_frames="auto")

    assert not native.creates(), "探测没过就不许直送（那是最贵的一次浪费）"
    assert "video_probe_l1_failed" in payload["notes"]
    assert ("video_probe_cached" in payload["notes"]) is (source == "cache")
    assert "video_fallback_frames" in payload["notes"]
    assert len(payload["frames"]) == 1


@pytest.mark.asyncio
async def test_probe_budget_short_skips_probe_and_native(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """剩余预算 < `L1_TIMEOUT + 30` ⇒ `video_skip_budget`，不探测也不直送。"""
    native = native_video()
    rig = build_rig(
        monkeypatch, tmp_path, kind="video", native=native, frames=[(2.5, "unused")]
    )
    monkeypatch.setattr(
        analyze_mod, "DEADLINE_S", probe.L1_TIMEOUT_S + analyze_mod.PROBE_BUDGET_RESERVE_S - 1
    )

    payload = await rig.run(include_frames="auto")

    assert rig.probe_calls["ensure_l1"] == 0
    assert not native.creates()
    assert "video_skip_budget" in payload["notes"]
    assert "video_fallback_frames" in payload["notes"], "预算还够抽帧（59 s ≥ 25 s）"


@pytest.mark.asyncio
async def test_non_openai_adapter_is_unavailable_not_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """适配器不是 `openai_chat_completion` ⇒ `video_native_unavailable` + 回退（**不判错**）。"""
    native = native_video(adapter_type="gemini_chat_completion")
    rig = build_rig(
        monkeypatch, tmp_path, kind="video", native=native, frames=[(2.5, "unused")]
    )

    payload = await rig.run(include_frames="auto")

    assert payload["ok"] is True
    assert "video_native_unavailable" in payload["notes"]
    assert not native.creates()
    assert rig.probe_calls["ensure_l1"] == 0, "没有通道就不该去探测"
    assert len(payload["frames"]) == 1


@pytest.mark.asyncio
async def test_provider_without_client_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    native = native_video(with_client=False)
    rig = build_rig(
        monkeypatch, tmp_path, kind="video", native=native, frames=[(2.5, "unused")]
    )
    payload = await rig.run(include_frames="auto")

    assert "video_native_unavailable" in payload["notes"]
    assert not native.creates()


@pytest.mark.asyncio
async def test_configured_video_id_not_found_is_provider_not_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """配了 id 但核心没有 ⇒ `PROVIDER_NOT_FOUND`（这是**配置写错**，不是"没有视频能力"）。"""
    from astrbot_plugin_media_analysis import settings

    cfg = settings.Settings(vision_provider_id="fake-chat", video_provider_id="ghost")
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        cfg=cfg,
        context=FakeContext(by_id={}),
        providers_real=True,
    )
    payload = await rig.tool()
    assert payload["error"] == envelope.ERR_PROVIDER_NOT_FOUND
    assert list(payload) == list(envelope.FAIL_KEYS)


@pytest.mark.asyncio
async def test_video_id_pointing_at_a_non_provider_is_capability_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from astrbot_plugin_media_analysis import settings

    cfg = settings.Settings(vision_provider_id="fake-chat", video_provider_id="not-a-provider")
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        cfg=cfg,
        context=FakeContext(by_id={"not-a-provider": object()}),
        providers_real=True,
    )
    payload = await rig.tool()
    assert payload["error"] == envelope.ERR_PROVIDER_CAPABILITY_MISMATCH


# --------------------------------------------------------------------------- #
# [4] 预算与护栏：timeout = min(配置, remaining-15)；max_tokens 来自配置
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_native_timeout_is_capped_by_remaining_minus_reserve(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """自建 `wait_for` 的预算是 `min(native_timeout_seconds, remaining - NATIVE_RESERVE_S)`（§7.2 护栏③）。

    用记录型假 `call_native` 抓 `timeout`（真超时要等 60 s，没必要）。

    **评审 F6**：`NATIVE_RESERVE_S` 从 15 提到 `FRAMES_MIN_BUDGET_S + 10 = 35` —— 原来
    15 < 25，原生把预算吃到只剩 15 s 时抽帧必然 `video_skip_budget`，主打回退在最该生效时失效。
    """
    from astrbot_plugin_media_analysis import settings

    native = native_video()
    # 关掉探测：L1 自己有"总预算 < 60 s 就不跑"的门槛，会把这条用例的 40 s 预算先拒掉
    cfg = settings.Settings(vision_provider_id="fake-chat", video_probe_enabled=False)
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        native=native,
        cfg=cfg,
        frames=[(2.5, "unused")],
    )
    captured: list[dict] = []

    async def _record(_provider, **kwargs):
        captured.append(kwargs)
        return video_native.NativeOutcome(ok=False, note=video_native.NOTE_FAILED, detail="test")

    monkeypatch.setattr(video_native, "call_native", _record)
    monkeypatch.setattr(analyze_mod, "DEADLINE_S", 40.0)

    payload = await rig.run(include_frames="auto")

    assert captured, "原生通道可用时必有一次调用"
    # 40 s 预算 - 35 s 预留 = 5 s（原来是 40-15=25 s）
    assert captured[0]["timeout"] <= 5.01, captured[0]["timeout"]
    assert analyze_mod.NATIVE_RESERVE_S >= analyze_mod.FRAMES_MIN_BUDGET_S, (
        "评审 F6 的核心不变式：原生阶段至少要给抽帧留够最小预算"
    )
    assert payload["ok"] is True


@pytest.mark.asyncio
async def test_no_native_call_when_remaining_budget_is_exhausted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`remaining - 15 <= 0` ⇒ 一次都不发（`video_skip_budget`）。"""
    from astrbot_plugin_media_analysis import settings

    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        native=native_video(),
        cfg=settings.Settings(vision_provider_id="fake-chat", video_probe_enabled=False),
    )
    called: list[dict] = []

    async def _record(_provider, **kwargs):
        called.append(kwargs)
        return video_native.NativeOutcome(ok=False, note=video_native.NOTE_FAILED)

    monkeypatch.setattr(video_native, "call_native", _record)
    monkeypatch.setattr(analyze_mod, "DEADLINE_S", 10.0)

    payload = await rig.run(include_frames="auto")

    assert called == []
    assert "video_skip_budget" in payload["notes"]


@pytest.mark.asyncio
async def test_native_max_tokens_comes_from_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from astrbot_plugin_media_analysis import settings

    cfg = settings.Settings(vision_provider_id="fake-chat", video_native_max_tokens=2048)
    native = native_video()
    rig = build_rig(monkeypatch, tmp_path, kind="video", native=native, cfg=cfg)

    await rig.run(include_frames="auto")

    assert native.payloads()[0]["max_tokens"] == 2048


@pytest.mark.asyncio
async def test_native_max_tokens_below_the_floor_is_raised_at_call_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """设置层允许字段是 64（`Settings` 不做钳位，`main._settings()` 才钳），
    `call_native` 必须自己再抬到 1024 —— 两道保险，缺一条就会踩 mimo 空文本。"""
    from astrbot_plugin_media_analysis import settings

    cfg = settings.Settings(
        vision_provider_id="fake-chat", video_native_max_tokens=64
    )
    native = native_video()
    rig = build_rig(monkeypatch, tmp_path, kind="video", native=native, cfg=cfg)

    await rig.run(include_frames="auto")

    assert native.payloads()[0]["max_tokens"] == 1024


# --------------------------------------------------------------------------- #
# [6] 摘要跳：原生成功不调、失败时按材料调
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_summary_runs_only_when_native_did_not_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        native=native_video(fail_with=RuntimeError("x")),
        frames=[(2.5, "unused")],
    )
    payload = await rig.run(include_frames="auto")

    assert payload["ok"] is True
    assert rig.prov_calls["chat"] == 1, "回退链中摘要仍要做"
    assert payload["summary"]
    prompts = rig.chat.prompts()
    assert any("转写：" in prompt for prompt in prompts), prompts


# --------------------------------------------------------------------------- #
# [7] 缓存键：mode 与 video_fp 各自独立（§9.1）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_mode_auto_and_false_do_not_share_a_cache_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """同媒体：`false` 与 `auto` **不互相命中**（否则会把"没看画面"当"看过"返回）。

    **评审 F7** 之后要让这条用例仍测到"两种 mode 结果不同"，必须让 `auto` 真的能抽帧：
    配一个视频位（`native` 给会失败的假身）⇒ `auto` 走回退抽帧，`false` 不抽。
    """
    from astrbot_plugin_media_analysis import settings

    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        native=native_video(fail_with=RuntimeError("boom")),
        cfg=settings.Settings(
            video_provider_id="fake-video", vision_provider_id="fake-chat"
        ),
        frames=[(2.5, "unused")],
    )

    forbidden = await rig.run(include_frames="false")
    auto = await rig.run(include_frames="auto")
    auto_again = await rig.run(include_frames="auto")

    assert forbidden["cached"] is False
    assert auto["cached"] is False, "false 的结果不许被 auto 命中"
    assert auto["frames"] and forbidden["frames"] == []
    assert auto_again["cached"] is True, "同 mode 第二次才该命中"
    assert rig.prov_calls["audio"] == 2, "audio 阶段各跑一次（false 与 auto 各一次）"


@pytest.mark.asyncio
async def test_video_fp_change_invalidates_the_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """改直送门槛（`video_fp` 的分量之一）⇒ 旧条目不许再命中。"""
    from astrbot_plugin_media_analysis import settings

    rig = build_rig(monkeypatch, tmp_path, kind="video", frames=[(2.5, "unused")])
    first = await rig.run(include_frames="auto")

    other = settings.Settings(
        vision_provider_id="fake-chat", video_max_direct_bytes=MiB
    )
    second = await rig.run(include_frames="auto", settings_obj=other)
    third = await rig.run(include_frames="auto", settings_obj=other)

    assert first["cached"] is False
    assert second["cached"] is False, "video_fp 变了却命中了旧结果"
    assert third["cached"] is True


# --------------------------------------------------------------------------- #
# 回退矩阵：mode(3) × 原生(3) × vision(2) × 音轨(2) = 36 组合
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", ["auto", "true", "false"])
@pytest.mark.parametrize("native_state", ["ok", "failed", "unavailable"])
@pytest.mark.parametrize("vision_configured", [True, False], ids=["vision", "no-vision"])
@pytest.mark.parametrize("has_audio", [True, False], ids=["audio", "no-audio"])
@pytest.mark.asyncio
async def test_fallback_matrix_lands_on_the_frozen_terminal_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
    native_state: str,
    vision_configured: bool,
    has_audio: bool,
) -> None:
    """T5 的 36 组合（SPEC 文字写"24"，按 3×3×2×2 实算 36 —— 本用例覆盖全集）。

    每格断言三件事：
    ① **终态**（`ok:true` 或 §7.4 的某个码）；
    ② **成本不变式**：`mode=false` ⇒ 任何组合下 `extract_frames` 与 `resolve_vision_provider`
       都是 0；原生成功 ⇒ 视觉/音轨调用都是 0；
    ③ **note 一致性**：走到了哪一跳，就有那条 note（`video_native` / `video_fallback_frames` /
       `video_frames_forbidden` / `video_no_vision_provider`）。
    """
    from astrbot_plugin_media_analysis import settings

    cfg = settings.Settings(vision_provider_id="fake-chat") if vision_configured else settings.Settings()
    native = None
    if native_state == "ok":
        native = native_video(text="原生看见")
    elif native_state == "failed":
        native = native_video(fail_with=RuntimeError("boom"))
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        native=native,
        cfg=cfg,
        streams=(has_audio, True),
        frames=[(2.5, "unused")],
    )

    code = ""
    payload: dict[str, object] = {}
    try:
        payload = await rig.run(include_frames=mode)
    except envelope.ToolError as exc:
        code = exc.code

    notes = list(payload.get("notes") or [])

    # ② 成本不变式（对所有格子都成立）
    if mode == "false":
        assert rig.media_calls["extract_frames"] == 0, "mode=false 绝不许抽帧"
        assert rig.prov_calls["vision"] == 0, "mode=false 绝不许解析视觉位"
        assert "video_fallback_frames" not in notes
    if native_state == "ok":
        assert rig.prov_calls["audio"] == 0 and rig.prov_calls["vision"] == 0
        assert rig.media_calls["extract_frames"] == 0

    # ① 终态 + ③ note
    if native_state == "ok":
        assert code == "", f"原生成功不该失败：{code}"
        assert payload["ok"] is True
        assert payload["summary"] == "原生看见"
        assert payload["transcript"] == [] and payload["frames"] == []
        assert "video_native" in notes
        return

    if mode == "false":
        if has_audio:
            assert code == ""
            assert payload["ok"] is True
            assert payload["frames"] == []
            assert payload["transcript"], "有音轨 ⇒ 交转写"
            assert "video_frames_forbidden" in notes
            assert ("video_native_failed" in notes) or ("video_native_unavailable" in notes)
        else:
            expected = (
                envelope.ERR_VIDEO_FAILED
                if native_state == "failed"
                else envelope.ERR_VIDEO_UNAVAILABLE
            )
            assert code == expected
        return

    # mode ∈ {auto, true}
    if vision_configured and mode == "auto" and native_state == "unavailable":
        # 评审 F7：`auto` + **未配视频模型** ⇒ 不抽帧（成本回归修复），只转写。
        # 成本不变式先断言（失败信封只有 3 键、没有 notes）。
        assert rig.media_calls["extract_frames"] == 0 and rig.prov_calls["vision"] == 0
        assert "video_fallback_frames" not in notes
        if has_audio:
            assert code == "" and payload["ok"] is True
            assert payload["frames"] == [] and payload["transcript"]
            assert "video_unavailable_no_provider" in notes
        else:
            assert code == envelope.ERR_VIDEO_UNAVAILABLE
        return

    if vision_configured:
        assert code == "", f"有 vision ⇒ 应回退抽帧并成功：{code}"
        assert payload["ok"] is True
        assert len(payload["frames"]) == 1
        assert "video_fallback_frames" in notes
        if has_audio:
            assert f"audio_provider:{rig.audio.provider_config['id']}" in notes
        else:
            assert "audio_provider" not in " ".join(notes), "无音轨就不该解析音频位"
            assert "no_audio_track" in notes
        return

    if mode == "true":
        assert code == envelope.ERR_VISION_UNAVAILABLE
        return

    # auto + 未配 vision
    if has_audio:
        assert code == ""
        assert payload["ok"] is True
        assert payload["frames"] == []
        assert "video_no_vision_provider" in notes
    else:
        expected = (
            envelope.ERR_VIDEO_FAILED
            if native_state == "failed"
            else envelope.ERR_VIDEO_UNAVAILABLE
        )
        assert code == expected


# --------------------------------------------------------------------------- #
# [8] 目录纪律：压缩产物也只落 /tmp，且用完即删
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_workdir_and_compressed_product_are_removed_after_the_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """工作目录（含压缩产物）用完即删：不落库、不落工作区、不落生产 `data/`。"""
    from astrbot_plugin_media_analysis import settings

    created: list[Path] = []
    real_mkdtemp = analyze_mod.tempfile.mkdtemp

    def _record(**kwargs):
        path = real_mkdtemp(**kwargs)
        created.append(Path(path))
        return path

    monkeypatch.setattr(analyze_mod.tempfile, "mkdtemp", _record)
    cfg = settings.Settings(vision_provider_id="fake-chat", video_max_direct_bytes=MiB)
    rig = build_rig(
        monkeypatch, tmp_path, kind="video", native=native_video(text="好"), cfg=cfg
    )
    _bigger_source(rig)
    calls = _compress_stub(monkeypatch, ok=True, size=2048)

    await rig.run(include_frames="auto")

    assert calls, "这条用例要先真的走压缩"
    assert created and not created[0].exists(), "工作目录必须在 finally 里删掉"
    assert list(tmp_path.glob("compressed*")) == [], "压缩产物不许落在媒体目录旁边"


# --------------------------------------------------------------------------- #
# 全部新分支的 note 合规（与 test_analyze 的同一份 40 条词表）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_every_v03_branch_emits_only_vocabulary_notes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """v0.3 新分支（原生/压缩/探测/预算/F7 成本闸）产出的 notes 全部落在 40 条词表里。"""
    from astrbot_plugin_media_analysis import settings

    seen: set[str] = set()

    # 原生成功 + probe cached
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        native=native_video(text="好"),
        probe_source="cache",
        digest="aaaaaaaaaaaaaaa1",
    )
    seen.update((await rig.run(include_frames="auto"))["notes"])

    # 原生空文本 + 回退抽帧
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        native=native_video(text=""),
        frames=[(2.5, "u")],
        digest="aaaaaaaaaaaaaaa2",
    )
    seen.update((await rig.run(include_frames="auto"))["notes"])

    # 压缩成功 + 无音轨压缩
    cfg = settings.Settings(vision_provider_id="fake-chat", video_max_direct_bytes=MiB)
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        native=native_video(text="好"),
        cfg=cfg,
        digest="aaaaaaaaaaaaaaa3",
    )
    _bigger_source(rig)
    _compress_stub(monkeypatch, ok=True, no_audio=True)
    seen.update((await rig.run(include_frames="auto"))["notes"])

    # 压缩失败 / 不可行 / 被禁
    for index, (reason, enabled) in enumerate(
        (("budget_exceeded", True), ("infeasible", True), ("", False)), start=4
    ):
        cfg2 = settings.Settings(
            vision_provider_id="fake-chat",
            video_max_direct_bytes=MiB,
            video_compress_enabled=enabled,
        )
        rig = build_rig(
            monkeypatch,
            tmp_path,
            kind="video",
            native=native_video(),
            cfg=cfg2,
            frames=[(2.5, "u")],
            digest=f"aaaaaaaaaaaaaa{index}",
        )
        _bigger_source(rig)
        _compress_stub(monkeypatch, ok=False, reason=reason or "ok")
        seen.update((await rig.run(include_frames="auto"))["notes"])

    # mode=false + 原生失败（无音轨 ⇒ VIDEO_*）
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        native=native_video(fail_with=RuntimeError("x")),
        streams=(False, True),
        digest="aaaaaaaaaaaaaaa7",
    )
    with pytest.raises(envelope.ToolError):
        await rig.run(include_frames="false")

    # 探测未过（缓存）
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        native=native_video(),
        frames=[(2.5, "u")],
        probe_ok=False,
        probe_source="cache",
        digest="aaaaaaaaaaaaaaa8",
    )
    seen.update((await rig.run(include_frames="auto"))["notes"])

    # 探测被关 + 预算不足
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        native=native_video(),
        cfg=settings.Settings(vision_provider_id="fake-chat", video_probe_enabled=False),
        digest="aaaaaaaaaaaaaaa9",
    )
    seen.update((await rig.run(include_frames="auto"))["notes"])

    # 没有原生通道（video_provider_id 空）⇒ video_native_unavailable
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        frames=[(2.5, "u")],
        digest="aaaaaaaaaaaaaa10",
    )
    seen.update((await rig.run(include_frames="auto"))["notes"])

    assert seen, "一个 note 都没收到？"
    assert not bad_notes(sorted(seen)), f"词表外的 note：{bad_notes(sorted(seen))}"
    words = {split_note(note)[0] for note in seen}
    # 关键分支确实被覆盖到了（不是空跑）
    for expected in (
        "video_native",
        "video_native_empty",
        "video_native_unavailable",
        "video_probe_cached",
        "video_probe_l1_failed",
        "video_probe_disabled",
        "video_compressed",
        "video_compressed_noaudio",
        "video_compress_failed",
        "video_compress_infeasible",
        "video_compress_skipped",
        "video_fallback_frames",
    ):
        assert expected in words, f"没覆盖到 {expected}：{sorted(words)}"


# --------------------------------------------------------------------------- #
# 静态：v0.3 的新模块没有把 STT/v0.2 的面带回来
# --------------------------------------------------------------------------- #
def test_v03_modules_are_wired_into_the_package() -> None:
    from astrbot_plugin_media_analysis import probe as probe_mod
    from astrbot_plugin_media_analysis import video_native as native_mod

    assert probe_mod.__package__ == video_native.__package__ == "astrbot_plugin_media_analysis"
    assert hasattr(analyze_mod, "normalize_mode")
    assert analyze_mod.MODES == frozenset({"auto", "true", "false"})
    assert not hasattr(analyze_mod, "resolve_stt_provider")


def test_provider_layer_has_no_video_modality_probe() -> None:
    """视频位**不读** `modalities`（核心没有 video 选项）⇒ 不该有 `resolve_*video*` 的能力判定。

    视频能力只能靠"调用一次且答对"（`probe.run_l1`）来证 —— 任何"读 modalities 判视频"的
    代码都是把恒 False 的判据当成事实。
    """
    import inspect

    source = inspect.getsource(providers.resolve_video_provider)
    assert "modalities" not in source
    assert "channel_reason" in source


# --------------------------------------------------------------------------- #
# 建议表**不参与运行期判定**（否决项 §16.2-6）：门槛只有一个事实源
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_runtime_threshold_comes_from_config_not_from_the_suggestion_table(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Gemini 的建议值是 8 MiB，但把 `max_direct_bytes` 配成 1 MiB ⇒ **按 1 MiB 走压缩**。

    这张表只用于 `ready` 日志提示与校准工具（SPEC-v0.3 §4 末段、否决项 §16.2-6：
    "按 provider 自动调门槛"会造出"表与配置打架"的第二事实源）。这条用例把
    "表 ≠ 运行期事实"钉在行为面上。
    """
    from astrbot_plugin_media_analysis import settings

    assert settings.suggest_direct_bytes("gemini-3-flash-preview") == 8 * MiB
    cfg = settings.Settings(
        vision_provider_id="fake-chat", video_max_direct_bytes=MiB
    )
    native = native_video(model_name="gemini-3-flash-preview", text="好")
    rig = build_rig(monkeypatch, tmp_path, kind="video", native=native, cfg=cfg)
    _bigger_source(rig, 2 * MiB)
    calls = _compress_stub(monkeypatch, ok=True)

    payload = await rig.run(include_frames="auto")

    assert calls, "2 MiB > 配置的 1 MiB ⇒ 必须压缩（不是按 8 MiB 的建议值放行）"
    assert calls[0]["budget_bytes"] == MiB
    assert "video_compressed:2097152->1024" in payload["notes"]


def test_analyze_never_reads_probe_latency_for_thresholds() -> None:
    """静态守卫：探测缓存里的 `latency` 字段**不参与任何判定**（当前恒 `None`）。

    运行期只认 `video.max_direct_bytes` 一个来源；一旦 `analyze` 里出现 `latency`，
    就说明有人开始用"探测到的延迟"自动改门槛 —— 那正是 §16.2-6 否决的做法。
    """
    import inspect

    source = inspect.getsource(analyze_mod)
    assert "latency" not in source
    assert "max_direct_bytes" in source


# --------------------------------------------------------------------------- #
# [9] 评审裁决：F7（成本回归）/ F4（静音视频）/ F6（预算饿死）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_auto_without_video_provider_never_spends_vision_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """**评审 F7**：`auto` + 未配视频模型 ⇒ **不抽帧**（v0.2 的 0 次视觉调用）。

    生产形态：`video_provider_id` 空、`vision_provider_id` 早就配好了。v0.3 原实现里
    `auto` 会拿 6 次视觉调用去补"没有原生通道"，等于把每条视频的成本从 0 抬到 6。
    裁决：`auto` 在"没有视频模型"时退化为**只转写**（note `video_unavailable_no_provider`），
    只有 `mode=true`（用户点名要画面）才强制抽帧。
    """
    from astrbot_plugin_media_analysis import settings as settings_mod

    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        cfg=settings_mod.Settings(vision_provider_id="fake-chat"),
        frames=[(2.5, "unused")],
    )

    auto = await rig.run(include_frames="auto")

    assert auto["ok"] is True
    assert auto["frames"] == []
    assert auto["transcript"], "有音轨 ⇒ 照常交转写"
    assert "video_unavailable_no_provider" in auto["notes"]
    assert "video_fallback_frames" not in auto["notes"]
    assert rig.media_calls["extract_frames"] == 0, "auto + 无视频模型 ⇒ 一次抽帧都不许跑"
    assert rig.prov_calls["vision"] == 0, "更不许解析视觉位（那 6 次调用是真金白银）"

    # 同一份配置下 `mode=true` 仍然强制抽帧（缓存键含 mode，不会命中上一条）
    forced = await rig.run(include_frames="true")

    assert forced["ok"] is True
    assert len(forced["frames"]) == 1
    assert "video_fallback_frames" in forced["notes"]
    assert rig.media_calls["extract_frames"] == 1
    assert rig.prov_calls["vision"] == 1


@pytest.mark.asyncio
async def test_silent_video_with_mode_false_is_ok_with_audio_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """**评审 F4**：静音视频（有音轨、转写空）+ 原生失败 + `mode=false` ⇒ `ok:true`。

    旧实现会落到"诚实兜底"抛出 `FRAME_EXTRACT_FAILED`，hint 还让用户去换媒体 —— 但用户
    本来就没要画面（`false`），"没有画面诉求"不该用"抽帧失败"的码来描述。
    正确形态：`ok:true` + `transcript=[]` + `frames=[]` + note `audio_empty`。
    """
    from astrbot_plugin_media_analysis import settings as settings_mod

    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        native=native_video(fail_with=RuntimeError("boom")),
        audio=audio_chat(text=""),
        cfg=settings_mod.Settings(
            video_provider_id="fake-video", vision_provider_id="fake-chat"
        ),
    )

    payload = await rig.run(include_frames="false")

    assert payload["ok"] is True, "静音 + 不要画面 ⇒ 正常降级，不是失败"
    assert payload["transcript"] == [] and payload["frames"] == []
    assert "audio_empty" in payload["notes"]
    assert "video_frames_forbidden" in payload["notes"]
    assert "video_native_failed" in payload["notes"]
    assert rig.media_calls["extract_frames"] == 0 and rig.prov_calls["vision"] == 0


@pytest.mark.asyncio
async def test_silent_video_with_auto_and_no_vision_is_ok_with_audio_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """**评审 F4** 的另一半：`auto` 但视觉位没配（抽帧这一跳按设计不存在）⇒ 同样 `ok:true`。"""
    from astrbot_plugin_media_analysis import settings as settings_mod

    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        native=native_video(fail_with=RuntimeError("boom")),
        audio=audio_chat(text=""),
        cfg=settings_mod.Settings(
            video_provider_id="fake-video", vision_provider_id=""
        ),
    )

    payload = await rig.run(include_frames="auto")

    assert payload["ok"] is True
    assert payload["transcript"] == [] and payload["frames"] == []
    assert "audio_empty" in payload["notes"]
    assert "video_no_vision_provider" in payload["notes"]
    assert rig.prov_calls["vision"] == 0


@pytest.mark.asyncio
async def test_native_burning_its_whole_budget_still_leaves_frames_usable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """**评审 F6**：原生把预算吃满后，抽帧仍必须有预算可用（注入时钟，不真等）。

    旧配置 `NATIVE_RESERVE_S=15 < FRAMES_MIN_BUDGET_S=25`：原生吃完 85 s 只剩 15 s ⇒
    抽帧必然 `video_skip_budget` —— **主打回退在最该生效时失效**。
    现在预留 = 25 + 10 = 35 s ⇒ 原生最多吃 65 s，剩下的 35 s 足够起抽帧。
    """
    from astrbot_plugin_media_analysis import settings as settings_mod

    clock = {"now": 1000.0}
    monkeypatch.setattr(analyze_mod.time, "monotonic", lambda: clock["now"])

    native = native_video()
    cfg = settings_mod.Settings(
        vision_provider_id="fake-chat",
        video_probe_enabled=False,  # L1 自己有预算闸，这里只测原生/抽帧的分配
        video_native_timeout_seconds=110,  # 上限，逼出"能吃多少"的极限
    )
    rig = build_rig(
        monkeypatch, tmp_path, kind="video", native=native, cfg=cfg,
        frames=[(2.5, "unused")],
    )
    captured: list[dict] = []

    async def _record(_provider, **kwargs):
        captured.append(kwargs)
        clock["now"] += float(kwargs["timeout"])  # 最坏情况：原生把自己的预算整段吃掉
        return video_native.NativeOutcome(
            ok=False, note=video_native.NOTE_FAILED, detail="timeout"
        )

    monkeypatch.setattr(video_native, "call_native", _record)
    monkeypatch.setattr(analyze_mod, "DEADLINE_S", 100.0)

    payload = await rig.run(include_frames="auto")

    assert captured, "原生通道可用 ⇒ 必有一次调用"
    assert captured[0]["timeout"] == pytest.approx(65.0), "100 - (25 + 10) = 65"
    assert "video_skip_budget" not in payload["notes"], "抽帧没被饿死"
    assert "video_fallback_frames" in payload["notes"]
    assert len(payload["frames"]) == 1
    assert rig.media_calls["extract_frames"] == 1
    assert payload["ok"] is True


@pytest.mark.parametrize(
    ("remaining", "cap", "reserve", "expected"),
    [
        (100.0, 60.0, 35.0, 60.0),  # 预算宽裕 ⇒ 取配置上限
        (40.0, 60.0, 35.0, 5.0),  # 扣完预留只剩 5 s
        (30.0, 60.0, 35.0, 30.0),  # 扣完不足 floor(5 s) ⇒ 不扣（后面本来也做不成）
        (0.0, 60.0, 35.0, 0.0),  # 预算已尽
        (-3.0, 60.0, 0.0, 0.0),
    ],
)
def test_budgeted_timeout_is_a_pure_function(
    remaining: float, cap: float, reserve: float, expected: float
) -> None:
    """**评审 F6** 的分配规则（纯函数）：先扣预留、扣不动就不扣、耗尽即 0。"""
    assert analyze_mod.budgeted_timeout(remaining, cap, reserve) == pytest.approx(expected)
