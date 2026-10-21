"""媒体侧：组件定位 / 路径解析 / 判流 / 时长 / 抽音轨 / 抽帧 / 内容哈希（SPEC §2.1 / §2.5 / §7.3-7.4）。

刻意用**真组件 + 合成夹具**（`lavfi sine` + `testsrc`）：`Record.fromFileSystem` /
`Video.fromFileSystem` 与 `isinstance(x, Record | Video)` 都是真类型面（SPEC §11.1），
只有 `get_media_duration` 与 ffmpeg 失败面用 monkeypatch 造 —— 那些是"平台坏天气"，
真造不出来。

平台两处坑在这里被钉住（SPEC §1 #8 / #9）：
- 无音轨时 `ffprobe -select_streams a` **rc=0 但 stdout 为空** ⇒ 判流只能看输出；
- 拿无音轨文件直接转码会抛**未归一化**的裸 `Exception` ⇒ 必须先判流，失败归一成
  `AUDIO_EXTRACT_FAILED`。
"""

from __future__ import annotations

import asyncio
import inspect
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot.api.message_components import Record, Reply, Video

from astrbot_plugin_media_analysis import envelope, media
from _fakes import (
    FakeEvent,
    TrackingFakeEvent,
    UnresolvableRecord,
    make_event,
    make_record,
    make_video,
)

# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _code(exc_info: pytest.ExceptionInfo) -> str:
    assert isinstance(exc_info.value, envelope.ToolError), type(exc_info.value)
    return exc_info.value.code


def _ffprobe_one(path: Path, entries: str) -> str:
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", entries, "-of", "csv=p=0", str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _dead_component(path: Path | None = None, *, error: Exception | None = None):
    """鸭子类型组件：只有 `path` 与 `convert_to_file_path`（`resolve_media_path` 的入参面）。"""

    class _Comp:
        def __init__(self) -> None:
            self.path = str(path) if path is not None else None
            self.file = "file://" + str(path) if path is not None else "http://example.invalid/x"

        async def convert_to_file_path(self) -> str:
            if error is not None:
                raise error
            return str(path) if path is not None else ""

    return _Comp()


# --------------------------------------------------------------------------- #
# 1. find_media：四种情形 + target 精确匹配
# --------------------------------------------------------------------------- #
def test_find_media_current_audio(media_fixtures: dict[str, Path]) -> None:
    record = make_record(media_fixtures["audio_30s"])
    handle = media.find_media(make_event(current=[record]), "current")
    assert handle.comp is record
    assert handle.quoted is False
    assert handle.kind == "audio"


def test_find_media_current_video(media_fixtures: dict[str, Path]) -> None:
    video = make_video(media_fixtures["video_30s"])
    handle = media.find_media(make_event(current=[video]), "current")
    assert handle.comp is video
    assert handle.kind == "video"


def test_find_media_current_picks_first_media(media_fixtures: dict[str, Path]) -> None:
    """一条消息里有多个媒体时只取第一条（SPEC §13-7：单条信封）。"""
    first = make_record(media_fixtures["audio_30s"])
    second = make_video(media_fixtures["video_30s"])
    handle = media.find_media(make_event(current=[first, second]), "current")
    assert handle.comp is first  # 当前消息链不复制，identity 成立


def test_find_media_quoted(media_fixtures: dict[str, Path]) -> None:
    video = make_video(media_fixtures["video_30s"])
    event = make_event(current=[], quoted=[video])
    handle = media.find_media(event, "quoted")
    # `Reply(chain=[...])` 会经 pydantic 校验复制组件 ⇒ 比字段，不比 identity
    assert isinstance(handle.comp, Video)
    assert handle.comp.file == video.file
    assert handle.quoted is True


def test_find_media_quoted_needs_a_reply_component() -> None:
    """引用链只从 `Reply.chain` 取（`components.py:585-600`）；普通消息不参与 quoted。"""
    with pytest.raises(envelope.ToolError) as exc_info:
        media.find_media(make_event(current=[], quoted=[]), "quoted")
    assert _code(exc_info) == envelope.ERR_MEDIA_NOT_FOUND


def test_find_media_quoted_ignores_media_outside_reply(media_fixtures: dict[str, Path]) -> None:
    record = make_record(media_fixtures["audio_30s"])
    with pytest.raises(envelope.ToolError) as exc_info:
        media.find_media(make_event(current=[record], quoted=[]), "quoted")
    assert _code(exc_info) == envelope.ERR_MEDIA_NOT_FOUND


def test_find_media_quoted_only_one_layer(media_fixtures: dict[str, Path]) -> None:
    """引用里的引用取不到链（aiocqhttp 递归时 `get_reply=False`，`components.py:325`）。"""
    record = make_record(media_fixtures["audio_30s"])
    nested = Reply(id="nested", chain=[record])
    outer = Reply(id="outer", chain=[nested])
    event = FakeEvent(chain=[outer])
    with pytest.raises(envelope.ToolError) as exc_info:
        media.find_media(event, "quoted")
    assert _code(exc_info) == envelope.ERR_MEDIA_NOT_FOUND


def test_find_media_without_get_messages_is_media_not_found() -> None:
    """平台给了个没有 `get_messages` 的事件对象也不许抛裸异常。"""
    with pytest.raises(envelope.ToolError) as exc_info:
        media.find_media(SimpleNamespace(), "current")
    assert _code(exc_info) == envelope.ERR_MEDIA_NOT_FOUND


def test_find_media_survives_broken_message_chain() -> None:
    class _BrokenEvent:
        def get_messages(self):
            raise RuntimeError("platform exploded")

    with pytest.raises(envelope.ToolError) as exc_info:
        media.find_media(_BrokenEvent(), "current")
    assert _code(exc_info) == envelope.ERR_MEDIA_NOT_FOUND


@pytest.mark.parametrize("target", ["x", "Current", "QUOTED", "当前", "", " current"])
def test_find_media_rejects_any_other_target(target: str) -> None:
    """`target` 只认精确的 `current` / `quoted`，不做大小写或别名归一（SPEC §2.1）。"""
    with pytest.raises(envelope.ToolError) as exc_info:
        media.find_media(FakeEvent(), target)
    assert _code(exc_info) == envelope.ERR_BAD_TARGET


# --------------------------------------------------------------------------- #
# 2. resolve_media_path：path 优先 / convert 兜底 / 两段都失败
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_resolve_media_path_prefers_existing_path(media_fixtures: dict[str, Path]) -> None:
    video = make_video(media_fixtures["video_5s"])
    handle = media.MediaHandle(comp=video, quoted=False)
    resolved = await media.resolve_media_path(handle)
    assert Path(resolved) == media_fixtures["video_5s"].resolve()

    # 真组件上再挂一个会炸的 convert，证明 path 优先时根本没走到它
    class _Boom(SimpleNamespace):
        async def convert_to_file_path(self) -> str:
            raise AssertionError("path 已可用，不该调用 convert_to_file_path")

    handle = media.MediaHandle(
        comp=_Boom(path=str(media_fixtures["video_5s"])), quoted=False
    )
    assert Path(await media.resolve_media_path(handle)) == media_fixtures["video_5s"].resolve()


@pytest.mark.asyncio
async def test_resolve_media_path_falls_back_to_convert(tmp_path: Path) -> None:
    target = tmp_path / "downloaded.wav"
    target.write_bytes(b"RIFF")
    handle = media.MediaHandle(comp=_dead_component(target), quoted=False)
    assert await media.resolve_media_path(handle) == str(target)


@pytest.mark.asyncio
async def test_resolve_media_path_unresolvable_variants(tmp_path: Path) -> None:
    # convert 抛错（真 URL 下载失败的样子；用 UnresolvableRecord 不联网）
    handle = media.MediaHandle(comp=UnresolvableRecord(file="http://example.invalid/a.wav"), quoted=False)
    with pytest.raises(envelope.ToolError) as exc_info:
        await media.resolve_media_path(handle)
    assert _code(exc_info) == envelope.ERR_MEDIA_UNRESOLVABLE

    # convert 返回不存在的路径
    handle = media.MediaHandle(comp=_dead_component(tmp_path / "missing.wav"), quoted=False)
    with pytest.raises(envelope.ToolError) as exc_info:
        await media.resolve_media_path(handle)
    assert _code(exc_info) == envelope.ERR_MEDIA_UNRESOLVABLE

    # 连 convert 都没有
    handle = media.MediaHandle(comp=SimpleNamespace(path=None, file="http://x"), quoted=False)
    with pytest.raises(envelope.ToolError) as exc_info:
        await media.resolve_media_path(handle)
    assert _code(exc_info) == envelope.ERR_MEDIA_UNRESOLVABLE


@pytest.mark.asyncio
async def test_resolve_media_path_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Slow:
        path = None
        file = "http://example.invalid/slow.mp4"

        async def convert_to_file_path(self) -> str:
            await asyncio.sleep(0.2)
            return "/tmp/never"

    monkeypatch.setattr(media, "RESOLVE_TIMEOUT_S", 0.01)
    with pytest.raises(envelope.ToolError) as exc_info:
        await media.resolve_media_path(media.MediaHandle(comp=_Slow(), quoted=False))
    assert _code(exc_info) == envelope.ERR_MEDIA_UNRESOLVABLE


# --------------------------------------------------------------------------- #
# 3. probe_duration_s：核心 get_media_duration（毫秒）+ 三种失败
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_probe_duration_real_fixture(media_fixtures: dict[str, Path]) -> None:
    assert await media.probe_duration_s(str(media_fixtures["video_5s"])) == 5.0
    assert await media.probe_duration_s(str(media_fixtures["video_70s"])) == 70.0


@pytest.mark.asyncio
async def test_probe_duration_rounds_to_two_decimals(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake(_path: str) -> int:
        return 58320  # ms

    monkeypatch.setattr(media, "get_media_duration", _fake)
    assert await media.probe_duration_s("x") == 58.32


@pytest.mark.asyncio
async def test_probe_duration_failures_map_to_probe_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _none(_path: str):
        return None

    monkeypatch.setattr(media, "get_media_duration", _none)
    with pytest.raises(envelope.ToolError) as exc_info:
        await media.probe_duration_s("x")
    assert _code(exc_info) == envelope.ERR_PROBE_FAILED

    async def _zero(_path: str):
        return 0

    monkeypatch.setattr(media, "get_media_duration", _zero)
    with pytest.raises(envelope.ToolError) as exc_info:
        await media.probe_duration_s("x")
    assert _code(exc_info) == envelope.ERR_PROBE_FAILED

    async def _boom(_path: str):
        raise RuntimeError("ffprobe missing")

    monkeypatch.setattr(media, "get_media_duration", _boom)
    with pytest.raises(envelope.ToolError) as exc_info:
        await media.probe_duration_s("x")
    assert _code(exc_info) == envelope.ERR_PROBE_FAILED


@pytest.mark.asyncio
async def test_probe_duration_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _slow(_path: str):
        await asyncio.sleep(0.2)
        return 1000

    monkeypatch.setattr(media, "get_media_duration", _slow)
    monkeypatch.setattr(media, "PROBE_TIMEOUT_S", 0.01)
    with pytest.raises(envelope.ToolError) as exc_info:
        await media.probe_duration_s("x")
    assert _code(exc_info) == envelope.ERR_PROBE_FAILED


# --------------------------------------------------------------------------- #
# 4. probe_streams：判据是 stdout 是否为空（不是 rc）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_probe_streams_all_shapes(media_fixtures: dict[str, Path]) -> None:
    assert await media.probe_streams(str(media_fixtures["video_5s"])) == (True, True)
    assert await media.probe_streams(str(media_fixtures["video_5s_noaudio"])) == (False, True)
    assert await media.probe_streams(str(media_fixtures["video_audio_only"])) == (True, False)
    assert await media.probe_streams(str(media_fixtures["audio_30s"])) == (True, False)


@pytest.mark.asyncio
async def test_probe_streams_missing_binary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))  # 目录里没有 ffprobe
    with pytest.raises(envelope.ToolError) as exc_info:
        await media.probe_streams("/tmp/whatever.mp4")
    assert _code(exc_info) == envelope.ERR_PROBE_FAILED


# --------------------------------------------------------------------------- #
# 5. extract_wav：16 kHz 单声道 + 无音轨归一成 AUDIO_EXTRACT_FAILED
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_extract_wav_produces_16k_mono(media_fixtures: dict[str, Path], tmp_path: Path) -> None:
    out = await media.extract_wav(str(media_fixtures["video_5s"]), workdir=str(tmp_path))
    assert Path(out).is_file() and Path(out).stat().st_size > 0
    assert Path(out).parent == tmp_path
    entries = _ffprobe_one(Path(out), "stream=sample_rate,channels")
    assert entries.split(",") == ["16000", "1"], f"期望 16 kHz 单声道，实际 {entries!r}"


@pytest.mark.asyncio
async def test_extract_wav_no_audio_track_is_normalized(
    media_fixtures: dict[str, Path], tmp_path: Path
) -> None:
    """SPEC §1 #8：直接转码无音轨文件会抛裸 Exception；插件必须归一成 AUDIO_EXTRACT_FAILED。"""
    with pytest.raises(envelope.ToolError) as exc_info:
        await media.extract_wav(str(media_fixtures["video_5s_noaudio"]), workdir=str(tmp_path))
    assert _code(exc_info) == envelope.ERR_AUDIO_EXTRACT_FAILED


@pytest.mark.asyncio
async def test_extract_wav_missing_binary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(envelope.ToolError) as exc_info:
        await media.extract_wav("/tmp/nope.mp4", workdir=str(tmp_path))
    assert _code(exc_info) == envelope.ERR_AUDIO_EXTRACT_FAILED


# --------------------------------------------------------------------------- #
# 6. 抽帧：中点网格 + 单帧失败降级 + 全失败返回空
# --------------------------------------------------------------------------- #
def test_frame_grid_is_midpoint_of_each_segment() -> None:
    """SPEC §7.3 冻结公式：`round((i+0.5) * duration / count, 2)`，取每段中点。"""
    assert media.frame_grid(5.0, 6) == [0.42, 1.25, 2.08, 2.92, 3.75, 4.58]
    assert media.frame_grid(60.0, 6) == [5.0, 15.0, 25.0, 35.0, 45.0, 55.0]
    assert media.frame_grid(30.0, 1) == [15.0]
    assert media.frame_grid(5.0, 0) == [2.5], "count<=0 也要钳到 1，不能空网格"


def test_frame_grid_never_touches_eof() -> None:
    for duration in (1.0, 5.0, 59.9, 300.0):
        for count in (1, 3, 6, 12):
            grid = media.frame_grid(duration, count)
            assert grid == sorted(grid)
            assert all(0 < at < duration for at in grid), (duration, count, grid)


@pytest.mark.asyncio
async def test_extract_frames_real_video(media_fixtures: dict[str, Path], tmp_path: Path) -> None:
    frames = await media.extract_frames(
        str(media_fixtures["video_5s"]),
        count=6,
        duration_s=5.0,
        workdir=str(tmp_path),
    )
    assert len(frames) == 6
    assert [at for at, _ in frames] == media.frame_grid(5.0, 6)
    for at_s, jpg in frames:
        assert Path(jpg).is_file() and Path(jpg).stat().st_size > 0
        assert Path(jpg).read_bytes()[:2] == b"\xff\xd8", f"{at_s}s 抽出来的不是 JPEG"
        assert at_s == float(media.format_at_s(at_s))


@pytest.mark.asyncio
async def test_extract_frames_beyond_eof_degrades_per_frame(
    media_fixtures: dict[str, Path], tmp_path: Path
) -> None:
    """`-ss` 越过 EOF 时 ffmpeg 返回 rc=234 且不产出文件 ⇒ 单帧降级，**不抛**。"""
    notes: list[str] = []
    frames = await media.extract_frames(
        str(media_fixtures["video_5s"]),
        count=6,
        duration_s=30.0,  # 网格点 2.5/7.5/…/27.5，后 5 个越界
        workdir=str(tmp_path),
        notes=notes,
    )
    assert len(frames) == 1
    assert frames[0][0] == 2.5
    assert len(notes) == 5
    assert all(note.startswith("frame_extract_failed:") for note in notes)
    assert notes[0] == "frame_extract_failed:7.50"


@pytest.mark.asyncio
async def test_extract_frames_total_failure_returns_empty_list(
    media_fixtures: dict[str, Path], tmp_path: Path
) -> None:
    """一帧都没产出时返回 `[]`（由 `analyze` 判 `FRAME_EXTRACT_FAILED`），抽帧层不抛。"""
    notes: list[str] = []
    frames = await media.extract_frames(
        str(media_fixtures["video_5s"]),
        count=1,
        duration_s=30.0,  # 唯一网格点 15.0 > 5.0
        workdir=str(tmp_path),
        notes=notes,
    )
    assert frames == []
    assert notes == ["frame_extract_failed:15.00"]


@pytest.mark.asyncio
async def test_extract_frames_without_notes_still_works(
    media_fixtures: dict[str, Path], tmp_path: Path
) -> None:
    frames = await media.extract_frames(
        str(media_fixtures["video_5s"]), count=1, duration_s=30.0, workdir=str(tmp_path)
    )
    assert frames == []


# --------------------------------------------------------------------------- #
# 7. 内容哈希 / 时间戳格式 / 临时文件登记
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_content_digest_is_16_hex_and_stable(media_fixtures: dict[str, Path]) -> None:
    digest = await media.content_digest(str(media_fixtures["video_5s"]))
    assert len(digest) == 16
    assert all(char in "0123456789abcdef" for char in digest)
    assert digest == await media.content_digest(str(media_fixtures["video_5s"]))


@pytest.mark.asyncio
async def test_content_digest_missing_file(media_fixtures: dict[str, Path], tmp_path: Path) -> None:
    with pytest.raises(envelope.ToolError) as exc_info:
        await media.content_digest(str(tmp_path / "nope.mp4"))
    assert _code(exc_info) == envelope.ERR_MEDIA_UNRESOLVABLE


def test_format_at_s_is_two_decimals() -> None:
    assert media.format_at_s(2.5) == "2.50"
    assert media.format_at_s(0.416666) == "0.42"
    assert media.format_at_s(round(4.5833, 2)) == "4.58"


def test_track_temporary_local_file_is_best_effort() -> None:
    """SPEC §9-4：fake event 没有该方法 ⇒ 整段 best-effort，不许抛。"""
    media.track_temporary_local_file(FakeEvent(), "/tmp/x.wav")  # 不抛即通过

    tracking = TrackingFakeEvent()
    media.track_temporary_local_file(tracking, "/tmp/x.wav")
    assert tracking.tracked_files == ["/tmp/x.wav"]

    class _Boom:
        def track_temporary_local_file(self, _path: str) -> None:
            raise RuntimeError("metadata gone")

    media.track_temporary_local_file(_Boom(), "/tmp/x.wav")  # 异常被吞掉


def test_media_handle_kind_comes_from_component_type(media_fixtures: dict[str, Path]) -> None:
    record = make_record(media_fixtures["audio_30s"])
    video = make_video(media_fixtures["video_30s"])
    assert media.MediaHandle(comp=record, quoted=False).kind == "audio"
    assert media.MediaHandle(comp=video, quoted=True).kind == "video"
    assert isinstance(record, Record) and isinstance(video, Video)


def test_ffmpeg_binaries_reach_the_expected_absolute_paths() -> None:
    """SPEC §1 #2：`/usr/bin/ffmpeg` → `/opt/ffmpeg/bin/ffmpeg`（走 PATH，不写绝对路径）。"""
    for name in ("ffmpeg", "ffprobe"):
        resolved = subprocess.run(["which", name], capture_output=True, text=True, check=False)
        assert resolved.returncode == 0, f"{name} 不在 PATH 上（抽帧/转码会失败）"
        assert os.path.isabs(resolved.stdout.strip())


# --------------------------------------------------------------------------- #
# F2：captain 2026-10-22 裁决 —— 带默认值的追加参数属**批准偏离**，不回退签名
# --------------------------------------------------------------------------- #
def test_extract_frames_notes_param_is_an_approved_extension() -> None:
    """`media.extract_frames(..., notes=None)` 是批准过的追加参数，不是漂移。

    captain 2026-10-22 裁决：带默认值的追加参数属批准偏离，见 t2 evidence_note。
    它让抽帧层把 `frame_extract_failed:<at_s>` 降级词交给调用方（`analyze` 传同一个 notes 列表）；
    不传时行为与 SPEC §3.2 的冻结签名完全一致 —— 这条用例就是那枚「批准章」。
    """
    params = inspect.signature(media.extract_frames).parameters
    assert 'notes' in params, '批准过的 notes 参数被删了？先看 t2 evidence_note 再动手'
    assert params['notes'].default is None
    assert params['notes'].kind is inspect.Parameter.KEYWORD_ONLY


def test_frame_grid_and_format_at_s_are_pure_functions() -> None:
    """`frame_grid` / `format_at_s` 必须是纯函数：同输入同输出、不依赖也不修改模块状态。

    captain 2026-10-22 裁决：带默认值的追加参数属批准偏离，见 t2 evidence_note
    （这两个函数本身没有追加参数，它们是同一张"批准单"里被点名的公开面：必须存在且保持纯）。

    契约（SPEC §7.3 / §6.4 的 C8）：抽帧坐标与 `notes` 里的 `:<at_s>` 用同一套格式化，
    所以这两个函数被 `analyze` 与测试同时依赖 —— 一旦有人给它俩加缓存/全局状态，格式就会漂。
    """
    assert inspect.isfunction(media.frame_grid)
    assert inspect.isfunction(media.format_at_s)

    before = set(vars(media))
    assert media.frame_grid(5.0, 6) == media.frame_grid(5.0, 6) == [0.42, 1.25, 2.08, 2.92, 3.75, 4.58]
    assert media.format_at_s(2.5) == media.format_at_s(2.5) == '2.50'
    assert set(vars(media)) == before, '纯函数不该往模块里塞状态'
