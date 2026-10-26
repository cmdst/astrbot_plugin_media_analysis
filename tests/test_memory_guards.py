"""内存护栏（SPEC-v0.3 §10 / §15-T9）：读文件之前先看大小；该释放的立刻释放。

v0.3 第一次把**整段视频**读进内存做 base64。§10.1 的算式写死在这里：峰值 ≈3.7N
（保守按 5N 规划），50 MiB ⇒ 185–250 MiB。所以护栏是硬要求，不是优化：

1. **`stat` 先判**：`video.max_direct_bytes`（默认 8 MiB）先拦下大部分；`call_native` 里还有
   32 MiB 的硬上限兜底 —— 两道闸都在"读文件"**之前**；
2. **超硬上限 ⇒ 一次都不读**（用桩断言，不是只看错误码）；
3. **base64 之后立刻 `del raw`**：不保留第二份原始字节（峰值从 5N 降到 ≈3.7N）；
4. **内容哈希是流式的**（`DIGEST_CHUNK_BYTES = 1 MiB`）：算 `digest` 不需要把整个文件读进来；
5. **群文件下载是边读边数的**（`max_media_bytes` 到了就放弃）：这条在
   `tests/test_file_segment.py::test_download_bounded_*`，这里只钉"不读全文件"的另外两条。

本文件用**真文件 + 桩**验证"有没有读/读了多少"，不依赖内容（视频字节全是 0 也无所谓）。
"""

from __future__ import annotations

import builtins
import hashlib
import inspect
from pathlib import Path

import pytest

from astrbot_plugin_media_analysis import media, video_native
from _fakes import native_video


def test_hard_cap_is_32_mib_and_larger_than_the_default_direct_limit() -> None:
    """32 MiB 是"Python 侧 base64 峰值 ≈42.6 MiB、总峰值 ≤160 MiB"的上界（§10.2-③）。

    它必须 ≥ 配置钳位上限（1–32 MiB）：否则用户把门槛配到 32 MiB 时，每次调用都会
    在 `call_native` 里 stat 失败 —— 白跑一次、还查不出原因。
    """
    from astrbot_plugin_media_analysis import settings

    assert video_native.HARD_MAX_DIRECT_BYTES == 32 * 1024 * 1024
    # v0.5.1：配置面是 MB（`video.max_direct_mb`），字节由 `settings.mb_to_bytes()` 派生
    assert (
        settings.mb_to_bytes(settings.MAX_VIDEO_MAX_DIRECT_MB)
        <= video_native.HARD_MAX_DIRECT_BYTES
    )
    assert (
        settings.mb_to_bytes(settings.DEFAULT_VIDEO_MAX_DIRECT_MB)
        < video_native.HARD_MAX_DIRECT_BYTES
    )


@pytest.mark.asyncio
async def test_oversize_video_is_rejected_before_any_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T9 原话：超标时**不得调用** `read_bytes` —— 让桩直接抛错，读一次就红。"""
    path = tmp_path / "huge.mp4"
    path.write_bytes(b"\x00" * 1024)

    def _never(_path: str) -> bytes:
        raise AssertionError("超硬上限的文件被读了（护栏失效）")

    monkeypatch.setattr(media, "read_bytes", _never)
    monkeypatch.setattr(media, "_size_of", lambda _p: video_native.HARD_MAX_DIRECT_BYTES + 1)

    outcome = await video_native.call_native(
        native_video(), prompt="p", video_path=str(path), max_tokens=1024, timeout=5
    )

    assert outcome.ok is False
    assert outcome.note == video_native.NOTE_FAILED
    assert outcome.detail.startswith("too_large ")


@pytest.mark.asyncio
async def test_under_the_cap_reads_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上限内**恰好读一次**（不是"读一次再读一次算 base64"）。"""
    path = tmp_path / "small.mp4"
    path.write_bytes(b"\x00" * 2048)
    reads: list[str] = []
    real = media.read_bytes

    def _record(where: str) -> bytes:
        reads.append(where)
        return real(where)

    monkeypatch.setattr(media, "read_bytes", _record)

    outcome = await video_native.call_native(
        native_video(), prompt="p", video_path=str(path), max_tokens=1024, timeout=5
    )

    assert outcome.ok is True
    assert reads == [str(path)]


def test_raw_bytes_are_released_right_after_base64() -> None:
    """`del raw` 必须在 base64 之后（§10.1：峰值 ≈3.7N 而不是 5N）。

    静态守卫（白盒，重构可改）：只要实现里出现"把 `raw` 留到调用之后"的写法，
    峰值就会多出一份原始字节 —— 50 MiB 时那是 50 MiB 的差距。
    """
    import re

    source = inspect.getsource(video_native.call_native)
    assert "base64.b64encode(raw)" in source
    assert "del raw" in source, "base64 之后必须立刻释放原始字节"
    # `del raw` 之后**一不许再碰** raw（峰值会多一份原始字节）。
    # 用最后一次出现定位：docstring 里也提到了 `del raw`（那是说明，不是代码）。
    after_del = source.rpartition("del raw")[2]
    assert not re.search(r"\braw\b", after_del), "del raw 之后又引用了 raw"


@pytest.mark.asyncio
async def test_content_digest_streams_in_bounded_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """内容哈希是**流式**的：每次 `read(DIGEST_CHUNK_BYTES)`，不把整个文件读进来。

    一条 3 MiB 的视频如果被一次性读入，digest 这一步就白占 3 MiB（而它只是查缓存用的
    16 个 hex）。这条用例用 `builtins.open` 的桩断言**每块大小**。
    """
    size = 3 * media.DIGEST_CHUNK_BYTES + 123
    target = tmp_path / "big.mp4"
    target.write_bytes(bytes(range(256)) * (size // 256) + b"\x00" * (size % 256))
    expected = hashlib.sha256(target.read_bytes()).hexdigest()[:16]

    sizes: list[int] = []
    real_open = builtins.open

    class _Recorder:
        def __init__(self, handle) -> None:
            self._handle = handle

        def read(self, amount=-1):
            sizes.append(amount)
            return self._handle.read(amount)

        def __enter__(self):
            return self

        def __exit__(self, *_exc) -> None:
            self._handle.close()

    def _open(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        if str(path) == str(target) and "b" in str(mode):
            return _Recorder(handle)
        return handle

    monkeypatch.setattr(builtins, "open", _open)

    digest = await media.content_digest(str(target))

    assert digest == expected
    assert sizes, "一次 read 都没发生？"
    assert all(amount == media.DIGEST_CHUNK_BYTES for amount in sizes), sizes
    # 3 MiB + 123 B ⇒ 4 个满/尾块 + 1 次读到 EOF 的空块（`iter(..., b"")` 的判定）
    assert len(sizes) == 5, f"期望 4 块 + 1 次 EOF 探测，实际 {len(sizes)}"
    assert media.DIGEST_CHUNK_BYTES == 1 << 20
