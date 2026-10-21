"""主链路行为：信封契约、分支、降级、缓存、预算（SPEC §5 / §6 / §7 / §8 / §9）。

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
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_plugin_media_analysis import analyze as analyze_mod
from astrbot_plugin_media_analysis import cache as cache_mod
from astrbot_plugin_media_analysis import envelope, main, media, providers, settings
from _fakes import (
    FakeChatProvider,
    FakeContext,
    FakeSTTProvider,
    NotAProvider,
    RecordingLogger,
    make_event,
    make_record,
    make_video,
)

#: SPEC §6.4 的 12 条 notes 词表（含 `:<参数>` 形式）。
NOTE_PATTERN = re.compile(
    r"^(?:stt_provider|vision_provider|audio_extracted|stt_empty|no_audio_track|"
    r"frames_skipped_audio_only|frames_truncated|frame_extract_failed|frame_desc_failed|"
    r"summary_skipped_budget|summary_failed|focus_trimmed)(?::.+)?$"
)


# --------------------------------------------------------------------------- #
# 测试台：真组件 + 假媒体层 + 假 provider 层
# --------------------------------------------------------------------------- #
@dataclass
class Rig:
    media_path: Path
    event: object
    context: FakeContext
    settings: settings.Settings
    cache: cache_mod.ResultCache
    plugin: main.MediaAnalysisPlugin
    media_calls: dict[str, int]
    prov_calls: dict[str, int]
    stt: FakeSTTProvider
    chat: FakeChatProvider

    async def run(
        self,
        *,
        target: str = "current",
        focus: str = "",
        include_frames: bool = False,
        settings_obj: settings.Settings | None = None,
        cache_obj: cache_mod.ResultCache | None = None,
    ) -> dict[str, object]:
        """直接跑 `analyze.analyze`（受控失败会抛 `ToolError`）。"""
        return await analyze_mod.analyze(
            context=self.context,
            event=self.event,
            target=target,
            focus=focus,
            include_frames=include_frames,
            settings=settings_obj or self.settings,
            cache=cache_obj or self.cache,
        )

    async def tool(
        self,
        *,
        target: str = "current",
        focus: str = "",
        include_frames: bool = False,
    ) -> dict[str, object]:
        """走工具门面（模型看到的路径）：返回值必须是 JSON 字符串。"""
        raw = await self.plugin.media_analyze(
            self.event, target=target, focus=focus, include_frames=include_frames
        )
        assert isinstance(raw, str), f"工具返回值必须是 str，实际 {type(raw)}"
        payload = json.loads(raw)
        assert isinstance(payload, dict)
        return payload


def _stub_media(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    kind: str,
    duration: float | BaseException = 30.0,
    streams: tuple[bool, bool] = (True, True),
    frames: list[tuple[float, str]] | None = None,
    digest: str = "0123456789abcdef",
) -> tuple[Path, dict[str, int]]:
    """把 `media.*` 全换成内存实现；返回 (组件用的本地路径, 调用计数)。"""
    media_path = tmp_path / ("clip.wav" if kind == "audio" else "clip.mp4")
    media_path.write_bytes(b"\x00" * 2048)
    wav_path = tmp_path / "audio.wav"
    wav_path.write_bytes(b"RIFF" + b"\x00" * 64)

    calls = {
        "probe_duration_s": 0,
        "probe_streams": 0,
        "extract_wav": 0,
        "extract_frames": 0,
        "content_digest": 0,
    }

    async def _duration(_path: str) -> float:
        calls["probe_duration_s"] += 1
        if isinstance(duration, BaseException):
            raise duration
        return float(duration)

    async def _digest(_path: str) -> str:
        calls["content_digest"] += 1
        return digest

    async def _streams(_path: str) -> tuple[bool, bool]:
        calls["probe_streams"] += 1
        return streams

    async def _wav(_path: str, *, workdir: str) -> str:
        calls["extract_wav"] += 1
        return str(wav_path)

    async def _frames(
        _path: str, *, count: int, duration_s: float, workdir: str, notes: list[str] | None = None
    ) -> list[tuple[float, str]]:
        calls["extract_frames"] += 1
        produced: list[tuple[float, str]] = []
        for index, (at_s, _unused) in enumerate(frames or []):
            jpg = tmp_path / f"frame_{index}.jpg"
            jpg.write_bytes(b"\xff\xd8" + b"\x00" * 32)
            produced.append((at_s, str(jpg)))
        return produced

    monkeypatch.setattr(media, "probe_duration_s", _duration)
    monkeypatch.setattr(media, "content_digest", _digest)
    monkeypatch.setattr(media, "probe_streams", _streams)
    monkeypatch.setattr(media, "extract_wav", _wav)
    monkeypatch.setattr(media, "extract_frames", _frames)
    return media_path, calls


def _stub_providers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stt: object,
    chat: object,
    vision: object | None = None,
) -> dict[str, int]:
    calls = {"stt": 0, "vision": 0, "chat": 0}

    async def _stt(_context, _provider_id, _umo):
        calls["stt"] += 1
        if isinstance(stt, BaseException):
            raise stt
        return stt

    async def _vision(_context, _provider_id, _umo):
        calls["vision"] += 1
        resolved = vision if vision is not None else chat
        if isinstance(resolved, BaseException):
            raise resolved
        return resolved

    async def _chat(_context, _provider_id, _umo, *, require_image):
        calls["chat"] += 1
        if isinstance(chat, BaseException):
            raise chat
        return chat

    monkeypatch.setattr(providers, "resolve_stt_provider", _stt)
    monkeypatch.setattr(providers, "resolve_vision_provider", _vision)
    monkeypatch.setattr(providers, "resolve_chat_provider", _chat)
    return calls


def build_rig(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    kind: str = "audio",
    duration: float | BaseException = 30.0,
    streams: tuple[bool, bool] = (True, True),
    frames: list[tuple[float, str]] | None = None,
    digest: str = "0123456789abcdef",
    stt: object | None = None,
    chat: object | None = None,
    vision: object | None = None,
    cfg: settings.Settings | None = None,
    cache: cache_mod.ResultCache | None = None,
    event=None,
    context: FakeContext | None = None,
    providers_real: bool = False,
) -> Rig:
    media_path, media_calls = _stub_media(
        monkeypatch,
        tmp_path,
        kind=kind,
        duration=duration,
        streams=streams,
        frames=frames,
        digest=digest,
    )
    stt_obj = stt if stt is not None else FakeSTTProvider(provider_id="fake_stt")
    chat_obj = chat if chat is not None else FakeChatProvider(model_name="fake-chat")
    if providers_real:
        # 走真 `providers.resolve_*` + FakeContext：专测"空串回落阶梯"在 analyze 里的落点
        prov_calls = {"stt": 0, "vision": 0, "chat": 0}
    else:
        prov_calls = _stub_providers(monkeypatch, stt=stt_obj, chat=chat_obj, vision=vision)

    if event is None:
        component = (
            make_record(media_path) if kind == "audio" else make_video(media_path)
        )
        event = make_event(current=[component])

    cfg = cfg if cfg is not None else settings.Settings()
    cache_obj = cache if cache is not None else cache_mod.ResultCache()
    ctx = context or FakeContext()
    plugin = main.MediaAnalysisPlugin(context=ctx, config={})
    plugin._settings_cache = cfg

    return Rig(
        media_path=media_path,
        event=event,
        context=ctx,
        settings=cfg,
        cache=cache_obj,
        plugin=plugin,
        media_calls=media_calls,
        prov_calls=prov_calls,
        stt=stt_obj if isinstance(stt_obj, FakeSTTProvider) else FakeSTTProvider(),
        chat=chat_obj if isinstance(chat_obj, FakeChatProvider) else FakeChatProvider(),
    )


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
    assert f"stt_provider:{providers.provider_id_of(rig.stt)}" in payload["notes"]
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
    """语音天然无视频流：不该调用 `probe_streams`（§7 第 6 步的 else 分支）。"""
    rig = build_rig(monkeypatch, tmp_path, kind="audio")
    await rig.run()
    assert rig.media_calls["probe_streams"] == 0
    assert rig.media_calls["extract_wav"] == 0


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
    assert rig.media_calls["probe_duration_s"] == 0, "target 校验必须在最前面"


@pytest.mark.asyncio
async def test_quoted_target_reads_reply_chain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§5-6：`target=quoted` 从 `Reply.chain` 里取媒体。"""
    media_path, _calls = _stub_media(monkeypatch, tmp_path, kind="audio")
    _stub_providers(monkeypatch, stt=FakeSTTProvider(), chat=FakeChatProvider())
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
    assert rig.prov_calls["stt"] == 0, "超限必须在调 provider 之前拒绝（成本闸门）"


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
async def test_no_audio_track_without_frames_is_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§7.2 左半：只要转写但视频无音轨 ⇒ 错误 `NO_AUDIO_TRACK`（hint 指向 include_frames=true）。"""
    rig = build_rig(monkeypatch, tmp_path, kind="video", streams=(False, True))
    payload = await rig.tool()
    assert payload == {
        "ok": False,
        "error": envelope.ERR_NO_AUDIO_TRACK,
        "hint": envelope.HINTS[envelope.ERR_NO_AUDIO_TRACK],
    }


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
    assert rig.prov_calls["stt"] == 0, "无音轨就别再解析 STT"


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

    async def _boom(_path: str, *, workdir: str) -> str:
        raise envelope.ToolError(envelope.ERR_AUDIO_EXTRACT_FAILED)

    monkeypatch.setattr(media, "extract_wav", _boom)
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
# §5 STT 失败面
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_stt_exception_is_stt_failed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """§5：`get_text` 抛错 ⇒ `STT_FAILED`（provider 侧什么都可能抛，必须归一）。"""
    rig = build_rig(
        monkeypatch, tmp_path, stt=FakeSTTProvider(fail_with=RuntimeError("quota exceeded"))
    )
    payload = await rig.tool()
    assert payload == {
        "ok": False,
        "error": envelope.ERR_STT_FAILED,
        "hint": envelope.HINTS[envelope.ERR_STT_FAILED],
    }


@pytest.mark.asyncio
async def test_stt_timeout_is_stt_failed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """§7.1：STT 单步 60 s 超时 ⇒ `STT_FAILED`（不是冒泡成框架超时）。"""
    rig = build_rig(monkeypatch, tmp_path, stt=FakeSTTProvider(delay=0.2))
    monkeypatch.setattr(analyze_mod, "STT_TIMEOUT_S", 0.01)
    payload = await rig.tool()
    assert payload["error"] == envelope.ERR_STT_FAILED


@pytest.mark.asyncio
async def test_empty_transcript_is_not_an_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """§6.4：`get_text` 返回空串 ⇒ `transcript=[]` + `stt_empty`，**不是错误**。"""
    rig = build_rig(monkeypatch, tmp_path, stt=FakeSTTProvider(text="   "))
    payload = await rig.run()

    assert payload["ok"] is True
    assert payload["transcript"] == []
    assert "stt_empty" in payload["notes"]


@pytest.mark.asyncio
async def test_stt_provider_unavailable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """§5：阶梯 ①–④ 全落空 ⇒ `STT_UNAVAILABLE`。"""
    rig = build_rig(
        monkeypatch, tmp_path, stt=envelope.ToolError(envelope.ERR_STT_UNAVAILABLE)
    )
    payload = await rig.tool()
    assert payload["error"] == envelope.ERR_STT_UNAVAILABLE


@pytest.mark.asyncio
async def test_stt_capability_mismatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rig = build_rig(
        monkeypatch,
        tmp_path,
        stt=envelope.ToolError(envelope.ERR_PROVIDER_CAPABILITY_MISMATCH),
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
    rig = build_rig(monkeypatch, tmp_path, stt=FakeSTTProvider(text=""))
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
    for key in ("kind", "duration_s", "transcript", "frames", "summary", "notes"):
        assert second[key] == first[key], key
    assert isinstance(second["elapsed_ms"], int)
    assert rig.prov_calls["stt"] == 1, "命中最多次只该调一次 STT"
    assert rig.media_calls["extract_frames"] == 0


@pytest.mark.asyncio
async def test_different_focus_misses_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """§8 键含参数指纹：`focus` 不同 ⇒ 不命中。"""
    rig = build_rig(monkeypatch, tmp_path)
    await rig.run(focus="他在说什么")
    second = await rig.run(focus="画面里有什么")

    assert second["cached"] is False
    assert rig.prov_calls["stt"] == 2


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
async def test_lru_evicts_oldest_when_maxsize_is_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§8：`maxsize` 可注入 ⇒ 容量 1 时，第二次写就把第一条挤出去（淘汰策略可验证）。"""
    rig = build_rig(monkeypatch, tmp_path, cache=cache_mod.ResultCache(maxsize=1))
    await rig.run()
    monkeypatch.setattr(media, "content_digest", _digest_of("cccc2222cccc2222"))
    await rig.run()  # 挤掉第一条

    monkeypatch.setattr(media, "content_digest", _digest_of("0123456789abcdef"))
    third = await rig.run()
    assert third["cached"] is False, "被淘汰的第一条不该命中"
    assert len(rig.cache) == 1


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
    rig = build_rig(monkeypatch, tmp_path, stt=FakeSTTProvider(text="转写正文不许进日志"))
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
    _stub_providers(monkeypatch, stt=FakeSTTProvider(), chat=FakeChatProvider())
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
        asyncio.CancelledError(),
        KeyboardInterrupt(),
    ],
    ids=["RuntimeError", "ValueError", "KeyError", "CancelledError", "KeyboardInterrupt"],
)
@pytest.mark.asyncio
async def test_tool_facade_never_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, boom: BaseException
) -> None:
    """§3.3 的 `except BaseException` 收口：连 `CancelledError` 都走信封（框架硬超时会先取消我们）。"""
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
async def test_tool_controlled_failure_is_logged_and_enveloped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§3.3：`ToolError` 走 INFO 日志 + 3 键信封；`log_detail` 不进信封。"""
    log = RecordingLogger()
    monkeypatch.setattr(main, "LOG", log)
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
        monkeypatch, tmp_path, stt=FakeSTTProvider(fail_with=RuntimeError("x"))
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


@pytest.mark.asyncio
async def test_notes_always_come_from_the_frozen_vocabulary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C7：`notes` 词必须 ⊆ §6.4 的 12 条词表（含 `:<参数>` 形式）。"""
    seen: set[str] = set()

    rig = build_rig(monkeypatch, tmp_path, kind="video", frames=[(2.5, "unused")])
    seen.update((await rig.run(include_frames=True))["notes"])
    seen.update((await rig.run(include_frames=True))["notes"])  # 命中缓存时 notes 原样返回
    seen.update((await build_rig(monkeypatch, tmp_path, kind="video", streams=(False, True), frames=[(2.5, "u")]).run(include_frames=True))["notes"])
    seen.update((await build_rig(monkeypatch, tmp_path, kind="audio").run(include_frames=True))["notes"])
    seen.update((await build_rig(monkeypatch, tmp_path, stt=FakeSTTProvider(text="")).run())["notes"])
    seen.update((await build_rig(monkeypatch, tmp_path).run(focus="问" * 260))["notes"])

    assert seen, "一个 note 都没收到？"
    bad = sorted(note for note in seen if not NOTE_PATTERN.match(note))
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
        )
    assert exc_info.value.code == envelope.ERR_VISION_FAILED
    assert notes == ["frame_desc_failed:2.50"]


# --------------------------------------------------------------------------- #
# F1（t12 已改实现，SPEC §7.1 口径不变）：预算截断是降级，不是失败
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_slow_stt_eating_budget_keeps_transcript_and_degrades_frames(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§7.1 第 1 面：慢 STT 吃光预算 + `include_frames=true` ⇒ **成功信封 + 保住转写**。

    断言四件事：① `ok:true`；② `transcript` 非空（t12 的核心承诺）；③ `notes` 里有
    `frames_truncated:0/<want>`；④ `frames=[]` 且视觉 provider 一次都没被解析/调用。
    """
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        duration=30.0,
        frames=[(2.5, "unused")],
        stt=FakeSTTProvider(text="慢 STT 的转写必须保住", delay=0.08),
        cfg=settings.Settings(max_frames=6),
    )
    monkeypatch.setattr(analyze_mod, "DEADLINE_S", 0.05)  # 预算被 STT 吃光

    payload = await rig.run(include_frames=True)

    assert payload["ok"] is True
    assert list(payload) == list(envelope.OK_KEYS)
    assert [item["text"] for item in payload["transcript"]] == ["慢 STT 的转写必须保住"]
    assert "frames_truncated:0/6" in payload["notes"]
    assert payload["frames"] == []
    assert rig.prov_calls["vision"] == 0, "预算不足就别再发起视觉调用（省一次 API 花费）"
    assert rig.media_calls["extract_frames"] == 0
    assert "summary_skipped_budget" in payload["notes"]


@pytest.mark.asyncio
async def test_budget_exhausted_with_nothing_to_deliver_is_still_a_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§7.1 第 2 面：预算耗尽 **且** 转写与帧都为空 ⇒ 不给"空成功"，仍是 `FRAME_EXTRACT_FAILED`。

    两种"什么都没有"的来源都测：① 视频无音轨（转写天然为空）；② 有音轨但 STT 返回空串。
    """
    # ① 无音轨
    rig = build_rig(
        monkeypatch, tmp_path, kind="video", duration=30.0, streams=(False, True)
    )
    monkeypatch.setattr(analyze_mod, "DEADLINE_S", 0.001)
    payload = await rig.tool(include_frames=True)

    assert list(payload) == list(envelope.FAIL_KEYS), "失败信封必须恰好 3 键"
    assert payload == {
        "ok": False,
        "error": envelope.ERR_FRAME_EXTRACT_FAILED,
        "hint": envelope.HINTS[envelope.ERR_FRAME_EXTRACT_FAILED],
    }
    # ② 有音轨但转写为空
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        duration=30.0,
        stt=FakeSTTProvider(text=""),
    )
    monkeypatch.setattr(analyze_mod, "DEADLINE_S", 0.001)
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
        stt=FakeSTTProvider(text="正常预算下的转写"),
        cfg=settings.Settings(max_frames=6),
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
async def test_empty_stt_id_falls_back_to_curr_stt_instance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§2.2 阶梯 ②→③：空串 + 核心 `enable=False`（②返回 None）⇒ ③ `curr_stt_provider_inst`。

    这是生产实况：`provider_stt_settings.enable=False`，只有 `mimo_stt` 一个实例。
    """
    stt = FakeSTTProvider(provider_id="mimo_stt")
    ctx = FakeContext(using_stt=None, curr_stt=stt)
    rig = build_rig(monkeypatch, tmp_path, context=ctx, providers_real=True)
    payload = await rig.run()

    assert payload["ok"] is True
    assert "stt_provider:mimo_stt" in payload["notes"]
    assert ctx.calls.named("get_using_stt_provider_async"), "空串必须先问核心当前 STT"


@pytest.mark.asyncio
async def test_empty_stt_id_with_nothing_loaded_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§2.2 阶梯 ⑤：一个 STT 都没有 ⇒ `STT_UNAVAILABLE`（错误信封，不是裸异常）。"""
    ctx = FakeContext(using_stt=None, all_stt=[], curr_stt=None)
    rig = build_rig(monkeypatch, tmp_path, context=ctx, providers_real=True)
    payload = await rig.tool()
    assert payload["error"] == envelope.ERR_STT_UNAVAILABLE


@pytest.mark.asyncio
async def test_configured_missing_stt_id_is_provider_not_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§5：配了 id 但核心没有 ⇒ `PROVIDER_NOT_FOUND`（不是回落到"当前 STT"）。"""
    ctx = FakeContext(by_id={}, using_stt=FakeSTTProvider(provider_id="core"))
    rig = build_rig(
        monkeypatch,
        tmp_path,
        context=ctx,
        cfg=settings.Settings(stt_provider_id="ghost"),
        providers_real=True,
    )
    payload = await rig.tool()
    assert payload["error"] == envelope.ERR_PROVIDER_NOT_FOUND


@pytest.mark.asyncio
async def test_empty_vision_id_uses_current_chat_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§2.2：`vision_provider_id=""` ⇒ 回落核心当前对话 provider（有 image 能力才能读图）。"""
    chat = FakeChatProvider(modalities=["text", "image"], model_name="core-chat")
    ctx = FakeContext(using_provider=chat, using_stt=FakeSTTProvider(provider_id="core_stt"))
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        frames=[(2.5, "unused")],
        context=ctx,
        providers_real=True,
    )
    payload = await rig.run(include_frames=True)

    assert payload["ok"] is True
    assert len(payload["frames"]) == 1
    assert "vision_provider:core-chat" in payload["notes"]


@pytest.mark.asyncio
async def test_empty_vision_id_with_text_only_provider_is_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§2.2 严格口径：落回到的 provider 没有 image 能力 ⇒ `PROVIDER_CAPABILITY_MISMATCH`。"""
    ctx = FakeContext(
        using_provider=FakeChatProvider(modalities=["text"]),
        using_stt=FakeSTTProvider(provider_id="core_stt"),
    )
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        frames=[(2.5, "unused")],
        context=ctx,
        providers_real=True,
    )
    payload = await rig.tool(include_frames=True)
    assert payload["error"] == envelope.ERR_PROVIDER_CAPABILITY_MISMATCH


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

    monkeypatch.setattr(providers, "resolve_stt_provider", _boom)
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
