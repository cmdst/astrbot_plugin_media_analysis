"""主链路行为：信封契约、分支、降级、缓存、预算（SPEC §5 / §6 / §7 / §8 / §9 + SPEC-v0.2 §9）。

v0.2 → v0.3 的测试面变化（逐条对应 SPEC-v0.3 §7 / §15）：

- **视频主路径变原生直送**：`video_provider_id` 配了就走 `provider.client`（`test_video_chain.py`）；
  本文件里的视频用例因此**都要配 `vision_provider_id`**（`build_rig` 默认给 video 加上），
  因为"auto/true + vision 未配"在 v0.3 是**不抽帧**（auto 记 `video_no_vision_provider`）或
  `VISION_UNAVAILABLE`（true）；
- **`include_frames` 变三态 `auto|true|false`**：非法值回 `auto`（v0.2 是回 `false`）；
- **抽帧只是回退**：默认视频用例的 notes 多一条 `video_native_unavailable`（没配视频位）；
- 探测（L1）、压缩、原生调用与 24 组合回退矩阵在 `tests/test_video_chain.py`，
  本文件只保留"v0.2 语义不变"的骨架 + v0.3 的分支口径。

v0.1 → v0.2 的测试面变化（逐条对应 SPEC-v0.2）：
- **STT 链路没了**：`providers.resolve_stt_provider` / `FakeSTTProvider` 换成"多模态 chat
  provider"（`_fakes.audio_chat()`），断言的是 `text_chat(prompt=…, audio_urls=[本地 wav])`；
- **判型合并进 `media.classify`**（一次调用拿 `kind/duration/has_audio/has_video`），
  所以这里的假媒体层桩的是 `classify` 与 `ensure_provider_wav`，不再是 `probe_*` / `extract_wav`；
- **缓存落盘带 TTL**：`ResultCache` → `cache.DiskCache`（每个用例一个 `tmp_path` 下的
  插件数据目录），键是 10 个分量哈希 —— `focus` / 提示词 / provider 配置都会改变键。

测试策略（与 SPEC §11.2 一致）：

- **信封与分支**在 `analyze.analyze` 与**工具门面**两个层面各测一遍：门面那层是"模型真正看到
  的东西"，必须证明任何异常都不会漏成裸异常（框架会把异常包成 `error: …`，语义比信封差）。
- **IO 全部替换**（`media.*` / `providers.resolve_*` 用 monkeypatch），所以这里不跑 ffmpeg、
  不联网、不需要真 provider；真夹具与真 ffmpeg 的验证在 `test_media.py`。
- 每条用例的 docstring 都标了它守的契约条目（`§5-x` / `§6.x` / `§7.x` / C5-C8），
  方便评审时逐条对齐。

`analyze` 的模块级常量在**调用期**读取（SPEC §3.2 的形态要求），所以预算类用例用
`monkeypatch.setattr(analyze, "SUMMARY_TIMEOUT_S", …)` 就能驱动，不需要真的 sleep 100 秒。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_plugin_media_analysis import analyze as analyze_mod
from astrbot_plugin_media_analysis import background
from astrbot_plugin_media_analysis import cache as cache_mod
from astrbot_plugin_media_analysis import envelope, main, media, providers, settings
from _fakes import (
    FakeChatProvider,
    FakeContext,
    FakeEvent,
    RecordingLogger,
    audio_chat,
    make_event,
    make_file,
    make_image,
    make_record,
    make_video,
)

#: notes 词表（v0.3 的 41 条 + v0.4 的 2 词 = 43 词）的唯一副本在 `tests/_vocab.py` ——
#: 两个主链路测试文件共用一份，避免"一边加了新词、另一边还按旧词表断言"。
from _vocab import NOTE_PATTERN, NOTE_WORDS, bad_notes  # noqa: E402


from _rig import Rig, build_rig, _stub_media, _stub_providers  # noqa: E402

# --------------------------------------------------------------------------- #
# §6.1 成功信封：9 键、顺序、类型
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_success_envelope_audio_has_exactly_nine_ordered_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C6 / §6.1：成功信封 9 键、顺序固定、类型正确；语音只有转写、没有帧。"""
    rig = build_rig(monkeypatch, tmp_path, kind="audio")
    payload = await rig.run()

    assert list(payload) == list(envelope.OK_KEYS)
    assert payload["ok"] is True
    assert payload["kind"] == "audio"
    assert payload["duration_s"] == 30.0
    assert payload["transcript"] == [{"t": 0.0, "text": "这是一段合成测试音轨的转写结果。"}]
    assert payload["frames"] == []
    assert isinstance(payload["summary"], str) and payload["summary"]
    assert f"audio_provider:{providers.provider_id_of(rig.audio)}" in payload["notes"]
    assert isinstance(payload["elapsed_ms"], int) and payload["elapsed_ms"] >= 0
    assert payload["cached"] is False


@pytest.mark.asyncio
async def test_success_envelope_video_with_frames(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§7 第 7-8 步：视频先抽音轨转写，再逐帧描述；`frames` 按 `at_s` 升序、字段是 (at_s, desc)。"""
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        frames=[(2.5, "unused"), (7.5, "unused")],
        chat=FakeChatProvider(text="画面里是一个测试图样。"),
    )
    payload = await rig.run(include_frames=True)

    assert payload["kind"] == "video"
    assert payload["transcript"][0]["t"] == 0.0
    assert [frame["at_s"] for frame in payload["frames"]] == [2.5, 7.5]
    assert [frame["desc"] for frame in payload["frames"]] == ["画面里是一个测试图样。"] * 2
    assert "audio_extracted" in payload["notes"]
    assert "vision_provider:fake-chat" in payload["notes"]
    assert rig.media_calls["extract_wav"] == 1
    assert rig.media_calls["extract_frames"] == 1
    assert rig.prov_calls["vision"] == 1
    # 每帧一次 text_chat，图片是本地 jpg 路径（SPEC §2.4）
    assert len(rig.chat.images_seen()) == 2
    assert all(path.endswith(".jpg") for path in rig.chat.images_seen())


@pytest.mark.asyncio
async def test_audio_has_no_video_stream_in_envelope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """语音天然无视频流：判型只调一次、且**不抽音轨**（`extracted=False`）。

    v0.2 把"判型 + 时长 + 流"合并成一次 `classify`（`File` 段的三段梯子在里面），
    所以这里的判据是"classify 恰好一次 + 没有走抽音轨分支"，等价于 v0.1 的
    "不调 probe_streams + 不调 extract_wav"。
    """
    rig = build_rig(monkeypatch, tmp_path, kind="audio")
    await rig.run()
    assert rig.media_calls["classify"] == 1, "判型只该发生一次（v0.2 合并了三次 ffprobe）"
    assert rig.media_calls["extract_wav"] == 0, "语音不该走视频抽音轨那条路"
    assert rig.media_calls["ensure_provider_wav"] == 1


# --------------------------------------------------------------------------- #
# §5 target 校验 / §6.2 失败信封
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("target", ["x", "Current", "quoted ", "当前", ""])
@pytest.mark.asyncio
async def test_invalid_target_returns_bad_target_envelope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target: str
) -> None:
    """§5 首行：`target ∉ {current, quoted}` ⇒ `BAD_TARGET`（不做大小写/别名归一）。"""
    rig = build_rig(monkeypatch, tmp_path)
    payload = await rig.tool(target=target)
    assert list(payload) == list(envelope.FAIL_KEYS)
    assert payload == {
        "ok": False,
        "error": envelope.ERR_BAD_TARGET,
        "hint": envelope.HINTS[envelope.ERR_BAD_TARGET],
    }
    assert rig.media_calls["classify"] == 0, "target 校验必须在最前面"


@pytest.mark.asyncio
async def test_quoted_target_reads_reply_chain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§5-6：`target=quoted` 从 `Reply.chain` 里取媒体。"""
    media_path, _calls = _stub_media(monkeypatch, tmp_path, kind="audio")
    _stub_providers(monkeypatch, audio=audio_chat(), chat=FakeChatProvider())
    event = make_event(current=[], quoted=[make_record(media_path)])
    rig = build_rig(monkeypatch, tmp_path, event=event)
    payload = await rig.run(target="quoted")
    assert payload["ok"] is True
    assert payload["kind"] == "audio"


@pytest.mark.asyncio
async def test_missing_media_is_media_not_found(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """§5：两处遍历都找不到媒体 ⇒ `MEDIA_NOT_FOUND`。"""
    rig = build_rig(monkeypatch, tmp_path, event=make_event(current=[]))
    payload = await rig.tool()
    assert payload["error"] == envelope.ERR_MEDIA_NOT_FOUND
    assert list(payload) == list(envelope.FAIL_KEYS)


@pytest.mark.asyncio
async def test_unresolvable_media_is_media_unresolvable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§5：组件在但取不到本地文件 ⇒ `MEDIA_UNRESOLVABLE`。"""
    rig = build_rig(monkeypatch, tmp_path)
    from _fakes import UnresolvableVideo

    rig.event = make_event(current=[UnresolvableVideo(file="http://example.invalid/a.mp4")])
    payload = await rig.tool()
    assert payload["error"] == envelope.ERR_MEDIA_UNRESOLVABLE


# --------------------------------------------------------------------------- #
# §5 / §4.2 时长上限
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_over_limit_is_media_too_long(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """§5 + §4.2：`duration_s > max_media_seconds` ⇒ `MEDIA_TOO_LONG`（默认 60 的意图就是挡住 70 s）。"""
    rig = build_rig(monkeypatch, tmp_path, kind="video", duration=70.0)
    payload = await rig.tool()
    assert payload == {
        "ok": False,
        "error": envelope.ERR_MEDIA_TOO_LONG,
        "hint": envelope.HINTS[envelope.ERR_MEDIA_TOO_LONG],
    }
    assert rig.prov_calls["audio"] == 0, "超限必须在调 provider 之前拒绝（成本闸门）"


@pytest.mark.asyncio
async def test_exactly_at_limit_is_allowed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """边界：`duration_s == max` 放行（判据是 `>`，不是 `>=`）。"""
    rig = build_rig(monkeypatch, tmp_path, duration=60.0)
    assert (await rig.run())["ok"] is True


@pytest.mark.asyncio
async def test_limit_follows_configured_value(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """§4.2：上限可配（钳位后 5 s 是下限）⇒ 5.01 s 就该被挡。"""
    rig = build_rig(
        monkeypatch, tmp_path, duration=5.01, cfg=settings.Settings(max_media_seconds=5)
    )
    payload = await rig.tool()
    assert payload["error"] == envelope.ERR_MEDIA_TOO_LONG


@pytest.mark.asyncio
async def test_probe_failure_is_probe_failed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """§5：读不出时长 ⇒ `PROBE_FAILED`（无法判上限也无法抽帧）。"""
    rig = build_rig(monkeypatch, tmp_path, duration=envelope.ToolError(envelope.ERR_PROBE_FAILED))
    payload = await rig.tool()
    assert payload["error"] == envelope.ERR_PROBE_FAILED


# --------------------------------------------------------------------------- #
# §7 第 7-8 步：音轨优先 / 抽帧按需（不隐式改道）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_include_frames_false_never_calls_vision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§5-7：默认只做音轨转写 ⇒ 视觉 provider 一次都不许被解析或调用。"""
    rig = build_rig(monkeypatch, tmp_path, kind="video", frames=[(2.5, "unused")])
    payload = await rig.run(include_frames=False)

    assert payload["ok"] is True
    assert payload["frames"] == []
    assert rig.prov_calls["vision"] == 0
    assert rig.media_calls["extract_frames"] == 0
    assert not rig.chat.images_seen(), "没要帧的画面描述不该发生"


@pytest.mark.asyncio
async def test_include_frames_true_triggers_vision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§5-7 的对称面：`include_frames=True` 才抽帧、才调视觉。"""
    rig = build_rig(monkeypatch, tmp_path, kind="video", frames=[(2.5, "unused")])
    payload = await rig.run(include_frames=True)

    assert payload["ok"] is True
    assert rig.prov_calls["vision"] == 1
    assert rig.media_calls["extract_frames"] == 1
    assert len(payload["frames"]) == 1


@pytest.mark.asyncio
async def test_audio_with_include_frames_is_ignored_with_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§6.4：语音传 `include_frames=True` ⇒ 忽略开关 + `frames_skipped_audio_only`，不报错。"""
    rig = build_rig(monkeypatch, tmp_path, kind="audio")
    payload = await rig.run(include_frames=True)

    assert payload["ok"] is True
    assert payload["frames"] == []
    assert "frames_skipped_audio_only" in payload["notes"]
    assert rig.prov_calls["vision"] == 0


@pytest.mark.asyncio
async def test_no_audio_track_without_frames_is_video_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SPEC-v0.3 §7.4 第一行：无音轨 + `mode=false`（不得抽帧）+ 无原生通道 ⇒ `VIDEO_UNAVAILABLE`。

    **这是"按表不按括注"的那条用例**（评审要求给证据）：v0.2 这里判 `NO_AUDIO_TRACK`
    （hint 让人"把 include_frames 设为 true 改走抽帧"），但 v0.3 的 `false` 是**显式成本约束**
    —— 用户/模型说了不要画面，回退链就只剩"音轨"这一跳，没有音轨时唯一诚实的答案是
    "视频这条路走不通"（hint 指向"配视频模型 或 放开 include_frames"）。
    `NO_AUDIO_TRACK` 仍在 21 码表里（v0.2 的 18 条一个不删），但本版**不再由这条分支产出**。
    """
    rig = build_rig(
        monkeypatch, tmp_path, kind="video", streams=(False, True)
    )
    payload = await rig.tool()  # 默认 include_frames="auto"？—— 不：Rig.tool 默认 False

    assert payload == {
        "ok": False,
        "error": envelope.ERR_VIDEO_UNAVAILABLE,
        "hint": envelope.HINTS[envelope.ERR_VIDEO_UNAVAILABLE],
    }
    # 同一情形下"只花该跳的钱"：没抽帧、没解析视觉位
    assert rig.media_calls["extract_frames"] == 0
    assert rig.prov_calls["vision"] == 0


@pytest.mark.asyncio
async def test_no_audio_track_with_frames_is_success_with_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§7.2 右半：要看画面而视频无音轨 ⇒ 成功 + `no_audio_track`，转写为空、抽帧照常。"""
    rig = build_rig(
        monkeypatch, tmp_path, kind="video", streams=(False, True), frames=[(2.5, "unused")]
    )
    payload = await rig.run(include_frames=True)

    assert payload["ok"] is True
    assert payload["transcript"] == []
    assert "no_audio_track" in payload["notes"]
    assert len(payload["frames"]) == 1
    assert rig.prov_calls["audio"] == 0, "无音轨就别再解析音频模型"


@pytest.mark.asyncio
async def test_no_video_track_is_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """§7.2 对称：要画面但没有视频流 ⇒ `NO_VIDEO_TRACK`。"""
    rig = build_rig(monkeypatch, tmp_path, kind="video", streams=(True, False))
    payload = await rig.tool(include_frames=True)
    assert payload["error"] == envelope.ERR_NO_VIDEO_TRACK


@pytest.mark.asyncio
async def test_frame_extract_total_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """§5：一帧都没抽出来 ⇒ `FRAME_EXTRACT_FAILED`（抽帧层返回 `[]`，由 analyze 判死）。"""
    rig = build_rig(monkeypatch, tmp_path, kind="video", frames=[])
    payload = await rig.tool(include_frames=True)
    assert payload["error"] == envelope.ERR_FRAME_EXTRACT_FAILED


@pytest.mark.asyncio
async def test_audio_extract_failure_is_normalized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§5：转码失败 ⇒ `AUDIO_EXTRACT_FAILED`（不是裸异常）。"""
    rig = build_rig(monkeypatch, tmp_path, kind="video")

    async def _boom(_path: str, *, workdir: str, extracted: bool = False) -> str:
        raise envelope.ToolError(envelope.ERR_AUDIO_EXTRACT_FAILED)

    # v0.2 的抽音轨入口是 `ensure_provider_wav(..., extracted=True)`（`extract_wav` 成了它的内层）
    monkeypatch.setattr(media, "ensure_provider_wav", _boom)
    payload = await rig.tool()
    assert payload["error"] == envelope.ERR_AUDIO_EXTRACT_FAILED


# --------------------------------------------------------------------------- #
# §5 / §6.4 视觉阶段：全失败 vs 单帧降级
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_all_frame_descriptions_failing_is_vision_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§5：所有帧描述都失败 ⇒ `VISION_FAILED`（主诉求"看画面"没满足）。"""
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        frames=[(2.5, "unused"), (7.5, "unused")],
        chat=FakeChatProvider(fail_with=RuntimeError("upstream 500")),
    )
    payload = await rig.tool(include_frames=True)
    assert payload["error"] == envelope.ERR_VISION_FAILED


@pytest.mark.asyncio
async def test_partial_frame_failure_keeps_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§6.4：单帧失败只记 `frame_desc_failed:<at_s>`，其余帧照常返回、`ok` 不变。"""

    class _Flaky(FakeChatProvider):
        def __init__(self) -> None:
            super().__init__(text="第二帧描述")
            self._calls = 0

        async def text_chat(self, *args, **kwargs):  # type: ignore[override]
            self._calls += 1
            if self._calls == 1:
                self.calls.record("text_chat", **kwargs)
                raise RuntimeError("first frame failed")
            return await super().text_chat(*args, **kwargs)

    rig = build_rig(
        monkeypatch, tmp_path, kind="video", frames=[(2.5, "unused"), (7.5, "unused")], chat=_Flaky()
    )
    payload = await rig.run(include_frames=True)

    assert payload["ok"] is True
    assert [frame["at_s"] for frame in payload["frames"]] == [7.5]
    assert "frame_desc_failed:2.50" in payload["notes"]


@pytest.mark.asyncio
async def test_vision_provider_capability_mismatch_is_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§5：视觉位置拿到没有 image 能力的 provider ⇒ `PROVIDER_CAPABILITY_MISMATCH`（零 API 花费）。"""
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        frames=[(2.5, "unused")],
        vision=envelope.ToolError(envelope.ERR_PROVIDER_CAPABILITY_MISMATCH),
    )
    payload = await rig.tool(include_frames=True)
    assert payload["error"] == envelope.ERR_PROVIDER_CAPABILITY_MISMATCH


# --------------------------------------------------------------------------- #
# §5 / SPEC-v0.2 §2.3：音频阶段失败面（多模态 `text_chat` 的四种坏结果）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_audio_call_exception_is_audio_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§2.3：`text_chat` 抛错 ⇒ `AUDIO_FAILED`（provider 侧什么都可能抛，必须归一）。"""
    rig = build_rig(
        monkeypatch, tmp_path, audio=audio_chat(fail_with=RuntimeError("quota exceeded"))
    )
    payload = await rig.tool()
    assert payload == {
        "ok": False,
        "error": envelope.ERR_AUDIO_FAILED,
        "hint": envelope.HINTS[envelope.ERR_AUDIO_FAILED],
    }


@pytest.mark.asyncio
async def test_audio_call_timeout_is_audio_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§2.2：`min(cap, 剩余预算)` 超时 ⇒ `AUDIO_FAILED`（不冒泡成框架超时）。

    v0.5.1 起 cap 来自配置（`limits.audio_timeout_seconds`，整数秒，10–95）⇒ 想跑
    **亚秒级**超时只能从 `analyze.audio_cap_s()` 这个显式测试缝进（真等 10 s 不现实）。
    D1 的重试语义不影响本条的结论：两次都超时仍是同一个码、同一个 3 键信封。
    """
    rig = build_rig(monkeypatch, tmp_path, audio=audio_chat(delay=0.2))
    monkeypatch.setattr(analyze_mod, "audio_cap_s", lambda _settings: 0.01)
    payload = await rig.tool()
    assert payload["error"] == envelope.ERR_AUDIO_FAILED


@pytest.mark.asyncio
async def test_audio_budget_exhausted_before_call_is_audio_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`remaining <= 0` 时**一次调用都不发**，直接 `AUDIO_FAILED`（省一次真金白银）。"""
    rig = build_rig(monkeypatch, tmp_path)
    monkeypatch.setattr(analyze_mod, "DEADLINE_S", 0.0)
    payload = await rig.tool()
    assert payload["error"] == envelope.ERR_AUDIO_FAILED
    assert not rig.audio.calls.named("text_chat"), "预算已耗尽就不许再发请求"


@pytest.mark.asyncio
async def test_empty_transcript_is_not_an_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """§9 第 9 步：返回空串 ⇒ `transcript=[]` + `audio_empty`，**不是错误**。"""
    rig = build_rig(monkeypatch, tmp_path, audio=audio_chat(text="   "))
    payload = await rig.run()

    assert payload["ok"] is True
    assert payload["transcript"] == []
    assert "audio_empty" in payload["notes"]


@pytest.mark.asyncio
async def test_no_speech_sentinel_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`[无语音]` 哨兵与空串同义：成功但没内容（纯音乐 / 环境音）。"""
    rig = build_rig(monkeypatch, tmp_path, audio=audio_chat(text=analyze_mod.NO_SPEECH_SENTINEL))
    payload = await rig.run()

    assert payload["ok"] is True
    assert payload["transcript"] == []
    assert "audio_empty" in payload["notes"]


@pytest.mark.asyncio
async def test_audio_provider_unavailable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """§2.5：没配音频模型且回落也不符合 ⇒ `AUDIO_UNAVAILABLE`（缺配置，不是配错）。"""
    rig = build_rig(
        monkeypatch, tmp_path, audio=envelope.ToolError(envelope.ERR_AUDIO_UNAVAILABLE)
    )
    payload = await rig.tool()
    assert payload["error"] == envelope.ERR_AUDIO_UNAVAILABLE


@pytest.mark.asyncio
async def test_audio_capability_mismatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """§2.5：显式配了但不符合（例如填了 STT provider）⇒ `PROVIDER_CAPABILITY_MISMATCH`。"""
    rig = build_rig(
        monkeypatch,
        tmp_path,
        audio=envelope.ToolError(envelope.ERR_PROVIDER_CAPABILITY_MISMATCH),
    )
    payload = await rig.tool()
    assert payload["error"] == envelope.ERR_PROVIDER_CAPABILITY_MISMATCH


# --------------------------------------------------------------------------- #
# §7 第 9 步：摘要（失败只降级，不判死）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_summary_uses_transcript_material(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§7.5：摘要材料＝`转写：<text>`（有帧时再逐行追加）。"""
    rig = build_rig(monkeypatch, tmp_path, kind="audio")
    await rig.run()

    prompts = rig.chat.prompts()
    assert len(prompts) == 1, prompts
    assert "转写：" in prompts[0]
    assert "这是一段合成测试音轨的转写结果。" in prompts[0]


@pytest.mark.asyncio
async def test_summary_failure_keeps_ok_with_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§5/§7 第 9 步：摘要调用失败 ⇒ `summary=""` + `summary_failed`，`ok` 仍是 `true`。"""
    rig = build_rig(
        monkeypatch, tmp_path, chat=FakeChatProvider(fail_with=RuntimeError("timeout"))
    )
    payload = await rig.run()

    assert payload["ok"] is True
    assert payload["summary"] == ""
    assert "summary_failed" in payload["notes"]


@pytest.mark.asyncio
async def test_summary_provider_unavailable_is_degraded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§7 第 9 步：摘要 provider 解析不出来 ⇒ note `summary_failed`，**不**把整次调用判死。"""
    rig = build_rig(
        monkeypatch,
        tmp_path,
        chat=envelope.ToolError(envelope.ERR_VISION_UNAVAILABLE),
    )
    payload = await rig.run()

    assert payload["ok"] is True
    assert payload["summary"] == ""
    assert "summary_failed" in payload["notes"]


@pytest.mark.asyncio
async def test_summary_skipped_when_budget_is_short(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§7.1：剩余预算 < `SUMMARY_TIMEOUT_S` ⇒ 不做摘要 + `summary_skipped_budget`。"""
    rig = build_rig(monkeypatch, tmp_path)
    monkeypatch.setattr(analyze_mod, "SUMMARY_TIMEOUT_S", 1e9)
    payload = await rig.run()

    assert payload["ok"] is True
    assert payload["summary"] == ""
    assert "summary_skipped_budget" in payload["notes"]
    assert not rig.chat.calls.named("text_chat"), "预算不足就别调模型"


@pytest.mark.asyncio
async def test_empty_material_skips_summary_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§7.5：材料为空（无转写且无帧）⇒ 不调模型，`summary=""`，也不记 `summary_failed`。"""
    rig = build_rig(monkeypatch, tmp_path, audio=audio_chat(text=""))
    payload = await rig.run()

    assert payload["summary"] == ""
    assert "summary_failed" not in payload["notes"]
    assert not rig.chat.calls.named("text_chat")


# --------------------------------------------------------------------------- #
# §8 缓存
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_second_identical_call_hits_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§8：同内容同参数第二次 `cached:true`，字段一致，且**不再调 provider**。"""
    rig = build_rig(monkeypatch, tmp_path)
    first = await rig.run()
    second = await rig.run()

    assert first["cached"] is False
    assert second["cached"] is True
    for key in ("kind", "duration_s", "transcript", "frames", "summary"):
        assert second[key] == first[key], key
    # §6.3：命中时 notes 是"原样 notes + cache_hit + cache_age_s:<n>"，其余逐字相同
    assert second["notes"][: len(first["notes"])] == first["notes"]
    assert second["notes"][-2] == "cache_hit"
    assert re.fullmatch(r"cache_age_s:\d+", second["notes"][-1]), second["notes"]
    assert isinstance(second["elapsed_ms"], int)
    assert rig.prov_calls["audio"] == 1, "命中最多次只该调一次音频模型"
    assert rig.media_calls["extract_frames"] == 0


@pytest.mark.asyncio
async def test_different_focus_misses_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """§8 键含参数指纹：`focus` 不同 ⇒ 不命中。"""
    rig = build_rig(monkeypatch, tmp_path)
    await rig.run(focus="他在说什么")
    second = await rig.run(focus="画面里有什么")

    assert second["cached"] is False
    assert rig.prov_calls["audio"] == 2


@pytest.mark.asyncio
async def test_include_frames_flag_is_part_of_cache_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§8 键含 `include_frames`：先只转写、再要帧 ⇒ 两次都不许命中。"""
    rig = build_rig(monkeypatch, tmp_path, kind="video", frames=[(2.5, "unused")])
    first = await rig.run(include_frames=False)
    second = await rig.run(include_frames=True)

    assert first["cached"] is False
    assert second["cached"] is False
    assert rig.media_calls["extract_frames"] == 1


@pytest.mark.asyncio
async def test_different_digest_misses_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """§8 键含内容哈希：内容变了（digest 变）⇒ 不命中。"""
    rig = build_rig(monkeypatch, tmp_path, digest="aaaa0000aaaa0000")
    await rig.run()
    monkeypatch.setattr(media, "content_digest", _digest_of("bbbb1111bbbb1111"))
    second = await rig.run()

    assert second["cached"] is False


def _digest_of(value: str):
    async def _inner(_path: str) -> str:
        return value

    return _inner


@pytest.mark.asyncio
async def test_cache_survives_a_new_cache_instance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§6.4：缓存落在插件数据目录 ⇒ **新建一个实例**（等价于进程重启）仍能命中。

    v0.1 的内存 LRU 重启即失效，做不到"同一媒体二次引用不再花 token"；这条用例把
    v0.2 的落盘承诺钉在**行为面**（容量淘汰的细口径在 `tests/test_cache.py`）。
    """
    rig = build_rig(monkeypatch, tmp_path)
    first = await rig.run()

    reopened = cache_mod.DiskCache(
        plugin_name=settings.PLUGIN_NAME, ttl_seconds=rig.settings.cache_ttl_seconds
    )
    second = await rig.run(cache_obj=reopened)

    assert first["cached"] is False
    assert second["cached"] is True
    assert rig.prov_calls["audio"] == 1, "跨实例命中不该再花 token"


@pytest.mark.asyncio
async def test_cached_payload_is_a_copy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """§8：缓存存的是序列化快照 ⇒ 调用方改写返回值不会污染缓存。"""
    rig = build_rig(monkeypatch, tmp_path)
    first = await rig.run()
    first["transcript"].clear()
    first["notes"].append("tampered")

    second = await rig.run()
    assert second["transcript"], "缓存被调用方改写了"
    assert "tampered" not in second["notes"]


# --------------------------------------------------------------------------- #
# §7 第 1 步：focus 截断
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_long_focus_is_trimmed_with_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§6.4 / §7 第 1 步：`focus` 超 200 字被截断，并记 `focus_trimmed`。"""
    rig = build_rig(monkeypatch, tmp_path)
    payload = await rig.run(focus="问" * 260)

    assert "focus_trimmed" in payload["notes"]
    prompt = rig.chat.prompts()[0]
    assert "问" * 200 in prompt
    assert "问" * 201 not in prompt


@pytest.mark.asyncio
async def test_blank_focus_is_treated_as_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """空白 `focus` 走"通篇摘要"提示词，不进定向从句、不记 `focus_trimmed`。"""
    rig = build_rig(monkeypatch, tmp_path)
    payload = await rig.run(focus="   ")

    assert "focus_trimmed" not in payload["notes"]
    assert "用户关心的是" not in rig.chat.prompts()[0]


# --------------------------------------------------------------------------- #
# §9 敏感面 + 目录清理
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_success_log_contains_counts_not_transcript(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§9-1：成功日志只打结构/计数，**不打**转写正文与画面描述。"""
    log = RecordingLogger()
    monkeypatch.setattr(analyze_mod, "LOG", log)
    rig = build_rig(monkeypatch, tmp_path, audio=audio_chat(text="转写正文不许进日志"))
    await rig.run()

    text = log.text()
    assert "ok kind=audio" in text
    assert "转写正文不许进日志" not in text


@pytest.mark.asyncio
async def test_workdir_is_removed_even_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§9-4：中间产物目录用完即删（成功与受控失败两条路都要删）。"""
    created: list[Path] = []
    real_mkdtemp = analyze_mod.tempfile.mkdtemp

    def _record(**kwargs):
        path = real_mkdtemp(**kwargs)
        created.append(Path(path))
        return path

    monkeypatch.setattr(analyze_mod.tempfile, "mkdtemp", _record)

    rig = build_rig(monkeypatch, tmp_path)
    await rig.run()
    assert created and not created[0].exists()

    created.clear()
    rig_fail = build_rig(monkeypatch, tmp_path, duration=70.0, kind="video")
    with pytest.raises(envelope.ToolError):
        await rig_fail.run()
    assert created and not created[0].exists()


@pytest.mark.asyncio
async def test_extracted_wav_is_tracked_on_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§9-4：拿到本地 wav 后登记给事件（`track_temporary_local_file`），事件结束由框架清理。"""
    from _fakes import TrackingFakeEvent

    media_path, _calls = _stub_media(monkeypatch, tmp_path, kind="video")
    _stub_providers(monkeypatch, audio=audio_chat(), chat=FakeChatProvider())
    event = TrackingFakeEvent(chain=[make_video(media_path)])
    rig = build_rig(monkeypatch, tmp_path, kind="video", event=event)
    await rig.run()

    assert len(event.tracked_files) == 1
    assert event.tracked_files[0].endswith("audio.wav")


# --------------------------------------------------------------------------- #
# 工具门面：任何异常都不许漏出去（§3.3 / §5-9）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "boom",
    [
        RuntimeError("analyze exploded"),
        ValueError("weird"),
        KeyError("missing"),
        KeyboardInterrupt(),
    ],
    ids=["RuntimeError", "ValueError", "KeyError", "KeyboardInterrupt"],
)
@pytest.mark.asyncio
async def test_tool_facade_turns_unexpected_errors_into_envelope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, boom: BaseException
) -> None:
    """§3.3：除「取消」之外的任何异常都收成 3 键 `INTERNAL_ERROR` 信封（含 `KeyboardInterrupt`）。

    `BaseException` 也在网里：框架的硬超时/信号最终都表现为异常，收成信封比抛裸异常好读。
    """
    rig = build_rig(monkeypatch, tmp_path)

    async def _boom(**_kwargs):
        raise boom

    monkeypatch.setattr(analyze_mod, "analyze", _boom)
    payload = await rig.tool()

    assert payload == {
        "ok": False,
        "error": envelope.ERR_INTERNAL_ERROR,
        "hint": envelope.HINTS[envelope.ERR_INTERNAL_ERROR],
    }


@pytest.mark.asyncio
async def test_tool_facade_propagates_cancelled_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """评审 F5：`asyncio.CancelledError` **必须向上传播**，不许被收成信封。

    取消语义来自插件卸载 / 进程停止 / 框架取消任务：把它吞成 `INTERNAL_ERROR` 信封会让
    取消变成"一次失败的模型调用"，上层就再也没法正确收尾（t18 之前那条断言恰好写反了）。
    """
    rig = build_rig(monkeypatch, tmp_path)

    async def _boom(**_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(analyze_mod, "analyze", _boom)
    # v0.4：取消发生在**后台任务**里（门面已返回受理回执），所以断言变成两条 ——
    # ① 任务以 cancelled 收尾（`wait()` 拿不到任何信封，绝不能被收成 INTERNAL_ERROR）；
    # ② 取消**不投递**（没有结果消息，也没有兜底发送）。
    payload = await rig.tool_expect_accept()
    assert await background.wait(payload["task_id"], timeout=10.0) is None
    assert rig.context.calls.named("send_message") == []
    assert rig.event.sent == []


@pytest.mark.asyncio
async def test_tool_controlled_failure_is_logged_and_enveloped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§3.3：`ToolError` 走 INFO 日志 + 3 键信封；`log_detail` 不进信封。"""
    log = RecordingLogger()
    # v0.4：`ToolError` 在**后台任务**里被捕获（`background._run`），日志从那里出，
    # 因此两处 LOG 都收进同一个 recorder（门面在受理前仍可能记一条）
    monkeypatch.setattr(main, "LOG", log)
    monkeypatch.setattr(background, "LOG", log)
    rig = build_rig(monkeypatch, tmp_path, duration=70.0, kind="video")

    payload = await rig.tool()
    assert payload["error"] == envelope.ERR_MEDIA_TOO_LONG
    assert "duration_s=70" not in json.dumps(payload, ensure_ascii=False)
    assert "media_analyze rejected" in log.text()


@pytest.mark.asyncio
async def test_tool_failure_envelope_always_has_exactly_three_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§6.2：失败信封恰好 3 键（不论哪条分支：参数、媒体、provider、转码、抽帧）。"""
    cases: list[tuple[str, dict[str, object]]] = []

    rig = build_rig(monkeypatch, tmp_path)
    cases.append(("bad_target", await rig.tool(target="x")))

    rig = build_rig(monkeypatch, tmp_path, duration=70.0, kind="video")
    cases.append(("too_long", await rig.tool()))

    rig = build_rig(monkeypatch, tmp_path, kind="video", streams=(False, True))
    cases.append(("no_audio", await rig.tool()))

    rig = build_rig(
        monkeypatch, tmp_path, audio=audio_chat(fail_with=RuntimeError("x"))
    )
    cases.append(("stt_failed", await rig.tool()))

    for name, payload in cases:
        assert list(payload) == ["ok", "error", "hint"], name
        assert payload["ok"] is False, name
        assert payload["hint"] == envelope.HINTS[str(payload["error"])], name


@pytest.mark.asyncio
async def test_hint_is_static_across_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """C9：同一错误码两次运行的 `hint` 逐字相同（hint 是常量，不夹带运行时数据）。"""
    first = await build_rig(monkeypatch, tmp_path, duration=70.0, kind="video").tool()
    second = await build_rig(monkeypatch, tmp_path, duration=95.0, kind="video").tool()
    assert first == second


def test_note_vocabulary_has_forty_three_words() -> None:
    """词表 = 43 **词**（v0.2 的 20 + v0.3 的 21 + v0.4 的 2），且带参形态可解析。

    条数口径：v0.4 新增 3 条 note **形态**，其中 `source:gif` 是既有词 `source` 的带参
    形态（与 `source:file` 同构）⇒ 只加 2 个新词。

    这条在**不跑主链路**的情况下就能红：谁在 `analyze.py` 里新加一个 note 却忘了同步词表，
    `test_notes_always_come_from_the_frozen_vocabulary` 会红，而这条说明"正确的条数是多少"。
    """
    from _vocab import V02_NOTES, V03_NOTES, V04_NOTES

    assert len(V02_NOTES) == 20
    assert len(V03_NOTES) == 21  # v0.3 的 19 条 + 评审 F7 的 1 条 + R1 的 1 条
    assert len(V04_NOTES) == 2  # v0.4：gif_converted / gif_static
    assert len(NOTE_WORDS) == 43
    assert V02_NOTES & V03_NOTES == set(), "v0.3 的 19 条不许与 v0.2 重名"
    assert not (V04_NOTES & (V02_NOTES | V03_NOTES)), "v0.4 的 2 条不许与旧词重名"
    # 带参形态：`:参数` 可含 `->` / `/` / `:`，但不许有空白（否则日志会被拆词）
    assert NOTE_PATTERN.match("video_compressed:1234->567")
    assert NOTE_PATTERN.match("frames_truncated:0/6")
    assert NOTE_PATTERN.match("video_provider:some-id")
    assert not NOTE_PATTERN.match("video_compressed:1 2")
    assert not NOTE_PATTERN.match("not_a_note")
    assert not NOTE_PATTERN.match("video_native_extra")


@pytest.mark.asyncio
async def test_notes_always_come_from_the_frozen_vocabulary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C7：`notes` 词必须 ⊆ 43 词表（含 `:<参数>` 形式；F7/R1/v0.4 的 2 条也在内）。

    覆盖的场景要足够杂：带帧回退 / 无音轨 / 仅语音 / 空转写 / 截断 focus / 缓存命中。
    v0.3 的原生-压缩-回退链另在 `tests/test_video_chain.py` 里做同一份断言。
    """
    seen: set[str] = set()

    rig = build_rig(monkeypatch, tmp_path, kind="video", frames=[(2.5, "unused")])
    seen.update((await rig.run(include_frames=True))["notes"])
    seen.update((await rig.run(include_frames=True))["notes"])  # 命中缓存时 notes 原样返回
    seen.update((await build_rig(monkeypatch, tmp_path, kind="video", streams=(False, True), frames=[(2.5, "u")]).run(include_frames=True))["notes"])
    seen.update((await build_rig(monkeypatch, tmp_path, kind="audio").run(include_frames=True))["notes"])
    seen.update((await build_rig(monkeypatch, tmp_path, audio=audio_chat(text="")).run())["notes"])
    seen.update((await build_rig(monkeypatch, tmp_path).run(focus="问" * 260))["notes"])
    seen.update((await build_rig(monkeypatch, tmp_path, kind="video").run(include_frames=False))["notes"])

    assert seen, "一个 note 都没收到？"
    bad = bad_notes(sorted(seen))
    assert not bad, f"notes 里出现了词表外的词：{bad}"


@pytest.mark.asyncio
async def test_frames_and_notes_share_at_s_formatting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C8：`frames[].at_s` 与 `notes` 里的 `:<at_s>` 用同一套 2 位小数格式化。"""
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        frames=[(2.5, "unused"), (7.5, "unused")],
        chat=FakeChatProvider(fail_with=RuntimeError("all fail")),
    )
    payload = await rig.tool(include_frames=True)

    assert payload["error"] == envelope.ERR_VISION_FAILED
    # 直接测 describe_frames：一帧成功一帧失败时，两者格式必须同形
    chat = FakeChatProvider()
    notes: list[str] = []
    described = await analyze_mod.describe_frames(
        chat,
        [(2.5, str(tmp_path / "a.jpg")), (7.5, str(tmp_path / "b.jpg"))],
        focus="",
        deadline=time.monotonic() + 30,
        notes=notes,
        settings=settings.Settings(),
    )
    assert [frame["at_s"] for frame in described] == [2.5, 7.5]
    assert all(isinstance(frame["at_s"], float) for frame in described)


# --------------------------------------------------------------------------- #
# 预算（§7.1）：直接测 describe_frames 的截断口径
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_describe_frames_truncates_without_raising_when_no_call_started(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§6.4 / §7.1：**第 0 帧前**就因预算 break ⇒ 返回 `[]` + `frames_truncated:0/1`，**不抛**。

    t12 之后"预算不足"与"模型失败"分家：这里没有发起过任何 `text_chat`，
    所以是降级而不是 `VISION_FAILED`（同一条契约另一面见下一条用例）。
    """
    chat = FakeChatProvider()
    notes: list[str] = []
    described = await analyze_mod.describe_frames(
        chat,
        [(2.5, str(tmp_path / "a.jpg"))],
        focus="",
        deadline=time.monotonic() - 1,  # 已经过期：一帧都不该发起
        notes=notes,
        settings=settings.Settings(),
    )
    assert described == []
    assert notes == ["frames_truncated:0/1"]
    assert not chat.calls.named("text_chat")


@pytest.mark.asyncio
async def test_describe_frames_all_attempts_failed_is_vision_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§5 / §7.1：**发起过**调用但全部失败 ⇒ 仍是 `VISION_FAILED`。

    预算降级不能顺手把这条也降级 —— 否则"模型侧挂了"会伪装成"没好画面"，
    用户拿到的会是 `{ok:true, frames:[]}`。
    """
    chat = FakeChatProvider(fail_with=RuntimeError("upstream 500"))
    notes: list[str] = []
    with pytest.raises(envelope.ToolError) as exc_info:
        await analyze_mod.describe_frames(
            chat,
            [(2.5, str(tmp_path / "a.jpg"))],
            focus="",
            deadline=time.monotonic() + 30,
            notes=notes,
            settings=settings.Settings(),
        )
    assert exc_info.value.code == envelope.ERR_VISION_FAILED
    assert notes == ["frame_desc_failed:2.50"]
    assert chat.calls.named("text_chat"), "必须先真的发起过调用，才能判「全失败」"


@pytest.mark.asyncio
async def test_describe_frames_single_call_timeout_degrades(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§7.1：单帧 15 s 超时 ⇒ 只降级这一帧（`frame_desc_failed`），其余帧继续。"""
    monkeypatch.setattr(analyze_mod, "FRAME_CALL_TIMEOUT_S", 0.01)
    chat = FakeChatProvider(delay=0.2)
    notes: list[str] = []
    with pytest.raises(envelope.ToolError) as exc_info:
        await analyze_mod.describe_frames(
            chat,
            [(2.5, str(tmp_path / "a.jpg"))],
            focus="",
            deadline=time.monotonic() + 30,
            notes=notes,
            settings=settings.Settings(),
        )
    assert exc_info.value.code == envelope.ERR_VISION_FAILED
    assert notes == ["frame_desc_failed:2.50"]


# --------------------------------------------------------------------------- #
# F1（t12 已改实现，SPEC §7.1 口径不变）：预算截断是降级，不是失败
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_slow_audio_eating_budget_keeps_transcript_and_degrades_frames(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§7.1 第 1 面：慢音频调用吃光预算 + `include_frames=true` ⇒ **成功信封 + 保住转写**。

    断言四件事：① `ok:true`；② `transcript` 非空（t12 的核心承诺）；③ `notes` 里有
    `frames_truncated:0/<want>`；④ `frames=[]` 且视觉 provider 一次都没被解析/调用。

    v0.3 的预算闸（SPEC-v0.3 §5.2 推论 3）：进抽帧回退前先看 `剩余 < FRAMES_MIN_BUDGET_S(25 s)`
    —— 不够就记 `video_skip_budget` 并**只交转写**（不再启动 ffmpeg）。所以 `DEADLINE_S=0.5`
    + 100 ms 的慢调用正好落在"音频成功、抽帧被预算拒"这条缝里（无需真的 sleep 100 s）。
    """
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        duration=30.0,
        frames=[(2.5, "unused")],
        audio=audio_chat(text="慢音频调用的转写必须保住", delay=0.1),
        cfg=settings.Settings(max_frames=6, vision_provider_id="fake-chat"),
    )
    monkeypatch.setattr(analyze_mod, "DEADLINE_S", 0.5)  # 音频吃得下、抽帧吃不下

    payload = await rig.run(include_frames=True)

    assert payload["ok"] is True
    assert list(payload) == list(envelope.OK_KEYS)
    assert [item["text"] for item in payload["transcript"]] == ["慢音频调用的转写必须保住"]
    assert "video_skip_budget" in payload["notes"]
    assert "video_frames_forbidden" not in payload["notes"], "auto/true 且配了 vision ⇒ 不是「禁止」"
    assert payload["frames"] == []
    assert rig.prov_calls["vision"] == 0, "预算不足就别再发起视觉调用（省一次 API 花费）"
    assert rig.media_calls["extract_frames"] == 0
    assert "summary_skipped_budget" in payload["notes"]


@pytest.mark.asyncio
async def test_budget_exhausted_with_nothing_to_deliver_is_still_a_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§7.1 第 2 面：预算耗尽 **且** 转写与帧都为空 ⇒ 不给"空成功"。

    两种"什么都没有"的来源都测，**终态码不同**（v0.3 的分工）：
    ① 无音轨 + `mode=true`：预算不够连抽帧都不起 ⇒ 这条路走不通 ⇒ `VIDEO_UNAVAILABLE`
       （§7.4：无原生通道 + 无音轨 + 没抽帧）；
    ② 有音轨但音频模型返回空串：转写与帧都空 ⇒ `FRAME_EXTRACT_FAILED`（诚实兜底）。
    """
    # ① 无音轨（audio 阶段根本不解析）+ 预算耗尽 ⇒ 抽帧那一跳被预算拒
    rig = build_rig(
        monkeypatch, tmp_path, kind="video", duration=30.0, streams=(False, True)
    )
    monkeypatch.setattr(analyze_mod, "DEADLINE_S", 0.5)
    payload = await rig.tool(include_frames=True)

    assert list(payload) == list(envelope.FAIL_KEYS), "失败信封必须恰好 3 键"
    assert payload == {
        "ok": False,
        "error": envelope.ERR_VIDEO_UNAVAILABLE,
        "hint": envelope.HINTS[envelope.ERR_VIDEO_UNAVAILABLE],
    }
    assert "video_skip_budget" in payload["notes"] if "notes" in payload else True
    # ② 有音轨但转写为空
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        duration=30.0,
        audio=audio_chat(text=""),
    )
    monkeypatch.setattr(analyze_mod, "DEADLINE_S", 0.5)
    payload = await rig.tool(include_frames=True)
    assert list(payload) == list(envelope.FAIL_KEYS)
    assert payload["error"] == envelope.ERR_FRAME_EXTRACT_FAILED


@pytest.mark.asyncio
async def test_frame_extract_process_timeout_is_still_a_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§7.1 第 3 面：抽帧 **总预算 wait_for 超时**（不是"预算不足"的 truncated 分支）⇒ 仍判失败。

    t12 只把"预算不足"改成降级；`wait_for` 超时那条路照旧抛 `FRAME_EXTRACT_FAILED`
    —— 那是"本机抽帧卡死"，不是"没时间"，语义不同。
    """
    rig = build_rig(monkeypatch, tmp_path, kind="video", frames=[(2.5, "unused")])

    async def _timeout(*_args, **_kwargs):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(media, "extract_frames", _timeout)
    payload = await rig.tool(include_frames=True)

    assert payload == {
        "ok": False,
        "error": envelope.ERR_FRAME_EXTRACT_FAILED,
        "hint": envelope.HINTS[envelope.ERR_FRAME_EXTRACT_FAILED],
    }


@pytest.mark.asyncio
async def test_budget_sufficient_path_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """回归护栏：预算充足时 6 帧 + 转写 + 摘要齐全，**不许**被上面的降级逻辑顺手简化掉。"""
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        duration=30.0,
        frames=[(5.0, "unused"), (15.0, "unused"), (25.0, "unused")],
        audio=audio_chat(text="正常预算下的转写"),
        cfg=settings.Settings(max_frames=6, vision_provider_id="fake-chat"),
    )
    payload = await rig.run(include_frames=True)

    assert payload["ok"] is True
    assert payload["frames"] and len(payload["frames"]) == 3
    assert payload["transcript"][0]["text"] == "正常预算下的转写"
    assert payload["summary"]
    assert "frames_truncated:0/6" not in payload["notes"]
    assert rig.prov_calls["vision"] == 1


# --------------------------------------------------------------------------- #
# F2：captain 2026-10-22 裁决 —— 带默认值的追加参数属**批准偏离**，不回退签名
# --------------------------------------------------------------------------- #
def test_build_summary_kind_param_is_an_approved_extension() -> None:
    """`analyze.build_summary(..., kind="")` 是批准过的追加参数，不是漂移。

    captain 2026-10-22 裁决：带默认值的追加参数属批准偏离，见 t2 evidence_note。
    它比 SPEC §3.2 的冻结签名多一个 `kind`（提示词 `{kind_cn}` 需要它）；不传时回落「媒体」，
    行为与 SPEC 一致 —— 这条用例就是那枚"批准章"，谁要删它得先改裁决。
    """
    params = inspect.signature(analyze_mod.build_summary).parameters
    assert "kind" in params, "批准过的 kind 参数被删了？先看 t2 evidence_note 再动手"
    assert params["kind"].default == ""
    assert params["kind"].kind is inspect.Parameter.KEYWORD_ONLY


# --------------------------------------------------------------------------- #
# ⑥ 真阶梯 × analyze：空串回落（不 patch providers，只 patch media）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_empty_audio_id_falls_back_to_current_chat_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§2.5 阶梯 ②：`audio_provider_id=""` ⇒ 回落核心当前对话 provider（它必须能吃音频）。

    v0.1 这里走的是 STT 五级阶梯（`enable=False` 时靠 `curr_stt_provider_inst` 兜底）；
    v0.2 没有 STT 链路，回落面就是**当前对话 provider** 一个 —— 也因此少了一条
    "绕过 enable 开关"的隐式分支。
    """
    provision = audio_chat(model_name="mimo_audio")
    ctx = FakeContext(using_provider=provision)
    rig = build_rig(monkeypatch, tmp_path, context=ctx, providers_real=True)
    payload = await rig.run()

    assert payload["ok"] is True
    assert "audio_provider:mimo_audio" in payload["notes"]
    assert ctx.calls.named("get_using_provider_async"), "空串必须先问核心当前对话 provider"


@pytest.mark.asyncio
async def test_empty_audio_id_with_nothing_loaded_is_audio_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§2.5 阶梯 ② 落空：核心没有可用对话 provider ⇒ `AUDIO_UNAVAILABLE`（不是裸异常）。"""
    ctx = FakeContext(using_provider=None)
    rig = build_rig(monkeypatch, tmp_path, context=ctx, providers_real=True)
    payload = await rig.tool()
    assert payload["error"] == envelope.ERR_AUDIO_UNAVAILABLE


@pytest.mark.asyncio
async def test_configured_missing_audio_id_is_provider_not_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§2.5 阶梯 ①：配了 id 但核心没有 ⇒ `PROVIDER_NOT_FOUND`（不是回落到当前对话 provider）。"""
    ctx = FakeContext(by_id={}, using_provider=audio_chat(model_name="core"))
    rig = build_rig(
        monkeypatch,
        tmp_path,
        context=ctx,
        cfg=settings.Settings(audio_provider_id="ghost"),
        providers_real=True,
    )
    payload = await rig.tool()
    assert payload["error"] == envelope.ERR_PROVIDER_NOT_FOUND


@pytest.mark.asyncio
async def test_empty_vision_id_does_not_fall_back_for_frames(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """v0.3 覆盖 v0.2 的一处判据：**抽帧回退不猜 provider**（`vision_provider_id=""` ⇒ 不抽帧）。

    v0.2 里视觉位空串会回落到核心当前对话 provider（"反正它也能读图"）。v0.3 改了：
    抽帧是**回退**，回退的开销（6 次视觉调用）必须是用户**显式**配过的 —— 所以
    `auto` + 空 vision 只交音轨转写，并记 `video_no_vision_provider`（不是错误）。

    这条用例故意让核心 provider **能读图**（`modalities` 含 image）也不许抽帧：
    "能读"不等于"用户允许花这笔钱"。
    """
    chat = FakeChatProvider(modalities=["text", "audio", "image"], model_name="core-chat")
    ctx = FakeContext(using_provider=chat)
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        frames=[(2.5, "unused")],
        context=ctx,
        providers_real=True,
        cfg=settings.Settings(vision_provider_id=""),
    )
    payload = await rig.run(include_frames="auto")

    assert payload["ok"] is True
    assert payload["frames"] == []
    assert "video_no_vision_provider" in payload["notes"]
    assert "audio_provider:core-chat" in payload["notes"], "音轨仍回落核心 provider（v0.2 语义不变）"
    assert rig.media_calls["extract_frames"] == 0
    assert not chat.images_seen(), "空视觉位不许偷偷调核心 provider 读图"


@pytest.mark.asyncio
async def test_empty_vision_id_with_mode_true_is_vision_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`mode=true`（明确要画面）+ `vision_provider_id=""` ⇒ `VISION_UNAVAILABLE`（§7.1 表格）。

    与上一条同形不同判：`auto` 只降级（转写照给），`true` 是"用户点名要画面" ⇒ 必须报错
    —— 给一个 `ok:true` + 空 frames 等于把"没看画面"记成成功。
    """
    chat = FakeChatProvider(modalities=["text", "audio", "image"], model_name="core-chat")
    ctx = FakeContext(using_provider=chat)
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        frames=[(2.5, "unused")],
        context=ctx,
        providers_real=True,
        cfg=settings.Settings(vision_provider_id=""),
    )
    payload = await rig.tool(include_frames=True)

    assert payload == {
        "ok": False,
        "error": envelope.ERR_VISION_UNAVAILABLE,
        "hint": envelope.HINTS[envelope.ERR_VISION_UNAVAILABLE],
    }
    assert rig.media_calls["extract_frames"] == 0
    assert not chat.images_seen()


# --------------------------------------------------------------------------- #
# 异常路径逐个证明"不漏成裸异常"（除 5 种异常类型外的 IO 层）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_provider_resolver_leaking_raw_exception_is_internal_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """provider 解析里漏出**裸**异常（不该发生，但必须兜住）⇒ `INTERNAL_ERROR` 信封。"""
    rig = build_rig(monkeypatch, tmp_path)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("resolver blew up")

    monkeypatch.setattr(providers, "resolve_audio_provider", _boom)
    payload = await rig.tool()
    assert payload == {
        "ok": False,
        "error": envelope.ERR_INTERNAL_ERROR,
        "hint": envelope.HINTS[envelope.ERR_INTERNAL_ERROR],
    }


@pytest.mark.asyncio
async def test_frame_extractor_leaking_raw_exception_is_internal_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = build_rig(monkeypatch, tmp_path, kind="video", frames=[(2.5, "unused")])

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("ffmpeg wrapper exploded")

    monkeypatch.setattr(media, "extract_frames", _boom)
    payload = await rig.tool(include_frames=True)
    assert payload["error"] == envelope.ERR_INTERNAL_ERROR


@pytest.mark.asyncio
async def test_unserializable_envelope_never_reaches_the_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """信封字段不可序列化时 `dumps` 会炸 ⇒ 也必须走 `INTERNAL_ERROR`，不许把异常丢给模型。"""
    rig = build_rig(monkeypatch, tmp_path)
    monkeypatch.setattr(envelope, "ok_envelope", lambda **_kwargs: {"ok": object()})
    payload = await rig.tool()
    assert payload["error"] == envelope.ERR_INTERNAL_ERROR


# --------------------------------------------------------------------------- #
# 评审 F2 / F4：预算截断的计数不变式与端到端降级（t18 落地的口径）
# --------------------------------------------------------------------------- #
class _ScriptedClock:
    """按脚本返回 `time.monotonic()` 的假时钟：预算类用例靠它，不 sleep。"""

    def __init__(self, *values: float) -> None:
        self._values = list(values)
        self.calls = 0

    def monotonic(self) -> float:
        self.calls += 1
        if len(self._values) > 1:
            return self._values.pop(0)
        return self._values[0]


class _FailOnNthCall(FakeChatProvider):
    """第 `fail_at` 次 `text_chat` 抛错，其余正常返回（造"单帧失败"用）。"""

    def __init__(self, *, text: str, fail_at: int) -> None:
        super().__init__(text=text)
        self._fail_at = fail_at
        self._seen = 0

    async def text_chat(self, *args, **kwargs):  # type: ignore[override]
        self._seen += 1
        if self._seen == self._fail_at:
            self.calls.record("text_chat", **kwargs)
            raise RuntimeError(f"frame #{self._fail_at} failed")
        return await super().text_chat(*args, **kwargs)


@pytest.mark.asyncio
async def test_frames_truncated_kept_matches_frames_length(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """评审 F2 不变式：`frames_truncated:<kept>/<want>` 的 **kept 必须等于 `len(frames)`**。

    3 帧场景：第 1 帧成功、第 2 帧描述失败、第 3 帧前预算到期 ⇒ `frames_truncated:1/3` 且
    `len(frames)==1`。t18 之前这里按"循环下标"记成 `2/3`，与信封里的帧数对不上 ——
    这条用例就是防它回潮（同一帧在两个地方必须同一个数）。
    """
    provider = _FailOnNthCall(text="第一帧描述", fail_at=2)
    notes: list[str] = []
    clock = _ScriptedClock(10.0, 10.0, 200.0)  # 第 3 帧前越过 deadline=100
    monkeypatch.setattr(analyze_mod, "time", SimpleNamespace(monotonic=clock.monotonic))

    described = await analyze_mod.describe_frames(
        provider,
        [(1.0, str(tmp_path / "a.jpg")), (2.0, str(tmp_path / "b.jpg")), (3.0, str(tmp_path / "c.jpg"))],
        focus="",
        deadline=100.0,
        notes=notes,
        settings=settings.Settings(),
    )

    assert len(described) == 1
    assert [frame["at_s"] for frame in described] == [1.0]
    truncated = [note for note in notes if note.startswith("frames_truncated:")]
    assert truncated == ["frames_truncated:1/3"], notes
    kept = int(truncated[0].split(":")[1].split("/")[0])
    assert kept == len(described), "kept 必须与 frames 长度一致（t18 的不变式）"
    assert "frame_desc_failed:2.00" in notes


@pytest.mark.asyncio
async def test_budget_exhausted_before_first_frame_keeps_transcript(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """评审 F4：**首帧前**预算就到期 ⇒ `ok:true`、转写仍在、`frames=[]`、有 `frames_truncated`。

    与 t13 那条（慢音频调用吃掉预算、抽帧层就截断）互补：这条走的是**描述层**截断
    （抽帧已完成、第一帧描述前预算就没了）。两者都不许把整次调用判死。
    """
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        frames=[(2.5, "unused")],
        audio=audio_chat(text="描述层截断也要保住转写"),
        chat=FakeChatProvider(text="这段描述不该被调用"),
    )
    # v0.3 的 `monotonic()` 序列（前 4 次都还没到期，第 5 次正好到期）：
    #   ① `analyze` 的 started；② `transcribe` 的剩余预算；③ 抽帧回退的 25 s 闸；
    #   ④ `_extract_frames_budgeted` 的剩余预算；⑤ `describe_frames` 首帧前 ⇒ 描述层截断。
    clock = _ScriptedClock(0.0, 0.0, 0.0, 0.0, 100.0)
    monkeypatch.setattr(analyze_mod, "time", SimpleNamespace(monotonic=clock.monotonic))

    payload = await rig.run(include_frames=True)

    assert payload["ok"] is True
    assert list(payload) == list(envelope.OK_KEYS)
    assert payload["transcript"][0]["text"] == "描述层截断也要保住转写"
    assert payload["frames"] == []
    # 描述层的 `want` 是**已抽出的帧数**（1），不是配置里的 `max_frames` —— 两种截断的计数口径不同
    assert "frames_truncated:0/1" in payload["notes"]
    assert rig.prov_calls["vision"] == 1, "抽帧成功时视觉 provider 会被解析（截断发生在描述阶段）"
    assert not rig.chat.images_seen(), "首帧前就没预算了，不该发起任何描述调用"


@pytest.mark.asyncio
async def test_six_failed_descriptions_with_full_budget_still_vision_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """评审 F4 的反向面：预算充足但 6 次描述调用**全失败** ⇒ 仍 `VISION_FAILED`。

    预算降级只放过"没时间"，不放过"模型挂了"——否则用户会拿到一个 `ok:true` 的空帧信封。
    """
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        frames=[(float(index * 5 + 5), "unused") for index in range(6)],
        audio=audio_chat(text="转写仍在，但画面一帧没拿到"),
        chat=FakeChatProvider(fail_with=RuntimeError("upstream 500")),
    )
    payload = await rig.tool(include_frames=True)

    assert list(payload) == list(envelope.FAIL_KEYS)
    assert payload["error"] == envelope.ERR_VISION_FAILED
    assert rig.chat.calls.named("text_chat"), "必须真发起过调用，才谈得上「全失败」"


# --------------------------------------------------------------------------- #
# 评审 F6：`include_frames` 的模型入参归一（字符串/数字都要能理解）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", ["false", "no", "0", "  FALSE  ", "False", "", "off", False])
@pytest.mark.asyncio
async def test_include_frames_false_like_values_skip_vision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: object
) -> None:
    """评审 F6：模型给 `"false"` / `"no"` / `0` ⇒ 按 False 理解，**零视觉调用**。

    模型经常把布尔参数写成字符串；`bool("false") is True` 是 Python 的经典坑，
    不归一就会把"只要转写"变成"抽 6 帧 + 6 次视觉调用"（成本与延迟都翻倍）。
    """
    rig = build_rig(monkeypatch, tmp_path, kind="video", frames=[(2.5, "unused")])
    payload = await rig.tool(include_frames=value)  # type: ignore[arg-type]

    assert payload["ok"] is True
    assert payload["frames"] == []
    assert rig.prov_calls["vision"] == 0
    assert not rig.chat.images_seen()
    assert rig.media_calls["extract_frames"] == 0


@pytest.mark.parametrize("value", ["true", "yes", "1", "  True  ", "TRUE", "on", True])
@pytest.mark.asyncio
async def test_include_frames_true_like_values_trigger_vision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: object
) -> None:
    """评审 F6 的对称面：`"true"` / `"yes"` / `1` ⇒ 按 True 理解，抽帧 + 描述各一次。"""
    rig = build_rig(monkeypatch, tmp_path, kind="video", frames=[(2.5, "unused")])
    payload = await rig.tool(include_frames=value)  # type: ignore[arg-type]

    assert payload["ok"] is True
    assert len(payload["frames"]) == 1
    assert rig.prov_calls["vision"] == 1
    assert len(rig.chat.images_seen()) == 1


@pytest.mark.parametrize("value", ["maybe", 2.5, 1.0, 2, -1, 0, 1])
@pytest.mark.asyncio
async def test_include_frames_invalid_value_falls_back_to_auto_with_one_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: object
) -> None:
    """白名单之外的一切 ⇒ 按 **auto** + **恰好一条**告警（v0.3 与 v0.2 的**反向**）。

    白名单只有三种形态：`bool`、大小写不敏感的 `"true/yes/on/1"` 与
    `"false/no/off/0/\"\""`。**整数 `0`/`1` 不再当布尔**（`bool` 是 `int` 的子类，
    但参数类型在 v0.3 已从 boolean 改成 string —— 收到裸整数说明调用方没按 schema 发值）。

    为什么默认 `auto` 而**不再**默认 `false`（SPEC-v0.3 §7.1）：v0.3 的主路径本身就是
    "看视频"。按 `auto` 走只多一次原生调用；按 `false` 会把误发值悄悄降级成"只转写"，
    而用户以为画面也被看了 —— 那是**假成功**，比多花一点钱糟得多。

    用例证明"没有静默变成 false"：`false` 会记 `video_frames_forbidden`（用户的显式成本
    约束），`auto` 记的是另一条 —— 两条 note 互斥，改回 `false` 立刻红。

    > 评审 **F7** 之后 `auto` 的形态变了：`auto` 且**未配视频模型**时不再抽帧（成本回归），
    > 所以这里的判别信号从"frames 非空"换成"note 不是 forbidden"（语义没变、断言更准）。
    """
    log = RecordingLogger()
    monkeypatch.setattr(main, "LOG", log)
    rig = build_rig(monkeypatch, tmp_path, kind="video", frames=[(2.5, "unused")])

    payload = await rig.tool(include_frames=value)  # type: ignore[arg-type]

    assert payload["ok"] is True
    assert "video_frames_forbidden" not in payload["notes"], "非法值不许按 false 处理"
    assert "video_unavailable_no_provider" in payload["notes"], "auto + 无视频模型 ⇒ 只转写（F7）"
    assert rig.prov_calls["vision"] == 0
    warnings = [message for level, message in log.records if level == "WARNING" and "include_frames" in message]
    assert len(warnings) == 1, warnings


@pytest.mark.asyncio
async def test_include_frames_none_is_auto_without_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`None` ⇒ `auto`，且**不告警**：它是"调用方没传值"的合法形态（不是坏值）。

    核心不校验工具参数类型，"没传"可能以 `None` 到达；把它当成坏值刷告警会让日志噪声
    掩盖真正的坏值（`2.5` / `maybe` 那些）。
    """
    log = RecordingLogger()
    monkeypatch.setattr(main, "LOG", log)
    rig = build_rig(monkeypatch, tmp_path, kind="video", frames=[(2.5, "unused")])

    payload = await rig.tool(include_frames=None)  # type: ignore[arg-type]

    assert payload["ok"] is True
    # 评审 F7 之后 `auto` + 无视频模型 ⇒ 只转写（判别信号从"frames 非空"换成 note）
    assert "video_unavailable_no_provider" in payload["notes"]
    assert "video_frames_forbidden" not in payload["notes"]
    assert not [m for level, m in log.records if level == "WARNING" and "include_frames" in m]


# --------------------------------------------------------------------------- #
# SPEC-v0.2 §2.1：音频阶段的**调用形状**（多模态 chat，不是 STTProvider）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_transcribe_call_shape_is_text_chat_with_one_audio_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """冻结形状：`text_chat(prompt=…, audio_urls=[本地 wav])`，**不传** image/system/contexts。

    这是 v0.2 的产品裁决 ①：音频不再依赖 `STTProvider`，而是让多模态对话模型"听"。
    调用形状本身就是契约 —— `image_urls` 一并传会让模型看到空图像列表，
    `contexts` / `system_prompt` 则会把"一次独立转写请求"变成一段对话。
    """
    rig = build_rig(monkeypatch, tmp_path, kind="audio")
    await rig.run()

    calls = rig.audio.calls.named("text_chat")
    assert len(calls) == 1, f"音轨阶段只该调一次：{calls}"
    kwargs = calls[0].kwargs
    assert list(kwargs.get("audio_urls") or []) == [str(rig.media_path)], "必须是本地 wav 路径"
    assert kwargs.get("image_urls") == [], "音轨阶段不许带图像"
    assert kwargs.get("session_id") is None
    for forbidden in ("contexts", "system_prompt", "func_tool", "tools"):
        assert forbidden not in kwargs, f"不该传 {forbidden}"
    assert kwargs.get("prompt")


@pytest.mark.asyncio
async def test_video_audio_urls_point_at_the_extracted_wav(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """视频音轨：交给模型的是**抽出来的 16k wav**，不是原始 mp4（v0.1 的 `extract_wav` 语义）。"""
    rig = build_rig(monkeypatch, tmp_path, kind="video")
    await rig.run()

    audio_urls = rig.audio.audios_seen()
    assert len(audio_urls) == 1
    assert audio_urls[0].endswith("audio.wav"), audio_urls
    assert audio_urls[0] != str(rig.media_path)


@pytest.mark.asyncio
async def test_only_the_audio_provider_is_used_for_transcription(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """转写只走音频 provider；摘要走对话 provider —— 两个角色不许混用同一个解析入口。"""
    rig = build_rig(monkeypatch, tmp_path, kind="audio")
    await rig.run()

    assert rig.prov_calls["audio"] == 1
    assert rig.prov_calls["chat"] == 1, "摘要阶段单独解析对话 provider"
    assert len(rig.audio.calls.named("text_chat")) == 1
    assert len(rig.chat.calls.named("text_chat")) == 1
    assert rig.audio.audios_seen() and not rig.chat.audios_seen(), "摘要不许带音频"


def test_stt_provider_symbols_are_gone_from_the_provider_layer() -> None:
    """静态半条：`providers` 不再导出 STT 面（`resolve_stt_provider` / `STTProvider`）。

    行为半条在 `tests/test_providers.py`（`test_stt_resolver_is_gone_in_v02`）；这里再钉一次
    是因为**主链路测试**也在 import `providers` —— 谁把 STT 链路加回来都会两处同红。
    """
    assert not hasattr(providers, "resolve_stt_provider")
    assert not hasattr(providers, "STTProvider")
    assert hasattr(providers, "resolve_audio_provider")


@pytest.mark.asyncio
async def test_configured_text_only_audio_provider_is_capability_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """真实解析器（不注入错误）：配了 id 但那个 provider 没有 audio 模态 ⇒ 能力不匹配。

    这是"降级错误码"的正面证据：走真 `providers.resolve_audio_provider` + `FakeContext`，
    拿到的是 `PROVIDER_CAPABILITY_MISMATCH`（配错了），而不是 `AUDIO_UNAVAILABLE`（没配）。
    """
    text_only = FakeChatProvider(modalities=["text"], model_name="text-only")
    ctx = FakeContext(by_id={"text-only": text_only}, using_provider=audio_chat())
    rig = build_rig(
        monkeypatch,
        tmp_path,
        context=ctx,
        cfg=settings.Settings(audio_provider_id="text-only"),
        providers_real=True,
    )
    payload = await rig.tool()
    assert payload == {
        "ok": False,
        "error": envelope.ERR_PROVIDER_CAPABILITY_MISMATCH,
        "hint": envelope.HINTS[envelope.ERR_PROVIDER_CAPABILITY_MISMATCH],
    }
    assert not text_only.calls.named("text_chat"), "能力不符就不许发请求（零 token）"


@pytest.mark.asyncio
async def test_configured_stt_provider_id_in_audio_slot_is_capability_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """v0.1 的旧配置形态（音频位存的是 STT provider id）⇒ 明确的能力不匹配。

    这条是迁移脚本"搬值必须带能力校验"的行为依据：搬过去只会让每次调用都撞这个错。
    """
    from _fakes import FakeSTTProvider

    stt = FakeSTTProvider(provider_id="mimo_stt")
    ctx = FakeContext(by_id={"mimo_stt": stt}, using_provider=audio_chat())
    rig = build_rig(
        monkeypatch,
        tmp_path,
        context=ctx,
        cfg=settings.Settings(audio_provider_id="mimo_stt"),
        providers_real=True,
    )
    payload = await rig.tool()
    assert payload["error"] == envelope.ERR_PROVIDER_CAPABILITY_MISMATCH
    assert not stt.calls.named("get_text"), "STT 面一次都不该被调用"


# --------------------------------------------------------------------------- #
# v0.4 GIF 分支：`[3.5] confirm_gif` → `[4-pre]` 时长闸 → 归一 → 既有七跳（SPEC-v0.4 §5.12）
#
# 与 `tests/test_gif.py` 的分工：那边测**媒体层**（魔数/候选/四道闸/argv/真 ffmpeg），
# 这里测**主链路里的接线**——顺序、跳过 `classify`、`kind`、notes、缓存键取源字节。
# 归一的三个外部依赖（源时长 / 归一 / 帧数）在这里打桩 ⇒ 这些用例不依赖 ffmpeg。
# --------------------------------------------------------------------------- #
def _gif_event(tmp_path: Path, name: str = "sticker.gif", **kwargs: object):
    """造一条 `Image`(GIF) 事件 + 源文件路径（`_gif.write_gif`，零依赖手工字节）。"""
    from _gif import write_gif

    path = write_gif(tmp_path / name, **kwargs)
    return make_event(current=[make_image(path)]), path


def _stub_gif_convert(
    monkeypatch: pytest.MonkeyPatch,
    *,
    source_duration: float = 8.0,
    frames: int = 8,
    product: bytes | None = None,
    error: BaseException | None = None,
    product_duration: float = 8.0,
) -> dict[str, object]:
    """把 GIF 归一的外围依赖换成可注入假身：源时长 / 归一 / 产物。"""
    calls: dict[str, object] = {"duration": 0, "normalize": 0, "source": ""}

    async def _duration(path: str) -> float:
        calls["duration"] = int(calls["duration"]) + 1
        calls["duration_path"] = path
        return float(source_duration)

    async def _normalize(source: str, *, workdir: str, max_media_bytes: int):
        calls["normalize"] = int(calls["normalize"]) + 1
        calls["source"] = source
        calls["max_media_bytes"] = max_media_bytes
        if error is not None:
            raise error
        out = Path(workdir) / "gif.mp4"
        out.write_bytes(
            product
            if product is not None
            else b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64
        )
        return media.GifResult(
            path=str(out),
            size=out.stat().st_size,
            source_size=Path(source).stat().st_size,
            duration_s=float(product_duration),
            frames=frames,
        )

    monkeypatch.setattr(media, "probe_gif_duration_s", _duration)
    monkeypatch.setattr(media, "normalize_gif", _normalize)
    return calls


@pytest.mark.asyncio
async def test_gif_branch_reports_kind_video_with_three_notes_and_no_classify(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§5.10 / §5.12：`kind` 仍取 `video`、notes 三条、**跳过 `classify`**、零抽帧零转写。"""
    from _fakes import native_video

    event, gif_path = _gif_event(tmp_path, width=32, height=32, frames=4)
    rig = build_rig(
        monkeypatch, tmp_path, kind="video", event=event, native=native_video()
    )
    calls = _stub_gif_convert(monkeypatch, frames=4)

    payload = await rig.run()

    assert payload["ok"] is True
    assert payload["kind"] == "video"
    assert payload["frames"] == [] and payload["transcript"] == []
    notes = list(payload["notes"])
    assert "source:gif" in notes
    converted = [note for note in notes if note.startswith("gif_converted:")]
    assert len(converted) == 1
    size_in, _, size_out = converted[0].partition(":")[2].partition("->")
    assert size_in == str(gif_path.stat().st_size)
    assert size_out == str(len(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64))
    assert "gif_static" not in notes
    assert rig.media_calls["classify"] == 0, "GIF 必须跳过 classify（否则 Image 会被判成 kind=audio）"
    assert calls["normalize"] == 1
    assert calls["source"] == str(gif_path)  # 归一拿到的是**源**文件


@pytest.mark.asyncio
async def test_gif_branch_confirms_the_magic_before_normalising(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§5.12 的顺序冻结：`[3.5] confirm_gif` 必须早于 `[4-pre] normalize_gif`。

    顺序错了也不会"看起来错"：先归一后确认 ⇒ 改名党（`.gif` + PNG 字节）会被无谓地
    喂给 ffmpeg，且失败码会从 `MEDIA_NOT_FOUND` 漂成 `GIF_CONVERT_FAILED`。
    """
    from _fakes import native_video

    event, _path = _gif_event(tmp_path, width=32, height=32, frames=2)
    rig = build_rig(
        monkeypatch, tmp_path, kind="video", event=event, native=native_video()
    )
    order: list[str] = []
    real_confirm = media.confirm_gif

    def _confirm(handle, path):  # type: ignore[no-untyped-def]
        order.append("confirm")
        return real_confirm(handle, path)

    monkeypatch.setattr(media, "confirm_gif", _confirm)
    calls = _stub_gif_convert(monkeypatch)
    real_normalize = media.normalize_gif

    async def _normalize(source: str, *, workdir: str, max_media_bytes: int):
        order.append("normalize")
        return await real_normalize(
            source, workdir=workdir, max_media_bytes=max_media_bytes
        )

    monkeypatch.setattr(media, "normalize_gif", _normalize)

    await rig.run()

    assert order == ["confirm", "normalize"]
    assert calls["normalize"] == 1


@pytest.mark.asyncio
async def test_gif_source_duration_gate_rejects_before_conversion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§5.7 闸②：**源**时长超 `limits.max_media_seconds` ⇒ `MEDIA_TOO_LONG`，一次都不转。"""
    from _fakes import native_video

    event, _path = _gif_event(tmp_path, width=32, height=32, frames=2)
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        event=event,
        native=native_video(),
        cfg=settings.Settings(max_media_seconds=5, vision_provider_id="fake-chat"),
    )
    calls = _stub_gif_convert(monkeypatch, source_duration=9.5)

    with pytest.raises(envelope.ToolError) as exc_info:
        await rig.run()
    assert exc_info.value.code == envelope.ERR_MEDIA_TOO_LONG
    assert "gif duration_s=9.5" in exc_info.value.log_detail
    assert calls["normalize"] == 0, "时长闸在归一**之前**（闸②）"


@pytest.mark.asyncio
async def test_gif_conversion_failure_is_a_delivered_failure_envelope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§5.9：归一失败 ⇒ `GIF_CONVERT_FAILED`，**经后台投递一条失败消息**（不是静默）。"""
    from _fakes import native_video

    event, _path = _gif_event(tmp_path, width=32, height=32, frames=2)
    rig = build_rig(
        monkeypatch, tmp_path, kind="video", event=event, native=native_video()
    )
    _stub_gif_convert(
        monkeypatch,
        error=envelope.ToolError(
            envelope.ERR_GIF_CONVERT_FAILED, log_detail="ffmpeg rc=187"
        ),
    )

    payload = await rig.tool()

    assert list(payload) == list(envelope.FAIL_KEYS)
    assert payload["error"] == envelope.ERR_GIF_CONVERT_FAILED
    # 后台那条用户消息也必须是失败文案（`render` 的失败形态）
    from astrbot.api.message_components import Plain

    texts = [
        comp.text
        for call in rig.context.calls.named("send_message")
        for comp in call.args[1].chain
        if isinstance(comp, Plain)
    ]
    assert len(texts) == 1
    assert texts[0].startswith("媒体分析失败：GIF_CONVERT_FAILED。")


@pytest.mark.asyncio
async def test_gif_cache_key_uses_source_bytes_on_both_sides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§5.11：缓存 `digest` 取**源**字节 —— 换产物**仍命中**、换源**必 miss**。

    两侧都断言才算钉住（只测一侧的话，"拿产物算 digest"也能让某一侧碰巧过）。
    """
    from _fakes import native_video
    from _gif import gif_bytes

    real_digest = media.content_digest  # build_rig 会把 content_digest 桩掉 ⇒ 先抓真身
    event, gif_path = _gif_event(tmp_path, width=32, height=32, frames=2)
    rig = build_rig(
        monkeypatch, tmp_path, kind="video", event=event, native=native_video()
    )
    calls = _stub_gif_convert(monkeypatch)
    seen: list[str] = []

    async def _digest(path: str) -> str:
        seen.append(str(path))
        return await real_digest(str(path))

    monkeypatch.setattr(media, "content_digest", _digest)

    first = await rig.run()
    assert first["cached"] is False
    assert seen == [str(gif_path)], "digest 只应针对**源**算一次（不是 gif.mp4）"

    # ① 同一份源字节、**换掉产物字节**（重新打桩归一）⇒ 键不变 ⇒ 命中
    _stub_gif_convert(monkeypatch, product=b"\x00\x00\x00\x18ftypisom" + b"\xff" * 16)
    second = await rig.run()
    assert second["cached"] is True
    assert second["summary"] == first["summary"]

    # ② 换掉源字节 ⇒ digest 变 ⇒ 必 miss
    gif_path.write_bytes(gif_bytes(width=40, height=40, frames=3))
    third = await rig.run()
    assert third["cached"] is False
    assert seen[-1] == str(gif_path)


@pytest.mark.asyncio
async def test_file_without_gif_suffix_is_promoted_by_magic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """v0.4.1 §5.4 补漏：`File` 没有 `.gif` 后缀，但内容魔数是 GIF ⇒ 也要转正。

    旧行为只认后缀 ⇒ 群文件里的无后缀 GIF 走 v0.3 梯子、把原始字节贴 `data:video/mp4`
    直送。现在 `[3.5]` 对 `File` 一律验魔数；后缀只决定"要不要先当候选"。
    """
    from _fakes import native_video
    from _gif import write_gif

    path = write_gif(tmp_path / "sticker.bin", width=32, height=32, frames=1, delay_cs=4)
    event = make_event(current=[make_file("sticker.bin", file=str(path))])
    rig = build_rig(
        monkeypatch, tmp_path, kind="video", event=event, native=native_video()
    )
    calls = _stub_gif_convert(monkeypatch, frames=1, product_duration=1.0)

    payload = await rig.run()

    assert payload["ok"] is True
    assert payload["kind"] == "video"
    notes = list(payload["notes"])
    assert "source:gif" in notes
    assert "gif_static" in notes
    assert calls["normalize"] == 1
    assert rig.media_calls["classify"] == 0, "GIF 内容必须跳过 classify"


@pytest.mark.asyncio
async def test_file_with_png_content_is_rejected_as_unsupported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """v0.4.1：`File` 里是图片（PNG/APNG/WebP/JPEG）⇒ 显式拒绝，不再靠"ffprobe 没时长"。

    APNG / 动画 WebP 在 ffprobe 里也是 `codec_type=video` 且可能带时长；不显式拦，
    原始图片字节会被贴 `data:video/mp4` 送进原生链。
    """
    from _fakes import native_video
    from _gif import png_bytes

    path = tmp_path / "photo.bin"
    path.write_bytes(png_bytes())
    event = make_event(current=[make_file("photo.bin", file=str(path))])
    rig = build_rig(
        monkeypatch, tmp_path, kind="video", event=event, native=native_video()
    )

    with pytest.raises(envelope.ToolError) as exc_info:
        await rig.run()

    assert exc_info.value.code == envelope.ERR_UNSUPPORTED_MEDIA_TYPE
    assert "image" in exc_info.value.log_detail
    assert rig.media_calls["classify"] == 0
    assert rig.prov_calls.get("vision", 0) == 0


@pytest.mark.asyncio
async def test_image_gif_and_file_gif_with_same_bytes_share_one_cache_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§5.11-1：同一张 GIF 以 `Image` / 群文件两种形态到达 ⇒ 同一键 ⇒ 共享缓存。"""
    from _fakes import native_video

    real_digest = media.content_digest  # build_rig 会把 content_digest 桩成常量 ⇒ 先抓真身
    event, gif_path = _gif_event(tmp_path, width=32, height=32, frames=3)
    rig = build_rig(
        monkeypatch, tmp_path, kind="video", event=event, native=native_video()
    )
    _stub_gif_convert(monkeypatch)
    monkeypatch.setattr(media, "content_digest", real_digest)

    image_payload = await rig.run()
    assert image_payload["cached"] is False

    second_copy = tmp_path / "same-bytes.gif"
    second_copy.write_bytes(gif_path.read_bytes())  # 字节相同、路径不同、形态不同
    rig.event = make_event(current=[make_file("same-bytes.gif", file=str(second_copy))])
    file_payload = await rig.run()

    assert file_payload["cached"] is True
    assert file_payload["summary"] == image_payload["summary"]
    # 命中时 notes 是**缓存里那一份**（信封整体入缓存，`source:file` 属本次调用、不进缓存）
    assert "source:gif" in file_payload["notes"]
    assert "source:file" not in file_payload["notes"]


@pytest.mark.asyncio
async def test_remote_gif_image_is_rejected_after_download_when_magic_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§5.4：远程 `Image` + `.gif` 后缀 ⇒ 受理；下载后魔数不符 ⇒ `MEDIA_NOT_FOUND`。

    下载本身用桩（`resolve_media_path` 返回一份 PNG 落地文件）—— 测试不联网，
    但走的是**同一条** `[3] resolve → [3.5] confirm_gif` 顺序。
    """
    from _gif import png_bytes
    from _fakes import native_video

    landed = tmp_path / "downloaded.gif"
    landed.write_bytes(png_bytes())
    event = make_event(
        current=[make_image("", url="https://x.invalid/sticker.gif?sig=1")]
    )
    rig = build_rig(
        monkeypatch, tmp_path, kind="video", event=event, native=native_video()
    )
    _stub_gif_convert(monkeypatch)

    async def _resolve(_handle, *, workdir: str, max_media_bytes: int) -> str:
        return str(landed)

    monkeypatch.setattr(media, "resolve_media_path", _resolve)

    with pytest.raises(envelope.ToolError) as exc_info:
        await rig.run()
    assert exc_info.value.code == envelope.ERR_MEDIA_NOT_FOUND
    assert "普通图片不进链" in exc_info.value.log_detail


@pytest.mark.asyncio
async def test_gif_static_note_tracks_the_source_frame_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§5.10：`gif_static` 只在**源** GIF ≤ 1 帧时出现（多帧不许误标）。"""
    from _fakes import native_video
    from _gif import gif_bytes

    real_digest = media.content_digest  # 同上：要让 digest 真的跟着源字节走
    event, gif_path = _gif_event(tmp_path, width=32, height=32, frames=1, delay_cs=10)
    rig = build_rig(
        monkeypatch, tmp_path, kind="video", event=event, native=native_video()
    )
    _stub_gif_convert(monkeypatch, frames=1)
    monkeypatch.setattr(media, "content_digest", real_digest)

    single = await rig.run()
    assert "gif_static" in single["notes"]

    gif_path.write_bytes(gif_bytes(width=32, height=32, frames=5))  # 源字节变 ⇒ 新键
    _stub_gif_convert(monkeypatch, frames=5)
    animated = await rig.run()
    assert animated["cached"] is False
    assert "gif_static" not in animated["notes"]


# --------------------------------------------------------------------------- #
# v0.5.1 D1：音轨超时**配置化 + 恰好一次重试**（SPEC-v0.5.1 §4 / §9.4 的 G4 六格）
#
# 六格的分工（每一格都能**单独**变红）：
#   ⑮ 超时→重试→仍超时：`attempts==2`，`log_detail` 双字段；
#   ⑯ 预算不足：`retry_skipped=budget` 且 `attempts==1`；
#   ⑰ 重试成功：notes 与"一次就成"**逐字相同**（缓存中立）；
#   ⑱ `soft_timeout`：不重试（重试会吃掉抽帧预留）；
#   ⑲ provider 抛错：不重试；
#   ⑳ 总耗时 ≤ `DEADLINE_S`（把 `DEADLINE_S` 缩到 1.2 s 跑）。
#
# 为什么直接测 `analyze.transcribe` 而不是整条链：重试是**这一跳**的性质，整条链会把
# 音轨预算与抽帧预留、摘要预算混在一起（那样变红时说不清是谁的问题）。整条链的接线由
# 下面 `test_audio_cap_comes_from_the_config_key` + 既有 `budgeted_timeout` 用例守。
# --------------------------------------------------------------------------- #
class _AudioSpy:
    """只实现 `transcribe` 用到那两件事的 provider 桩（`text_chat` + `completion_text`）。

    `delays` 是**逐次**延迟（第 N 次用第 N 个，用尽后重复最后一个）⇒ 能造
    "第一次超时、第二次秒回"这种序列，而不用真等 60 s。
    """

    def __init__(
        self,
        *,
        delays: tuple[float, ...] = (0.0,),
        text: str = "合成转写",
        fail_at: int | None = None,
    ) -> None:
        self.delays = tuple(delays) or (0.0,)
        self.text = text
        self.fail_at = fail_at
        self.calls = 0

    async def text_chat(self, *, prompt=None, audio_urls=None):  # noqa: ANN001
        self.calls += 1
        if self.fail_at is not None and self.calls == self.fail_at:
            raise RuntimeError("quota exceeded")
        delay = self.delays[min(self.calls - 1, len(self.delays) - 1)]
        if delay:
            await asyncio.sleep(delay)
        return SimpleNamespace(completion_text=self.text)


async def _run_transcribe(
    monkeypatch: pytest.MonkeyPatch,
    provider: _AudioSpy,
    *,
    cap: float,
    deadline_after: float = 100.0,
    reserve_s: float = 0.0,
    soft_timeout: bool = False,
) -> tuple[str, list[str], RecordingLogger]:
    """跑一次 `transcribe`，返回 `(文本, notes, 日志桩)`（日志桩用于断言"只进日志"）。"""
    logger = RecordingLogger()
    monkeypatch.setattr(analyze_mod, "LOG", logger)
    notes: list[str] = []
    text = await analyze_mod.transcribe(
        provider,
        "/tmp/never-read.wav",
        "把这段语音转写出来",
        deadline=time.monotonic() + deadline_after,
        notes=notes,
        reserve_s=reserve_s,
        soft_timeout=soft_timeout,
        cap=cap,
    )
    return text, notes, logger


def _info_lines(logger: RecordingLogger) -> list[str]:
    return [message for level, message in logger.records if level == "INFO"]


@pytest.mark.asyncio
async def test_audio_hard_timeout_retries_exactly_once_then_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⑮ 两次都超时 ⇒ `AUDIO_FAILED`；`attempts==2` 且 `log_detail` 能分辨两趟。

    `log_detail` 必须仍以 `text_chat timeout` 起头（运维既有 grep 句式
    `media_analyze rejected: code=AUDIO_FAILED detail=text_chat timeout …` 不许失效），
    后面才带 `first_timeout=` / `retry_timeout=` 两个字段。
    """
    provider = _AudioSpy(delays=(5.0,))
    logger = RecordingLogger()
    monkeypatch.setattr(analyze_mod, "LOG", logger)
    notes: list[str] = []

    with pytest.raises(envelope.ToolError) as exc_info:
        await analyze_mod.transcribe(
            provider,
            "/tmp/never-read.wav",
            "转写",
            deadline=time.monotonic() + 100.0,
            notes=notes,
            cap=0.05,
        )

    assert exc_info.value.code == envelope.ERR_AUDIO_FAILED
    assert provider.calls == 2, "恰好一次重试 ⇒ 共 2 次尝试"
    detail = exc_info.value.log_detail
    assert detail.startswith("text_chat timeout"), detail
    assert "first_timeout=" in detail and "retry_timeout=" in detail, detail
    assert "retry_skipped" not in detail, "这是「重试了仍失败」，不是「预算不足」"
    assert notes == [], "重试标记只准进日志（notes 缓存中立）"
    assert any("audio retry attempt=2/2" in line for line in _info_lines(logger))


@pytest.mark.asyncio
async def test_audio_retry_skipped_when_budget_is_below_the_minimum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⑯ 首次超时但剩余 < `AUDIO_RETRY_MIN_S` ⇒ **不重试**，`retry_skipped=budget`。

    为什么要有这条闸：起一次注定超时的调用只会白占预算、把 `DEADLINE_S` 吃满。
    """
    provider = _AudioSpy(delays=(5.0,))
    notes: list[str] = []
    monkeypatch.setattr(analyze_mod, "LOG", RecordingLogger())

    with pytest.raises(envelope.ToolError) as exc_info:
        await analyze_mod.transcribe(
            provider,
            "/tmp/never-read.wav",
            "转写",
            deadline=time.monotonic() + 0.5,  # 首趟就会把预算吃光
            notes=notes,
            cap=0.1,
        )

    assert exc_info.value.code == envelope.ERR_AUDIO_FAILED
    assert provider.calls == 1, "预算不足 ⇒ 只有 1 次尝试"
    detail = exc_info.value.log_detail
    assert detail.startswith("text_chat timeout")
    assert "first_timeout=" in detail and "retry_skipped=budget" in detail, detail
    assert "retry_timeout=" not in detail


@pytest.mark.asyncio
async def test_audio_retry_success_keeps_notes_verbatim_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⑰ 首次超时 → 重试成功：拿到正文，且 notes 与"一次就成"**逐字相同**。

    §6.5 缓存中立的机械判据：信封（含 notes）会整体入缓存 ⇒ 任何"第几次/重试过"的标记
    都会被复用给别的调用；重试信息**只准**进日志。
    """
    one_shot = _AudioSpy(delays=(0.0,), text="同一段转写")
    one_text, one_notes, _ = await _run_transcribe(monkeypatch, one_shot, cap=0.5)

    retried = _AudioSpy(delays=(5.0, 0.0), text="同一段转写")
    text, notes, logger = await _run_transcribe(monkeypatch, retried, cap=0.05)

    assert retried.calls == 2, "第一次超时后才重试"
    assert text == one_text == "同一段转写"
    assert notes == one_notes == [], "重试过的 notes 必须与一次就成逐字相同"
    lines = _info_lines(logger)
    assert any("audio retry attempt=2/2" in line for line in lines)
    assert any("audio retry ok attempt=2/2" in line for line in lines)


@pytest.mark.asyncio
async def test_audio_soft_timeout_never_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """⑱ `soft_timeout=True`（本轮要画面）⇒ 超时只记 `audio_timeout`，**不重试**。

    J4 的理由：整条链只给抽帧留 25 s（`FRAMES_MIN_BUDGET_S`），重试会把它吃光，
    于是"主打回退在最该生效时 `video_skip_budget`"（评审 F6 修过的坑）。
    """
    provider = _AudioSpy(delays=(5.0,))
    text, notes, _logger = await _run_transcribe(
        monkeypatch, provider, cap=0.05, soft_timeout=True
    )

    assert text == ""
    assert notes == ["audio_timeout"]
    assert provider.calls == 1, "软超时不许重试（会吃掉抽帧预留）"


@pytest.mark.asyncio
async def test_audio_provider_error_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⑲ provider 抛错（非超时）⇒ 立即 `AUDIO_FAILED`，**不重试**、不带 timeout 字段。

    非超时异常意味着模型侧真出问题（4xx/5xx/内容过滤），重试只是再花一次钱。
    """
    provider = _AudioSpy(fail_at=1)
    notes: list[str] = []
    monkeypatch.setattr(analyze_mod, "LOG", RecordingLogger())

    with pytest.raises(envelope.ToolError) as exc_info:
        await analyze_mod.transcribe(
            provider,
            "/tmp/never-read.wav",
            "转写",
            deadline=time.monotonic() + 100.0,
            notes=notes,
            cap=1.0,
        )

    assert exc_info.value.code == envelope.ERR_AUDIO_FAILED
    assert provider.calls == 1, "抛错不重试"
    detail = exc_info.value.log_detail
    assert "first_timeout" not in detail and "retry_timeout" not in detail, detail
    assert detail.startswith("RuntimeError"), detail


@pytest.mark.asyncio
async def test_audio_total_duration_stays_within_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⑳ 含重试的总耗时 ≤ `DEADLINE_S`（把 `DEADLINE_S` 缩到 **1.2 s** 实跑）。

    预算不变式（§4.2）：`t1 = min(cap, R0 − r)`、`t2 = min(cap, R0 − t1 − r')`
    ⇒ 第二个 `min` 的上界就是 `R0 − t1` ⇒ `t1 + t2 ≤ R0`。这里用真实时钟验它：
    两次尝试都超时，总耗时不许越过 `DEADLINE_S`。
    """
    monkeypatch.setattr(analyze_mod, "DEADLINE_S", 1.2)
    # 把"起第 2 次至少要剩 10 s"按同一比例缩小，否则 1.2 s 预算下永远不会重试
    monkeypatch.setattr(analyze_mod, "AUDIO_RETRY_MIN_S", 0.2)
    monkeypatch.setattr(analyze_mod, "LOG", RecordingLogger())

    provider = _AudioSpy(delays=(30.0,))
    started = time.monotonic()
    notes: list[str] = []
    with pytest.raises(envelope.ToolError) as exc_info:
        await analyze_mod.transcribe(
            provider,
            "/tmp/never-read.wav",
            "转写",
            deadline=started + analyze_mod.DEADLINE_S,
            notes=notes,
            cap=0.6,
        )
    elapsed = time.monotonic() - started

    assert provider.calls == 2, "两个 0.6 s 的尝试都在预算内"
    assert elapsed <= analyze_mod.DEADLINE_S + 0.05, (
        f"总耗时 {elapsed:.3f}s 不得突破 DEADLINE_S={analyze_mod.DEADLINE_S}"
    )
    assert "first_timeout=" in exc_info.value.log_detail
    assert "retry_timeout=" in exc_info.value.log_detail


def test_audio_cap_comes_from_the_config_key() -> None:
    """⑳′ 上限**来自配置**（`limits.audio_timeout_seconds`），不是硬编码 60。

    这条把 D1 的"配置化"那一半钉住：`audio_cap_s()` 读的是 `Settings` 的字段，
    而默认值仍等于旧硬编码的 60 s（⇒ 存量行为不变，§9.2 的 E3 行）。
    """
    from astrbot_plugin_media_analysis import settings as settings_mod

    assert settings_mod.DEFAULT_AUDIO_TIMEOUT_SECONDS == 60
    assert analyze_mod.AUDIO_TIMEOUT_S == float(settings_mod.DEFAULT_AUDIO_TIMEOUT_SECONDS)
    assert analyze_mod.AUDIO_RETRY_MAX == 1, "恰好一次（§4.5：不加第 3 次、不加退避）"

    assert analyze_mod.audio_cap_s(settings_mod.Settings()) == 60.0
    assert analyze_mod.audio_cap_s(
        settings_mod.Settings(audio_timeout_seconds=95)
    ) == 95.0
    # 坏配置（0 / 取不到）不许把预算直接判死，退回默认值
    assert analyze_mod.audio_cap_s(settings_mod.Settings(audio_timeout_seconds=0)) == 60.0
    assert analyze_mod.audio_cap_s(SimpleNamespace()) == 60.0


# --------------------------------------------------------------------------- #
# v0.5.1 D2：唤醒轮短路（SPEC-v0.5.1 §5 / §9.4 的 G5 三格）
#
# 事故形状：核心的后台任务唤醒轮会**再给模型一轮**（带工具集），模型看不到"上一轮"的媒体
# ⇒ 这轮必然 `MEDIA_NOT_FOUND`，但那条 hint 是写给"用户忘了发媒体"的，于是模型开始劝用户
# 重发同一条消息。修法：识别合成事件 ⇒ **不进 `background.submit`**（不产生 task_id）、
# 回一条**专用 hint**、日志里留 `wake_round=1`。
# --------------------------------------------------------------------------- #
class _CronEvent(FakeEvent):
    """核心 `CronMessageEvent` 的形态（`cron/events.py:24-27`：`platform_meta.name="cron"`）。"""

    def get_platform_name(self) -> str:  # noqa: D102 —— 覆盖父类即可
        return "cron"


class _BackgroundResultEvent(FakeEvent):
    """后台任务结果唤醒轮（`extr["background_task_result"]`，`astr_agent_tool_exec.py:568-576`）。"""

    def get_extra(self, key: str = "", default=None):  # noqa: ANN001
        if key == "background_task_result":
            return {"task_id": "x", "state": "failed"}
        return default


class _ExplodingPlatformEvent(FakeEvent):
    """`get_platform_name()` 抛异常的事件（测 fail-open：判定失败 ≠ 唤醒轮）。"""

    def get_platform_name(self) -> str:  # noqa: D102
        raise RuntimeError("platform meta unavailable")


def _route_lines(logger: RecordingLogger) -> list[str]:
    """从日志桩里挑出 `route=` 行（它们以固定前缀起头，且必须单行）。"""
    return [
        message
        for level, message in logger.records
        if level == "INFO" and message.startswith("media_analysis: route=")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("event_factory", [_CronEvent, _BackgroundResultEvent])
async def test_wake_round_short_circuits_with_the_dedicated_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, event_factory
) -> None:
    """㉑ 合成事件 ⇒ **0 次** `background.submit`、专用 hint、`wake_round=1`、无 `task_id`。

    两个信号各测一次（`platform_name=="cron"` 与 `background_task_result`）——
    §5.3 的 J12 用**双信号**，少测一个就等于放过一类同形态事件。
    """
    rig = build_rig(monkeypatch, tmp_path, kind="audio")
    event = event_factory(chain=[])
    logger = RecordingLogger()
    monkeypatch.setattr(analyze_mod, "LOG", logger)

    submits: list[dict[str, object]] = []

    async def _spy_submit(**kwargs):  # noqa: ANN003
        submits.append(kwargs)
        raise AssertionError("唤醒轮不许提交后台任务")

    monkeypatch.setattr(background, "submit", _spy_submit)

    raw = await rig.plugin.media_analyze(event, target="current")
    payload = json.loads(raw)

    assert submits == [], "唤醒轮短路必须发生在 `background.submit` 之前"
    assert list(payload) == list(envelope.FAIL_KEYS), "失败信封仍是 3 键、顺序不变"
    assert payload["error"] == envelope.ERR_MEDIA_NOT_FOUND, "错误码仍是 23 条里的那一条"
    assert payload["hint"] == envelope.HINT_MEDIA_NOT_FOUND_WAKE
    assert payload["hint"] != envelope.HINTS[envelope.ERR_MEDIA_NOT_FOUND]

    lines = _route_lines(logger)
    assert len(lines) == 1, f"恰好一行 route=，实际 {lines}"
    assert "route=none stage=accept engine=plugin kind=- fallback=-" in lines[0]
    assert "error=MEDIA_NOT_FOUND wake_round=1" in lines[0]
    # §5.5 判据：日志含 `wake_round=1` 且**不含** `task_id=`
    joined = "\n".join(message for _lvl, message in logger.records)
    assert "task_id=" not in joined


@pytest.mark.asyncio
async def test_normal_empty_message_keeps_the_plain_media_not_found_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """㉒ 普通消息里确实没媒体 ⇒ **旧 hint 一个字都不改**、`route=` 行没有 `wake_round`。

    这条是"别把正常用户消息误杀"的反向判据：D2 只该改唤醒轮那一种情形。
    """
    rig = build_rig(monkeypatch, tmp_path, kind="audio")
    logger = RecordingLogger()
    monkeypatch.setattr(analyze_mod, "LOG", logger)

    raw = await rig.plugin.media_analyze(make_event(current=[]), target="current")
    payload = json.loads(raw)

    assert list(payload) == list(envelope.FAIL_KEYS)
    assert payload["error"] == envelope.ERR_MEDIA_NOT_FOUND
    assert payload["hint"] == envelope.HINTS[envelope.ERR_MEDIA_NOT_FOUND], "普通「没发媒体」用旧 hint"

    lines = _route_lines(logger)
    assert len(lines) == 1
    assert "error=MEDIA_NOT_FOUND" in lines[0]
    assert "wake_round=1" not in lines[0]


@pytest.mark.asyncio
async def test_wake_detection_failure_falls_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """㉓ 判定抛异常 ⇒ **回落到原逻辑**且不崩（fail-open 的机械判据）。

    `get_platform_name()` 抛错 / 缺 `get_extra` 都算"判不出来" ⇒ 当普通消息处理。
    宁可多提交一次后台任务，也不许把一条正常用户消息误判成唤醒轮。
    """
    rig = build_rig(monkeypatch, tmp_path, kind="audio")
    logger = RecordingLogger()
    monkeypatch.setattr(analyze_mod, "LOG", logger)

    raw = await rig.plugin.media_analyze(_ExplodingPlatformEvent(chain=[]), target="current")
    payload = json.loads(raw)

    assert payload["error"] == envelope.ERR_MEDIA_NOT_FOUND
    assert payload["hint"] == envelope.HINTS[envelope.ERR_MEDIA_NOT_FOUND]
    assert "wake_round=1" not in _route_lines(logger)[0]


def test_is_wake_round_is_totally_fail_open() -> None:
    """`is_wake_round` **永不抛**：缺方法 / 怪返回值 / 属性抛错全按 `False` 处理。"""
    assert analyze_mod.is_wake_round(SimpleNamespace()) is False
    assert analyze_mod.is_wake_round(_ExplodingPlatformEvent()) is False
    # 两个真信号各命中一次
    assert analyze_mod.is_wake_round(_CronEvent()) is True
    assert analyze_mod.is_wake_round(_BackgroundResultEvent()) is True
    # `get_extra` 返回非 dict（比如平台塞了个字符串）⇒ 不算唤醒轮
    weird = FakeEvent()
    weird.get_extra = lambda key="", default=None: "not-a-dict"
    assert analyze_mod.is_wake_round(weird) is False


@pytest.mark.asyncio
async def test_wake_round_with_real_media_still_runs_normally(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§5.2 第 3 条（fail-open 的退路）：合成事件但链里**真有**媒体 ⇒ 正常受理并跑完。

    "唤醒轮"只是"通常没有媒体"的推断，不是"禁止分析"的判决；真有媒体时它必须让路。
    """
    rig = build_rig(monkeypatch, tmp_path, kind="audio")
    wake_event = _CronEvent(chain=[])
    wake_event._chain.append(make_record(rig.media_path))  # noqa: SLF001 —— 造"合成了但有媒体"
    rig.event = wake_event

    payload = await rig.tool()

    assert payload["ok"] is True
    assert payload["transcript"], "正常跑完，不是短路"


# --------------------------------------------------------------------------- #
# v0.5.1 C7：`route=` 结构化观测行（SPEC-v0.5.1 §6 / §9.4 的 G6 六格）
#
# 六格：㉔字段名与顺序 ㉕枚举集合相等 ㉖判定表逐行（5 个既有场景）㉗每调用至多一行
#      ㉘两引擎分别传 plugin/core（在 `test_background` / `test_core_mode` 里）
#      ㉙`route` 行不写 notes + 缓存回放 notes 逐字相同
#
# 行格式本身就是契约：这里的正则**逐字**抄自 SPEC-v0.5.1 §6.7（含 6 正 1 负样本），
# 所以"自创枚举"（如 `route=compressed`）不是靠人眼挡，而是靠正则挡。
# --------------------------------------------------------------------------- #
ROUTE_LINE = re.compile(
    r"^media_analysis: route=(?P<route>cache|native|frames|audio|none) "
    r"stage=(?P<stage>analyze|accept) engine=(?P<engine>plugin|core) kind=(?P<kind>audio|video|-) "
    r"fallback=(?P<fb>-|native_unavailable|native_failed|native_empty|native_too_large"
    r"|frames_forbidden|no_video_provider|no_vision_provider|skip_budget|no_audio_track) "
    r"cached=(?P<cached>[01]) duration_s=(?P<dur>\d+\.\d\d) frames=(?P<frames>\d+) "
    r"transcript_chars=(?P<tc>\d+) elapsed_ms=(?P<ms>\d+)"
    r"(?: error=(?P<err>[A-Z_]+))?(?: wake_round=1)?$"
)

#: SPEC §6.7 自带的 6 条正例（逐字抄；改一个字都要先改规格）
_SPEC_ROUTE_SAMPLES = (
    "media_analysis: route=native stage=analyze engine=core kind=video fallback=- cached=0 duration_s=12.34 frames=0 transcript_chars=0 elapsed_ms=6400",
    "media_analysis: route=audio stage=analyze engine=plugin kind=video fallback=frames_forbidden cached=0 duration_s=125.38 frames=0 transcript_chars=1010 elapsed_ms=73775",
    "media_analysis: route=cache stage=analyze engine=core kind=video fallback=- cached=1 duration_s=5.00 frames=0 transcript_chars=0 elapsed_ms=1",
    "media_analysis: route=none stage=analyze engine=core kind=- fallback=- cached=0 duration_s=0.00 frames=0 transcript_chars=0 elapsed_ms=61113 error=AUDIO_FAILED",
    "media_analysis: route=none stage=accept engine=plugin kind=- fallback=- cached=0 duration_s=0.00 frames=0 transcript_chars=0 elapsed_ms=0 error=MEDIA_NOT_FOUND wake_round=1",
    "media_analysis: route=frames stage=analyze engine=plugin kind=video fallback=native_too_large cached=0 duration_s=20.00 frames=6 transcript_chars=120 elapsed_ms=41000",
)

#: SPEC §6.7 的负例：自创枚举必须被挡掉
_SPEC_ROUTE_NEGATIVE = (
    "media_analysis: route=compressed stage=analyze engine=plugin kind=video fallback=- "
    "cached=0 duration_s=1.00 frames=0 transcript_chars=0 elapsed_ms=1"
)


def test_route_line_regex_accepts_the_six_frozen_samples() -> None:
    """㉔ 六条冻结正例逐字通过（字段名 + **顺序** + 分隔符一起钉）。"""
    for line in _SPEC_ROUTE_SAMPLES:
        assert ROUTE_LINE.match(line), line


def test_route_line_regex_rejects_a_self_invented_enum() -> None:
    """㉕ 负例：`route=compressed` 这类自创值被正则挡掉（枚举封闭性不靠人眼守）。"""
    assert not ROUTE_LINE.match(_SPEC_ROUTE_NEGATIVE)


def test_route_enums_are_exactly_the_frozen_sets() -> None:
    """㉕ 枚举集合**相等**（不是包含）：route 5 / stage 2 / engine 2 / fallback 10 / kind 3。"""
    assert set(analyze_mod.ROUTES) == {"cache", "native", "frames", "audio", "none"}
    assert set(analyze_mod.STAGES) == {"analyze", "accept"}
    assert set(analyze_mod.ENGINES) == {"plugin", "core"}
    assert set(analyze_mod.KINDS) == {"audio", "video", "-"}
    assert set(analyze_mod.FALLBACKS) == {
        "-",
        "native_unavailable",
        "native_failed",
        "native_empty",
        "native_too_large",
        "frames_forbidden",
        "no_video_provider",
        "no_vision_provider",
        "skip_budget",
        "no_audio_track",
    }
    assert len(analyze_mod.FALLBACKS) == 10 and len(set(analyze_mod.FALLBACKS)) == 10


def test_log_route_emits_one_matching_line_for_every_enum_value(monkeypatch) -> None:
    """㉔ `log_route` 对**每一个**枚举取值都产出能过正则的单行（且不换行）。"""
    logger = RecordingLogger()
    monkeypatch.setattr(analyze_mod, "LOG", logger)

    for route in analyze_mod.ROUTES:
        for stage in analyze_mod.STAGES:
            for engine in analyze_mod.ENGINES:
                for kind in analyze_mod.KINDS:
                    for fallback in analyze_mod.FALLBACKS:
                        analyze_mod.log_route(
                            route=route,
                            stage=stage,
                            engine=engine,
                            kind=kind,
                            fallback=fallback,
                        )

    lines = _route_lines(logger)
    assert len(lines) == 5 * 2 * 2 * 3 * 10
    for line in lines:
        assert ROUTE_LINE.match(line), line
        assert "\n" not in line


def test_log_route_normalizes_out_of_enum_values(monkeypatch) -> None:
    """自创枚举**不许**写进日志（归一成 `none` / `plugin` / `-`），否则运维 grep 与集合判据同时失效。"""
    logger = RecordingLogger()
    monkeypatch.setattr(analyze_mod, "LOG", logger)

    analyze_mod.log_route(
        route="compressed", stage="middle", engine="native", kind="gif", fallback="faster"
    )

    line = _route_lines(logger)[0]
    assert ROUTE_LINE.match(line), line
    assert "route=none stage=analyze engine=plugin kind=- fallback=-" in line


@pytest.mark.parametrize(
    ("route", "mode", "notes", "native_state", "expected"),
    [
        # R1 —— 没有"更贵的那一跳"的三种走法
        ("native", "auto", [], "ok", "-"),
        ("cache", "false", ["video_skip_budget"], "failed", "-"),
        ("none", "auto", [], "", "-"),
        # R2 —— 显式不要画面
        ("audio", "false", [], "", "frames_forbidden"),
        # R2 优先于 R3–R6（按序首命中的机械判据）
        ("frames", "false", ["video_skip_budget"], "failed", "frames_forbidden"),
        # R3–R6 —— notes 子串
        ("audio", "auto", ["video_unavailable_no_provider"], "", "no_video_provider"),
        ("audio", "auto", ["video_no_vision_provider"], "", "no_vision_provider"),
        ("audio", "auto", ["video_skip_budget"], "", "skip_budget"),
        ("audio", "auto", ["no_audio_track"], "", "no_audio_track"),
        # R7–R10 —— native_state
        ("audio", "auto", [], "unavailable", "native_unavailable"),
        ("audio", "auto", [], "failed", "native_failed"),
        ("audio", "auto", [], "empty", "native_empty"),
        ("audio", "auto", [], "too_large", "native_too_large"),
        # R11 —— 其余
        ("audio", "auto", [], "", "-"),
    ],
)
def test_fallback_table_r1_to_r11_row_by_row(
    route: str, mode: str, notes: list[str], native_state: str, expected: str
) -> None:
    """㉖ `fallback` 判定表 R1–R11 **逐行**可测（含"按序首命中"的优先级行）。"""
    assert (
        analyze_mod.fallback_of(route, mode=mode, notes=list(notes), native_state=native_state)
        == expected
    )


@pytest.mark.parametrize(
    ("cached", "error", "native_state", "frames", "expected"),
    [
        (True, "AUDIO_FAILED", "ok", 3, "cache"),  # 命中缓存优先级最高
        (False, "VIDEO_FAILED", "ok", 3, "none"),  # 有错码 ⇒ none
        (False, "", "ok", 3, "native"),  # 原生成功
        (False, "", "failed", 3, "frames"),  # 有帧
        (False, "", "failed", 0, "audio"),  # 其它
    ],
)
def test_route_priority_is_cache_none_native_frames_audio(
    cached: bool, error: str, native_state: str, frames: int, expected: str
) -> None:
    """㉖ `route` 也是总函数：cache > none > native > frames > audio。"""
    assert (
        analyze_mod.route_of(
            cached=cached, error=error, native_state=native_state, frames=frames
        )
        == expected
    )


@pytest.mark.asyncio
async def test_analyze_emits_exactly_one_route_line_right_after_the_ok_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """㉗ 成功路径：`route=` 行**恰好一条**，且紧挨在既有 `ok` 行**之后**（§6.1 末段）。"""
    from _fakes import native_video

    logger = RecordingLogger()
    monkeypatch.setattr(analyze_mod, "LOG", logger)
    rig = build_rig(monkeypatch, tmp_path, kind="video", native=native_video())

    payload = await rig.run(include_frames="auto")

    assert payload["ok"] is True
    lines = [message for level, message in logger.records if level == "INFO"]
    route_lines = [line for line in lines if line.startswith("media_analysis: route=")]
    assert len(route_lines) == 1, route_lines
    assert ROUTE_LINE.match(route_lines[0]), route_lines[0]
    assert "route=native stage=analyze engine=plugin kind=video fallback=-" in route_lines[0]
    assert "cached=0" in route_lines[0]

    index = lines.index(route_lines[0])
    assert lines[index - 1].startswith("media_analysis: ok "), "`route=` 紧跟在 `ok` 行之后"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "expect"),
    [
        ("native_ok", "route=native stage=analyze engine=plugin kind=video fallback=-"),
        ("native_too_large_then_frames", "route=frames stage=analyze engine=plugin kind=video fallback=native_too_large"),
        ("mode_false", "route=audio stage=analyze engine=plugin kind=video fallback=frames_forbidden"),
        ("auto_without_video_provider", "route=audio stage=analyze engine=plugin kind=video fallback=no_video_provider"),
    ],
)
async def test_five_existing_scenarios_land_on_the_frozen_route_and_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scenario: str, expect: str
) -> None:
    """㉖ 五个既有场景中的四个（第五个"缓存命中"在下一条用例里）逐条落到冻结的走法上。"""
    from _fakes import native_video

    logger = RecordingLogger()
    monkeypatch.setattr(analyze_mod, "LOG", logger)

    if scenario == "native_ok":
        rig = build_rig(monkeypatch, tmp_path, kind="video", native=native_video())
        await rig.run(include_frames="auto")
    elif scenario == "native_too_large_then_frames":
        # 超门槛 + 压缩关 ⇒ `native_state=too_large` ⇒ 抽帧回退接管（route=frames）
        rig = build_rig(
            monkeypatch,
            tmp_path,
            kind="video",
            cfg=settings.Settings(
                vision_provider_id="fake-chat",
                video_compress_enabled=False,
                video_max_direct_mb=1,
            ),
            native=native_video(),
            frames=[(2.5, "unused")],
        )
        rig.media_path.write_bytes(b"\x00" * (2 * 1024 * 1024))
        await rig.run(include_frames="auto")
    elif scenario == "mode_false":
        rig = build_rig(monkeypatch, tmp_path, kind="video", native=native_video(text=""))
        await rig.run(include_frames="false")
    else:  # auto_without_video_provider
        # 视觉位配了、视频位空着 ⇒ `auto` 不许拿 6 次视觉调用去补（评审 F7）
        rig = build_rig(
            monkeypatch,
            tmp_path,
            kind="video",
            cfg=settings.Settings(vision_provider_id="fake-chat"),
        )
        await rig.run(include_frames="auto")

    lines = _route_lines(logger)
    assert len(lines) == 1, lines
    assert expect in lines[0], lines[0]


@pytest.mark.asyncio
async def test_cache_hit_lands_on_route_cache_without_running_the_chain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """㉖ 第五个场景：命中缓存 ⇒ `route=cache`、`fallback=-`、`cached=1`，链路不再跑。"""
    logger = RecordingLogger()
    rig = build_rig(monkeypatch, tmp_path, kind="audio")
    first = await rig.run()

    monkeypatch.setattr(analyze_mod, "LOG", logger)
    second = await rig.run()

    assert first["cached"] is False and second["cached"] is True
    lines = _route_lines(logger)
    assert len(lines) == 1, lines
    assert "route=cache stage=analyze engine=plugin kind=audio fallback=-" in lines[0]
    assert "cached=1" in lines[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "code"),
    [("bogus", envelope.ERR_BAD_TARGET), ("current", envelope.ERR_MEDIA_NOT_FOUND)],
)
async def test_accept_phase_rejection_emits_one_route_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target: str, code: str
) -> None:
    """㉗ 受理期拒绝 ⇒ 一行 `stage=accept`、`kind=-`、`fallback=-`，且**没有** `stage=analyze` 行。

    覆盖 `BAD_TARGET` 与 `MEDIA_NOT_FOUND` 两个受理面；`TOO_MANY_TASKS` 在
    `tests/test_background.py`（那里才有并发闸的夹具）。
    """
    rig = build_rig(monkeypatch, tmp_path, kind="audio")
    logger = RecordingLogger()
    monkeypatch.setattr(analyze_mod, "LOG", logger)

    raw = await rig.plugin.media_analyze(make_event(current=[]), target=target)
    payload = json.loads(raw)

    assert payload["error"] == code
    lines = _route_lines(logger)
    assert len(lines) == 1, lines
    assert f"route=none stage=accept engine=plugin kind=- fallback=-" in lines[0]
    assert f"error={code}" in lines[0]
    assert "stage=analyze" not in "\n".join(lines)


@pytest.mark.asyncio
async def test_route_line_never_writes_into_notes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """㉙ `route=` 行**只读** notes 的子串，绝不回写（缓存中立的机械判据）。

    断言面上就看两件事：① notes 里没有任何 `route`/`engine`/`wake` 形状的痕迹；
    ② 同一个媒体跑两次（miss + hit）时，**回放前的 notes** 与首跑逐字相同。
    """
    rig = build_rig(monkeypatch, tmp_path, kind="video")
    first = await rig.run(include_frames="auto")

    assert not [note for note in first["notes"] if note.startswith(("route", "engine", "wake_round"))]
    assert first["notes"], "这条用例要有一串真实 notes 才有说服力"

    second = await rig.run(include_frames="auto")
    replay_notes = [note for note in second["notes"] if note not in ("cache_hit",)]
    replay_notes = [note for note in replay_notes if not note.startswith("cache_age_s:")]
    assert replay_notes == first["notes"], "缓存回放前后 notes 必须逐字相同"


@pytest.mark.asyncio
async def test_cancelled_analysis_emits_no_route_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """取消（卸载/进程停止）**不打** `route=` 行：这次调用没有"走法"可言。

    硬填一个 `route=audio` 会让运维以为它真跑成过；而"根本没到插件"由核心那行
    tool 调用记录区分（§6.6 的缺席判据），取消有自己的一行（`bg cancelled`）。
    """
    rig = build_rig(monkeypatch, tmp_path, kind="audio")
    logger = RecordingLogger()
    monkeypatch.setattr(analyze_mod, "LOG", logger)

    gate = asyncio.Event()
    stubbed = media.classify

    async def _slow_classify(*args, **kwargs):  # noqa: ANN002, ANN003
        await gate.wait()
        return await stubbed(*args, **kwargs)

    monkeypatch.setattr(media, "classify", _slow_classify)

    task = asyncio.create_task(rig.run())
    await asyncio.sleep(0.02)  # 让它挂在那道闸上（还没走到信封）
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _route_lines(logger) == [], "取消不许产 route= 行"


@pytest.mark.asyncio
async def test_uncontrolled_exception_still_lands_on_route_none_internal_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """t5-F7：非受控异常也必须在 `route=` 行里说真话（`route=none error=INTERNAL_ERROR`）。

    修之前：`analyze` 只捕获 `ToolError` 与 `CancelledError`，`_video_stage` 抛
    `RuntimeError` 会直接穿过 `finally` —— 于是那行按累积态打成**假的** `route=audio`
    （看起来像"只交了转写"），事故被藏起来。修之后：记 `INTERNAL_ERROR` 再上抛
    （收口仍在调用方，本层不吞异常）。
    """
    logger = RecordingLogger()
    monkeypatch.setattr(analyze_mod, "LOG", logger)
    rig = build_rig(monkeypatch, tmp_path, kind="video", frames=[(2.5, "unused")])

    async def _boom(**_kwargs):  # noqa: ANN003 —— 模拟"链路里漏出的非受控异常"
        raise RuntimeError("boom from the chain")

    monkeypatch.setattr(analyze_mod, "_video_stage", _boom)

    with pytest.raises(RuntimeError, match="boom from the chain"):
        await rig.run(include_frames="auto")

    lines = _route_lines(logger)
    assert len(lines) == 1, lines
    assert "route=none stage=analyze engine=plugin kind=- fallback=-" in lines[0]
    assert "error=INTERNAL_ERROR" in lines[0]
    assert ROUTE_LINE.match(lines[0]), lines[0]


@pytest.mark.asyncio
async def test_tool_error_lands_on_route_none_with_its_own_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """t5-F7 的第二条路径：受控失败 ⇒ `route=none error=<那个码>`（不是 INTERNAL_ERROR）。

    与上一条、`test_cancelled_analysis_emits_no_route_line` 一起覆盖三条出口：
    `ToolError` ⇒ 带码的 `none` / 取消 ⇒ **0 行** / 非受控异常 ⇒ `INTERNAL_ERROR`。
    """
    logger = RecordingLogger()
    monkeypatch.setattr(analyze_mod, "LOG", logger)
    # 时长 999 s > limits.max_media_seconds(300) ⇒ `ToolError(MEDIA_TOO_LONG)` 从链路里抛出
    rig = build_rig(monkeypatch, tmp_path, kind="audio", duration=999.0)

    with pytest.raises(envelope.ToolError) as exc_info:
        await rig.run()
    assert exc_info.value.code == envelope.ERR_MEDIA_TOO_LONG

    lines = _route_lines(logger)
    assert len(lines) == 1, lines
    assert "route=none stage=analyze engine=plugin" in lines[0]
    assert f"error={envelope.ERR_MEDIA_TOO_LONG}" in lines[0]
    assert "INTERNAL_ERROR" not in lines[0]
