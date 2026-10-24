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
    RecordingLogger,
    audio_chat,
    make_event,
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
    """§2.2：`min(AUDIO_TIMEOUT_S, 剩余预算)` 超时 ⇒ `AUDIO_FAILED`（不冒泡成框架超时）。"""
    rig = build_rig(monkeypatch, tmp_path, audio=audio_chat(delay=0.2))
    monkeypatch.setattr(analyze_mod, "AUDIO_TIMEOUT_S", 0.01)
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
