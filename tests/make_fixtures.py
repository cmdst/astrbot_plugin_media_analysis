#!/usr/bin/env python3
"""合成媒体夹具生成器（**幂等、可重跑、零真实群媒体**）。

造什么：一批用 ffmpeg `lavfi` 源凭空合成的文件 —— 音轨是 `sine`（440 Hz 正弦波），
画面是 `testsrc`（彩条 + 计数器），**没有任何真实群媒体、没有真人画面、没有 OCR 文本**。
这既是 SPEC §9-7 的要求（测试夹具自造），也让夹具可以随手重跑、小心提交。

为什么落 `/tmp` 而不是 `tests/fixtures/`：
本 lane 的房屋约定是"中间产物只落 /tmp"（`AGENTS.md` §8），二进制不进库；`tests/` 只留
**生成脚本**。`pytest` 首次运行会由 `conftest.py` 的 `media_fixtures` fixture 自动生成；
也可以手动：

    python3 tests/make_fixtures.py                 # 生成/补齐（已存在且形状对的跳过）
    python3 tests/make_fixtures.py --verify        # 只复核，不生成
    python3 tests/make_fixtures.py --force         # 全部重造
    python3 tests/make_fixtures.py --dir /tmp/xx   # 换目录（conftest 认 $MA_FIXTURES_DIR）

幂等口径：同样参数的 ffmpeg 命令在本机**逐字节可复现**（实测 2026-10-22：`-fflags +bitexact`
+ `-flags:v/+flags:a +bitexact` + `-map_metadata -1` 后，两次生成的 mp4/wav sha256 相同）
⇒ `MANIFEST.sha256` 既用于"夹具没被换过"的比对，也能在别人机器上复现。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "FIXTURES",
    "FixtureError",
    "ProbeResult",
    "default_dir",
    "ensure_all",
    "main",
    "probe",
    "verify_all",
]

# 单文件上限：1 MB（任务要求）。实测最大的是 30 s/16 kHz 单声道 wav ≈ 960 KB。
MAX_BYTES = 1_000_000

WIDTH, HEIGHT, FPS = 320, 180, 10
TONE_HZ = 440
AAC_RATE = 8000  # 合成音轨的采样率（视频里的 aac），只为把体积压到 1 MB 内
WAV_RATE = 16000  # wav 夹具用 16 kHz —— 与插件抽音轨的目标格式一致（SPEC §7.4）
CRF = 40

_ENV_DIR = "MA_FIXTURES_DIR"
_DEFAULT_DIR = Path("/tmp/media_analysis_fixtures")


class FixtureError(RuntimeError):
    """夹具生成/复核失败（缺 ffmpeg、ffmpeg 返回非 0、形状不符……）。"""


@dataclass(frozen=True)
class ProbeResult:
    duration_s: float | None
    has_audio: bool
    has_video: bool


@dataclass(frozen=True)
class FixtureSpec:
    """一份夹具：文件名 + 生成 argv + 期望形状。"""

    name: str
    duration_s: float | None
    has_audio: bool
    has_video: bool
    note: str
    argv: tuple[str, ...]


# --------------------------------------------------------------------------- #
# argv（**argv 列表，不经 shell**；与 SPEC §7.4 的插件调用同形）
# --------------------------------------------------------------------------- #
def _video_argv(out: Path, *, seconds: int, with_audio: bool) -> list[str]:
    argv = [
        "ffmpeg", "-nostdin", "-y", "-v", "error", "-fflags", "+bitexact",
        "-f", "lavfi", "-i", f"testsrc=size={WIDTH}x{HEIGHT}:rate={FPS}:duration={seconds}",
    ]
    if with_audio:
        argv += [
            "-f", "lavfi", "-i", f"sine=frequency={TONE_HZ}:sample_rate={AAC_RATE}:duration={seconds}",
            "-map", "0:v", "-map", "1:a",
            "-c:a", "aac", "-b:a", "16k", "-flags:a", "+bitexact",
        ]
    else:
        argv += ["-an"]
    argv += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(CRF),
        "-pix_fmt", "yuv420p", "-flags:v", "+bitexact",
        "-movflags", "+faststart", "-map_metadata", "-1", str(out),
    ]
    return argv


def _wav_argv(out: Path, *, seconds: int) -> list[str]:
    return [
        "ffmpeg", "-nostdin", "-y", "-v", "error", "-fflags", "+bitexact",
        "-f", "lavfi", "-i", f"sine=frequency={TONE_HZ}:sample_rate={WAV_RATE}:duration={seconds}",
        "-ac", "1", "-ar", str(WAV_RATE), "-c:a", "pcm_s16le", "-flags:a", "+bitexact",
        "-map_metadata", "-1", str(out),
    ]


def _audio_only_mp4_argv(out: Path, *, seconds: int) -> list[str]:
    return [
        "ffmpeg", "-nostdin", "-y", "-v", "error", "-fflags", "+bitexact",
        "-f", "lavfi", "-i", f"sine=frequency={TONE_HZ}:sample_rate={AAC_RATE}:duration={seconds}",
        "-vn", "-c:a", "aac", "-b:a", "16k", "-flags:a", "+bitexact",
        "-movflags", "+faststart", "-map_metadata", "-1", str(out),
    ]


def _build_specs() -> tuple[FixtureSpec, ...]:
    specs = [
        FixtureSpec(
            "audio_30s.wav", 30.0, True, False,
            "30 s 纯音轨（Record 那条路；16 kHz 单声道，与抽音轨目标同形）",
            tuple(_wav_argv(Path("audio_30s.wav"), seconds=30)),
        ),
        FixtureSpec(
            "video_30s.mp4", 30.0, True, True,
            "30 s 视频 + 音轨（include_frames=false/true 两种路径的主样本）",
            tuple(_video_argv(Path("video_30s.mp4"), seconds=30, with_audio=True)),
        ),
        FixtureSpec(
            "video_70s.mp4", 70.0, True, True,
            "70 s 视频（> 默认 limits.max_media_seconds=60 ⇒ MEDIA_TOO_LONG）",
            tuple(_video_argv(Path("video_70s.mp4"), seconds=70, with_audio=True)),
        ),
        FixtureSpec(
            "video_5s.mp4", 5.0, True, True,
            "5 s 视频（SPEC §11.2：6 帧中点网格全部落在片内）",
            tuple(_video_argv(Path("video_5s.mp4"), seconds=5, with_audio=True)),
        ),
        FixtureSpec(
            "video_5s_noaudio.mp4", 5.0, False, True,
            "5 s 无音轨视频（SPEC #9 口径：ffprobe rc=0 但 stdout 为空）",
            tuple(_video_argv(Path("video_5s_noaudio.mp4"), seconds=5, with_audio=False)),
        ),
        FixtureSpec(
            "video_audio_only.mp4", 5.0, True, False,
            "只有音轨的 mp4（伪 Video 组件 ⇒ NO_VIDEO_TRACK 的那条路）",
            tuple(_audio_only_mp4_argv(Path("video_audio_only.mp4"), seconds=5)),
        ),
    ]
    return tuple(specs)


FIXTURES: tuple[FixtureSpec, ...] = _build_specs()
_BY_NAME = {spec.name: spec for spec in FIXTURES}


# --------------------------------------------------------------------------- #
# 路径与工具定位
# --------------------------------------------------------------------------- #
def default_dir() -> Path:
    """夹具目录：`$MA_FIXTURES_DIR` > `/tmp/media_analysis_fixtures`。"""
    raw = os.environ.get(_ENV_DIR)
    return Path(raw).expanduser().resolve() if raw else _DEFAULT_DIR


def _binary(name: str) -> str | None:
    return shutil.which(name)


def ffmpeg_available() -> bool:
    return _binary("ffmpeg") is not None and _binary("ffprobe") is not None


# --------------------------------------------------------------------------- #
# ffprobe
# --------------------------------------------------------------------------- #
def _run(argv: list[str], *, timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)


def _probe_stream(path: Path, kind: str) -> bool:
    """`kind` ∈ {"a","v"}；**无流时 rc=0 但 stdout 为空**（SPEC #9），所以只看输出。"""
    proc = _run([
        "ffprobe", "-v", "error", "-select_streams", kind,
        "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path),
    ], timeout=20.0)
    return bool(proc.stdout.strip())


def _probe_duration(path: Path) -> float | None:
    proc = _run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "csv=p=0", str(path),
    ], timeout=20.0)
    raw = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""
    try:
        return float(raw)
    except ValueError:
        return None


def probe(path: str | Path) -> ProbeResult:
    path = Path(path)
    return ProbeResult(_probe_duration(path), _probe_stream(path, "a"), _probe_stream(path, "v"))


# --------------------------------------------------------------------------- #
# 生成 / 复核
# --------------------------------------------------------------------------- #
def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _problems(path: Path, spec: FixtureSpec, *, deep: bool) -> list[str]:
    """形状复核：体积 + （deep 时）时长与流。"""
    found: list[str] = []
    if not path.is_file():
        return [f"{spec.name}: 不存在"]
    size = path.stat().st_size
    if size == 0:
        found.append(f"{spec.name}: 空文件")
    elif size > MAX_BYTES:
        found.append(f"{spec.name}: {size} 字节 > 上限 {MAX_BYTES}")
    if not deep:
        return found
    result = probe(path)
    if spec.duration_s is not None:
        if result.duration_s is None:
            found.append(f"{spec.name}: ffprobe 读不出时长")
        elif abs(result.duration_s - spec.duration_s) > 0.15:
            found.append(f"{spec.name}: 时长 {result.duration_s}s ≠ 期望 {spec.duration_s}s")
    if result.has_audio != spec.has_audio:
        found.append(f"{spec.name}: 音轨 {result.has_audio} ≠ 期望 {spec.has_audio}")
    if result.has_video != spec.has_video:
        found.append(f"{spec.name}: 视频流 {result.has_video} ≠ 期望 {spec.has_video}")
    return found


def _build(spec: FixtureSpec, directory: Path) -> None:
    out = directory / spec.name
    if not ffmpeg_available():
        raise FixtureError(f"生成 {spec.name} 需要 ffmpeg/ffprobe，本机找不到（PATH 里没有）")
    # argv 里的输出项记的是裸文件名（跟着 spec 走），这里换成目标目录下的绝对路径
    argv = [str(out) if item == spec.name else item for item in spec.argv]
    proc = _run(argv, timeout=180.0)
    if proc.returncode != 0:
        raise FixtureError(f"{spec.name}: ffmpeg rc={proc.returncode}\n{proc.stderr.strip()}")
    if not out.is_file():
        raise FixtureError(f"{spec.name}: ffmpeg 成功但没有产出文件")


def ensure_all(
    directory: str | Path | None = None,
    *,
    force: bool = False,
    quiet: bool = False,
) -> dict[str, Path]:
    """生成（或补齐）全部夹具，返回 `{stem: 路径}`。

    - 已存在且形状对的**不重造**（幂等；`force=True` 才全量重造）。
    - 缺 ffmpeg 但文件齐 ⇒ 直接返回（只在 `pytest` 里跑、不带 `--verify` 时够用）。
    - 缺 ffmpeg 且文件不齐 ⇒ `FixtureError`（conftest 会把它转成 skip）。
    """
    directory = Path(directory).expanduser().resolve() if directory is not None else default_dir()
    directory.mkdir(parents=True, exist_ok=True)
    deep = ffmpeg_available()

    for spec in FIXTURES:
        target = directory / spec.name
        if not force and target.is_file() and not _problems(target, spec, deep=deep):
            if not quiet:
                print(f"[skip] {spec.name}（已存在且形状正确）")
            continue
        if not deep:
            raise FixtureError(
                f"缺少 {spec.name} 且本机没有 ffmpeg/ffprobe，无法生成（目录：{directory}）"
            )
        _build(spec, directory)
        problems = _problems(target, spec, deep=True)
        if problems:
            raise FixtureError("生成后形状不符：" + "; ".join(problems))
        if not quiet:
            print(f"[make] {spec.name}（{target.stat().st_size} 字节）")

    manifest = directory / "MANIFEST.sha256"
    lines = [f"{_sha256(directory / spec.name)}  {spec.name}" for spec in FIXTURES]
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {Path(spec.name).stem: directory / spec.name for spec in FIXTURES}


def verify_all(directory: str | Path | None = None) -> list[str]:
    """复核全部夹具，返回问题清单（空＝全好）。"""
    directory = Path(directory).expanduser().resolve() if directory is not None else default_dir()
    problems: list[str] = []
    for spec in FIXTURES:
        problems.extend(_problems(directory / spec.name, spec, deep=ffmpeg_available()))
    return problems


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成/复核合成媒体夹具（纯 lavfi，无真实媒体）")
    parser.add_argument("--dir", default=None, help=f"夹具目录（默认 $%s 或 {_DEFAULT_DIR}）" % _ENV_DIR)
    parser.add_argument("--force", action="store_true", help="全部重造")
    parser.add_argument("--verify", action="store_true", help="只复核，不生成")
    parser.add_argument("--quiet", action="store_true", help="少打印")
    args = parser.parse_args(argv)

    directory = Path(args.dir).expanduser().resolve() if args.dir else default_dir()

    if args.verify:
        problems = verify_all(directory)
        if problems:
            print("夹具复核失败：", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            return 2
        print(f"夹具复核通过：{len(FIXTURES)} 个文件全部符合形状（{directory}）")
        return 0

    try:
        paths = ensure_all(directory, force=args.force, quiet=args.quiet)
    except FixtureError as exc:
        print(f"生成失败：{exc}", file=sys.stderr)
        return 3

    total = sum(path.stat().st_size for path in paths.values())
    print(f"夹具就绪：{len(paths)} 个，合计 {total} 字节（{directory}）")
    for stem, path in paths.items():
        print(f"  {path.name:<24} {path.stat().st_size:>8} 字节  {_BY_NAME[path.name].note}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
