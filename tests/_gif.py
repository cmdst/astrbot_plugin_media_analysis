"""手工构造的最小 GIF / 图片字节（**零依赖、零网络、零真实群媒体**）。

为什么自己造，而不是用夹具或三方库（SPEC-v0.4 §9 的测试纪律）：

- 本仓库**刻意不引第三方依赖**（无 `pyproject.toml` / 无 requirements）⇒ 不能用 Pillow；
- v0.4 的 GIF 判定是**魔数**（前 6 字节）⇒ "有 / 没有 GIF 魔数"这两种字节必须能**精确
  控制**；用真实样本既不可复现，也会把群媒体带进库（AGENTS.md §8）；
- 转换链（`media.normalize_gif`）的用例需要**能被 ffmpeg 解码**的 GIF ⇒ 这里实现的是一个
  **真的**最小 GIF 编码器（GIF87a/GIF89a + GCE + Netscape 循环扩展 + LZW + 子块），
  不是"只有 6 字节魔数的假文件"。真 ffmpeg 的解码验证见
  `tests/test_gif.py::test_hand_built_gif_is_decodable_by_ffmpeg`（缺 ffmpeg 则 skip）。

GIF89a 的字节结构（本模块产物的规范形态）::

    47 49 46 38 39 61                 "GIF89a"（单帧且 delay_cs=0 时是 "GIF87a"）
    <w:u16le> <h:u16le> 0xF1 00 00    逻辑屏幕描述符（0xF1 = 有 4 色全局色表）
    <12 字节 GCT: 黑 / 红 / 绿 / 蓝>
    21 FF 0B "NETSCAPE2.0" 03 01 00 00 00   循环扩展（帧数 > 1 才有）
    [每帧]
      21 F9 04 00 <delay:u16le> 00 00      图形控制扩展（delay 单位 1/100 s）
      2C <l> <t> <w> <h> 00                 图像描述符（无局部色表）
      <mcs=2> <LZW 子块...> 00              图像数据（min code size=2 ⇒ 4 色）
    3B                                   trailer

两个**踩过的坑**（都在本文件里被注释钉住，别再踩一遍）：

1. GCE 的 4 字节载荷后面**还有一个** `00` 块终止符 —— 少写这一个字节，`ffprobe` 仍能
   报出宽高（所以"能 probe 出宽高"不等于结构正确），但 `ffmpeg` 解码直接 rc=69；
2. LZW 的**码宽增长**必须在"下一个可用码 == 2**width"时进位（而不是 `>` 之后），
   且要与解码器逐码模拟 —— 写错时 `ffprobe` 报 `duration=1.0`（只认到第一帧）而看不出
   "码流坏"，只有真解码才暴露。
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

__all__ = [
    "GIF87A",
    "GIF89A",
    "gif_bytes",
    "jpeg_bytes",
    "png_bytes",
    "webp_bytes",
    "write_gif",
]

GIF87A = b"GIF87a"
GIF89A = b"GIF89a"

#: 全局色表：4 色（黑 / 红 / 绿 / 蓝）⇒ `packed` 的 size 字段 = 1，min code size = 2。
_GCT = bytes([0, 0, 0, 255, 0, 0, 0, 255, 0, 0, 0, 255])
_MIN_CODE_SIZE = 2


# --------------------------------------------------------------------------- #
# GIF：LZW（真编码器，不是"能过魔数就算"）
# --------------------------------------------------------------------------- #
def _lzw_codes(pixels: list[int], min_code_size: int) -> list[int]:
    """标准 GIF LZW：输出 `clear … <码> … end` 的码序列（不含码宽信息）。"""
    clear = 1 << min_code_size
    end = clear + 1
    table: dict[tuple[int, ...], int] = {(index,): index for index in range(clear)}
    next_code = end + 1
    codes = [clear]
    prefix: tuple[int, ...] = ()
    for pixel in pixels:
        candidate = prefix + (pixel,)
        if candidate in table:
            prefix = candidate
            continue
        codes.append(table[prefix])
        if next_code < 4096:
            table[candidate] = next_code
            next_code += 1
        else:  # 字典满：发 clear 重置（本模块的小图永远走不到，保留以防将来加尺寸）
            codes.append(clear)
            table = {(index,): index for index in range(clear)}
            next_code = end + 1
        prefix = (pixel,)
    if prefix:
        codes.append(table[prefix])
    codes.append(end)
    return codes


def _pack_codes(pixels: list[int], min_code_size: int) -> bytes:
    """把码序列按**解码器视角**的码宽逐码写进字节流（LSB 优先）。

    码宽增长必须与解码器**逐码**同步：解码器在"处理完一个码、表里新增一项使
    `next_code == 1 << width`"时进位。写成 `>` 就会晚一位 ⇒ 高位码被截断 ⇒
    `ffmpeg` 报 `Invalid data found when processing input`（坑 2）。
    """
    clear = 1 << min_code_size
    end = clear + 1
    width = min_code_size + 1
    next_code = end + 1
    previous: int | None = None
    out = bytearray()
    accumulator = 0
    bits = 0
    for code in _lzw_codes(pixels, min_code_size):
        accumulator |= (code & ((1 << width) - 1)) << bits
        bits += width
        while bits >= 8:
            out.append(accumulator & 0xFF)
            accumulator >>= 8
            bits -= 8
        if code == clear:
            width = min_code_size + 1
            next_code = end + 1
            previous = None
            continue
        if code == end:
            continue
        if previous is not None and next_code < 4096:
            next_code += 1
            if next_code == (1 << width) and width < 12:
                width += 1
        previous = code
    if bits:
        out.append(accumulator & 0xFF)
    return bytes(out)


def _sub_blocks(data: bytes, *, size: int = 255) -> bytes:
    """LZW 数据装进 GIF 的"长度前缀子块"，末尾 `00` 块终止符。"""
    out = bytearray()
    for offset in range(0, len(data), size):
        chunk = data[offset : offset + size]
        out.append(len(chunk))
        out += chunk
    out.append(0)
    return bytes(out)


def _image_data(width: int, height: int) -> bytes:
    """一个图像描述符之后的"min code size + LZW 子块"（4 色渐变，逐像素不重复）。"""
    colors = 1 << _MIN_CODE_SIZE
    pixels = [((x + y) % colors) for y in range(height) for x in range(width)]
    return bytes([_MIN_CODE_SIZE]) + _sub_blocks(_pack_codes(pixels, _MIN_CODE_SIZE))


def gif_bytes(
    *,
    width: int = 8,
    height: int = 8,
    frames: int = 1,
    delay_cs: int = 10,
    version: bytes | None = None,
) -> bytes:
    """造一份最小 GIF（默认 8x8 / 单帧 / 0.1 s）。

    Args:
        width / height: 像素尺寸。**奇数宽**是 v0.4 的关键回归用例（M9：奇数宽 +
            `yuv420p` + libx264 ⇒ rc=187、0 字节 ⇒ 冻结 argv 里的 `trunc(.../2)*2`）。
        frames: 帧数；`> 1` 时写 Netscape 循环扩展并逐帧写 GCE（真动图）。
        delay_cs: 每帧时长（1/100 s）。`frames == 1 and delay_cs == 0` ⇒ **不写 GCE**，
            这是"没有 duration 的静态 GIF"（`get_media_duration` 实测返回 `None`）。
        version: 强制魔数（`b"GIF87a"` / `b"GIF89a"`）；默认单帧用 87a、多帧用 89a。
    """
    if frames < 1:
        raise ValueError("frames 至少 1")
    if version is None:
        version = GIF87A if frames <= 1 else GIF89A

    out = bytearray(version)
    out += struct.pack("<HH", width, height)
    out.append(0xF1)  # 全局色表存在（0x80）| 色彩分辨率 7（0x70）| 表大小 1（4 色）
    out.append(0x00)  # 背景色索引
    out.append(0x00)  # 像素宽高比
    out += _GCT
    if frames > 1:
        # 循环扩展（NETSCAPE2.0）：子块 `03 01 <loop:u16le> 00`，loop=0 表示无限循环
        out += b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00"
    for _ in range(frames):
        if frames > 1 or delay_cs:
            # GCE：21 F9 04 <packed> <delay:u16le> <透明色索引> **00 块终止符**
            out += b"\x21\xf9\x04" + bytes([0x00])
            out += struct.pack("<H", int(delay_cs))
            out += b"\x00\x00"  # 透明色索引 + 块终止符（缺一字节 = 坑 1）
        out += b"\x2c" + struct.pack("<HHHH", 0, 0, width, height) + bytes([0x00])
        out += _image_data(width, height)
    out += b"\x3b"
    return bytes(out)


def write_gif(path: str | Path, **kwargs: object) -> Path:
    """把 `gif_bytes(**kwargs)` 写到 `path`（父目录自动建），返回 `Path`。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(gif_bytes(**kwargs))  # type: ignore[arg-type]
    return target


# --------------------------------------------------------------------------- #
# 反例字节：PNG / APNG / JPEG / WebP（**只有 PNG 是可解码的真图**）
# --------------------------------------------------------------------------- #
def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def png_bytes(*, width: int = 8, height: int = 8, apng: bool = False) -> bytes:
    """一份**真能解码**的最小 8 bit 真彩 PNG（`apng=True` 时变成 **2 帧的真 APNG**）。

    为什么要真图：SPEC-v0.4 §1.2-M6 的事实是"`ffprobe` 对 PNG/APNG **也**报
    `codec_type=video`" ⇒ 只有拿真 PNG/APNG 才能断言"能解出视频流 ≠ 是 GIF"，
    以及"PNG 今天被拒靠的是**时长缺失**这个副作用"（两者的 `format.duration` 都是 `N/A`）。

    APNG 的块序必须是 `acTL` → `fcTL(0)` → `IDAT` → `fcTL(1)` → `fdAT(1)` → `IEND`；
    只写 `acTL` 不写 `fcTL`/`fdAT` 时 `ffprobe` 直接报 `Invalid data`（实测），
    那样就不是"能解出视频流的 APNG"了，反例也就失去意义。
    """
    rows = bytearray()
    for y in range(height):
        rows.append(0x00)  # filter type 0（None）
        for x in range(width):
            rows += bytes([(x * 32) % 256, (y * 32) % 256, 128])
    data = zlib.compress(bytes(rows), 9)

    out = bytearray(b"\x89PNG\r\n\x1a\n")
    out += _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    if apng:
        out += _png_chunk(b"acTL", struct.pack(">II", 2, 0))  # 2 帧 / 无限循环
        out += _png_chunk(
            b"fcTL", struct.pack(">IIIIIHHBB", 0, width, height, 0, 0, 2, 10, 0, 0)
        )
    out += _png_chunk(b"IDAT", data)
    if apng:
        out += _png_chunk(
            b"fcTL", struct.pack(">IIIIIHHBB", 1, width, height, 0, 0, 2, 10, 0, 0)
        )
        out += _png_chunk(b"fdAT", struct.pack(">I", 2) + data)
    out += _png_chunk(b"IEND", b"")
    return bytes(out)


def jpeg_bytes(*, width: int = 8, height: int = 8) -> bytes:
    """JPEG 头（SOI + APP0 JFIF + SOF0 + EOI）——**只用于魔数反例**。

    本插件从不解码图片（判据只有 GIF 魔数），所以这里不需要真能解码的熵编码数据；
    刻意不造完整 JPEG（那要手写 Huffman 表，纯噪声）。文档里写清，免得后来者误以为
    这是"能解码的 JPEG"。
    """
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = (
        b"\xff\xc0"
        + struct.pack(">H", 11)
        + bytes([8])
        + struct.pack(">HH", height, width)
        + bytes([1, 0x01, 0x11, 0x00])
    )
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def webp_bytes(*, width: int = 8, height: int = 8) -> bytes:
    """WebP（RIFF/WEBP + VP8 关键帧头）——**只用于魔数反例**（同 `jpeg_bytes`）。"""
    payload = (
        b"VP8 "
        + struct.pack("<I", 10)
        + b"\x9d\x01\x2a"
        + struct.pack("<HH", width & 0x3FFF, height & 0x3FFF)
        + b"\x00\x00"
    )
    return b"RIFF" + struct.pack("<I", len(payload) + 4) + b"WEBP" + payload
