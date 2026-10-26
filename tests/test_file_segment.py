"""`File` 段（群文件投递）—— v0.2 的**上线阻塞缺陷**所在面（SPEC-v0.2 §3）。

背景（ops 查出的生产事实）：本平台群里发的视频以 **`File` 段**投递，而 v0.1 只认
`Record | Video` ⇒ 上线至今**零成功**。v0.2 的三处改动各自对应一组用例：

1. **定位**：`_MEDIA_KINDS = (Record, Video, File)`，优先级是**全链**的；找不到时 `log_detail`
   必须带**真实组件类型名**（`[Plain]` / `[Plain, Image]`）—— 否则 ops 在生产上永远看不出
   "消息里其实有一个 File 段"。
2. **取件决策树**：`file_` 命中零 IO → 有界下载 `url` → `MEDIA_UNRESOLVABLE`。
   **禁用** `.file` 属性（异步上下文恒 `""` + WARN）与 `get_file()`（1800 s / 无上限 / 落生产树）。
3. **三段判型梯子**：白名单 → `ensure_wav`（silk 靠它救）→ 视频流。"ffprobe 双保险"被实测
   推翻：**silk 是合法音频但 ffprobe rc=1**，所以梯子必须允许"ffprobe 失败但能解成音频"。

夹具口径：只用 `make_fixtures` 的 **lavfi 合成样本**（`video_5s.mp4` / `audio_30s.wav`）
与自造的垃圾字节，**不碰任何真实群媒体**；下载面全部是内存假 aiohttp（零网络）。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from astrbot_plugin_media_analysis import analyze as analyze_mod
from astrbot_plugin_media_analysis import background
from astrbot_plugin_media_analysis import cache as cache_mod
from astrbot_plugin_media_analysis import envelope, media, providers, settings
from _fakes import (
    FakeChatProvider,
    FakeContext,
    audio_chat,
    make_event,
    make_file,
    make_record,
)

#: `File` 段取件的必填关键字（`workdir` 由 caller 清理、`max_media_bytes` 来自配置）。
#: v0.5.1 起配置面存 MB（`limits.max_media_mb`）⇒ 字节只由 `settings.mb_to_bytes()` 派生。
_MAX_BYTES = settings.mb_to_bytes(settings.DEFAULT_MAX_MEDIA_MB)


def _code(exc_info: pytest.ExceptionInfo) -> str:
    assert isinstance(exc_info.value, envelope.ToolError), type(exc_info.value)
    return exc_info.value.code


async def _file_handle(path: Path, *, name: str | None = None, quoted: bool = False):
    """从一条真实的 `File` 组件拿到 `MediaHandle`（走真 `find_media`，不手搓 handle）。"""
    comp = make_file(name or path.name, file=str(path))
    event = (
        make_event(quoted=[comp])
        if quoted
        else make_event(current=[comp])
    )
    return await media.find_media(event, "quoted" if quoted else "current")


# --------------------------------------------------------------------------- #
# 1. 定位：File 段进候选集、优先级、`log_detail` 带真实组件类型
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_find_media_accepts_a_file_segment(tmp_path: Path) -> None:
    """核心事实：`File` 也是媒体候选 ⇒ `from_file=True`（取件走决策树）。"""
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00")
    handle = await _file_handle(path)
    assert handle.from_file is True
    assert handle.quoted is False
    assert handle.name == "clip.mp4"


@pytest.mark.asyncio
async def test_find_media_quoted_file_segment(tmp_path: Path) -> None:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00")
    handle = await _file_handle(path, quoted=True)
    assert handle.from_file is True
    assert handle.quoted is True


@pytest.mark.asyncio
async def test_find_media_priority_is_record_then_video_then_file(
    media_fixtures: dict[str, Path], tmp_path: Path
) -> None:
    """优先级是**全链**的：同一条消息里既有 `File` 又有 `Record` ⇒ 选 `Record`。

    "先来先得"会让一条 `[File, Record]` 的消息走上体积更大、还要下载的那条路。
    """
    file_comp = make_file("附件.mp4", file=str(tmp_path / "x.mp4"))
    record = make_record(media_fixtures["audio_30s"])
    handle = await media.find_media(make_event(current=[file_comp, record]), "current")
    assert handle.from_file is False, "Record 优先于 File"

    video = make_record(media_fixtures["video_5s"])  # 用 Video 更贴切，见下一条
    assert video is not None


@pytest.mark.asyncio
async def test_find_media_priority_video_beats_file(tmp_path: Path, media_fixtures: dict[str, Path]) -> None:
    from _fakes import make_video

    file_comp = make_file("附件.mp4", file=str(tmp_path / "x.mp4"))
    video = make_video(media_fixtures["video_5s"])
    handle = await media.find_media(make_event(current=[file_comp, video]), "current")
    assert handle.from_file is False, "Video 优先于 File"


@pytest.mark.asyncio
async def test_missing_media_log_detail_carries_real_component_names() -> None:
    """`log_detail` 必须写**真实组件类型名**（v0.1 只写 `no Record/Video`，定位不了 File 段）。"""
    event = make_event(current=[], quoted=None).plain("只有文字")
    with pytest.raises(envelope.ToolError) as exc_info:
        await media.find_media(event, "current")
    assert _code(exc_info) == envelope.ERR_MEDIA_NOT_FOUND
    assert "chain=[Plain]" in exc_info.value.log_detail, exc_info.value.log_detail


@pytest.mark.asyncio
async def test_missing_media_log_detail_quoted_shows_both_layers() -> None:
    """引用链里没有媒体时，`log_detail` 同时给引用链与**外层**链的类型名。"""
    from astrbot.api.message_components import Plain, Reply

    inner = [Plain("被引用的文字")]
    event = make_event(current=[Plain("外层"), Reply(id="r1", chain=inner)])
    with pytest.raises(envelope.ToolError) as exc_info:
        await media.find_media(event, "quoted")
    detail = exc_info.value.log_detail
    assert "chain=[Plain]" in detail, detail
    assert "outer=[Plain, Reply]" in detail, detail


@pytest.mark.asyncio
async def test_broken_message_chain_is_still_media_unresolvable() -> None:
    """读消息链本身失败 ⇒ `MEDIA_UNRESOLVABLE`，不许伪装成"消息里没有媒体"。"""

    class _Broken:
        def get_messages(self):
            raise RuntimeError("adapter exploded")

    with pytest.raises(envelope.ToolError) as exc_info:
        await media.find_media(_Broken(), "current")
    assert _code(exc_info) == envelope.ERR_MEDIA_UNRESOLVABLE
    assert "RuntimeError" in exc_info.value.log_detail


# --------------------------------------------------------------------------- #
# 2. `file_` URI 与"要不要下载"的判定
# --------------------------------------------------------------------------- #
def test_file_uri_to_path_variants(tmp_path: Path) -> None:
    assert media.file_uri_to_path(f"file://{tmp_path}/a b.mp4") == f"{tmp_path}/a b.mp4"
    assert media.file_uri_to_path(f"file:{tmp_path}/a.mp4") == f"{tmp_path}/a.mp4"
    assert media.file_uri_to_path("/plain/path.mp4") == "/plain/path.mp4"
    assert media.file_uri_to_path("") == ""
    # 百分号编码（平台可能把中文/空格编码进 URI）
    assert media.file_uri_to_path("file:///tmp/%E9%99%84%E4%BB%B6.mp4") == "/tmp/附件.mp4"


@pytest.mark.asyncio
async def test_file_download_needed_truth_table(tmp_path: Path) -> None:
    """`file_download_needed` 的判定必须与 `resolve_media_path` 的分支一致（notes 溯源用）。"""
    existing = tmp_path / "a.mp4"
    existing.write_bytes(b"\x00")
    assert media.file_download_needed(await _file_handle(existing)) is False

    missing_but_url = await media.find_media(
        make_event(current=[make_file("a.mp4", url="http://example.invalid/a.mp4")]), "current"
    )
    assert media.file_download_needed(missing_but_url) is True

    neither = await media.find_media(make_event(current=[make_file("a.pdf")]), "current")
    assert media.file_download_needed(neither) is False

    # 非 File 段永远是 False（Record 走预处理落地，Video 走 convert_to_file_path）
    record = make_record(existing)
    assert media.file_download_needed(media.MediaHandle(comp=record, quoted=False)) is False


# --------------------------------------------------------------------------- #
# 3. 取件决策树：`file_` → 有界下载 → UNRESOLVABLE
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_resolve_file_path_uses_file_uri_without_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "clip.mp4"
    target.write_bytes(b"\x00" * 16)
    handle = await _file_handle(target)

    async def _boom(*_args, **_kwargs):  # pragma: no cover - 走到就是错
        raise AssertionError("file_ 已可用，不许走下载")

    monkeypatch.setattr(media, "download_bounded", _boom)
    resolved = await media.resolve_media_path(
        handle, workdir=str(tmp_path), max_media_bytes=_MAX_BYTES
    )
    assert Path(resolved) == target.resolve()


@pytest.mark.asyncio
async def test_resolve_file_path_downloads_when_only_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`file_` 空 + `url` 有 ⇒ 走我们自己的有界下载，产物落 **本次工作目录**。"""
    workdir = tmp_path / "work"
    workdir.mkdir()
    seen: dict[str, object] = {}

    async def _download(url: str, *, workdir: str, max_bytes: int, timeout: float, suffix: str) -> str:
        seen.update(url=url, workdir=workdir, max_bytes=max_bytes, timeout=timeout, suffix=suffix)
        out = Path(workdir) / f"file{suffix}"
        out.write_bytes(b"\x00")
        return str(out)

    monkeypatch.setattr(media, "download_bounded", _download)
    handle = await media.find_media(
        make_event(current=[make_file("clip.mp4", url="http://example.invalid/c.mp4")]), "current"
    )
    resolved = await media.resolve_media_path(
        handle, workdir=str(workdir), max_media_bytes=12345
    )
    assert Path(resolved).parent == workdir
    assert seen == {
        "url": "http://example.invalid/c.mp4",
        "workdir": str(workdir),
        "max_bytes": 12345,
        "timeout": media.FILE_DOWNLOAD_TIMEOUT_S,
        "suffix": ".mp4",
    }


@pytest.mark.asyncio
async def test_resolve_file_path_without_file_and_url_is_unresolvable(tmp_path: Path) -> None:
    """`file_` 与 `url` 都空 ⇒ `MEDIA_UNRESOLVABLE`（**不**去碰 `.file` 属性 / `get_file()`）。"""
    handle = await media.find_media(make_event(current=[make_file("a.pdf")]), "current")
    with pytest.raises(envelope.ToolError) as exc_info:
        await media.resolve_media_path(
            handle, workdir=str(tmp_path), max_media_bytes=_MAX_BYTES
        )
    assert _code(exc_info) == envelope.ERR_MEDIA_UNRESOLVABLE
    assert "no file_ and no url" in exc_info.value.log_detail


@pytest.mark.asyncio
async def test_resolve_file_path_never_touches_file_property_or_get_file(
    tmp_path: Path,
) -> None:
    """硬守卫：取件路径**不许**读 `.file`、**不许**调 `get_file()`（两条都是生产事故形态）。

    `.file` 在异步上下文里恒返回 `""` 并打 WARN；`get_file()` 超时 1800 s、无大小上限、
    把下载物落进生产 `data/temp`。用属性探针把这两条钉死。
    """

    touched: list[str] = []

    class _Trapped(media.File):
        @property
        def file(self):  # type: ignore[override]
            touched.append("file")
            raise AssertionError("不许读 File.file（异步上下文恒空串）")

        async def get_file(self, allow_return_url: bool = False):  # type: ignore[override]
            touched.append("get_file")
            raise AssertionError("不许调 File.get_file（1800 s / 无上限 / 落生产树）")

    comp = _Trapped(name="a.pdf")
    handle = media.MediaHandle(comp=comp, quoted=False, from_file=True)
    with pytest.raises(envelope.ToolError):
        await media.resolve_media_path(handle, workdir=str(tmp_path), max_media_bytes=_MAX_BYTES)
    assert touched == [], f"取件路径碰了禁用面：{touched}"
    # 顺带证明 `file_download_needed` 也只读 `file_` / `url`
    assert media.file_download_needed(handle) is False
    assert touched == []


# --------------------------------------------------------------------------- #
# 4. 有界下载：字节上限 / 状态码 / 空产物 / scheme / 后缀清洗（内存假 aiohttp）
# --------------------------------------------------------------------------- #
class _FakeContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_chunked(self, size: int):
        for chunk in self._chunks:
            yield chunk


class _FakeResponse:
    def __init__(self, status: int, chunks: list[bytes]) -> None:
        self.status = status
        self.content = _FakeContent(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _FakeSession:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    def get(self, url: str) -> _FakeResponse:
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _FakeAiohttp:
    """只实现 `download_bounded` 用到的那三个面（零网络、零超时）。"""

    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.timeouts: list[object] = []

    def ClientTimeout(self, *, total: float):  # noqa: N802 - 复刻 aiohttp 的名字
        self.timeouts.append(total)
        return ("timeout", total)

    def ClientSession(self, *, timeout=None):  # noqa: N802
        return _FakeSession(self._response)


def _install_fake_aiohttp(monkeypatch: pytest.MonkeyPatch, *, status: int, chunks: list[bytes]):
    fake = _FakeAiohttp(_FakeResponse(status, chunks))
    monkeypatch.setattr(media, "aiohttp", fake)
    return fake


@pytest.mark.asyncio
async def test_download_bounded_streams_into_workdir_with_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _install_fake_aiohttp(monkeypatch, status=200, chunks=[b"abc", b"def"])
    out = await media.download_bounded(
        "http://example.invalid/x.mp4",
        workdir=str(tmp_path),
        max_bytes=1000,
        timeout=5.0,
        suffix=".mp4",
    )
    assert Path(out) == tmp_path / "file.mp4"
    assert Path(out).read_bytes() == b"abcdef"
    assert fake.timeouts == [5.0], "总时长必须有界（这是否决 get_file() 的第一条理由）"


@pytest.mark.asyncio
async def test_download_bounded_over_limit_is_media_too_large_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """边读边数：超 `max_bytes` 立刻放弃 ⇒ `MEDIA_TOO_LARGE`，半截文件被删掉。"""
    _install_fake_aiohttp(monkeypatch, status=200, chunks=[b"x" * 10, b"y" * 10])
    with pytest.raises(envelope.ToolError) as exc_info:
        await media.download_bounded(
            "http://example.invalid/big.mp4",
            workdir=str(tmp_path),
            max_bytes=15,
        )
    assert _code(exc_info) == envelope.ERR_MEDIA_TOO_LARGE
    assert list(tmp_path.iterdir()) == [], "超限的半截文件必须删掉（不许留残骸）"


@pytest.mark.asyncio
async def test_download_bounded_non_200_is_unresolvable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_aiohttp(monkeypatch, status=404, chunks=[])
    with pytest.raises(envelope.ToolError) as exc_info:
        await media.download_bounded(
            "http://example.invalid/gone.mp4", workdir=str(tmp_path), max_bytes=1000
        )
    assert _code(exc_info) == envelope.ERR_MEDIA_UNRESOLVABLE
    assert "404" in exc_info.value.log_detail


@pytest.mark.asyncio
async def test_download_bounded_empty_body_is_unresolvable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_aiohttp(monkeypatch, status=200, chunks=[])
    with pytest.raises(envelope.ToolError) as exc_info:
        await media.download_bounded(
            "http://example.invalid/empty.mp4", workdir=str(tmp_path), max_bytes=1000
        )
    assert _code(exc_info) == envelope.ERR_MEDIA_UNRESOLVABLE
    assert "empty" in exc_info.value.log_detail


@pytest.mark.parametrize("url", ["ftp://example.invalid/a.mp4", "file:///etc/passwd", "", "   "])
@pytest.mark.asyncio
async def test_download_bounded_rejects_non_http_schemes(tmp_path: Path, url: str) -> None:
    """只认 `http(s)://`：`file://` / `ftp://` 会让工具变成一个"读任意本地文件"的原语。"""
    with pytest.raises(envelope.ToolError) as exc_info:
        await media.download_bounded(url, workdir=str(tmp_path), max_bytes=1000)
    assert _code(exc_info) == envelope.ERR_MEDIA_UNRESOLVABLE


@pytest.mark.asyncio
async def test_download_bounded_without_aiohttp_is_unresolvable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """aiohttp 不可用（核心瘦身/导入失败）⇒ 明确 `MEDIA_UNRESOLVABLE`，不是 AttributeError。"""
    monkeypatch.setattr(media, "aiohttp", None)
    with pytest.raises(envelope.ToolError) as exc_info:
        await media.download_bounded(
            "http://example.invalid/a.mp4", workdir=str(tmp_path), max_bytes=1000
        )
    assert _code(exc_info) == envelope.ERR_MEDIA_UNRESOLVABLE
    assert "aiohttp" in exc_info.value.log_detail


@pytest.mark.parametrize(
    ("suffix", "expected"),
    [
        (".mp4", ".mp4"),
        ("mp4", ".mp4"),
        (".MP4", ".mp4"),
        ("", ""),
        (".", ""),
        ("..", ""),
        ("../evil", ""),
        (".tar.gz", ""),  # 超过 8 位之外的怪值一律丢弃
        (".toolongextension", ""),
        ("/abs", ""),
    ],
)
def test_safe_suffix_blocks_path_traversal(suffix: str, expected: str) -> None:
    """下载物的后缀来自 `File.name`（不可信）：只允许 `[A-Za-z0-9]{1,8}`。"""
    assert media._safe_suffix(suffix) == expected


# --------------------------------------------------------------------------- #
# 5. 三段判型梯子（真夹具 + 垃圾字节）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_classify_file_whitelisted_real_video(
    media_fixtures: dict[str, Path], tmp_path: Path
) -> None:
    """① 白名单内 + 真视频 ⇒ `("video", 5.0, has_audio=True, has_video=True)`。"""
    target = tmp_path / "clip.mp4"
    shutil.copyfile(media_fixtures["video_5s"], target)
    handle = await _file_handle(target)
    kind, duration, has_audio, has_video = await media.classify(
        handle, str(target), max_media_bytes=_MAX_BYTES, workdir=str(tmp_path)
    )
    assert (kind, duration, has_audio, has_video) == ("video", 5.0, True, True)


@pytest.mark.asyncio
async def test_classify_file_whitelisted_real_audio(
    media_fixtures: dict[str, Path], tmp_path: Path
) -> None:
    """① 白名单内 + 真音频（wav）⇒ `("audio", 30.0, True, False)`（没有视频流）。"""
    target = tmp_path / "voice.wav"
    shutil.copyfile(media_fixtures["audio_30s"], target)
    handle = await _file_handle(target)
    kind, duration, has_audio, has_video = await media.classify(
        handle, str(target), max_media_bytes=_MAX_BYTES, workdir=str(tmp_path)
    )
    assert (kind, duration, has_audio, has_video) == ("audio", 30.0, True, False)


@pytest.mark.asyncio
async def test_classify_file_outside_whitelist_non_media_is_unsupported(tmp_path: Path) -> None:
    """② 白名单外 + 非媒体（`.pdf`）⇒ `UNSUPPORTED_MEDIA_TYPE`（**不是** `PROBE_FAILED`）。

    错误码选择本身就是产品决定：对一个 PDF 说"读不出时长"会把用户引向"媒体坏了"，
    而他投的本来就不是媒体。
    """
    target = tmp_path / "manual.pdf"
    target.write_bytes(b"%PDF-1.4\n" + b"\x00" * 512)
    handle = await _file_handle(target)
    with pytest.raises(envelope.ToolError) as exc_info:
        await media.classify(
            handle, str(target), max_media_bytes=_MAX_BYTES, workdir=str(tmp_path)
        )
    assert _code(exc_info) == envelope.ERR_UNSUPPORTED_MEDIA_TYPE
    assert "ext=pdf" in exc_info.value.log_detail


@pytest.mark.asyncio
async def test_classify_file_whitelisted_but_probe_fails_is_unsupported(tmp_path: Path) -> None:
    """③ 白名单内但 `ffprobe` 探测失败、且解不成音频 ⇒ `UNSUPPORTED_MEDIA_TYPE`。

    `log_detail` 必须同时写出"ffprobe 失败"与"解不成音频"，否则运维看不出是哪一段梯子掉下来的。
    """
    target = tmp_path / "broken.mp4"
    target.write_bytes(b"\x00\x01\x02not a media file\xff" * 32)
    handle = await _file_handle(target)

    # 先证明"探测确实失败"（而不是恰好没有流）：rc != 0 ⇒ PROBE_FAILED
    with pytest.raises(envelope.ToolError) as probe_exc:
        await media.probe_streams(str(target))
    assert _code(probe_exc) == envelope.ERR_PROBE_FAILED

    with pytest.raises(envelope.ToolError) as exc_info:
        await media.classify(
            handle, str(target), max_media_bytes=_MAX_BYTES, workdir=str(tmp_path)
        )
    assert _code(exc_info) == envelope.ERR_UNSUPPORTED_MEDIA_TYPE
    assert "ffprobe failed" in exc_info.value.log_detail
    assert "not decodable as audio" in exc_info.value.log_detail


@pytest.mark.asyncio
async def test_classify_file_silk_ladder_saves_ffprobe_failure(
    media_fixtures: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**silk 反例**（"ffprobe 双保险"被推翻的地方）：ffprobe 读不懂 ≠ 不是音频。

    实测事实：QQ 语音的 silk 是合法音频，但 `ffprobe` 直接判"非法数据"（rc=1）。
    所以白名单命中（`.silk` ∈ `AUDIO_EXTS`）+ 探测失败时，梯子必须再试一次 `ensure_wav`
    —— 这一段救得回来就是 `audio`，而不是 `UNSUPPORTED_MEDIA_TYPE`。
    """
    target = tmp_path / "voice.silk"
    target.write_bytes(b"\x02#!SILK_V3" + b"\x00" * 64)  # ffprobe 读不懂的字节

    async def _fake_ensure_wav(path: str, output_path: str | None = None) -> str:
        # 复刻核心语义：解出来一份合法 wav（真夹具），返回它的路径
        shutil.copyfile(media_fixtures["audio_30s"], output_path)
        return output_path

    monkeypatch.setattr(media, "_core_ensure_wav", _fake_ensure_wav)
    handle = await _file_handle(target)
    kind, duration, has_audio, has_video = await media.classify(
        handle, str(target), max_media_bytes=_MAX_BYTES, workdir=str(tmp_path)
    )
    assert kind == "audio"
    assert duration == 30.0
    assert (has_audio, has_video) == (True, False)


@pytest.mark.asyncio
async def test_classify_file_silk_undecodable_still_unsupported(tmp_path: Path) -> None:
    """对称面：白名单命中 + 探测失败 + **解不成音频** ⇒ 仍是 `UNSUPPORTED_MEDIA_TYPE`。"""
    target = tmp_path / "voice.silk"
    target.write_bytes(b"\x02#!SILK_V3" + b"\x00" * 64)
    handle = await _file_handle(target)
    with pytest.raises(envelope.ToolError) as exc_info:
        await media.classify(
            handle, str(target), max_media_bytes=_MAX_BYTES, workdir=str(tmp_path)
        )
    assert _code(exc_info) == envelope.ERR_UNSUPPORTED_MEDIA_TYPE


@pytest.mark.asyncio
async def test_classify_file_without_extension_tries_audio_first(
    media_fixtures: dict[str, Path], tmp_path: Path
) -> None:
    """无扩展名 / 白名单外但真是 wav ⇒ 第 2 段（音频）救回来，不必要求文件名正确。"""
    target = tmp_path / "voice"
    shutil.copyfile(media_fixtures["audio_30s"], target)
    handle = await _file_handle(target)
    kind, duration, _has_audio, has_video = await media.classify(
        handle, str(target), max_media_bytes=_MAX_BYTES, workdir=str(tmp_path)
    )
    assert kind == "audio"
    assert duration == 30.0
    assert has_video is False


@pytest.mark.asyncio
async def test_classify_file_size_over_limit_is_media_too_large(tmp_path: Path) -> None:
    """体积闸在**判型之前**（成本闸门）：超 `max_media_bytes` ⇒ `MEDIA_TOO_LARGE`。"""
    target = tmp_path / "big.mp4"
    target.write_bytes(b"\x00" * 4096)
    handle = await _file_handle(target)
    with pytest.raises(envelope.ToolError) as exc_info:
        await media.classify(handle, str(target), max_media_bytes=1024, workdir=str(tmp_path))
    assert _code(exc_info) == envelope.ERR_MEDIA_TOO_LARGE


@pytest.mark.asyncio
async def test_classify_record_keeps_probe_failed_semantics(tmp_path: Path) -> None:
    """`Record` / `Video` 的老口径不变：时长读不出仍判 `PROBE_FAILED`（不是 UNSUPPORTED）。"""
    target = tmp_path / "broken.wav"
    target.write_bytes(b"\x00garbage\x00")
    handle = media.MediaHandle(comp=make_record(target), quoted=False)
    with pytest.raises(envelope.ToolError) as exc_info:
        await media.classify(handle, str(target), max_media_bytes=_MAX_BYTES, workdir=str(tmp_path))
    assert _code(exc_info) == envelope.ERR_PROBE_FAILED


# --------------------------------------------------------------------------- #
# 6. 端到端：一条群文件视频走完整主链路（真 ffprobe/ffmpeg，假 provider）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_analyze_file_segment_end_to_end(
    media_fixtures: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`File` 段视频 ⇒ `ok:true`、`kind=video`、`notes` 带 `source:file`。

    v0.1 在这里返回的是 `MEDIA_NOT_FOUND`（这就是"上线至今零成功"的那条路）。
    真 ffprobe + 真 ffmpeg 抽音轨（亚秒级），只有 provider 是假的。
    """
    target = tmp_path / "群文件.mp4"
    shutil.copyfile(media_fixtures["video_5s"], target)
    event = make_event(current=[make_file("群文件.mp4", file=str(target))])

    plugin_data = tmp_path / "plugin_data"
    monkeypatch.setattr(cache_mod, "_core_plugin_data_path", lambda: str(plugin_data))
    monkeypatch.setattr(providers, "resolve_audio_provider", _returning(audio_chat(text="群文件的转写")))
    monkeypatch.setattr(providers, "resolve_chat_provider", _returning(FakeChatProvider(text="摘要")))

    payload = await analyze_mod.analyze(
        context=FakeContext(),
        event=event,
        target="current",
        focus="",
        include_frames=False,
        settings=settings.Settings(),
        cache=cache_mod.DiskCache(plugin_name=settings.PLUGIN_NAME, ttl_seconds=86400),
    )

    assert payload["ok"] is True
    assert payload["kind"] == "video"
    assert payload["duration_s"] == 5.0
    assert payload["transcript"] == [{"t": 0.0, "text": "群文件的转写"}]
    assert "source:file" in payload["notes"]
    assert "audio_extracted" in payload["notes"]
    assert payload["cached"] is False


@pytest.mark.asyncio
async def test_analyze_file_download_note(
    media_fixtures: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """走"有界下载"那条路时，`notes` 必须有 `file_downloaded`（缓存/排障都要能看出取件方式）。"""
    workdir_holder: dict[str, str] = {}

    async def _download(url: str, *, workdir: str, max_bytes: int, timeout: float, suffix: str) -> str:
        workdir_holder["workdir"] = workdir
        out = Path(workdir) / f"file{suffix or '.mp4'}"
        shutil.copyfile(media_fixtures["video_5s"], out)
        return str(out)

    monkeypatch.setattr(media, "download_bounded", _download)
    monkeypatch.setattr(providers, "resolve_audio_provider", _returning(audio_chat(text="下载后转写")))
    monkeypatch.setattr(providers, "resolve_chat_provider", _returning(FakeChatProvider(text="摘要")))

    monkeypatch.setattr(
        cache_mod, "_core_plugin_data_path", lambda: str(tmp_path / "plugin_data")
    )
    event = make_event(
        current=[make_file("远端.mp4", url="http://example.invalid/remote.mp4")]
    )
    payload = await analyze_mod.analyze(
        context=FakeContext(),
        event=event,
        target="current",
        focus="",
        include_frames=False,
        settings=settings.Settings(),
        cache=cache_mod.DiskCache(plugin_name=settings.PLUGIN_NAME, ttl_seconds=86400),
    )

    assert payload["ok"] is True
    assert "source:file" in payload["notes"]
    assert "file_downloaded" in payload["notes"]
    # 下载物落的是 analyze 自己的工作目录（`/tmp` 下、`finally` 里删掉）
    assert workdir_holder["workdir"].startswith("/tmp/")
    assert not Path(workdir_holder["workdir"]).exists(), "工作目录必须已被 finally 删掉"


@pytest.mark.asyncio
async def test_analyze_file_non_media_is_unsupported_media_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端到端：群文件里发的是 PDF ⇒ `UNSUPPORTED_MEDIA_TYPE` 信封（3 键、零 provider 花费）。"""
    target = tmp_path / "notes.pdf"
    target.write_bytes(b"%PDF-1.4\n" + b"\x00" * 256)
    monkeypatch.setattr(
        cache_mod, "_core_plugin_data_path", lambda: str(tmp_path / "plugin_data")
    )
    plugin = __import__(
        "astrbot_plugin_media_analysis.main", fromlist=["MediaAnalysisPlugin"]
    ).MediaAnalysisPlugin(context=FakeContext(), config={})

    raw = await plugin.media_analyze(
        make_event(current=[make_file("notes.pdf", file=str(target))])
    )
    payload = json.loads(raw)
    # v0.4：门面受理即返回（4 键），失败信封在后台任务里产生（`background.wait` 取回）
    assert set(payload) == set(envelope.ACCEPT_KEYS), payload
    payload = await background.wait(payload["task_id"], timeout=30.0)
    assert payload == {
        "ok": False,
        "error": envelope.ERR_UNSUPPORTED_MEDIA_TYPE,
        "hint": envelope.HINTS[envelope.ERR_UNSUPPORTED_MEDIA_TYPE],
    }


def _returning(value):
    async def _inner(*_args, **_kwargs):
        return value

    return _inner
