"""GIF 以视频解析：双判据 / 候选优先级 / 四道闸 / 冻结 argv（SPEC-v0.4 §5）。

本文件守的是 **v0.4 的第一条主契约**：GIF 走"魔数判据 → ffmpeg 归一 mp4 → 既有视频链"，
而**普通图片绝不进链**。三条最容易退化的地方各有一组用例：

1. **判据退化成"能解出视频流"**：`ffprobe` 对 PNG/APNG/WebP 也报 `codec_type=video`
   （SPEC-v0.4 §1.2-M6），v0.3 拒 PNG 靠的是"没有时长"这个**副作用**。所以这里用
   **真 PNG/真 APNG** 断言"是视频流但不是 GIF ⇒ 仍然拒绝"，而不是只造 "NotGIF" 字符串。
2. **归一参数被"优化"掉**：`scale='trunc(min(640,iw)/2)*2'` 里的 `trunc(.../2)*2` 是
   **硬要求**（奇数宽 + `yuv420p` + libx264 ⇒ rc=187、**0 字节产物**，M9 实测）；
   `gif_argv` 做逐字符快照，另有真 241x162 样本的端到端转换（输出 240 宽）。
3. **失败被静默成"看起来成功"**：ffmpeg 失败/超时/空产物/超限一律
   `GIF_CONVERT_FAILED`（或 `MEDIA_TOO_LARGE`），**绝不**回退成"把原始 GIF 直接送模型"
   —— 那正是 v0.3 `File .gif` 的缺陷形态（原始 GIF 字节被贴 `data:video/mp4` 直送）。

字节构造在 `tests/_gif.py`（零依赖手工编码器，真能被 ffmpeg 解码）；需要 ffmpeg 的用例
一律 `skip`（附原因），不用真群媒体（AGENTS.md §8）。
"""

from __future__ import annotations

import base64
import inspect
import shutil
import subprocess

import pytest

from astrbot.api.message_components import Image

from astrbot_plugin_media_analysis import envelope, media
from _fakes import (
    make_event,
    make_file,
    make_image,
    make_record,
    make_video,
    native_video,
)
from _gif import (
    GIF87A,
    GIF89A,
    gif_bytes,
    jpeg_bytes,
    png_bytes,
    webp_bytes,
    write_gif,
)
from _rig import build_rig

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def _require_ffmpeg() -> None:
    """归一/解码类用例的 skip 保护（本 lane 有 ffmpeg，但别的机器可能没有）。"""
    if not (FFMPEG and FFPROBE):
        pytest.skip("需要 /usr/bin/ffmpeg 与 /usr/bin/ffprobe（GIF 归一链的真解码验证）")


def _code(exc_info: pytest.ExceptionInfo) -> str:
    assert isinstance(exc_info.value, envelope.ToolError), type(exc_info.value)
    return exc_info.value.code


def _probe(path: str) -> dict[str, str]:
    """`ffprobe` 的 `format` + 首条流的关键字段（`key=value` 摊平，缺失就是 `"N/A"`）。"""
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-count_frames",
            "-show_entries",
            "format=duration,format_name:stream=codec_type,width,height,nb_read_frames",
            "-of",
            "default=noprint_wrappers=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    pairs: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        key, _, value = line.partition("=")
        if key:
            pairs.setdefault(key.strip(), value.strip())
    return pairs


def _patch_convert(
    monkeypatch: pytest.MonkeyPatch,
    *,
    rc: int = 0,
    stderr: bytes = b"",
    timed_out: bool = False,
    product: bytes | None = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32,
    frames: int = 8,
    duration: float = 0.08,
) -> dict[str, int]:
    """把 `normalize_gif` 的三个外部依赖换成可注入的假身（不跑真 ffmpeg）。

    三处：`gif_frame_count`（源帧数，真身要一次 ffprobe）、`_run_capture`（真 ffmpeg）、
    `probe_duration_s`（产物时长，真身要一次 ffprobe）。`duration=None` ⇒ 让探测抛
    `PROBE_FAILED`（测"产物时长读不出来"那条）。
    """
    calls = {"frame_count": 0, "run_capture": 0, "duration": 0}

    async def _frame_count(_path: str) -> int:
        calls["frame_count"] += 1
        return frames

    async def _run_capture(argv: list[str], *, timeout: float):
        calls["run_capture"] += 1
        assert argv[0] == "ffmpeg", argv
        assert timeout == media.GIF_CONVERT_TIMEOUT_S, timeout
        if product is not None:
            out_path = argv[-1]
            with open(out_path, "wb") as handle:
                handle.write(product)
        return rc, stderr, timed_out

    async def _duration(_path: str) -> float:
        calls["duration"] += 1
        if duration is None:
            raise envelope.ToolError(
                envelope.ERR_PROBE_FAILED, log_detail="duration unavailable"
            )
        return float(duration)

    monkeypatch.setattr(media, "gif_frame_count", _frame_count)
    monkeypatch.setattr(media, "_run_capture", _run_capture)
    monkeypatch.setattr(media, "probe_duration_s", _duration)
    return calls


# --------------------------------------------------------------------------- #
# §5.2 判据一：魔数（唯一判据）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("version", [GIF87A, GIF89A])
def test_has_gif_magic_accepts_both_versions(tmp_path, version: bytes) -> None:
    """§5.2：`GIF87a` 与 `GIF89a` 都是 GIF（魔数集合逐字冻结）。"""
    path = write_gif(tmp_path / "x.gif", version=version)
    assert media.has_gif_magic(str(path)) is True
    assert media.GIF_MAGICS == (b"GIF87a", b"GIF89a")
    assert media.GIF_SNIFF_BYTES == 6


@pytest.mark.parametrize(
    "payload",
    [png_bytes(), png_bytes(apng=True), jpeg_bytes(), webp_bytes(), b"plain text here"],
    ids=["png", "apng", "jpeg", "webp", "text"],
)
def test_has_gif_magic_rejects_everything_else(tmp_path, payload: bytes) -> None:
    """§5.5 反例矩阵：PNG/APNG/JPEG/WebP/文本**全都不是** GIF（后缀/容器类型都不看）。"""
    path = tmp_path / "x.gif"  # 名字骗人（.gif）也一样
    path.write_bytes(payload)
    assert media.has_gif_magic(str(path)) is False


def test_has_gif_magic_requires_six_readable_bytes(tmp_path) -> None:
    """拿不到 6 字节证据就不放行：缺文件 / 目录 / 只有 5 字节 ⇒ `False`（不是抛错）。"""
    assert media.has_gif_magic(str(tmp_path / "missing.gif")) is False
    assert media.has_gif_magic(str(tmp_path)) is False
    short = tmp_path / "short.gif"
    short.write_bytes(b"GIF89")
    assert media.has_gif_magic(str(short)) is False


def test_gif_normalisation_constants_are_frozen() -> None:
    """§5.6：归一参数是**代码常量**（0 新配置键）—— 改它们等于改版本，必须过评审。"""
    assert media.GIF_MAX_FPS == 12
    assert media.GIF_MAX_WIDTH_PX == 640
    assert media.GIF_CRF == 26
    assert media.GIF_CONVERT_TIMEOUT_S == 10.0


# --------------------------------------------------------------------------- #
# §5.2 判据二：后缀只作"要不要去证实"
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "values, expected",
    [
        (("clip.gif",), True),
        (("CLIP.GIF",), True),
        (("https://x/y.gif?sig=abc#frag",), True),
        (("clip.png",), False),
        (("clip.gifx",), False),
        (("", None, 42), False),
        ((), False),
    ],
)
def test_looks_like_gif_is_only_a_hint(values, expected: bool) -> None:
    """`_looks_like_gif` 只回答"要不要去证实"：大小写无关、砍掉 `?query`/`#fragment`。"""
    assert media._looks_like_gif(*values) is expected


# --------------------------------------------------------------------------- #
# §5.2 / §5.5 候选优先级：Record → Video → Image(GIF) → File
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_find_media_accepts_gif_image_with_gif_suffix(tmp_path) -> None:
    """`Image` 有本地文件 + 魔数 GIF ⇒ 受理（`gif=True`），`kind` 直接是 video。"""
    path = write_gif(tmp_path / "sticker.gif", width=16, height=16, frames=4)
    event = make_event(current=[make_image(path)])

    handle = await media.find_media(event, "current")

    assert isinstance(handle.comp, Image)
    assert handle.gif is True
    assert handle.kind == "video"
    assert handle.quoted is False and handle.from_file is False


@pytest.mark.asyncio
async def test_find_media_accepts_gif_image_without_any_suffix(tmp_path) -> None:
    """**核心预处理会把后缀改写成 `.bin`/`.jpg`**（SPEC §5.2）⇒ 无后缀也必须受理。

    这条是"后缀不是判据"的正面用例：文件叫 `clip.bin`、`Image` 上没有 `name`，
    只有魔数命中 —— 受理；同一份字节换成 PNG 就拒绝（下一条）。
    """
    path = tmp_path / "clip.bin"
    path.write_bytes(gif_bytes(width=16, height=16, frames=3))
    event = make_event(current=[make_image(path)])

    handle = await media.find_media(event, "current")
    assert handle.gif is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [png_bytes(), png_bytes(apng=True), jpeg_bytes(), webp_bytes()],
    ids=["png", "apng", "jpeg", "webp"],
)
async def test_find_media_rejects_plain_images(tmp_path, payload: bytes) -> None:
    """契约 3 / §5.5：普通图片（含 APNG 动图）在**受理路径**就 `MEDIA_NOT_FOUND`。

    注意这些字节的 `ffprobe` 结果是 `codec_type=video`（真 PNG/APNG，见
    `test_png_and_apng_probe_as_video_but_are_still_not_gif`）—— 所以这条同时是
    "别拿'能解出视频流'当判据"的回归守卫。后缀还故意写成 `.gif`（改名党）。
    """
    path = tmp_path / "renamed.gif"
    path.write_bytes(payload)
    event = make_event(current=[make_image(path)])

    with pytest.raises(envelope.ToolError) as exc_info:
        await media.find_media(event, "current")
    assert _code(exc_info) == envelope.ERR_MEDIA_NOT_FOUND


@pytest.mark.asyncio
async def test_find_media_prefers_gif_image_over_file(tmp_path) -> None:
    """SPEC §5.2：`Image(GIF)` 排在 `File` **之前**（`File` 是兜底大类，可能是个 zip）。"""
    image_path = write_gif(tmp_path / "sticker.gif", width=16, height=16, frames=2)
    file_path = write_gif(tmp_path / "same.gif", width=16, height=16, frames=2)
    event = make_event(
        current=[make_file("same.gif", file=str(file_path)), make_image(image_path)]
    )

    handle = await media.find_media(event, "current")

    assert isinstance(handle.comp, Image)
    assert handle.from_file is False and handle.gif is True


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["record", "video"])
async def test_find_media_keeps_record_and_video_ahead_of_gif_image(tmp_path, kind) -> None:
    """既有三档的相对顺序不变：`Record` / `Video` 仍优先于 GIF 候选。"""
    gif_path = write_gif(tmp_path / "sticker.gif", width=16, height=16, frames=2)
    other = tmp_path / ("clip.wav" if kind == "record" else "clip.mp4")
    other.write_bytes(b"\x00" * 64)
    component = make_record(other) if kind == "record" else make_video(other)
    event = make_event(current=[make_image(gif_path), component])

    handle = await media.find_media(event, "current")

    assert isinstance(handle.comp, type(component))
    assert handle.gif is False


@pytest.mark.asyncio
async def test_find_media_quoted_gif_image(tmp_path) -> None:
    """引用链里的 GIF 同样受理，且 `quoted=True`。"""
    path = write_gif(tmp_path / "quoted.gif", width=16, height=16, frames=2)
    event = make_event(quoted=[make_image(path)])

    handle = await media.find_media(event, "quoted")

    assert handle.quoted is True and handle.gif is True


@pytest.mark.asyncio
async def test_find_media_remote_image_needs_gif_suffix(tmp_path) -> None:
    """§5.4：远程 `Image` **只有**后缀命中才受理（不为一张 JPEG 付一次网络往返）。

    两种形态都不产生 IO（picker 里只有 6 字节本地读，**禁止下载**）：
    - `url` 以 `.gif` 结尾 ⇒ 受理为候选，任务内下载后再由 `confirm_gif` 验魔数；
    - `url` 无后缀 ⇒ 直接 `MEDIA_NOT_FOUND`。
    """
    candidate = make_image("", url="https://x.invalid/a.gif?sig=1")
    assert candidate.path == "" and candidate.url == "https://x.invalid/a.gif?sig=1"
    handle = await media.find_media(make_event(current=[candidate]), "current")
    assert handle.gif is True

    rejected = make_image("", url="https://x.invalid/a.jpg?sig=1")
    with pytest.raises(envelope.ToolError) as exc_info:
        await media.find_media(make_event(current=[rejected]), "current")
    assert _code(exc_info) == envelope.ERR_MEDIA_NOT_FOUND


# --------------------------------------------------------------------------- #
# §5.3 confirm_gif：取件后的统一出口（同步、幂等）
# --------------------------------------------------------------------------- #
def test_confirm_gif_is_synchronous_by_contract() -> None:
    """§5.3 的签名逐字是同步的（只读 6 字节，没有可等待的 IO）。"""
    assert inspect.iscoroutinefunction(media.confirm_gif) is False
    assert inspect.iscoroutinefunction(media.find_media) is True


def test_confirm_gif_promotes_file_gif_and_demotes_its_lookalike(tmp_path) -> None:
    """`File` 段：魔数命中 ⇒ 转正 `gif=True`；魔数不符 ⇒ **降回** `gif=False`（走旧梯子）。"""
    good = write_gif(tmp_path / "good.gif", width=16, height=16, frames=2)
    bad = tmp_path / "bad.gif"
    bad.write_bytes(png_bytes())

    handle = media.MediaHandle(comp=make_file("good.gif", file=str(good)), quoted=False, from_file=True, gif=True)
    assert media.confirm_gif(handle, str(good)).gif is True

    lookalike = media.MediaHandle(
        comp=make_file("bad.gif", file=str(bad)), quoted=False, from_file=True, gif=True
    )
    demoted = media.confirm_gif(lookalike, str(bad))
    assert demoted.gif is False
    assert demoted.from_file is True  # 其余字段原样（"改名党"不该改变既有行为）


def test_confirm_gif_rejects_image_that_is_not_gif(tmp_path) -> None:
    """`Image` 段魔数不符 ⇒ `MEDIA_NOT_FOUND`（普通图片绝不进链；远程 `.gif` 的收口点）。"""
    path = tmp_path / "remote.gif"
    path.write_bytes(jpeg_bytes())
    handle = media.MediaHandle(comp=make_image(path), quoted=False, gif=True)

    with pytest.raises(envelope.ToolError) as exc_info:
        media.confirm_gif(handle, str(path))
    assert _code(exc_info) == envelope.ERR_MEDIA_NOT_FOUND
    assert "普通图片不进链" in exc_info.value.log_detail


def test_confirm_gif_is_idempotent_and_returns_the_same_object(tmp_path) -> None:
    """幂等：已经确认过的 handle 再确认一次**返回同一个对象**（不额外 replace）。"""
    path = write_gif(tmp_path / "x.gif", width=16, height=16, frames=2)
    confirmed = media.MediaHandle(comp=make_file("x.gif", file=str(path)), quoted=False, from_file=True, gif=True)
    assert media.confirm_gif(confirmed, str(path)) is confirmed
    assert media.confirm_gif(media.confirm_gif(confirmed, str(path)), str(path)).gif is True

    # 非 GIF 字节 + 本来就 False 的 File 句柄 ⇒ 返回同一个对象（既不转正也不降级）
    png = tmp_path / "y.gif"
    png.write_bytes(png_bytes())
    plain = media.MediaHandle(
        comp=make_file("y.gif", file=str(png)), quoted=False, from_file=True, gif=False
    )
    assert media.confirm_gif(plain, str(png)) is plain


def test_media_handle_gif_defaults_false(tmp_path) -> None:
    """`MediaHandle.gif` 默认 `False`（既有三种类型的行为一字不变）。"""
    assert media.MediaHandle(comp=make_record(tmp_path / "a.wav"), quoted=False).gif is False
    assert media.MediaHandle(comp=make_image(tmp_path / "a.png"), quoted=False).gif is False


# --------------------------------------------------------------------------- #
# §5.6 冻结 argv
# --------------------------------------------------------------------------- #
def test_gif_argv_is_frozen_verbatim() -> None:
    """§5.6 的 argv 逐字符快照（改一个参数就是契约变更，必须过评审）。"""
    argv = media.gif_argv("/tmp/in.gif", "/tmp/out.mp4")
    assert argv == [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-i",
        "/tmp/in.gif",
        "-an",
        "-vf",
        "fps=12,scale='trunc(min(640,iw)/2)*2':-2:flags=lanczos",
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "26",
        "-threads",
        "1",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        "/tmp/out.mp4",
    ]


def test_gif_argv_forces_even_width_and_never_truncates_duration() -> None:
    """两条"不写就错"的参数：`trunc(.../2)*2`（防 M9 的 rc=187）与**没有** `-t`。

    `-t` 会在"ffprobe 低报时长"时静默截断用户内容 —— 比多花一秒 CPU 糟得多（§5.6）。
    """
    argv = media.gif_argv("in.gif", "out.mp4")
    vf = argv[argv.index("-vf") + 1]
    assert "trunc(min(640,iw)/2)*2" in vf
    assert "fps=12" in vf
    assert "-an" in argv
    assert "-t" not in argv and "-to" not in argv
    assert argv[argv.index("-threads") + 1] == "1"
    assert argv[argv.index("-movflags") + 1] == "+faststart"


# --------------------------------------------------------------------------- #
# §5.7 四道闸（闸①字节 / 闸③超时 / 闸④产物）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_normalize_gif_source_byte_gate_runs_before_ffmpeg(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """闸①：源字节超 `max_media_bytes` ⇒ `MEDIA_TOO_LARGE`，**一次 ffmpeg 都不跑**。"""
    source = write_gif(tmp_path / "src.gif", width=16, height=16, frames=2)
    calls = _patch_convert(monkeypatch)

    with pytest.raises(envelope.ToolError) as exc_info:
        await media.normalize_gif(str(source), workdir=str(tmp_path), max_media_bytes=10)
    assert _code(exc_info) == envelope.ERR_MEDIA_TOO_LARGE
    assert calls == {"frame_count": 0, "run_capture": 0, "duration": 0}


@pytest.mark.asyncio
async def test_normalize_gif_timeout_gate_is_gif_convert_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """闸③：整次 ffmpeg 超时 ⇒ `GIF_CONVERT_FAILED`，半成品 `unlink`，不重试。"""
    source = write_gif(tmp_path / "src.gif", width=16, height=16, frames=2)
    _patch_convert(monkeypatch, rc=-1, stderr=b"timeout", timed_out=True, product=b"partial")
    out = tmp_path / "gif.mp4"
    out.write_bytes(b"stale")  # 上一次的残渣必须被清掉

    with pytest.raises(envelope.ToolError) as exc_info:
        await media.normalize_gif(str(source), workdir=str(tmp_path), max_media_bytes=1 << 20)
    assert _code(exc_info) == envelope.ERR_GIF_CONVERT_FAILED
    assert "timeout" in exc_info.value.log_detail
    assert not out.exists(), "半成品必须被删干净（不留残渣）"


@pytest.mark.asyncio
async def test_normalize_gif_nonzero_rc_reports_rc_and_unlinks(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """M9 的失败形态（rc=187 / 0 字节）：归一到 `GIF_CONVERT_FAILED`，且**不回退直送 GIF**。"""
    source = write_gif(tmp_path / "src.gif", width=16, height=16, frames=2)
    _patch_convert(monkeypatch, rc=187, stderr=b"width not divisible by 2", product=b"")

    with pytest.raises(envelope.ToolError) as exc_info:
        await media.normalize_gif(str(source), workdir=str(tmp_path), max_media_bytes=1 << 20)
    assert _code(exc_info) == envelope.ERR_GIF_CONVERT_FAILED
    assert "rc=187" in exc_info.value.log_detail
    assert "not divisible" in exc_info.value.log_detail


@pytest.mark.asyncio
async def test_normalize_gif_empty_output_is_gif_convert_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """闸④前半：rc=0 但**没有产物**（`scale` 静默失败的老形态）⇒ `GIF_CONVERT_FAILED`。"""
    source = write_gif(tmp_path / "src.gif", width=16, height=16, frames=2)
    _patch_convert(monkeypatch, rc=0, product=None)

    with pytest.raises(envelope.ToolError) as exc_info:
        await media.normalize_gif(str(source), workdir=str(tmp_path), max_media_bytes=1 << 20)
    assert _code(exc_info) == envelope.ERR_GIF_CONVERT_FAILED
    assert "empty output" in exc_info.value.log_detail


@pytest.mark.asyncio
async def test_normalize_gif_output_byte_gate_is_media_too_large(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """闸④后半：产物超 `max_media_bytes` ⇒ `MEDIA_TOO_LARGE`（并 `unlink` 产物）。"""
    source = write_gif(tmp_path / "src.gif", width=16, height=16, frames=2)
    _patch_convert(monkeypatch, product=b"\x00" * 4096)

    with pytest.raises(envelope.ToolError) as exc_info:
        await media.normalize_gif(str(source), workdir=str(tmp_path), max_media_bytes=1024)
    assert _code(exc_info) == envelope.ERR_MEDIA_TOO_LARGE
    assert "gif output size=4096" in exc_info.value.log_detail
    assert not (tmp_path / "gif.mp4").exists()


@pytest.mark.asyncio
async def test_normalize_gif_missing_source_is_gif_convert_failed(tmp_path) -> None:
    """源不存在/空 ⇒ `GIF_CONVERT_FAILED`（不是 `PROBE_FAILED`、不是 `MEDIA_NOT_FOUND`）。"""
    with pytest.raises(envelope.ToolError) as exc_info:
        await media.normalize_gif(
            str(tmp_path / "nope.gif"), workdir=str(tmp_path), max_media_bytes=1 << 20
        )
    assert _code(exc_info) == envelope.ERR_GIF_CONVERT_FAILED
    assert "source missing" in exc_info.value.log_detail


@pytest.mark.asyncio
async def test_normalize_gif_product_duration_unavailable_is_gif_convert_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """产物**连流都探不出来**（ffprobe 真坏）⇒ `GIF_CONVERT_FAILED`（不放行、不留残渣）。"""
    source = write_gif(tmp_path / "src.gif", width=16, height=16, frames=2)
    _patch_convert(monkeypatch, duration=None)

    async def _broken(_path: str):
        raise envelope.ToolError(envelope.ERR_PROBE_FAILED, log_detail="ffprobe rc=1")

    monkeypatch.setattr(media, "probe_streams", _broken)

    with pytest.raises(envelope.ToolError) as exc_info:
        await media.normalize_gif(str(source), workdir=str(tmp_path), max_media_bytes=1 << 20)
    assert _code(exc_info) == envelope.ERR_GIF_CONVERT_FAILED
    assert "output duration unavailable" in exc_info.value.log_detail
    assert "stream probe failed" in exc_info.value.log_detail
    assert not (tmp_path / "gif.mp4").exists()


@pytest.mark.asyncio
async def test_normalize_gif_stream_confirmed_without_duration_is_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """**F2 裁决**：产物**有视频流但没有可度量时长** ⇒ 按 `0.0` 放行（唯一放行口）。

    这是 `format.duration=N/A` 的那一档：只要 ffprobe 能确认产物**有 video 流**（说明帧确实
    被编码出来了、只是 muxer 没写时长），就按静态假定收 `0.0`，不把整次分析判死。
    用例走桩（`probe_duration_s` 抛"时长缺失" + `probe_streams` 报有视频流）—— 当前冻结
    argv 产不出这种形态（实测：极短帧间隔产的是**0 流**空 mp4，走下面那条重试路径），
    所以这一档是**对未来 ffmpeg / 上游容器差异的防御**。
    """
    source = write_gif(tmp_path / "src.gif", width=16, height=16, frames=1)
    _patch_convert(monkeypatch, duration=None, frames=1)

    async def _streams(_path: str):
        return False, True

    monkeypatch.setattr(media, "probe_streams", _streams)

    result = await media.normalize_gif(
        str(source), workdir=str(tmp_path), max_media_bytes=1 << 20
    )
    assert result.duration_s == 0.0
    assert result.frames == 1 and result.static is True
    assert (tmp_path / "gif.mp4").exists(), "放行时产物必须保留"


@pytest.mark.asyncio
async def test_normalize_gif_streamless_product_is_gif_convert_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """产物**一个流都没有**且源不是单帧 ⇒ 仍 `GIF_CONVERT_FAILED`（不给空视频发"成功"）。"""
    source = write_gif(tmp_path / "src.gif", width=16, height=16, frames=4)
    _patch_convert(monkeypatch, duration=None, frames=4)

    async def _no_streams(_path: str):
        return False, False

    monkeypatch.setattr(media, "probe_streams", _no_streams)

    with pytest.raises(envelope.ToolError) as exc_info:
        await media.normalize_gif(str(source), workdir=str(tmp_path), max_media_bytes=1 << 20)
    assert _code(exc_info) == envelope.ERR_GIF_CONVERT_FAILED
    assert "no video stream" in exc_info.value.log_detail
    assert not (tmp_path / "gif.mp4").exists()


@pytest.mark.asyncio
async def test_normalize_gif_keeps_source_readonly_and_product_inside_workdir(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """§5.8：源**只读**（字节与 mtime 不变）、产物只落 `<workdir>/gif.mp4`。"""
    source = write_gif(tmp_path / "src.gif", width=16, height=16, frames=2)
    before = source.read_bytes()
    workdir = tmp_path / "work"
    workdir.mkdir()
    _patch_convert(monkeypatch, frames=1, product=b"\x00\x00\x00\x18ftypisom" + b"\x00" * 8)

    result = await media.normalize_gif(str(source), workdir=str(workdir), max_media_bytes=1 << 20)

    assert result.path == str(workdir / "gif.mp4")
    assert result.source_size == len(before)
    assert result.size == len(b"\x00\x00\x00\x18ftypisom") + 8
    assert result.frames == 1 and result.static is True  # 源是单帧 ⇒ gif_static 的判据
    assert source.read_bytes() == before, "源文件必须只读"


def test_gif_result_static_semantics() -> None:
    """`static` 三态：1 帧 ⇒ True；0 帧（读不出）⇒ False（不误判）；多帧 ⇒ False。"""
    assert media.GifResult("p", 1, 2, 0.1, 1).static is True
    assert media.GifResult("p", 1, 2, 0.1, 0).static is False
    assert media.GifResult("p", 1, 2, 0.1, 8).static is False


# --------------------------------------------------------------------------- #
# gif_frame_count（诊断面；读不出就回 0，绝不误判静态）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_gif_frame_count_reads_real_total(tmp_path) -> None:
    """真 ffprobe：手工构造的 8 帧 GIF ⇒ `nb_frames=8`（禁 py 侧猜"时长×fps"）。"""
    _require_ffmpeg()
    path = write_gif(
        tmp_path / "t3_handmade_frames.gif",
        width=32,
        height=32,
        frames=8,
        delay_cs=10,
    )
    assert await media.gif_frame_count(str(path)) == 8


@pytest.mark.asyncio
async def test_gif_frame_count_degrades_to_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """三种"读不出"都回 `0`（非零 rc / 非数字输出 / 探测直接抛错）⇒ 不记 `gif_static`。"""
    path = tmp_path / "x.gif"
    path.write_bytes(gif_bytes(width=8, height=8, frames=2))

    async def _rc_nonzero(argv, *, timeout, error_code):
        return 1, b"", b"boom"

    monkeypatch.setattr(media, "_run", _rc_nonzero)
    assert await media.gif_frame_count(str(path)) == 0

    async def _garbage(argv, *, timeout, error_code):
        return 0, b"N/A\n", b""

    monkeypatch.setattr(media, "_run", _garbage)
    assert await media.gif_frame_count(str(path)) == 0

    async def _raises(argv, *, timeout, error_code):
        raise envelope.ToolError(envelope.ERR_PROBE_FAILED, log_detail="spawn ffprobe")

    monkeypatch.setattr(media, "_run", _raises)
    assert await media.gif_frame_count(str(path)) == 0


# --------------------------------------------------------------------------- #
# 真 ffmpeg：手工 GIF 能被解码 + 归一产物是 mp4（含奇数宽回归）
# --------------------------------------------------------------------------- #
def test_hand_built_gif_is_decodable_by_ffmpeg(tmp_path) -> None:
    """夹具自证：`tests/_gif.py` 造的是**真** GIF（8 帧 / 8.0 s），不是"只有魔数的假文件"。"""
    _require_ffmpeg()
    path = write_gif(
        tmp_path / "t3_handmade_anim.gif",
        width=241,
        height=162,
        frames=8,
        delay_cs=100,  # 每帧 1 s ⇒ 8 s
    )
    info = _probe(str(path))
    assert info["format_name"] == "gif"
    assert info["nb_read_frames"] == "8"
    assert float(info["duration"]) == pytest.approx(8.0)
    # 奇数宽是 M9 的触发条件：这里先证明样本确实是奇数宽
    assert int(info["width"]) == 241


@pytest.mark.asyncio
async def test_normalize_gif_real_conversion_forces_even_width(tmp_path) -> None:
    """M9 的回归守卫：241 宽的 GIF 归一成功（`trunc(.../2)*2` ⇒ 240 宽、产物非空）。

    朴素 `scale='min(640,iw)'` 在这份样本上会 `rc=187` / 0 字节（SPEC-v0.4 §13-M9），
    所以"转换成功 + 宽度是偶数"就是那条参数的**行为级**证明。
    """
    _require_ffmpeg()
    source = write_gif(tmp_path / "odd.gif", width=241, height=162, frames=8, delay_cs=100)

    result = await media.normalize_gif(
        str(source), workdir=str(tmp_path), max_media_bytes=1 << 20
    )

    assert result.size > 0
    assert result.frames == 8 and result.static is False
    assert result.duration_s == pytest.approx(8.0, abs=0.2)
    info = _probe(result.path)
    assert "mp4" in info["format_name"]  # demuxer 名是 "mov,mp4,m4a,3gp,3g2,mj2"
    assert int(info["width"]) == 240
    assert int(info["height"]) % 2 == 0
    # 产物无声：`-an` 的确定行为（下游 probe_streams 得到 (False, True)）
    assert await media.probe_streams(result.path) == (False, True)
    # 源只读
    assert source.read_bytes() == gif_bytes(width=241, height=162, frames=8, delay_cs=100)


@pytest.mark.asyncio
async def test_normalize_gif_single_frame_is_static(tmp_path) -> None:
    """单帧 GIF（普通 GIF 图片）⇒ 归一成功 + `frames=1` ⇒ `analyze` 记 `gif_static`。"""
    _require_ffmpeg()
    source = write_gif(tmp_path / "static.gif", width=32, height=32, frames=1, delay_cs=10)

    result = await media.normalize_gif(
        str(source), workdir=str(tmp_path), max_media_bytes=1 << 20
    )

    assert result.frames == 1
    assert result.static is True
    assert result.duration_s > 0  # 12 fps ⇒ 约 0.08 s
    assert "mp4" in _probe(result.path)["format_name"]


@pytest.mark.asyncio
async def test_single_frame_short_delay_gif_is_retried_into_a_real_mp4(tmp_path) -> None:
    """**F2（medium）**：单帧 GIF 的保持时长 < 1/12 s ⇒ 冻结 argv 产 **0 流空 mp4** ⇒ 重试一次。

    真机（`VERIFY-v0.4.md` §12-F2）报的是"产物 `format.duration=N/A` 被误判
    `GIF_CONVERT_FAILED`"；本用例把**根因**钉成可执行事实：先证明同一份 argv 跑在源上
    确实只得到 262 字节、**一个流都没有**的空容器（`fps=12` 按输出帧间隔中点取帧，源帧
    只有 0.04 s ⇒ 唯一一帧被跳过），再断言 `normalize_gif` 会抬高保持时长重试并成功。

    修法刻意**不动冻结 argv**（SPEC §5.4/§5.6 的 argv 有逐字符快照）：只在一份**拷贝**上
    改 GCE 的 2 字节 delay，源文件只读。
    """
    _require_ffmpeg()
    source = write_gif(tmp_path / "f2.gif", width=200, height=150, frames=1, delay_cs=4)
    original = source.read_bytes()

    # ① 根因证据：冻结 argv 直接跑源 ⇒ 容器正常但**没有任何流**
    naive = tmp_path / "naive.mp4"
    proc = subprocess.run(media.gif_argv(str(source), str(naive)), capture_output=True)
    assert proc.returncode == 0, proc.stderr
    naive_info = _probe(str(naive))
    assert "codec_type" not in naive_info, naive_info  # 0 个流
    assert naive.stat().st_size < 1024, naive.stat().st_size

    # ② 归一：抬高保持时长重试一次 ⇒ 真 mp4（1 帧 @12 fps）
    result = await media.normalize_gif(
        str(source), workdir=str(tmp_path), max_media_bytes=1 << 20
    )
    assert result.size > 0
    assert result.frames == 1 and result.static is True
    # F2-R：抬到 1.0 s（不是 1/12 s）—— 0.083 s 的产物会被上游视频通道 400 拒收
    assert result.duration_s == pytest.approx(media.GIF_STATIC_HOLD_CS / 100.0, abs=0.1)
    assert result.duration_s >= 0.5, "必须明显高于上游最小可读时长（≈1 s）"
    assert await media.probe_streams(result.path) == (False, True)
    # 重试用的拷贝已清掉、源只读（SPEC §5.8）
    assert not (tmp_path / "gif_static.gif").exists()
    assert source.read_bytes() == original


def test_prepare_static_gif_copy_patches_only_the_delay_field(tmp_path) -> None:
    """`prepare_static_gif_copy` 的粒度：**只改 GCE 的 2 字节**，且三种情形不动手。

    这条守的是"修法最小"：如果哪天有人改成重编码/改 argv/改源文件，这里会红。
    同时钉住 **F2-R 的双数纪律**：触发阈值（9 cs，由 fps 网格决定）与"抬到多少"
    （100 cs = 1.0 s，由上游最小可读时长决定）是**两个**常量，不能合并、不能一起改大。
    """
    assert media.GIF_STATIC_MIN_DELAY_CS == 9, "触发阈值只由 fps 网格决定，不许为凑上游时长改大"
    assert media.GIF_STATIC_HOLD_CS == 100, "单帧重试写入的保持时长 = 1.00 s（上游可读）"
    source = write_gif(tmp_path / "short.gif", width=20, height=20, frames=1, delay_cs=4)
    found = media.gif_gce_delay(source.read_bytes())
    assert found is not None
    offset, delay = found
    assert delay == 4

    dest = tmp_path / "patched.gif"
    assert media.prepare_static_gif_copy(str(source), str(dest)) is True

    before, after = source.read_bytes(), dest.read_bytes()
    assert len(before) == len(after)
    diff = [i for i, (x, y) in enumerate(zip(before, after)) if x != y]
    assert diff == [offset], (diff, offset)  # 高字节本来就是 0，只动低字节
    # 写进去的是"抬到多少"（`GIF_STATIC_HOLD_CS` = 1.00 s），不是触发阈值（9 cs）
    assert after[offset] == media.GIF_STATIC_HOLD_CS
    assert source.read_bytes() == before, "源文件必须只读"

    # ① 已经够长的保持时长 ⇒ 不动手（不然会无谓改用户字节）
    long = write_gif(tmp_path / "long.gif", width=20, height=20, frames=1, delay_cs=10)
    assert media.prepare_static_gif_copy(str(long), str(tmp_path / "long2.gif")) is False
    # ② 没有 GCE 的 GIF87a（delay 0）⇒ 不动手（它本来就能正常归一）
    no_gce = write_gif(tmp_path / "nogce.gif", width=20, height=20, frames=1, delay_cs=0)
    assert media.gif_gce_delay(no_gce.read_bytes()) is None
    assert media.prepare_static_gif_copy(str(no_gce), str(tmp_path / "nogce2.gif")) is False
    # ③ 不是 GIF ⇒ 不动手
    junk = tmp_path / "junk.bin"
    junk.write_bytes(b"not a gif at all")
    assert media.prepare_static_gif_copy(str(junk), str(tmp_path / "junk2.gif")) is False


# --------------------------------------------------------------------------- #
# F2 的"最小修法"守卫：**只对单帧**、**只重试一次**（t7 变异检验补的两条）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_gif_retry_only_fires_for_single_frame_sources(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """条件①：产物 0 流时，**多帧源绝不许触发**保时长重试（改输入只对单帧成立）。

    为什么多帧必须拦住：抬高 GCE 保持时长对单帧源是**语义恒等**（那张静态图被展示多久
    与画面/帧数无关）；对多帧源它会改变**每一帧的时长** ⇒ 等于篡改用户动图的节奏。
    所以 `_gif_retry_single_frame` 的第一行就是 `frames != 1 ⇒ 直接失败`。

    变异守卫（t7 实测的两个未捕获变异）：删掉 `frames != 1` 守卫 ⇒ `prepare_static_gif_copy`
    被调用 ⇒ 本用例红；把重试加宽成"0 流就跑" ⇒ 同上。断言刻意用 **spy + 计数**，
    不依赖 `log_detail` 的措辞（那正是 t7 用子串断言漏掉这条变异的根因）。
    """
    source = write_gif(tmp_path / "multi.gif", width=16, height=16, frames=4, delay_cs=4)
    calls = _patch_convert(monkeypatch, duration=None, frames=4)

    async def _no_streams(_path: str):
        return False, False

    prepared: list[tuple[str, str]] = []

    def _spy_prepare(src: str, dest: str) -> bool:
        # 若守卫被删，这里会真的被调到；返回 True 让"多帧也重试"这条路走通（更狠）
        prepared.append((src, dest))
        return True

    monkeypatch.setattr(media, "probe_streams", _no_streams)
    monkeypatch.setattr(media, "prepare_static_gif_copy", _spy_prepare)

    with pytest.raises(envelope.ToolError) as exc_info:
        await media.normalize_gif(
            str(source), workdir=str(tmp_path), max_media_bytes=1 << 20
        )

    assert _code(exc_info) == envelope.ERR_GIF_CONVERT_FAILED
    assert prepared == [], f"多帧源不许触发保时长重试：{prepared}"
    assert calls["run_capture"] == 1, (
        f"多帧源只该跑 1 次 ffmpeg（首轮），实际 {calls['run_capture']} 次"
    )
    assert not (tmp_path / "gif_static.gif").exists()
    assert not (tmp_path / "gif.mp4").exists(), "失败必须 unlink 半成品"


@pytest.mark.asyncio
async def test_gif_retry_runs_exactly_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """条件①：**只重试一次**。首轮与重试都 0 流 ⇒ 恰好 2 次 ffmpeg，然后失败。

    为什么钉"恰好一次"：重试是**补救**不是重试策略。第 3 次、第 4 次只会在同一个确定性
    输入上重复同样的空产物（白花 CPU、把 30 s 的预算碎成一堆 10 s 超时），而且违背
    "失败要可见、不要磨"的口径。守卫是终态断言（不是措辞断言）：

    | 变异 | 期望 |
    | --- | --- |
    | 重试跑两次（复制一行） | `run_capture == 3` ⇒ 红 |
    | 重试被加成循环/退避 | 同上 ⇒ 红 |
    | 只在单帧才重试（当前实现） | `run_capture == 2` ⇒ 绿 |

    另外钉两件事：拷贝**只准备一次**（不能每轮都重新拷/改源），以及重试失败仍走
    `GIF_CONVERT_FAILED` 且不留残渣。
    """
    source = write_gif(tmp_path / "short.gif", width=16, height=16, frames=1, delay_cs=4)
    calls = _patch_convert(monkeypatch, duration=None, frames=1)

    async def _no_streams(_path: str):
        return False, False

    real_prepare = media.prepare_static_gif_copy
    prepared: list[str] = []

    def _spy_prepare(src: str, dest: str) -> bool:
        prepared.append(dest)
        return real_prepare(src, dest)

    monkeypatch.setattr(media, "probe_streams", _no_streams)
    monkeypatch.setattr(media, "prepare_static_gif_copy", _spy_prepare)

    with pytest.raises(envelope.ToolError) as exc_info:
        await media.normalize_gif(
            str(source), workdir=str(tmp_path), max_media_bytes=1 << 20
        )

    assert _code(exc_info) == envelope.ERR_GIF_CONVERT_FAILED
    assert "retried output still has no video stream" in exc_info.value.log_detail
    assert calls["run_capture"] == 2, (
        f"首轮 + 1 次重试 = 2 次 ffmpeg，实际 {calls['run_capture']} 次"
    )
    assert len(prepared) == 1, f"拷贝只该准备 1 次，实际 {len(prepared)} 次：{prepared}"
    assert not (tmp_path / "gif_static.gif").exists(), "重试用的拷贝必须被清掉"
    assert not (tmp_path / "gif.mp4").exists(), "失败必须 unlink 半成品"


def test_png_and_apng_probe_as_video_but_are_still_not_gif(tmp_path) -> None:
    """M6 的事实守卫：PNG/APNG 的 `codec_type` 也是 `video`，但它们**没有时长**、更不是 GIF。

    这条把"为什么判据必须是魔数"钉成可执行事实：`ffprobe` 的 `codec_type=video`
    不能当"GIF 候选"的判据（否则哪天 ffmpeg 给 APNG 补上时长，图片就静默流进视频链）。
    """
    _require_ffmpeg()
    for name, payload in (("x.png", png_bytes()), ("x.png", png_bytes(apng=True))):
        path = tmp_path / name
        path.write_bytes(payload)
        info = _probe(str(path))
        assert info["codec_type"] == "video", info
        assert info["duration"] == "N/A", info  # v0.3 拒 PNG 靠的就是这个副作用
        assert media.has_gif_magic(str(path)) is False


# --------------------------------------------------------------------------- #
# 源 GIF 的时长：GCE-less 静态 GIF（v0.4 修的实现缺陷）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_probe_gif_duration_treats_gc_less_static_gif_as_zero(tmp_path) -> None:
    """**实现缺陷修复**：没有 GCE 的单帧 GIF 没有 `format.duration`（真 ffprobe 实测）⇒ `0.0`。

    为什么必须这样：这类 GIF 是真实世界里**最常见**的静态 GIF（`/usr/share/doc/**/*.gif`
    里 8/9 是 GIF87a 且无 GCE）。若沿用视频口径抛 `PROBE_FAILED`，SPEC-v0.4 §5.5 的
    "单帧 GIF ⇒ 受理 → 视频链 + `gif_static`" 这一行就落不了地。
    闸②是**上界**闸（时长 > 上限才拒）⇒ "没有可度量的时长"等价于 0。
    """
    _require_ffmpeg()
    path = write_gif(tmp_path / "no-gce.gif", width=32, height=32, frames=1, delay_cs=0)
    assert media.has_gif_magic(str(path)) is True

    assert await media.probe_gif_duration_s(str(path)) == 0.0
    # 对照：有 GCE 的单帧 GIF 有真时长（不该被这条兼容吃掉）
    timed = write_gif(tmp_path / "with-gce.gif", width=32, height=32, frames=1, delay_cs=10)
    assert await media.probe_gif_duration_s(str(timed)) == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_probe_gif_duration_keeps_real_probe_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只有"时长缺失"被降级成 0；**ffprobe 真坏**（超时/异常）仍然 `PROBE_FAILED`。"""

    async def _broken(_path: str) -> float:
        raise envelope.ToolError(
            envelope.ERR_PROBE_FAILED, log_detail="get_media_duration timeout"
        )

    monkeypatch.setattr(media, "probe_duration_s", _broken)
    with pytest.raises(envelope.ToolError) as exc_info:
        await media.probe_gif_duration_s("whatever.gif")
    assert _code(exc_info) == envelope.ERR_PROBE_FAILED
    assert "timeout" in exc_info.value.log_detail


# --------------------------------------------------------------------------- #
# 端到端（真 ffmpeg + 假 provider）：Image(GIF) 走完整的视频链
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_gif_image_runs_through_the_whole_video_chain(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """真归一 + 真视频链：`kind=video`、notes 三条、**跳过 classify**、零抽帧。

    这是 GIF 与既有七跳的唯一接缝：产物 mp4照旧走原样链路（原生直送），
    所以这里用假原生 provider 断言"真的被当成视频送出去了"。
    """
    _require_ffmpeg()
    gif_path = write_gif(tmp_path / "sticker.gif", width=64, height=48, frames=6, delay_cs=10)
    native = native_video(text="动图里是一个来回跳动的方块。")
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        event=make_event(current=[make_image(gif_path)]),
        native=native,
    )

    payload = await rig.run()

    assert payload["ok"] is True
    assert payload["kind"] == "video"
    assert payload["summary"] == "动图里是一个来回跳动的方块。"
    assert payload["frames"] == [] and payload["transcript"] == []
    notes = payload["notes"]
    assert "source:gif" in notes
    assert any(note.startswith("gif_converted:") for note in notes)
    assert "video_native" in notes
    assert "gif_static" not in notes  # 6 帧 ⇒ 不是静态
    # GIF 分支**跳过 classify**（那一步会把 Image 判成 kind=audio，§1.2-M7）
    assert rig.media_calls["classify"] == 0
    # 原生通道收到的是 **mp4**（不是原始 GIF 字节）
    url = native.video_urls()[-1]
    assert url.startswith("data:video/mp4;base64,")
    assert len(native.creates()) == 1


@pytest.mark.asyncio
async def test_gc_less_static_gif_is_accepted_and_marked_static(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """同一条端到端，但源是**没有 GCE 的静态 GIF**（时长缺失）：受理 + `gif_static`。

    这条是上面那个实现缺陷修复的**行为级**证据：修复前它会以 `PROBE_FAILED` 失败。
    """
    _require_ffmpeg()
    gif_path = write_gif(tmp_path / "static.gif", width=40, height=40, frames=1, delay_cs=0)
    native = native_video(text="一张静态图片。")
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        event=make_event(current=[make_image(gif_path)]),
        native=native,
    )

    payload = await rig.run()

    assert payload["ok"] is True
    assert payload["kind"] == "video"
    assert "source:gif" in payload["notes"]
    assert "gif_static" in payload["notes"]
    assert rig.media_calls["classify"] == 0


@pytest.mark.asyncio
async def test_short_delay_static_gif_is_accepted_and_marked_static(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """**F2 的行为级证据**：`delay_cs=4`（0.04 s，即 `ffmpeg -frames:v 1` 的默认值）走完整链路。

    修复前它会以 `GIF_CONVERT_FAILED` 失败（REVIEW/VERIFY 的 F2）；修复后必须
    `ok=true` / `kind=video` / notes 含 `gif_static`，并且**只调用一次**原生视频通道。
    """
    _require_ffmpeg()
    gif_path = write_gif(tmp_path / "short.gif", width=64, height=48, frames=1, delay_cs=4)
    native = native_video(text="一张静态图片：一个方块。")
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        event=make_event(current=[make_image(gif_path)]),
        native=native,
    )

    payload = await rig.run()

    assert payload["ok"] is True, payload
    assert payload["kind"] == "video"
    # F2-R：信封里的时长是**产物真实时长**（1.0 s），不是 1/12 s 也不是 0.0
    assert payload["duration_s"] == pytest.approx(media.GIF_STATIC_HOLD_CS / 100.0, abs=0.1)
    assert "source:gif" in payload["notes"]
    assert "gif_static" in payload["notes"]
    assert any(note.startswith("gif_converted:") for note in payload["notes"])
    assert "video_native" in payload["notes"]
    assert rig.media_calls["classify"] == 0
    assert len(native.creates()) == 1
    # 送给模型的是真 mp4（`ftyp`），不是那份 0 流空容器
    url = native.video_urls()[-1]
    assert url.startswith("data:video/mp4;base64,")
    payload_bytes = base64.b64decode(url.split(",", 1)[1])
    assert b"ftyp" in payload_bytes[:16]
    assert len(payload_bytes) > 1024


@pytest.mark.asyncio
async def test_file_segment_gif_is_treated_as_video_not_as_raw_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """`File` + `.gif`：v0.3 会把**原始 GIF 字节**贴 `data:video/mp4` 直送（缺陷）。

    v0.4 的判据是：先 `confirm_gif` 转正，再归一成真 mp4 ⇒ 贴给 provider 的
    base64 必须是 mp4（`ftyp` 装箱），而不是 GIF 魔数。
    """
    _require_ffmpeg()
    gif_path = write_gif(tmp_path / "viafile.gif", width=48, height=32, frames=4, delay_cs=10)
    native = native_video(text="群文件里的动图在闪。")
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="video",
        event=make_event(current=[make_file("viafile.gif", file=str(gif_path))]),
        native=native,
    )

    payload = await rig.run()

    assert payload["ok"] is True
    assert "source:file" in payload["notes"]
    assert "source:gif" in payload["notes"]
    assert rig.media_calls["classify"] == 0
    url = native.video_urls()[-1]
    raw = base64.b64decode(url.split(",", 1)[1])
    assert raw[4:8] == b"ftyp", "送给 provider 的必须是 mp4 装箱，不是原始 GIF 字节"
    assert not raw.startswith(b"GIF8")


@pytest.mark.asyncio
async def test_file_lookalike_gif_falls_back_to_the_old_ladder(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """`File` + `.gif` 但魔数不符 ⇒ **降回** v0.3 的三段梯子（行为不变，不是 GIF 错码）。

    这里用假 `classify`（`_stub_media` 提供）断言：classify 被调了 1 次、`source:gif`
    没有出现、kind 由 classify 决定（audio）。这条守的是"改名党不该改变既有行为"。
    """
    path = tmp_path / "fake.gif"
    path.write_bytes(png_bytes())
    rig = build_rig(
        monkeypatch,
        tmp_path,
        kind="audio",  # _stub_media 的 classify 返回 audio
        event=make_event(current=[make_file("fake.gif", file=str(path))]),
    )

    payload = await rig.run()

    assert payload["ok"] is True
    assert payload["kind"] == "audio"
    assert rig.media_calls["classify"] == 1
    assert "source:gif" not in payload["notes"]
