"""tests/ 地基自检 —— **不 import 任何插件模块**。

本文件就是"空测试"的正式版本：它证明 `conftest.py` 能在**插件源码还不存在**时把
`tests/` 跑起来（t3 的完成条件之一），同时把夹具与假对象的形状钉住，避免 builder 写的
测试用一个"长得像但对不上真核心"的假世界自证成功。

四组断言：

1. 夹具：6 个文件都在、单文件 ≤ 1 MB、时长/流形状与 SPEC 的测试口径一致
   （§11.2 的 5 s 视频与无音轨视频、§7.3 的 6 帧中点网格）。
2. 幂等：重跑不重造；同参数重造**逐字节一致**（`MANIFEST.sha256` 才有意义）。
3. 组件：`make_record` / `make_video` 造出来的是**真核心类型**，`path` 与
   `convert_to_file_path()` 的行为符合 SPEC §2.1（path 优先、convert 兜底）。
4. 假对象：`FakeEvent`（含"没有 track 方法"这一条）、`FakeSTTProvider`、`FakeChatProvider`
   的 `modalities` 三态、`FakeContext.get_provider_by_id` 是**同步**的（误 await 即 `TypeError`）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import make_fixtures
from astrbot.api.message_components import Reply
from _fakes import (
    FakeChatProvider,
    FakeContext,
    FakeEvent,
    FakeSTTProvider,
    NotAProvider,
    RecordingLogger,
    TrackingFakeEvent,
    UnresolvableVideo,
    make_event,
    make_record,
    make_video,
)

EXPECTED_FIXTURES = {
    "audio_30s",
    "video_30s",
    "video_70s",
    "video_5s",
    "video_5s_noaudio",
    "video_audio_only",
}


# --------------------------------------------------------------------------- #
# 1. 夹具形状
# --------------------------------------------------------------------------- #
def test_core_backend_is_known(core_backend: str) -> None:
    """后端只有两种；正常门禁必须是真核心（桩只用来让 collection 不炸）。"""
    assert core_backend in {"real-core", "stub"}
    if core_backend == "stub":  # pragma: no cover
        pytest.skip("当前解释器没有 astrbot 真核心，只验证了桩路径")


def test_fixture_set_complete_and_small(media_fixtures: dict[str, Path]) -> None:
    """6 个夹具齐备、非空、单文件 ≤ 1 MB（任务口径）。"""
    assert set(media_fixtures) == EXPECTED_FIXTURES
    for stem, path in media_fixtures.items():
        assert path.is_file(), f"{stem} 不存在：{path}"
        size = path.stat().st_size
        assert 0 < size <= make_fixtures.MAX_BYTES, f"{stem} 体积 {size} 越界（上限 {make_fixtures.MAX_BYTES}）"


@pytest.mark.parametrize("spec", make_fixtures.FIXTURES, ids=lambda s: s.name)
def test_fixture_shape_matches_spec(spec: make_fixtures.FixtureSpec, media_fixtures: dict[str, Path]) -> None:
    """时长 / 音轨 / 视频流三项与 `FIXTURES` 的声明一致（用真 ffprobe 量的）。"""
    path = media_fixtures[Path(spec.name).stem]
    result = make_fixtures.probe(path)
    assert result.has_audio is spec.has_audio, f"{spec.name} 音轨不符"
    assert result.has_video is spec.has_video, f"{spec.name} 视频流不符"
    if spec.duration_s is not None:
        assert result.duration_s is not None, f"{spec.name} 读不出时长"
        assert abs(result.duration_s - spec.duration_s) <= 0.15, (
            f"{spec.name} 时长 {result.duration_s} ≠ {spec.duration_s}"
        )


def test_empty_stdout_is_how_no_stream_looks(media_fixtures: dict[str, Path]) -> None:
    """无音轨的视频：`ffprobe -select_streams a` 的 stdout 为空（SPEC #9 的口径）。"""
    path = media_fixtures["video_5s_noaudio"]
    proc = make_fixtures._run(  # noqa: SLF001 - 自检直接量 ffprobe 的原始输出
        [
            "ffprobe", "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path),
        ]
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_midpoint_frame_grid_stays_inside(media_fixtures: dict[str, Path]) -> None:
    """SPEC §7.3 的中点网格：5 s / 6 帧全部落在片内（不会撞 EOF 的 rc=234）。"""
    duration = make_fixtures.probe(media_fixtures["video_5s"]).duration_s
    assert duration is not None
    count = 6
    grid = [round((i + 0.5) * duration / count, 2) for i in range(count)]
    assert len(grid) == count
    assert all(0.0 < at < duration for at in grid), grid
    assert grid == sorted(grid)


def test_generation_is_idempotent(media_fixtures: dict[str, Path]) -> None:
    """重跑 `ensure_all` 不重造：6 个文件的 mtime 一个都不许变。"""
    before = {stem: path.stat().st_mtime_ns for stem, path in media_fixtures.items()}
    again = make_fixtures.ensure_all(make_fixtures.default_dir(), quiet=True)
    after = {stem: path.stat().st_mtime_ns for stem, path in again.items()}
    assert before == after


def test_generation_is_byte_reproducible(tmp_path: Path) -> None:
    """同参数重造逐字节一致 —— 否则 `MANIFEST.sha256` 对账没有意义。

    只挑最小的两件（5 s 视频 + 30 s wav）重造，避免把门禁拖慢。
    """
    picked = {spec.name for spec in make_fixtures.FIXTURES if spec.name in {"video_5s.mp4", "audio_30s.wav"}}
    hashes: list[dict[str, str]] = []
    for round_no in range(2):
        directory = tmp_path / f"round{round_no}"
        directory.mkdir()
        for spec in make_fixtures.FIXTURES:
            if spec.name in picked:
                make_fixtures._build(spec, directory)  # noqa: SLF001 - 自检直接驱动生成器
        hashes.append(
            {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in sorted(picked)}
        )
    assert hashes[0] == hashes[1], "同参数两次生成的字节不一致（bitexact 标志失效？）"


def test_manifest_matches_files(media_fixtures: dict[str, Path]) -> None:
    """`MANIFEST.sha256` 记录的哈希与磁盘上的文件一致。"""
    manifest = make_fixtures.default_dir() / "MANIFEST.sha256"
    assert manifest.is_file(), "manifest 没写出来"
    recorded = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        digest, name = line.split()
        recorded[name] = digest
    for stem, path in media_fixtures.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == recorded[path.name], f"{stem} 哈希不符"


# --------------------------------------------------------------------------- #
# 2. 组件的类型面（真核心）
# --------------------------------------------------------------------------- #
def test_make_record_and_video_are_core_types(media_fixtures: dict[str, Path]) -> None:
    """夹具造出来的组件必须是真 `Record` / `Video`，且 `path` 指向夹具文件。"""
    from astrbot.api.message_components import Record, Video

    record = make_record(media_fixtures["audio_30s"])
    video = make_video(media_fixtures["video_30s"])

    assert isinstance(record, Record) and not isinstance(record, Video)
    assert isinstance(video, Video) and not isinstance(video, Record)
    assert Path(record.path) == media_fixtures["audio_30s"]
    assert Path(video.path) == media_fixtures["video_30s"]
    # SPEC §2.1 的两处遍历用 `isinstance(comp, Record | Video)` 判定
    assert isinstance(record, Record | Video)
    assert isinstance(video, Record | Video)


def test_video_from_url_has_no_local_path() -> None:
    """`file` 是 http URL 时 `path` 是假值（真核心给 `""`）—— 只能走 convert 兜底。"""
    video = make_video(file="http://example.invalid/clip.mp4")
    assert not video.path, f"URL 组件的 path 应该是空的，实际 {video.path!r}"
    assert video.file == "http://example.invalid/clip.mp4"


@pytest.mark.asyncio
async def test_video_convert_to_file_path_is_local(media_fixtures: dict[str, Path]) -> None:
    """本地文件组件的 `convert_to_file_path()` 零拷贝（不联网、不转码）。"""
    video = make_video(media_fixtures["video_5s"])
    resolved = await video.convert_to_file_path()
    assert Path(resolved) == media_fixtures["video_5s"].resolve()


@pytest.mark.asyncio
async def test_unresolvable_video_raises() -> None:
    """`path` 空 + convert 抛错 ⇒ 插件该落 `MEDIA_UNRESOLVABLE`（不联网也能造这一例）。"""
    video = UnresolvableVideo(file="http://example.invalid/clip.mp4")
    assert not video.path
    with pytest.raises(Exception):
        await video.convert_to_file_path()


# --------------------------------------------------------------------------- #
# 3. 假 event
# --------------------------------------------------------------------------- #
def test_fake_event_current_and_quoted(media_fixtures: dict[str, Path]) -> None:
    """当前消息 / 引用消息两个来源都能造：引用是**一层** `Reply.chain`（SPEC §2.1 事实 C）。"""
    record = make_record(media_fixtures["audio_30s"])
    video = make_video(media_fixtures["video_30s"])

    current_only = make_event(current=[record])
    assert current_only.get_messages() == [record]

    quoted = make_event(current=[], quoted=[video])
    chain = quoted.get_messages()
    assert len(chain) == 1
    assert isinstance(chain[0], Reply)
    assert chain[0].chain == [video]
    assert quoted.unified_msg_origin == "aiocqhttp:GroupMessage:1000"


def test_fake_event_has_no_track_method() -> None:
    """SPEC §9-4：fake event **没有** `track_temporary_local_file`，代码必须 getattr 兜底。"""
    assert not hasattr(FakeEvent(), "track_temporary_local_file")
    tracking = TrackingFakeEvent()
    assert hasattr(tracking, "track_temporary_local_file")
    tracking.track_temporary_local_file("/tmp/x.jpg")
    assert tracking.tracked_files == ["/tmp/x.jpg"]


# --------------------------------------------------------------------------- #
# 4. 假 provider / 假 context
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_fake_stt_returns_text_and_sees_local_path(media_fixtures: dict[str, Path]) -> None:
    """`get_text` 收的是**本地路径**（SPEC §2.3"名字骗人"），且能按要求抛错。"""
    stt = FakeSTTProvider(text="转写正文", require_existing_file=True)
    text = await stt.get_text(str(media_fixtures["audio_30s"]))
    assert text == "转写正文"
    assert stt.paths_seen() == [str(media_fixtures["audio_30s"])]


@pytest.mark.asyncio
async def test_fake_stt_can_fail() -> None:
    stt = FakeSTTProvider(fail_with=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        await stt.get_text("/tmp/not-there.wav")


@pytest.mark.asyncio
async def test_fake_chat_modalities_three_states() -> None:
    """`modalities` 三态，对应 SPEC §2.2 的严格判定：缺省→不支持、`[]`→支持、含 image→支持。"""
    default = FakeChatProvider()
    assert default.provider_config["modalities"] == ["text", "image"]

    absent = FakeChatProvider(modalities=None)
    assert "modalities" not in absent.provider_config

    legacy_empty = FakeChatProvider(modalities=[])
    assert legacy_empty.provider_config["modalities"] == []

    text_only = FakeChatProvider(modalities=["text"])
    assert text_only.provider_config["modalities"] == ["text"]


@pytest.mark.asyncio
async def test_fake_chat_records_frames(media_fixtures: dict[str, Path]) -> None:
    """每次 `text_chat` 的 `image_urls` 被记下来（抽帧阶段逐帧一次调用）。"""
    chat = FakeChatProvider(text="一帧描述")
    resp = await chat.text_chat(prompt="描述这一帧", image_urls=[str(media_fixtures["video_5s"])])
    assert resp.completion_text == "一帧描述"
    assert chat.images_seen() == [str(media_fixtures["video_5s"])]
    assert chat.prompts() == ["描述这一帧"]


def test_fake_context_get_provider_by_id_is_sync() -> None:
    """`Context.get_provider_by_id` 是同步的（`star/context.py:447-470`）：误 await 会 `TypeError`。"""
    ctx = FakeContext(by_id={"x": object()})
    assert ctx.get_provider_by_id("x") is not None
    assert ctx.get_provider_by_id("missing") is None
    assert ctx.provider_ids_asked() == ["x", "missing"]


@pytest.mark.asyncio
async def test_awaiting_sync_provider_lookup_fails() -> None:
    """把同步查找写成 `await` ⇒ 立刻 `TypeError`（同名方法在 ProviderManager 上是异步）。"""
    ctx = FakeContext(by_id={"x": object()})
    with pytest.raises(TypeError):
        await ctx.get_provider_by_id("x")


@pytest.mark.asyncio
async def test_fake_context_ladder_states() -> None:
    """阶梯四处返回面都能单独摆位（SPEC §2.2 ①–⑤）。"""
    stt = FakeSTTProvider()
    ctx = FakeContext(
        by_id={"fake_stt": stt},
        using_provider=FakeChatProvider(),
        using_stt=None,
        all_stt=[stt],
        curr_stt=stt,
    )
    assert ctx.get_provider_by_id("fake_stt") is stt
    assert await ctx.get_using_stt_provider_async("umo") is None
    assert ctx.get_all_stt_providers() == [stt]
    assert ctx.provider_manager.curr_stt_provider_inst is stt

    bare = FakeContext(has_provider_manager=False)
    assert bare.provider_manager is None
    assert not hasattr(bare, "provider_manager") or bare.provider_manager is None


def test_not_a_provider_shape() -> None:
    """能力不匹配的对照物：有 `provider_config` 但**不是** `Provider`。"""
    from astrbot.api.provider import Provider, STTProvider

    fake = NotAProvider(modalities=["image"])
    assert not isinstance(fake, Provider)
    assert not isinstance(fake, STTProvider)
    assert fake.provider_config["modalities"] == ["image"]


def test_recording_logger_formats_and_searches() -> None:
    """日志录制器：% 模板要展开，`contains` 用来断言"正文没进日志"（SPEC §9-1）。"""
    log = RecordingLogger()
    log.info("media_analyze rejected: code=%s detail=%s", "MEDIA_TOO_LONG", "")
    log.warning("clamped to %d", 60)
    assert log.levels() == ["INFO", "WARNING"]
    assert "code=MEDIA_TOO_LONG" in log.text()
    assert log.contains("clamped to 60")
    assert not log.contains("转写正文")
