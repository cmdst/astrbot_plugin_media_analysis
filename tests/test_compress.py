"""视频压缩 `media.compress_video`（SPEC-v0.3 §6 / §15-T4）。

压缩是 v0.3 新增的一跳，价值判断写在 §6.5：**压比抽帧便宜得多**（t8：48.9 s 视频压到
4 MiB 用 4.8 s、0 token；抽帧要 6 次视觉调用）。所以这一层要钉死四件事：

1. **预算达成**：产物 ≤ `budget_bytes`（预算本身 = `min(compress_target_bytes, max_direct_bytes)`）；
2. **最多 2 趟 + 0.95 修正**：第 1 趟超了就按实测体积等比修正，第 2 趟仍超 ⇒ 判失败
   （不第 3 次：收益趋零、时间锯齿状上涨）；
3. **失败只是"原因"，不抛**：`infeasible` / `timeout` / `ffmpeg_failed` / `empty` /
   `budget_exceeded` 都返回 `CompressResult(ok=False, reason=…)` —— 回退链还有抽帧那一跳；
4. **产物只落 `/tmp` 的工作目录、源只读**：源文件的字节/mtime 都不许变，产物不出 workdir。

真 ffmpeg 用例（`big_video` 夹具）在现场造一份 640x360 的合成视频，**不用真实群媒体**。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from astrbot_plugin_media_analysis import media

#: 冻结参数（SPEC-v0.3 §6.2）
EXPECTED_HEIGHT = 480
EXPECTED_PRESET = "veryfast"
EXPECTED_AUDIO_KBPS = 64
MIN_KBPS = 64
SECOND_PASS_FACTOR = 0.95


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_source(tmp_path: Path, size: int = 500_000) -> Path:
    path = tmp_path / "src.mp4"
    path.write_bytes(b"\x00" * size)
    return path


@pytest.fixture(scope="module")
def big_video(tmp_path_factory) -> Path:
    """真 ffmpeg 合成的 640x360 / 5 s 视频（约 150 KB，CRF 10）—— 密到值得压。"""
    if shutil.which("ffmpeg") is None:
        pytest.skip("没有 ffmpeg，跳过真转码用例")
    directory = tmp_path_factory.mktemp("compress_fixture")
    path = directory / "big.mp4"
    argv = [
        "ffmpeg", "-nostdin", "-y", "-v", "error",
        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=5",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=5",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "10",
        "-c:a", "aac", "-b:a", "96k", "-pix_fmt", "yuv420p",
        str(path),
    ]
    proc = subprocess.run(argv, capture_output=True)
    if proc.returncode != 0 or not path.is_file():
        pytest.skip(f"合成压缩夹具失败：{proc.stderr[:200]!r}")
    return path


def _patch_capture(monkeypatch: pytest.MonkeyPatch, handler) -> list[dict]:
    """把 `media._run_capture` 换成"按脚本产出字节"的假身；返回它收到的调用参数。"""
    calls: list[dict] = []

    async def _fake(argv, *, timeout):
        calls.append({"argv": list(argv), "timeout": timeout})
        return handler(list(argv), timeout)

    monkeypatch.setattr(media, "_run_capture", _fake)
    return calls


def _write_size(path: str, size: int) -> None:
    Path(path).write_bytes(b"\x00" * size)


# --------------------------------------------------------------------------- #
# 1. 码率公式（§6.1）
# --------------------------------------------------------------------------- #
def test_target_kbps_subtracts_the_audio_overhead() -> None:
    """`floor(budget*8/duration/1000) - 64`：64 是 AAC 64 kbps 单声道的固定开销。"""
    assert media.compress_target_kbps(4 * 1024 * 1024, 48.9) == int(
        4 * 1024 * 1024 * 8 / 48.9 / 1000
    ) - 64
    assert media.compress_target_kbps(100_000, 5.0) == 96
    assert media.compress_target_kbps(1_000_000, 10.0) == 736


@pytest.mark.parametrize(("budget", "duration"), [(0, 5.0), (1000, 0.0), (-1, 5.0)])
def test_target_kbps_is_zero_for_degenerate_inputs(budget: int, duration: float) -> None:
    assert media.compress_target_kbps(budget, duration) == 0


def test_kbps_below_the_floor_is_infeasible_not_a_blurry_video() -> None:
    """预算太小 ⇒ 目标码率 < 64 kbps ⇒ `infeasible`（**不硬转**：那只会得到一段糊片）。"""
    assert media.compress_target_kbps(60_000, 5.0) < MIN_KBPS


# --------------------------------------------------------------------------- #
# 2. 冻结的 argv（§6.2）
# --------------------------------------------------------------------------- #
def test_compress_argv_is_frozen() -> None:
    argv = media.compress_argv("src.mp4", "out.mp4", video_kbps=635, has_audio=True)

    assert argv[0] == "ffmpeg"
    assert argv[argv.index("-i") + 1] == "src.mp4"
    assert argv[argv.index("-vf") + 1] == "scale=-2:480:flags=bicubic"
    assert argv[argv.index("-c:v") + 1] == "libx264"
    assert argv[argv.index("-preset") + 1] == EXPECTED_PRESET
    # ABR 双向钳制：-b:v 与 -maxrate 同值、bufsize 两倍
    assert argv[argv.index("-b:v") + 1] == "635k"
    assert argv[argv.index("-maxrate") + 1] == "635k"
    assert argv[argv.index("-bufsize") + 1] == "1270k"
    assert argv[argv.index("-pix_fmt") + 1] == "yuv420p"
    # 音频：AAC 64 kbps 单声道（压片场景里立体声没意义，省一半）
    assert argv[argv.index("-c:a") + 1] == "aac"
    assert argv[argv.index("-b:a") + 1] == f"{EXPECTED_AUDIO_KBPS}k"
    assert argv[argv.index("-ac") + 1] == "1"
    assert argv[-2:] == ["+faststart", "out.mp4"]
    assert EXPECTED_HEIGHT == media.COMPRESS_HEIGHT_PX
    assert SECOND_PASS_FACTOR == media.COMPRESS_SECOND_PASS_FACTOR


def test_compress_argv_without_audio_drops_the_three_audio_flags() -> None:
    argv = media.compress_argv("src.mp4", "out.mp4", video_kbps=635, has_audio=False)
    for flag in ("-c:a", "-b:a", "-ac"):
        assert flag not in argv, f"无音轨时不该出现 {flag}（ffmpeg 会报错/造空音轨）"


# --------------------------------------------------------------------------- #
# 3. 预算判定：源已在预算内 ⇒ 0 次转码
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_source_within_budget_costs_zero_transcodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """t8 的 8 MiB 档 SKIP：源已经够小 ⇒ **0 ms、0 次 ffmpeg**，直接返回原路径。"""
    source = _write_source(tmp_path, 500_000)
    before = (source.stat().st_size, source.stat().st_mtime_ns, _sha256(source))
    calls = _patch_capture(monkeypatch, lambda _argv, _t: (0, b"", False))
    workdir = tmp_path / "work"
    workdir.mkdir()

    result = await media.compress_video(
        str(source),
        budget_bytes=600_000,
        duration_s=5.0,
        workdir=str(workdir),
        has_audio=True,
        timeout_s=30,
    )

    assert result.ok is True
    assert result.passes == 0
    assert result.path == str(source), "没有压缩就不该换路径"
    assert result.size == 500_000
    assert calls == [], "源已在预算内 ⇒ 不许调 ffmpeg"
    assert list(workdir.iterdir()) == [], "不该产生任何产物"
    assert (source.stat().st_size, source.stat().st_mtime_ns, _sha256(source)) == before


@pytest.mark.asyncio
async def test_infeasible_kbps_never_runs_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_source(tmp_path, 500_000)
    calls = _patch_capture(monkeypatch, lambda _argv, _t: (0, b"", False))

    result = await media.compress_video(
        str(source),
        budget_bytes=60_000,  # → kbps < 64
        duration_s=5.0,
        workdir=str(tmp_path),
        has_audio=True,
        timeout_s=30,
    )

    assert result.ok is False
    assert result.reason == "infeasible"
    assert result.passes == 0
    assert calls == [], "不可行就不许硬转"


# --------------------------------------------------------------------------- #
# 4. 两趟策略与失败形态
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_second_pass_recomputes_the_bitrate_with_the_095_factor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第 1 趟超预算 ⇒ 按 `kbps * budget / out * 0.95` 修正；第 2 趟命中 ⇒ `passes=2`。

    t8 的实测正是这条：285k ⇒ 2,098,819 B（超 0.08%）⇒ 269k ⇒ 2,008,617 B（命中）。
    """
    source = _write_source(tmp_path, 500_000)
    workdir = tmp_path / "work"
    workdir.mkdir()
    sizes = [300_000, 190_000]

    def _handler(argv, _timeout):
        _write_size(argv[-1], sizes.pop(0))
        return 0, b"", False

    calls = _patch_capture(monkeypatch, _handler)
    result = await media.compress_video(
        str(source),
        budget_bytes=200_000,
        duration_s=5.0,
        workdir=str(workdir),
        has_audio=True,
        timeout_s=30,
    )

    assert result.ok is True
    assert result.passes == 2
    assert result.size == 190_000
    assert result.size <= 200_000, "产物必须 ≤ 预算"
    assert result.path is not None and Path(result.path).parent == workdir
    first_kbps = int(calls[0]["argv"][calls[0]["argv"].index("-b:v") + 1].rstrip("k"))
    second_kbps = int(calls[1]["argv"][calls[1]["argv"].index("-b:v") + 1].rstrip("k"))
    assert first_kbps == media.compress_target_kbps(200_000, 5.0)
    assert second_kbps == int(first_kbps * 200_000 / 300_000 * SECOND_PASS_FACTOR)
    # 两趟共享一个 timeout 预算（不是每趟各一份）
    assert calls[0]["timeout"] > calls[1]["timeout"]


@pytest.mark.asyncio
async def test_two_passes_still_oversize_is_reported_not_looped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_source(tmp_path, 500_000)
    workdir = tmp_path / "work"
    workdir.mkdir()
    _patch_capture(monkeypatch, lambda argv, _t: (_write_size(argv[-1], 300_000), 0, b"", False)[1:])

    result = await media.compress_video(
        str(source),
        budget_bytes=200_000,
        duration_s=5.0,
        workdir=str(workdir),
        has_audio=True,
        timeout_s=30,
    )

    assert result.ok is False
    assert result.reason == "budget_exceeded"
    assert result.passes == media.COMPRESS_MAX_PASSES == 2, "最多 2 趟，绝不第 3 次"


@pytest.mark.asyncio
async def test_ffmpeg_failure_is_a_reason_not_an_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_source(tmp_path, 500_000)
    _patch_capture(monkeypatch, lambda _argv, _t: (1, b"Encoder not found", False))
    result = await media.compress_video(
        str(source), budget_bytes=200_000, duration_s=5.0,
        workdir=str(tmp_path), has_audio=True, timeout_s=30,
    )
    assert result.ok is False
    assert result.reason == "ffmpeg_failed"


@pytest.mark.asyncio
async def test_empty_product_is_a_reason_not_an_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_source(tmp_path, 500_000)
    _patch_capture(monkeypatch, lambda _argv, _t: (0, b"", False))  # 不写产物
    result = await media.compress_video(
        str(source), budget_bytes=200_000, duration_s=5.0,
        workdir=str(tmp_path), has_audio=True, timeout_s=30,
    )
    assert result.ok is False
    assert result.reason == "empty"


@pytest.mark.asyncio
async def test_timeout_is_shared_across_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`compress_timeout_seconds` 是**整块**预算：第 1 趟吃光它，第 2 趟不许再来一次完整预算。"""
    source = _write_source(tmp_path, 500_000)
    workdir = tmp_path / "work"
    workdir.mkdir()

    async def _slow(argv, *, timeout):
        # 注意实现里的**下限**：`deadline = now + max(1.0, timeout_s)`（防 0 预算立刻退出）。
        # 所以要吃掉"整块预算"得让第 1 趟睡接近 1 s，第 2 趟才会撞上 `remaining <= 0.2`。
        await asyncio.sleep(0.9)
        _write_size(argv[-1], 300_000)  # 故意超预算，逼出第 2 趟
        return 0, b"", False

    monkeypatch.setattr(media, "_run_capture", _slow)
    result = await media.compress_video(
        str(source), budget_bytes=200_000, duration_s=5.0,
        workdir=str(workdir), has_audio=True, timeout_s=0.3,
    )

    assert result.ok is False
    assert result.reason == "timeout"
    assert result.passes == 2, "第 2 趟被剩余预算拒掉（不是再给一整份）"


@pytest.mark.asyncio
async def test_timeout_from_the_capture_layer_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_source(tmp_path, 500_000)
    _patch_capture(monkeypatch, lambda _argv, _t: (-1, b"timeout", True))
    result = await media.compress_video(
        str(source), budget_bytes=200_000, duration_s=5.0,
        workdir=str(tmp_path), has_audio=True, timeout_s=30,
    )
    assert result.ok is False
    assert result.reason == "timeout"
    assert result.passes == 1


@pytest.mark.asyncio
async def test_missing_source_is_ffmpeg_failed(tmp_path: Path) -> None:
    result = await media.compress_video(
        str(tmp_path / "nope.mp4"), budget_bytes=200_000, duration_s=5.0,
        workdir=str(tmp_path), has_audio=True, timeout_s=30,
    )
    assert result.ok is False
    assert result.reason == "ffmpeg_failed"


# --------------------------------------------------------------------------- #
# 5. 无音轨：去音频参 + 标记（供 analyze 记 `video_compressed_noaudio`）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_no_audio_source_records_the_flag_and_drops_audio_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_source(tmp_path, 500_000)
    workdir = tmp_path / "work"
    workdir.mkdir()
    calls = _patch_capture(
        monkeypatch, lambda argv, _t: (_write_size(argv[-1], 190_000), 0, b"", False)[1:]
    )

    result = await media.compress_video(
        str(source), budget_bytes=200_000, duration_s=5.0,
        workdir=str(workdir), has_audio=False, timeout_s=30,
    )

    assert result.ok is True
    assert result.no_audio is True
    assert "-c:a" not in calls[0]["argv"]


# --------------------------------------------------------------------------- #
# 6. 真 ffmpeg：预算真的达成、产物只在 workdir、源只读
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_real_transcode_hits_the_budget_in_tmp_and_leaves_the_source_alone(
    big_video: Path, tmp_path: Path
) -> None:
    """真转码：产物 ≤ 预算、落在 `/tmp/…` 的 workdir 里、源文件字节与 mtime 不变。

    这条是"§6.2 的 argv 真的能跑"的唯一证据（其它用例都用假 `_run_capture` 测策略）。
    """
    source = tmp_path / "source.mp4"
    shutil.copyfile(big_video, source)
    before = (source.stat().st_size, source.stat().st_mtime_ns, _sha256(source))
    assert before[0] > 110_000, "夹具不够大，压不出区别"

    workdir = Path(tempfile.mkdtemp(prefix="media_analysis_compress_test_"))
    try:
        result = await media.compress_video(
            str(source),
            budget_bytes=110_000,
            duration_s=5.0,
            workdir=str(workdir),
            has_audio=True,
            timeout_s=30,
        )

        assert result.ok is True, (result.reason, result.size)
        assert result.size is not None and result.size <= 110_000
        assert result.passes >= 1
        assert os.path.getsize(str(result.path)) == result.size
        # 产物只在 workdir 里，且 workdir 在 /tmp 下
        assert Path(str(result.path)).parent == workdir
        assert str(workdir).startswith(tempfile.gettempdir())
        assert [p.name for p in workdir.iterdir()] == [Path(str(result.path)).name]
        # 源只读：字节、mtime、sha 一样，且源目录里没有多出产物
        assert (source.stat().st_size, source.stat().st_mtime_ns, _sha256(source)) == before
        assert sorted(p.name for p in tmp_path.iterdir()) == ["source.mp4"]
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@pytest.mark.asyncio
async def test_real_fixture_too_small_for_the_kbps_floor_is_infeasible(
    big_video: Path, tmp_path: Path
) -> None:
    """真文件 + 太小预算 ⇒ `infeasible`（绝不硬转），且不产生任何产物。"""
    workdir = Path(tempfile.mkdtemp(prefix="media_analysis_compress_test_"))
    try:
        result = await media.compress_video(
            str(big_video),
            budget_bytes=60_000,
            duration_s=5.0,
            workdir=str(workdir),
            has_audio=True,
            timeout_s=30,
        )
        assert result.ok is False
        assert result.reason == "infeasible"
        assert list(workdir.iterdir()) == []
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
