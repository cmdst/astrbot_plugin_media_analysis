"""媒体侧：组件定位 / 路径解析 / 判流 / 时长 / 抽音轨 / 抽帧 / 内容哈希。

平台事实（SPEC §2.1，都是实测过的）：
- `Record` 到手时 `comp.path` **已经是本地 wav** —— 预处理阶段无条件把它落地并写回
  `component.path`（`preprocess_stage/stage.py:166-181`；引用链同 `:192-212`）。
- `Video` **不预处理**，要插件自己 `await comp.convert_to_file_path()`
  （`components.py:341-371`；URL 会由核心下载到临时目录）。
- `Reply.chain` 由平台适配器填一层（`aiocqhttp_platform_adapter.py:304-336`），
  引用里的引用拿不到链（`:325` 递归时 `get_reply=False`）。

本模块唯一的 core 内部导入是 `get_media_duration`（SPEC §2.5）：它不在
`astrbot.api.*` 的再导出面里，但 4.29.x 内稳定，失败会走我们自己的 `PROBE_FAILED`。
**该导入已加固**（t16）：核心把它改名/搬走时退化为本地 ffprobe 兜底，插件照常加载 ——
裸 import 会在导入期抛 `ModuleNotFoundError` ⇒ **静默拒载**（目录在、工具不注册）。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
from dataclasses import dataclass

from astrbot.api import logger as LOG
from astrbot.api.message_components import Record, Reply, Video

from . import envelope

# --------------------------------------------------------------------------- #
# 核心**内部面**加固：`get_media_duration` 不在 `astrbot.api.*` 的公开导出里，
# 一旦核心升级把它改名/搬走，裸 `from … import` 会在**导入期**炸 ⇒ 插件**静默拒载**
# （目录在、工具不注册、界面上像"没装"）。加固后最坏情况退化为
# 「本地 ffprobe 兜底 + 插件照常加载」，错误码仍是 `PROBE_FAILED`。
# 正常路径**逐字等价**：核心可导入时 `get_media_duration` 就是核心那个函数。
# --------------------------------------------------------------------------- #
async def _fallback_duration_ms(path: str) -> int | None:
    """本地兜底时长探测：自己起 `ffprobe` 取 `format.duration`（秒）× 1000 → 毫秒 int。

    与核心 `get_media_duration` 同契约：**返回毫秒**，任何异常/解析失败/超时一律 `None`
    （上层 `probe_duration_s` 据此抛 `PROBE_FAILED`）。必须 async —— 调用点是 `await`。
    复用本模块既有的子进程封装 `_run`，不写第二套 subprocess 样板。
    """
    argv = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        path,
    ]
    try:
        returncode, stdout, _stderr = await _run(
            argv, timeout=PROBE_TIMEOUT_S, error_code=envelope.ERR_PROBE_FAILED
        )
        if returncode != 0:
            return None
        payload = json.loads(stdout.decode("utf-8", "replace") or "{}")
        seconds = (payload.get("format") or {}).get("duration")
        if seconds is None:
            return None
        return int(round(float(seconds) * 1000))
    except Exception:  # noqa: BLE001 —— 兜底路径只认"拿到或拿不到"，绝不外抛
        return None


try:
    from astrbot.core.utils.media_utils import get_media_duration
except Exception:  # noqa: BLE001 —— 拿不到就退化为本地兜底，绝不因此拒载
    LOG.warning(
        "media_analysis: 核心内部面 astrbot.core.utils.media_utils.get_media_duration "
        "不可用，已切换到本地 ffprobe 兜底（插件仍加载）"
    )
    get_media_duration = _fallback_duration_ms

# 预算（SPEC §7.1）：正常路径实测都在 0.1 s 内，这些上限只防病态文件与平台慢下载。
RESOLVE_TIMEOUT_S = 20.0  # convert_to_file_path 兜底可能触发下载
PROBE_TIMEOUT_S = 10.0  # 一次 ffprobe
FFMPEG_TIMEOUT_S = 15.0  # 一次 ffmpeg（抽音轨 / 抽一帧）
FRAME_WIDTH_PX = 768  # 抽帧缩放长边；远低于核心图片上限 1280 px
DIGEST_CHUNK_BYTES = 1 << 20

_AUDIO_KINDS = (Record, Video)


@dataclass(frozen=True)
class MediaHandle:
    """一条待分析媒体：组件本身 + 是否来自引用消息。"""

    comp: object  # Record | Video
    quoted: bool

    @property
    def kind(self) -> str:
        """`"video"`（Video）或 `"audio"`（Record）。"""
        return "video" if isinstance(self.comp, Video) else "audio"


def _components(event) -> list:
    """`event.get_messages()`（`astr_message_event.py:180-182`）；拿不到就当空链。"""
    getter = getattr(event, "get_messages", None)
    if not callable(getter):
        return []
    try:
        components = getter()
    except Exception as exc:  # noqa: BLE001 —— 平台给了坏消息链也不许抛给模型
        LOG.warning("media_analysis: event.get_messages() 失败（%s）", type(exc).__name__)
        return []
    return list(components or [])


def find_media(event, target: str) -> MediaHandle:
    """定位媒体组件（SPEC §2.1 冻结实现）。

    `target` 只认精确的 `"current"` / `"quoted"`，不做大小写/别名归一
    （少一条隐式分支）。`BAD_TARGET` 判定在首行（SPEC §5 表）。
    """
    if target not in ("current", "quoted"):
        raise envelope.ToolError(envelope.ERR_BAD_TARGET, log_detail=f"target={target!r}")

    if target == "current":
        for comp in _components(event):
            if isinstance(comp, _AUDIO_KINDS):
                return MediaHandle(comp=comp, quoted=False)
        raise envelope.ToolError(
            envelope.ERR_MEDIA_NOT_FOUND, log_detail="current: no Record/Video"
        )

    for comp in _components(event):
        if not isinstance(comp, Reply):
            continue
        for inner in comp.chain or []:
            if isinstance(inner, _AUDIO_KINDS):
                return MediaHandle(comp=inner, quoted=True)
    raise envelope.ToolError(
        envelope.ERR_MEDIA_NOT_FOUND, log_detail="quoted: no Record/Video in chain"
    )


async def resolve_media_path(handle: MediaHandle) -> str:
    """拿到可读的本地路径：`path` 优先，`convert_to_file_path()` 兜底。

    两段都失败 ⇒ `MEDIA_UNRESOLVABLE`（组件在，但取不到本地文件）。
    """
    path = getattr(handle.comp, "path", None)
    if isinstance(path, str) and path and os.path.isfile(path):
        return os.path.abspath(path)

    converter = getattr(handle.comp, "convert_to_file_path", None)
    if callable(converter):
        try:
            resolved = await asyncio.wait_for(
                converter(), timeout=RESOLVE_TIMEOUT_S
            )
        except (asyncio.TimeoutError, TimeoutError):
            raise envelope.ToolError(
                envelope.ERR_MEDIA_UNRESOLVABLE,
                log_detail=f"convert_to_file_path timeout {RESOLVE_TIMEOUT_S}s",
            ) from None
        except Exception as exc:  # noqa: BLE001 —— 平台给的东西什么都可能抛
            raise envelope.ToolError(
                envelope.ERR_MEDIA_UNRESOLVABLE,
                log_detail=f"convert_to_file_path {type(exc).__name__}: {str(exc)[:120]}",
            ) from None
        if isinstance(resolved, str) and resolved and os.path.isfile(resolved):
            return resolved

    raise envelope.ToolError(
        envelope.ERR_MEDIA_UNRESOLVABLE,
        log_detail=f"kind={handle.kind} quoted={handle.quoted}",
    )


async def probe_duration_s(path: str) -> float:
    """时长（秒，2 位小数）：复用核心 `get_media_duration`（**返回毫秒**，失败 None）。"""
    try:
        duration_ms = await asyncio.wait_for(
            get_media_duration(path), timeout=PROBE_TIMEOUT_S
        )
    except (asyncio.TimeoutError, TimeoutError):
        raise envelope.ToolError(
            envelope.ERR_PROBE_FAILED, log_detail="get_media_duration timeout"
        ) from None
    except Exception as exc:  # noqa: BLE001
        raise envelope.ToolError(
            envelope.ERR_PROBE_FAILED,
            log_detail=f"get_media_duration {type(exc).__name__}",
        ) from None
    if not isinstance(duration_ms, (int, float)) or duration_ms <= 0:
        raise envelope.ToolError(
            envelope.ERR_PROBE_FAILED, log_detail="duration unavailable"
        )
    return round(float(duration_ms) / 1000.0, 2)


async def probe_streams(path: str) -> tuple[bool, bool]:
    """`(has_audio, has_video)`；ffprobe 不可用/超时 ⇒ `PROBE_FAILED`。

    **判据是 stdout 是否为空，不是 rc**（实测：无音轨时 `ffprobe -select_streams a`
    返回 rc=0 但 stdout 为空，SPEC §1 #9）。
    """
    async def has(kind: str) -> bool:
        argv = [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            kind,
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            path,
        ]
        _rc, out, _err = await _run(
            argv,
            timeout=PROBE_TIMEOUT_S,
            error_code=envelope.ERR_PROBE_FAILED,
        )
        return bool(out.strip())

    return await has("a"), await has("v")


async def extract_wav(path: str, *, workdir: str) -> str:
    """视频 → 16 kHz 单声道 wav（SPEC §7.4 冻结 argv）；失败抛 `AUDIO_EXTRACT_FAILED`。

    调用前必须已 `probe_streams` 判过有音轨：拿无音轨文件直接转码会抛**未归一化**的
    裸 `Exception`（SPEC §1 #8），错误码就不可控了。
    """
    out_wav = os.path.join(workdir, "audio.wav")
    argv = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-i",
        path,
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        out_wav,
    ]
    rc, _out, err = await _run(
        argv,
        timeout=FFMPEG_TIMEOUT_S,
        error_code=envelope.ERR_AUDIO_EXTRACT_FAILED,
    )
    if rc != 0 or not _is_nonempty_file(out_wav):
        raise envelope.ToolError(
            envelope.ERR_AUDIO_EXTRACT_FAILED,
            log_detail=f"rc={rc} stderr={err.decode('utf-8', 'replace')[:160]!r}",
        )
    return out_wav


def frame_grid(duration_s: float, count: int) -> list[float]:
    """抽帧坐标（SPEC §7.3 冻结公式）：每个等分段取**中点**，避开黑场与 EOF 越界。

    `at_s = round((i + 0.5) * duration_s / count, 2)`，i = 0..count-1。
    """
    safe_count = max(1, int(count))
    return [
        round((index + 0.5) * float(duration_s) / safe_count, 2)
        for index in range(safe_count)
    ]


async def extract_frames(
    video_path: str,
    *,
    count: int,
    duration_s: float,
    workdir: str,
    notes: list[str] | None = None,
) -> list[tuple[float, str]]:
    """按 §7.3 的网格逐点抽帧，返回 `[(at_s, jpg)]`（按 `at_s` 升序）。

    单帧失败**不抛**：记 `frame_extract_failed:<at_s>`（若给了 `notes`）后继续，
    其余帧照常返回（实测：`-ss` 超过 EOF 时 ffmpeg 返回 rc=234 且不产出文件）。
    一帧都没产出时返回 `[]`，由 `analyze` 判 `FRAME_EXTRACT_FAILED`。
    """
    frames: list[tuple[float, str]] = []
    for index, at_s in enumerate(frame_grid(duration_s, count)):
        out_jpg = os.path.join(workdir, f"frame_{index}.jpg")
        argv = [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-ss",
            f"{at_s:.3f}",
            "-i",
            video_path,
            "-frames:v",
            "1",
            "-vf",
            f"scale='min({FRAME_WIDTH_PX},iw)':-2",
            "-pix_fmt",
            "yuvj420p",
            "-q:v",
            "4",
            out_jpg,
        ]
        try:
            rc, _out, _err = await _run(
                argv,
                timeout=FFMPEG_TIMEOUT_S,
                error_code=envelope.ERR_FRAME_EXTRACT_FAILED,
            )
        except envelope.ToolError as exc:
            LOG.warning(
                "media_analysis: 抽帧 %ss 失败（%s）", at_s, exc.log_detail
            )
            rc = -1
        if rc == 0 and _is_nonempty_file(out_jpg):
            frames.append((at_s, out_jpg))
        elif notes is not None:
            notes.append(f"frame_extract_failed:{format_at_s(at_s)}")
    return frames


async def content_digest(path: str) -> str:
    """内容哈希：`sha256(文件字节).hexdigest()[:16]`（实测 5.3 MB → 0.01 s）。"""
    try:
        return await asyncio.to_thread(_digest_sync, path)
    except OSError as exc:
        raise envelope.ToolError(
            envelope.ERR_MEDIA_UNRESOLVABLE,
            log_detail=f"digest {type(exc).__name__}",
        ) from None


def format_at_s(at_s: float) -> str:
    """`frames[].at_s` 与 `notes` 里的 `:<at_s>` 用**同一个**格式化（2 位小数）。"""
    return f"{float(at_s):.2f}"


def track_temporary_local_file(event, path: str) -> None:
    """把中间产物登记给事件（`astr_message_event.py:237-239`，事件结束时框架清理）。

    测试用的 fake event 没有这个方法 ⇒ 整段 best-effort，失败即忽略；
    真正兜底的是 `analyze` 里 `finally` 的 `rmtree(workdir)`。
    """
    tracker = getattr(event, "track_temporary_local_file", None)
    if not callable(tracker):
        return
    with contextlib.suppress(Exception):
        tracker(path)


def _digest_sync(path: str) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(DIGEST_CHUNK_BYTES), b""):
            hasher.update(chunk)
    return hasher.hexdigest()[:16]


def _is_nonempty_file(path: str) -> bool:
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


async def _run(
    argv: list[str], *, timeout: float, error_code: str
) -> tuple[int, bytes, bytes]:
    """跑一次 argv（**不经 shell**），超时/起不来都归一成 `error_code`。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as exc:
        raise envelope.ToolError(
            error_code, log_detail=f"spawn {argv[0]} {type(exc).__name__}"
        ) from None
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        with contextlib.suppress(Exception):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise envelope.ToolError(
            error_code, log_detail=f"{argv[0]} timeout {timeout}s"
        ) from None
    return proc.returncode if proc.returncode is not None else -1, out, err
