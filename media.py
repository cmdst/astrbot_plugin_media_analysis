"""媒体侧：组件定位 / 路径解析 / 判型 / 判流 / 时长 / 抽音轨 / 抽帧 / GIF 归一 / 内容哈希。

v0.4（SPEC-v0.4 §5）在本模块新增两块，都是**纯函数或纯子进程**，不碰配置、不碰网络：
- **GIF 识别**：`has_gif_magic`（前 6 字节 `GIF87a`/`GIF89a`，**唯一**判据）+
  `_looks_like_gif`（后缀只作"要不要去证实"）+ `confirm_gif`（取件后二次确认的统一出口）；
  `Image` 只在这一档进链（`_pick_gif_image`），普通图片仍然拒绝。
- **GIF 归一**：`gif_argv`（冻结参数）+ `normalize_gif`（字节/超时/产物三道闸）——
  产物是 `<workdir>/gif.mp4`，源文件只读。

平台事实（SPEC.md §2.1 + SPEC-v0.2 §3.1，都是实测过的）：
- `Record` 到手时 `comp.path` **已经是本地 wav** —— 预处理阶段无条件把它落地并写回
  `component.path`（`preprocess_stage/stage.py:166-181`；引用链同 `:192-212`）。
- `Video` **不预处理**，要插件自己 `await comp.convert_to_file_path()`
  （`components.py:341-371`；URL 会由核心下载到临时目录）。
- `File`（群文件投递，v0.2 新增）**不许**用 `.file` 属性：它在异步上下文里恒返回空串并打
  WARN（`components.py:797-806`，实测）；`get_file()` 也不许用（`io.py:306`：超时 1800 s、
  无大小上限、落 `data/temp`）。我们只读 `file_`，或自己**有界下载** `url`（§3.2）。
- `Reply.chain` 由平台适配器填一层（`aiocqhttp_platform_adapter.py:304-336`），
  引用里的引用拿不到链（`:325` 递归时 `get_reply=False`）。

本模块的 core 内部导入只有三个（都**加固**过：搬家时退化为本地兜底，插件照常加载，
绝不因 `ModuleNotFoundError` **静默拒载**）：
`get_media_duration`、`ensure_wav`（silk 靠它救）、`astrbot_path` 无关（在 `cache` 里）。

DAG 纪律（SPEC-v0.2 §10）：本模块**不 import `settings`**。需要配置阈值的地方
（`classify` / `resolve_media_path` 的 `max_media_bytes`）取**传参**，保持依赖图不新增边。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import time
from dataclasses import dataclass, replace
from urllib.parse import unquote

from astrbot.api import logger as LOG
from astrbot.api.message_components import File, Image, Record, Reply, Video

from . import envelope

# --------------------------------------------------------------------------- #
# 核心**内部面**加固（`get_media_duration` / `ensure_wav` 不在 `astrbot.api.*` 的导出面里）。
# 正常路径**逐字等价**：能导入时就是核心那两个函数。
# --------------------------------------------------------------------------- #
async def _fallback_duration_ms(path: str) -> int | None:
    """本地兜底时长探测：自己起 `ffprobe` 取 `format.duration`（秒）× 1000 → 毫秒 int。

    与核心 `get_media_duration` 同契约：**返回毫秒**，任何异常/解析失败/超时一律 `None`
    （上层 `probe_duration_s` 据此抛 `PROBE_FAILED`）。必须 async —— 调用点是 `await`。
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

try:
    from astrbot.core.utils.media_utils import ensure_wav as _core_ensure_wav
except Exception:  # noqa: BLE001 —— 拿不到就只走 ffmpeg；silk 因此解不了
    LOG.warning(
        "media_analysis: 核心内部面 astrbot.core.utils.media_utils.ensure_wav 不可用，"
        "silk（QQ 语音）将无法解码（插件仍加载）"
    )
    _core_ensure_wav = None  # type: ignore[assignment]

try:
    import aiohttp
except Exception:  # noqa: BLE001 —— 拿不到就只能读已有的 file_（群文件 url 下不了）
    LOG.warning(
        "media_analysis: aiohttp 不可用，群文件（File 段）的有界下载已禁用（插件仍加载）"
    )
    aiohttp = None  # type: ignore[assignment]

# ---- 预算（SPEC.md §7.1 / SPEC-v0.2 §3.2）----
RESOLVE_TIMEOUT_S = 20.0  # convert_to_file_path 兜底可能触发下载
PROBE_TIMEOUT_S = 10.0  # 一次 ffprobe
FFMPEG_TIMEOUT_S = 15.0  # 一次 ffmpeg（抽音轨 / 转码 / 抽一帧）
FILE_DOWNLOAD_TIMEOUT_S = 20.0  # 我们自己下 File 的总时长（与 RESOLVE_TIMEOUT_S 同量级）
FRAME_WIDTH_PX = 768  # 抽帧缩放长边；远低于核心图片上限 1280 px
DIGEST_CHUNK_BYTES = 1 << 20
DOWNLOAD_CHUNK_BYTES = 64 * 1024

# 媒体候选集（**顺序＝优先级**：Record → Video → File，§3.5）。
# v0.4：`Image` 作为 **GIF 候选**插在 `Video` 与 `File` 之间（SPEC-v0.4 §5.2）——
# 它不是一个新 kind，只是一条"先证魔数再进视频链"的路径，所以不进这张纯类型表，
# 由 `_pick_gif_image` 单独处理（见 `find_media` 的四档顺序）。
_MEDIA_KINDS = (Record, Video, File)

# ---- v0.4 GIF 归一（SPEC-v0.4 §5.6 冻结；**代码常量，不新增配置键**）----
GIF_MAGICS = (b"GIF87a", b"GIF89a")  # 唯一判定依据（后缀只作"要不要去证实"）
GIF_SNIFF_BYTES = 6  # 只读前 6 字节
GIF_MAX_FPS = 12  # GIF 常见 10–25 fps；12 保留动感、帧数砍半
GIF_MAX_WIDTH_PX = 640  # 长边封顶（表情/短视频足够让模型说清"发生了什么"）
GIF_CRF = 26  # 体积优先；GIF 源已是索引色
GIF_CONVERT_TIMEOUT_S = 10.0  # 护栏不是产品参数（实测 0.62 s，16× 余量）
#: 图片魔数的**显式**判据（v0.4.1）：`File` 段里出现这些内容时直接拒绝，避免
#: "ffprobe 把它当视频流 + 有时长"的 APNG/动画 WebP 被贴 `video/mp4` 送进原生链。
#: 只覆盖常见静态/动图容器；GIF 单独由 `has_gif_magic` 判定。
IMAGE_SNIFF_BYTES = 12
IMAGE_MAGIC_PREFIXES = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
)
# 单帧 GIF 走**静止变体**（`gif_static_argv`）：在冻结滤镜前加
# `tpad=stop_mode=clone:stop_duration=1`，把唯一一帧克隆成 1 s，再按 `fps=12` 取样
# ⇒ 1.0–1.083 s / 12–13 帧（`-r 1` 只有 1 帧、`-r 12` 不复制帧，两者都实测无效）。
# 为什么不是"抬 GCE 保持时长再重试"：那套只在源里**有 GCE** 时可用（`ffmpeg -frames:v 1`
# 之外的老式 GIF87a 连 GCE 都没有）；静止变体对**全部**单帧形态一步到位，且不改用户字节、
# 不需要第二次 ffmpeg。


# 扩展名白名单（SPEC-v0.2 §3.3，大小写不敏感）。表外**不是拒绝**，是转内容探测（§3.4）。
VIDEO_EXTS = frozenset(
    {"mp4", "mov", "m4v", "mkv", "webm", "avi", "flv", "wmv", "3gp", "ts", "mpeg", "mpg"}
)
AUDIO_EXTS = frozenset(
    {
        "m4a",
        "mp3",
        "wav",
        "amr",
        "silk",
        "aac",
        "flac",
        "ogg",
        "oga",
        "opus",
        "wma",
        "ape",
        "m4b",
    }
)

# v0.3 视频压缩的冻结参数（SPEC-v0.3 §6.2，取自 t8 实测）
COMPRESS_HEIGHT_PX = 480  # `scale=-2:480`：720p→480p，竖屏 720x1280 同样落到高 480
COMPRESS_VIDEO_PRESET = "veryfast"
COMPRESS_AUDIO_KBPS = 64  # AAC 64 kbps 单声道
COMPRESS_MIN_VIDEO_KBPS = 64  # 低于它就判"不可行"，不硬转（§6.1）
COMPRESS_SECOND_PASS_FACTOR = 0.95  # 第二趟的修正系数（t8：285k→269k 命中）
COMPRESS_MAX_PASSES = 2  # 硬上限：第 3 次起收益趋零（§6.3）

# `ensure_provider_wav` 的目标形态（交给多模态 provider 的一律是这个）
PROVIDER_WAV_CODEC = "pcm_s16le"
PROVIDER_WAV_SAMPLE_RATE = "16000"
PROVIDER_WAV_CHANNELS = "1"


@dataclass(frozen=True)
class CompressResult:
    """一次压缩尝试的结果（`ok=False` 时 `reason` 只用于 note 与日志）。

    `reason` ∈ `ok` / `infeasible`（码率低于下限）/ `timeout` / `ffmpeg_failed` /
    `empty`（产物为空）/ `budget_exceeded`（两趟仍超预算）/ `not_smaller`（压了反而更大）。
    """

    ok: bool
    path: str | None
    size: int | None
    source_size: int
    reason: str
    passes: int
    no_audio: bool


@dataclass(frozen=True)
class MediaHandle:
    """一条待分析媒体：组件本身 + 是否来自引用消息 + 是否来自 `File` 段 + 是否是 GIF 候选。

    `gif` 的语义（SPEC-v0.4 §5.2/§5.3）：
    - `True` = **候选**（`Image` 已用魔数证实，或 `Image`/`File` 只有 `.gif` 后缀线索）；
      真正的判定点在 `analyze` 的 `[3.5] confirm_gif`（取件后二次验魔数）；
    - `File` + `.gif` 后缀但魔数不符 ⇒ `confirm_gif` 把它**降回** `gif=False`，
      继续走 v0.3 的三段梯子（行为不变）。
    """

    comp: object  # Record | Video | Image(GIF) | File
    quoted: bool
    from_file: bool = False
    gif: bool = False

    @property
    def kind(self) -> str:
        """`"video"`（Video / GIF）或 `"audio"`（Record / File）。

        `File` 的真实类型**不由扩展名决定**，由 `classify` 按"能解出什么流"判（§3.4），
        所以这里给的是占位值，任何取 `kind` 的地方都必须用 `classify` 的返回值。
        GIF 是例外：`kind` 恒 `"video"`（SPEC-v0.4 §5.10 —— 不新增 kind），
        且 `analyze` 对 GIF **跳过 `classify`**（那一步会把 `Image` 判成 `kind=audio`，
        见 SPEC-v0.4 §1.2 的 M7 实测）。
        """
        if isinstance(self.comp, Video) or self.gif:
            return "video"
        return "audio"

    @property
    def name(self) -> str:
        """文件名（`File` 段才有意义，用于取扩展名；其余回空串）。"""
        value = getattr(self.comp, "name", "")
        return value if isinstance(value, str) else ""


@dataclass(frozen=True)
class GifResult:
    """一次 gif→mp4 归一的结果（`analyze` 只读它，不回问媒体层）。

    - `path` / `size`：产物（`<workdir>/gif.mp4`）与它的字节数；
    - `source_size`：源 GIF 字节（`not`: `gif_converted:<in>-><out>`）；
    - `duration_s`：**产物**时长（信封与抽帧网格用它的，不是源 GIF 的）；
    - `frames`：**源 GIF** 的帧数（`<=1` ⇒ 静态，`analyze` 记 `gif_static`）。
    """

    path: str
    size: int
    source_size: int
    duration_s: float
    frames: int

    @property
    def static(self) -> bool:
        """源 GIF 只有 1 帧（静态图伪装成动图）。帧数读不出（0）时按非静态处理。"""
        return 0 < self.frames <= 1



def _components(event) -> list:
    """`event.get_messages()`（`astr_message_event.py:180-182`）；getter 不存在 ⇒ 空链。

    **读消息链本身失败**（平台给了坏对象、适配器抛错）不降级成空链：那会把真因伪装成
    「消息里没有语音/视频」（`MEDIA_NOT_FOUND`），让用户/管理员朝错方向查（评审 F8）。
    归一成 `MEDIA_UNRESOLVABLE`（"媒体取不到"），仍不把异常抛给模型。
    """
    getter = getattr(event, "get_messages", None)
    if not callable(getter):
        return []
    try:
        components = getter()
    except Exception as exc:  # noqa: BLE001 —— 归一成错误码，不外抛
        raise envelope.ToolError(
            envelope.ERR_MEDIA_UNRESOLVABLE,
            log_detail=f"event.get_messages() {type(exc).__name__}",
        ) from None
    return list(components or [])


def _type_names(chain) -> str:
    """链里实际出现的组件类型名（`log_detail` 用，**不记录内容**）。

    v0.1 的 `log_detail` 只写 `no Record/Video`，于是 ops 在生产上排查时看不出
    "消息里其实有一个 `File` 段"—— v0.2 把实际类型名带上（`File` 段缺陷就靠这条定位）。
    """
    names = [type(comp).__name__ for comp in chain]
    if not names:
        return "[]"
    if len(names) > 8:
        names = names[:8] + ["…"]
    return "[" + ", ".join(names) + "]"


def _pick_media(chain, *, quoted: bool) -> MediaHandle | None:
    """按固定优先级（Record → Video → **Image(GIF)** → File）挑第一个媒体组件。

    优先级是**全链**的，不是"先来先得"：同一条消息里既有 `Record` 又有 `File` 时选
    `Record`（更专、体积更小、预处理已落地），`File` 只是 v0.2 新增的兜底候选。
    v0.4 只在 `File` **之前**插了一档（SPEC-v0.4 §5.2）：既有三种类型的相对顺序不变。

    `Image` 这一档**只**接受 GIF：魔数不符的普通图片不产生 handle（契约 3 的第①条防线），
    因此继续往后找 `File`；全都没有 ⇒ 上层给 `MEDIA_NOT_FOUND`（与 v0.3 体验一致）。

    唯一的 IO 是"走到 Image 这一档"时至多 3 次 6 字节本地读（`has_gif_magic`），
    **禁止**在 picker 里下载/转换（受理路径的硬约束，SPEC-v0.4 §5.2）。
    """
    for kind in _MEDIA_KINDS:
        if kind is File:
            # `File` 是兜底大类（可能是个 zip）⇒ 必须排在 GIF 候选**之后**，故此处跳过
            continue
        for comp in chain:
            if isinstance(comp, kind):
                return MediaHandle(comp=comp, quoted=quoted)

    gif_image = _pick_gif_image(chain)
    if gif_image is not None:
        return MediaHandle(comp=gif_image, quoted=quoted, gif=True)

    for comp in chain:
        if isinstance(comp, File):
            # `File` + `.gif` 后缀 ⇒ **候选**（魔数在取件后由 `confirm_gif` 二次确认，
            # 不符就降回 `gif=False` 走 v0.3 的三段梯子，行为不变）
            return MediaHandle(
                comp=comp, quoted=quoted, from_file=True, gif=_looks_like_gif(
                    getattr(comp, "name", "")
                )
            )
    return None


def _pick_gif_image(chain) -> object | None:
    """在链里找**第一个** GIF 形态的 `Image`（无则 `None`）。

    两种形态（SPEC-v0.4 §5.4）：

    1. **有本地文件** ⇒ 读 6 字节魔数，命中才接受。后缀**不参与判定**：核心预处理会把
       `file`/`url`/`path` 全部覆写成落地后的临时路径（`preprocess_stage/stage.py:93-96`），
       QQ 原始文件名已经丢了，落地后缀还可能被按检测到的 MIME 改写。
    2. **无本地文件**（预处理失败的远程引用）⇒ 只有**后缀线索**（`.gif`）时才受理；
       任务内 `resolve_media_path` 有界下载后再由 `confirm_gif` 验魔数。
       没有后缀就**不下载、不接受**（不为一张 JPEG 付一次网络往返）。
    """
    for comp in chain:
        if not isinstance(comp, Image):
            continue
        local = image_local_path(comp)
        if local is not None:
            if has_gif_magic(local):
                return comp
            continue  # 本地文件但不是 GIF ⇒ 继续找下一个 Image（最终可能落到 File）
        if _looks_like_gif(
            getattr(comp, "name", ""),
            getattr(comp, "file", ""),
            getattr(comp, "url", ""),
        ):
            return comp
    return None


def image_local_path(comp) -> str | None:
    """`Image` 的本地文件路径（三选一第一个 `os.path.isfile` 命中的）；否则 `None`。

    `Image` 到手时三个字段通常都指向同一个落地文件（核心预处理无条件落地并回写，
    `preprocess_stage/stage.py:65-96`），但平台/版本不同可能只写其中一个 ⇒ 三个都试。
    """
    for field_name in ("path", "file", "url"):
        value = getattr(comp, field_name, "")
        if not isinstance(value, str) or not value:
            continue
        candidate = file_uri_to_path(value)
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return None


def has_gif_magic(path: str) -> bool:
    """前 6 字节是否是 GIF 魔数（`GIF87a` / `GIF89a`）——**GIF 的唯一判定依据**。

    为什么"能解出视频流"不能当判据（SPEC-v0.4 §1.2 M6 实测）：`ffprobe` 对 PNG / APNG /
    WebP 也报 `codec_type=video`，只有 GIF 带 `format.duration`。今天 PNG 被拒靠的是
    "时长缺失"这个**副作用**，不是显式判据 —— 一旦哪天 ffmpeg 给 APNG 补上时长，
    图片就会静默流进视频链。所以这里只认魔数。

    读不出来（文件不可读/不足 6 字节）一律 `False`：拿不到证据就不放行。
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(GIF_SNIFF_BYTES)
    except OSError:
        return False
    return head in GIF_MAGICS


def image_content_kind(path: str) -> str | None:
    """`File` 内容是否是图片（`"png"` / `"jpeg"` / `"webp"`），否则 `None`。

    v0.4.1 的补漏：`File` 段里没有后缀的图片（尤其 **APNG / 动画 WebP**）在旧梯子上会被
    `ffprobe` 判成"有视频流"、且可能带时长 ⇒ 原始图片字节被贴 `data:video/mp4` 送进原生链。
    这里在判型前显式识别并拒绝，让"普通图片不进本插件"的契约对 `File` 也成立。

    APNG 就是 PNG 容器（`acTL` 块只在 IHDR 之后），因此按 PNG 处理即可；动画 WebP 是
    RIFF 容器（`RIFF....WEBP`）。读不出来一律 `None`（拿不到证据就不下结论）。
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(IMAGE_SNIFF_BYTES)
    except OSError:
        return None
    for prefix, kind in IMAGE_MAGIC_PREFIXES:
        if head.startswith(prefix):
            return kind
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def _looks_like_gif(*values: object) -> bool:
    """名字/引用串是否以 `.gif` 结尾（大小写不敏感；**只作"要不要去证实"，不作判定**）。

    URL 形态会先砍掉 `?query` 与 `#fragment`（QQ 的图链常带签名参数）。
    """
    for value in values:
        if not isinstance(value, str) or not value:
            continue
        text = value.split("#", 1)[0].split("?", 1)[0]
        if text.lower().endswith(".gif"):
            return True
    return False


def confirm_gif(handle: MediaHandle, path: str) -> MediaHandle:
    """取件后**二次确认**魔数（SPEC-v0.4 §5.3 的统一出口），返回带确定 `gif` 的 handle。

    刻意**是同步函数**（与 SPEC-v0.4 §5.3 的签名逐字一致）：它只读 6 字节（`has_gif_magic`），
    没有可等待的 IO，做成协程只会让调用点多一个 `await` 而没有收益。

    三条分支（幂等：对已确认的 handle 再调一次结果相同）：

    1. 魔数命中 ⇒ `gif=True`（`Image` 与 `File` 都走这条）；
    2. `File` 段但魔数不符 ⇒ **降回** `gif=False`，调用方继续走 v0.3 的三段梯子
       （改名党说的 `.gif` 不该改变既有行为）；
    3. `Image` 段但魔数不符 ⇒ `MEDIA_NOT_FOUND`：普通图片**绝不进链**（契约 3）。
       这里也是"远程 `Image` + `.gif` 后缀"那条路的收口点。
    """
    if has_gif_magic(path):
        return handle if handle.gif else replace(handle, gif=True)
    if handle.from_file:
        return handle if not handle.gif else replace(handle, gif=False)
    raise envelope.ToolError(
        envelope.ERR_MEDIA_NOT_FOUND,
        log_detail="image: magic is not GIF (普通图片不进链)",
    )


async def find_media(event, target: str) -> MediaHandle:
    """定位媒体组件（SPEC.md §2.1 + SPEC-v0.2 §3.5 三态 + SPEC-v0.4 §5.2 GIF 候选）。

    **v0.4 起是协程**（契约变更，评审已批准）：`Image` 的 GIF 判定要读 6 字节魔数。
    纯类型阶段（Record/Video/File）不产生 IO。

    `target` 只认精确的 `"current"` / `"quoted"`，不做大小写/别名归一
    （少一条隐式分支），**绝不**接受文件路径（§3.6：那会给模型一个"读任意本地文件并
    送进远端模型"的原语）。`BAD_TARGET` 判定在首行。
    """
    if target not in ("current", "quoted"):
        raise envelope.ToolError(envelope.ERR_BAD_TARGET, log_detail=f"target={target!r}")

    if target == "current":
        chain = _components(event)
        found = _pick_media(chain, quoted=False)
        if found is not None:
            return found
        raise envelope.ToolError(
            envelope.ERR_MEDIA_NOT_FOUND,
            log_detail=f"current: no Record/Video/GIF/File; chain={_type_names(chain)}",
        )

    outer = _components(event)
    quoted_names: list[str] = []
    for comp in outer:
        if not isinstance(comp, Reply):
            continue
        inner_chain = list(comp.chain or [])
        quoted_names.extend(type(item).__name__ for item in inner_chain)
        found = _pick_media(inner_chain, quoted=True)
        if found is not None:
            return found
    raise envelope.ToolError(
        envelope.ERR_MEDIA_NOT_FOUND,
        log_detail=(
            "quoted: no Record/Video/GIF/File in chain; "
            f"chain={_type_names_str(quoted_names)}; outer={_type_names(outer)}"
        ),
    )



def _type_names_str(names: list[str]) -> str:
    if not names:
        return "[]"
    shown = names[:8] + (["…"] if len(names) > 8 else [])
    return "[" + ", ".join(shown) + "]"


def file_uri_to_path(value: str) -> str:
    """`file://` URI → 本地路径（核心 `components.py` 的 `file_uri_to_path` 同义）。

    自己实现而不是 import 核心内部面：这是个纯字符串变换，没有版本风险。
    `File.file_` 可能是 `file:///tmp/x.wav`（实测，SPEC-v0.2 §1.2-4）。
    """
    text = str(value or "")
    if not text.startswith("file:"):
        return text
    stripped = text[len("file://") :] if text.startswith("file://") else text[len("file:") :]
    return unquote(stripped)


def file_download_needed(handle: MediaHandle) -> bool:
    """`File` 段是否要走**我们自己**的有界下载（`file_` 不可用且 `url` 非空）。

    只用于 `notes` 溯源（`file_downloaded`），判定必须与 `resolve_media_path` 的分支一致。
    """
    if not handle.from_file:
        return False
    raw = getattr(handle.comp, "file_", "")
    if isinstance(raw, str) and raw and os.path.isfile(file_uri_to_path(raw)):
        return False
    url = getattr(handle.comp, "url", "")
    return isinstance(url, str) and bool(url)


async def resolve_media_path(
    handle: MediaHandle, *, workdir: str, max_media_bytes: int
) -> str:
    """拿到可读的本地路径（SPEC-v0.2 §3.2 冻结决策树）。

    - `File`：① `file_` 命中就零 IO 复用（核心多半已 `get_file()` 下好并回写）
      ② `file_` 空但 `url` 有 ⇒ 我们自己的**有界下载**（落 `<workdir>`，随 `finally` 删）
      ③ 两者都空 ⇒ `MEDIA_UNRESOLVABLE`；
    - **`Image` 的 GIF 候选（`handle.gif`）且无本地文件**：同样走**有界下载**
      （`_resolve_remote_gif_path`，落 `<workdir>`、受 `max_media_bytes` 约束）；
      **绝不**回落到 `Image.convert_to_file_path()` —— 那条路无字节上限、落
      `<ASTRBOT_ROOT>/data/temp`、调用后残留（评审 R1，v0.4 回归；SPEC-v0.4 §5.4 与
      AGENTS §8 都要求"任务内有界下载、字节只落 /tmp"）；
    - `Record` / `Video`：v0.1 原样（`path` 优先，`convert_to_file_path()` 兜底）。

    `workdir` / `max_media_bytes` 是**必填关键字参数**而不是默认值：`max_media_bytes`
    的唯一事实源是配置（`settings`），本模块不 import `settings`（DAG 纪律）；
    而"有界下载"必须有个 caller 负责清理的工作目录，默认成 `/tmp` 会漏删。
    """
    if handle.from_file:
        return await _resolve_file_path(
            handle.comp, workdir=workdir, max_media_bytes=max_media_bytes
        )

    path = getattr(handle.comp, "path", None)
    if isinstance(path, str) and path and os.path.isfile(path):
        return os.path.abspath(path)

    if handle.gif:
        # 远程 GIF 候选：**有界**下载（评审 R1）。放在 `convert_to_file_path` 之前，
        # 且下面那条兜底对 `handle.gif` 不会被执行到 —— 见 `_resolve_remote_gif_path`。
        return await _resolve_remote_gif_path(
            handle.comp, workdir=workdir, max_media_bytes=max_media_bytes
        )

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


def _http_url_of(comp) -> str | None:
    """组件上第一个 `http(s)://` 地址（`url` 优先，再看 `file`）；没有就 `None`。

    为什么两个字段都看：QQ 的图片组件有时只填 `url`、有时把地址塞在 `file` 里
    （`file` 也可能是 `file://` / `base64://` / 相对路径 —— 这些都不是可下载地址）。
    **只放行 http(s)**：其它 scheme 交给 `download_bounded` 抛 `MEDIA_UNRESOLVABLE`，
    而不是在这里猜。
    """
    for field_name in ("url", "file"):
        value = getattr(comp, field_name, "")
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    return None


async def _resolve_remote_gif_path(
    comp, *, workdir: str, max_media_bytes: int
) -> str:
    """远程 GIF 候选（`handle.gif` 且无本地文件）的**有界**取件（评审 R1）。

    为什么不能用 `Image.convert_to_file_path()`：它把响应**整段**落进
    `<ASTRBOT_ROOT>/data/temp`，**没有**字节上限（绕过 `limits.max_media_bytes`）、
    也不归调用方清理 —— 评审 R1 实测：`max_media_bytes=1024` 时仍把 6,253 B 全量写进
    `data/temp/media_image_*.gif` 并残留。这同时违反 SPEC-v0.4 §5.4「任务内有界下载」与
    AGENTS §8「下载字节只落 /tmp」。

    本函数只做三件事：解析 http(s) 地址 → `download_bounded`（**边读边数**、超限立刻放弃、
    落 `<workdir>`）→ 返回该路径。工作目录由 `analyze` 的 `finally: rmtree` 负责删。
    """
    url = _http_url_of(comp)
    if url is None:
        raise envelope.ToolError(
            envelope.ERR_MEDIA_UNRESOLVABLE,
            log_detail="gif image: no http(s) url and no local file",
        )
    return await download_bounded(
        url,
        workdir=workdir,
        max_bytes=max_media_bytes,
        timeout=FILE_DOWNLOAD_TIMEOUT_S,
        suffix=".gif",
    )


async def _resolve_file_path(comp, *, workdir: str, max_media_bytes: int) -> str:
    """`File` 段取件（§3.2）：`file_` → 有界下载 → `MEDIA_UNRESOLVABLE`。

    **禁用** `.file` 属性（异步上下文恒空串 + WARN）与 `get_file()`
    （1800 s 超时 + 无大小上限 + 落生产 `data/temp`）。
    """
    raw = getattr(comp, "file_", "")
    if isinstance(raw, str) and raw:
        candidate = file_uri_to_path(raw)
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)

    url = getattr(comp, "url", "")
    if isinstance(url, str) and url:
        name = getattr(comp, "name", "")
        suffix = os.path.splitext(name)[1] if isinstance(name, str) else ""
        return await download_bounded(
            url,
            workdir=workdir,
            max_bytes=max_media_bytes,
            timeout=FILE_DOWNLOAD_TIMEOUT_S,
            suffix=suffix,
        )

    raise envelope.ToolError(
        envelope.ERR_MEDIA_UNRESOLVABLE,
        log_detail="file segment: no file_ and no url",
    )


async def download_bounded(
    url: str,
    *,
    workdir: str,
    max_bytes: int,
    timeout: float = FILE_DOWNLOAD_TIMEOUT_S,
    suffix: str = "",
) -> str:
    """流式下载到 `<workdir>/file<后缀>`，**边读边数**字节，超限立刻放弃（§3.2）。

    三条与 `File.get_file()` 的区别（那三条正是否决它的理由）：总时长有界（默认 20 s）、
    字节数有界（超 `max_bytes` ⇒ `MEDIA_TOO_LARGE`）、落点是我们自己的 `/tmp` 工作目录
    （随 `analyze` 的 `finally: shutil.rmtree(workdir)` 一起消失）。

    URL 本身**不进日志**（可能带签名/临时 token），只记状态码与异常类名。
    """
    if aiohttp is None:
        raise envelope.ToolError(
            envelope.ERR_MEDIA_UNRESOLVABLE, log_detail="aiohttp unavailable"
        )
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise envelope.ToolError(
            envelope.ERR_MEDIA_UNRESOLVABLE,
            log_detail=f"unsupported url scheme {str(url)[:8]!r}",
        )
    out_path = os.path.join(workdir, f"file{_safe_suffix(suffix)}")
    total = 0
    try:
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    raise envelope.ToolError(
                        envelope.ERR_MEDIA_UNRESOLVABLE,
                        log_detail=f"download http status {response.status}",
                    )
                with open(out_path, "wb") as handle:
                    async for chunk in response.content.iter_chunked(
                        DOWNLOAD_CHUNK_BYTES
                    ):
                        total += len(chunk)
                        if max_bytes > 0 and total > max_bytes:
                            raise envelope.ToolError(
                                envelope.ERR_MEDIA_TOO_LARGE,
                                log_detail=f"downloaded >{max_bytes} bytes",
                            )
                        handle.write(chunk)
    except envelope.ToolError:
        _unlink(out_path)
        raise
    except (asyncio.TimeoutError, TimeoutError):
        _unlink(out_path)
        raise envelope.ToolError(
            envelope.ERR_MEDIA_UNRESOLVABLE,
            log_detail=f"download timeout {timeout}s",
        ) from None
    except Exception as exc:  # noqa: BLE001 —— 网络层什么都可能抛
        _unlink(out_path)
        raise envelope.ToolError(
            envelope.ERR_MEDIA_UNRESOLVABLE,
            log_detail=f"download {type(exc).__name__}: {str(exc)[:120]}",
        ) from None
    if not _is_nonempty_file(out_path):
        raise envelope.ToolError(
            envelope.ERR_MEDIA_UNRESOLVABLE, log_detail="download produced empty file"
        )
    return out_path


def _safe_suffix(suffix: str) -> str:
    """只保留 `[A-Za-z0-9]` 的 1–8 位扩展名（防路径穿越与怪文件名）。"""
    text = str(suffix or "")
    if text.startswith("."):
        text = text[1:]
    if not text or len(text) > 8 or not text.isalnum():
        return ""
    return "." + text.lower()


async def classify(
    handle: MediaHandle, path: str, *, max_media_bytes: int, workdir: str
) -> tuple[str, float, bool, bool]:
    """判型 + 时长 + 流标志，返回 `(kind, duration_s, has_audio, has_video)`。

    `Record` / `Video`：v0.1 口径（时长 `PROBE_FAILED`；`Video` 的流由 ffprobe 判）。

    `File`：**三段梯子**（§3.4），因为 `ffprobe` 单独做不了双保险 —— silk 是合法音频
    但 `ffprobe` 判"非法数据"（实测 rc=1）：
    1. 白名单命中 ⇒ 内容验证（防"改名党"）；`ffprobe` 说不像媒体 ⇒ 走第 2 段救一次；
    2. 白名单未命中 ⇒ 先当音频试（`ensure_wav`：magic 嗅探 + pysilk + ffmpeg）；
    3. 再当视频试（ffprobe 有视频流）。
    三段都不成立 ⇒ `UNSUPPORTED_MEDIA_TYPE`（**不是** `PROBE_FAILED`：对一个 `.pdf`/
    `.zip` 说"读不出时长"会把用户引向"媒体坏了"，而他投的本来就不是媒体，§3.5）。
    `workdir` 必填：解码中间物只落 `/tmp` 工作目录。
    """
    size = _size_of(path)
    if size is not None and max_media_bytes > 0 and size > max_media_bytes:
        raise envelope.ToolError(
            envelope.ERR_MEDIA_TOO_LARGE,
            log_detail=f"size={size} max_media_bytes={max_media_bytes}",
        )

    if not handle.from_file:
        duration_s = await probe_duration_s(path)
        if isinstance(handle.comp, Video):
            has_audio, has_video = await probe_streams(path)
            return "video", duration_s, has_audio, has_video
        return "audio", duration_s, True, False

    return await _classify_file(handle, path, workdir=workdir)


async def _classify_file(
    handle: MediaHandle, path: str, *, workdir: str
) -> tuple[str, float, bool, bool]:
    """`File` 段的三段判型梯子（§3.4 冻结实现）。"""
    name = handle.name or os.path.basename(path)
    ext = os.path.splitext(name)[1].lower().lstrip(".")

    if ext in VIDEO_EXTS or ext in AUDIO_EXTS:
        try:
            has_audio, has_video = await probe_streams(path)
        except envelope.ToolError:
            # 第 2 段：白名单说是媒体但 ffprobe 读不懂（silk 就是这一类）
            wav = await try_ensure_wav(path, workdir=workdir)
            if wav is None:
                raise envelope.ToolError(
                    envelope.ERR_UNSUPPORTED_MEDIA_TYPE,
                    log_detail=f"ext={ext} ffprobe failed and not decodable as audio",
                ) from None
            return "audio", await _file_duration(wav), True, False
        duration_s = await _file_duration(path)
        return ("video" if has_video else "audio"), duration_s, has_audio, has_video

    # 第 2 段：白名单未命中 ⇒ 先当音频试（覆盖 silk/amr/mp3/m4a 与"无扩展名"）
    wav = await try_ensure_wav(path, workdir=workdir)
    if wav is not None:
        return "audio", await _file_duration(wav), True, False

    # 第 3 段：再当视频试
    try:
        has_audio, has_video = await probe_streams(path)
    except envelope.ToolError:
        raise envelope.ToolError(
            envelope.ERR_UNSUPPORTED_MEDIA_TYPE,
            log_detail=f"ext={ext} not decodable as audio or video",
        ) from None
    if has_video:
        return "video", await _file_duration(path), has_audio, has_video

    raise envelope.ToolError(
        envelope.ERR_UNSUPPORTED_MEDIA_TYPE,
        log_detail=f"ext={ext} no video stream",
    )


async def _file_duration(path: str) -> float:
    """`File` 段的时长；读不出 ⇒ `UNSUPPORTED_MEDIA_TYPE`（§3.5 的 `File` 口径）。"""
    try:
        return await probe_duration_s(path)
    except envelope.ToolError as exc:
        raise envelope.ToolError(
            envelope.ERR_UNSUPPORTED_MEDIA_TYPE,
            log_detail=f"duration unavailable ({exc.log_detail})",
        ) from None


async def try_ensure_wav(path: str, *, workdir: str) -> str | None:
    """尽力把任意文件解成 wav（silk 靠 pysilk、其余靠 ffmpeg）；失败回 `None`。

    它救的是"合法音频但 ffprobe 读不懂"的 silk（§1.2-5：`ffprobe` rc=1、magic=`silk`、
    `ensure_wav` 解得出 2.000000 s）。核心的 `ensure_wav` 语义：已经是 wav 就**原样返回**
    （不重复转码），所以返回值可能等于入参。
    """
    if _core_ensure_wav is None:
        return None
    out_wav = os.path.join(workdir, "decode.wav")
    try:
        wav = await asyncio.wait_for(
            _core_ensure_wav(path, out_wav), timeout=FFMPEG_TIMEOUT_S
        )
    except (asyncio.TimeoutError, TimeoutError):
        return None
    except Exception as exc:  # noqa: BLE001 —— 非音频/解码失败都归 None
        LOG.debug("media_analysis: ensure_wav 失败（%s）", type(exc).__name__)
        return None
    if not isinstance(wav, str) or not _is_nonempty_file(wav):
        return None
    if await _audio_stream_info(wav) is None:
        return None
    return wav


async def ensure_provider_wav(path: str, *, workdir: str, extracted: bool = False) -> str:
    """把音频/视频源变成**交给 provider 的规范 wav**：16 kHz / 单声道 / `pcm_s16le`。

    为什么要自己转（§1.2-3 体积口径）：同一段 2 s 音频，我们 `-ac 1 -ar 16000` ⇒ base64
    85,440；让核心转（保留原采样率/声道）⇒ 470,504，**5.5×**。

    - `extracted=True`（视频音轨）：直接 `ffmpeg -vn -ac 1 -ar 16000 -c:a pcm_s16le`；
    - 已是规范 wav ⇒ **返回原路径**（零重编码，`st_mtime_ns` 不变）；
    - silk / m4a / mp3 / amr… ⇒ 先 `try_ensure_wav`（magic 嗅探 + pysilk），再按需重编码。

    返回值与入参 `path` 不同 ⇒ 说明确实重编码/解码过（`analyze` 据此加 `audio_normalized`）。
    """
    if extracted:
        return await extract_wav(path, workdir=workdir)

    if _is_provider_wav(await _audio_stream_info(path)):
        return path

    wav = await try_ensure_wav(path, workdir=workdir)
    source = wav if wav is not None else path
    if _is_provider_wav(await _audio_stream_info(source)):
        return source
    return await _ffmpeg_to_wav(source, os.path.join(workdir, "provider.wav"))


#: "容器里没有可读时长"的 `log_detail`（`probe_duration_s` 与 `probe_gif_duration_s`
#: 共用一份字面量，别在两处各写一遍导致比对失效）。
DURATION_UNAVAILABLE = "duration unavailable"


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
            envelope.ERR_PROBE_FAILED, log_detail=DURATION_UNAVAILABLE
        )
    return round(float(duration_ms) / 1000.0, 2)


async def probe_gif_duration_s(path: str) -> float:
    """**源 GIF** 的时长（秒）；"没有可度量的时长" ⇒ `0.0`（SPEC-v0.4 §5.7 闸②）。

    为什么要单独一个入口（v0.4 实测的**实现缺陷修复**）：`ffprobe`/`get_media_duration`
    对**没有 GCE 块**的单帧 GIF 直接读不到时长（实测 `format.duration=N/A` ⇒
    `could not convert string to float: 'N/A'`），而这类 GIF 恰恰是真实世界里**最常见**的
    静态 GIF（GIF87a 老图；`/usr/share/doc/**/*.gif` 抽样 8/9 无 GCE）。沿用视频口径抛
    `PROBE_FAILED` 会让 SPEC-v0.4 §5.5 的"单帧 GIF ⇒ 受理 → 视频链 + `gif_static`"落不了地。

    为什么 `0.0` 是安全的：闸②是**上界**闸（`duration > max_media_seconds` 才拒），
    "没有可度量的时长"只可能来自"单帧 / 延迟全为 0"这类**极短**源，不可能超上限；
    真实时长稍后由产物 mp4 提供（`GifResult.duration_s`，`normalize_gif` 里再探一次，
    那次失败仍然 `GIF_CONVERT_FAILED`）。

    **只降级这一种情形**：ffprobe 缺失/超时/异常（`log_detail` 不是 `duration unavailable`）
    照旧上抛 `PROBE_FAILED` —— 那是环境坏了，不该被伪装成"没有时长"。
    """
    try:
        return await probe_duration_s(path)
    except envelope.ToolError as exc:
        if exc.code == envelope.ERR_PROBE_FAILED and exc.log_detail == DURATION_UNAVAILABLE:
            return 0.0
        raise


async def probe_streams(path: str) -> tuple[bool, bool]:
    """`(has_audio, has_video)`；ffprobe 不可用/超时/非零退出 ⇒ `PROBE_FAILED`。

    两条判据（评审 F7 修正后，v0.2 在 `File` 路径上被 §3.4 局部覆盖）：
    - **rc != 0 ⇒ `PROBE_FAILED`**：ffprobe 自己失败了，它有没有流是未知的；
      只看 stdout 会把"探测失败"读成"没有这条流"，错误码漂成
      `NO_AUDIO_TRACK`/`NO_VIDEO_TRACK`（让用户去换片源，而其实是探测坏了）。
    - **rc == 0 且 stdout 为空 ⇒ `False`**：这是"文件正常、就是没有这条流"的**正常返回**
      （实测：无音轨时 `ffprobe -select_streams a` 返回 rc=0 但 stdout 为空，SPEC §1 #9）。
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
        returncode, out, _err = await _run(
            argv,
            timeout=PROBE_TIMEOUT_S,
            error_code=envelope.ERR_PROBE_FAILED,
        )
        if returncode != 0:
            raise envelope.ToolError(
                envelope.ERR_PROBE_FAILED,
                log_detail=f"ffprobe rc={returncode} stream={kind}",
            )
        return bool(out.strip())

    return await has("a"), await has("v")


async def extract_wav(path: str, *, workdir: str) -> str:
    """视频 → 16 kHz 单声道 wav（SPEC.md §7.4 冻结 argv）；失败抛 `AUDIO_EXTRACT_FAILED`。

    调用前必须已判过有音轨：拿无音轨文件直接转码会抛**未归一化**的裸 `Exception`
    （SPEC.md §1 #8），错误码就不可控了。
    """
    return await _ffmpeg_to_wav(path, os.path.join(workdir, "audio.wav"))


def frame_grid(duration_s: float, count: int) -> list[float]:
    """抽帧坐标（SPEC.md §7.3 冻结公式）：每个等分段取**中点**，避开黑场与 EOF 越界。

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


def gif_argv(source: str, out_path: str) -> list[str]:
    """gif→mp4 的**冻结 argv**（SPEC-v0.4 §5.6，逐参数理由见规格表；实测 §13-M9/M10）。

    两个必须写的理由（都不是"更保险"，而是"不写就错"）：

    - `scale='trunc(min(640,iw)/2)*2':-2`：**宽度必须偶数**。奇数宽 GIF + `yuv420p` +
      libx264 ⇒ `rc=187`、**0 字节产物**（M9 实测 241x162 的样本）；朴素
      `scale='min(640,iw)'` 会静默产出空文件。
    - `-an`：显式写"产物无声"，下游 `probe_streams` 得到确定的 `(False, True)`。

    **不加 `-t`**：时长闸在转换**之前**（`analyze` 的 `[4-pre]`）。加了 `-t` 只会在
    "ffprobe 低报时长"时静默截断用户内容 —— 那比多花一秒 CPU 糟得多。
    """
    return [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-i",
        source,
        "-an",
        "-vf",
        f"fps={GIF_MAX_FPS},"
        f"scale='trunc(min({GIF_MAX_WIDTH_PX},iw)/2)*2':-2:flags=lanczos",
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        str(GIF_CRF),
        "-threads",
        "1",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        out_path,
    ]


def gif_static_argv(source: str, out_path: str) -> list[str]:
    """**单帧 GIF** 的静止变体 argv：先 `tpad` 克隆出 1 s，再按 `fps=12` 取样（§5.6 的邻接面）。

    为什么单帧要单独一条 argv（真机 F2 / F2-R / F2-S 的最终修法）：
    - 冻结 argv 里的 `fps=12` 按输出帧间隔的**中点**取帧，源帧只保持 0.04 s 时**整帧被跳过**
      ⇒ 产物是 262 字节、**0 个流**的空 mp4（`ffmpeg -frames:v 1` 造的静态 GIF 正是 0.04 s）；
    - 就算勉强产出 1 帧（早先的 `-r 1` 写法），0.083 s 的产物会被上游视频通道 **400 拒收**；
    - 而 1 s 但**只有 1 帧**的产物（`-r 1` 的真实结果）在原生通道上会回**空文本**、回退抽帧
      又抽不满（0/6）⇒ `FRAME_EXTRACT_FAILED`（t12 的 F2-S 实测）。
      `-r 12` 也救不了：输出帧率**不复制帧**，实测仍 1 帧 / 0.083 s。

    解法是**先把帧克隆出来**再交给 `fps`：`tpad=stop_mode=clone:stop_duration=1` 把唯一一帧
    延展到 1 s，`fps=12` 便能均匀取到 **12–13 帧**。真 ffmpeg 实测（320x240 单帧）：

    | 源 `delay_cs` | 产物 | 流 | `nb_read_frames` | `duration` |
    | --- | --- | --- | --- | --- |
    | 0（GCE-less） | 23,768 B | 1 | **13** | **1.0833 s** |
    | 4（0.04 s） | 23,735 B | 1 | **12** | **1.0000 s** |
    | 10（0.1 s） | 23,768 B | 1 | **13** | **1.0833 s** |

    与 `gif_argv` 的差别**只在滤镜**（多一个 `tpad` 前置）；其余（`-an`、偶数宽 `scale`、
    `yuv420p`、libx264 veryfast / crf 26、`-threads 1`、`+faststart`、`-f mp4`）逐字相同，
    也**不再**需要 `-r 1`。
    **多帧源仍然用 `gif_argv`**：`tpad` 是给"只有一帧"的源补帧的，对多帧源只会多造 1 s 尾巴
    （实测 4 帧 10 cs ⇒ 17 帧 / 1.4167 s），而且帧率取样会丢帧。
    """
    return [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-i",
        source,
        "-an",
        "-vf",
        "tpad=stop_mode=clone:stop_duration=1,"
        f"fps={GIF_MAX_FPS},"
        f"scale='trunc(min({GIF_MAX_WIDTH_PX},iw)/2)*2':-2:flags=lanczos",
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        str(GIF_CRF),
        "-threads",
        "1",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        out_path,
    ]


async def gif_frame_count(path: str) -> int:
    """源 GIF 的帧数（`nb_frames`）；读不出回 `0`（⇒ 不记 `gif_static`，不误判静态）。

    为什么要单独探一次：`fps=12` 的滤镜会重排帧数（M10：源 500 帧 ⇒ 产物 240 帧），
    产物帧数**推不出**"源是不是单帧动图"。一次 ffprobe 约 0.02 s，可忽略。
    """
    argv = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=nb_frames",
        "-of",
        "csv=p=0",
        path,
    ]
    try:
        returncode, out, _err = await _run(
            argv, timeout=PROBE_TIMEOUT_S, error_code=envelope.ERR_PROBE_FAILED
        )
    except envelope.ToolError:
        return 0
    if returncode != 0:
        return 0
    text = out.decode("utf-8", "replace").strip().splitlines()
    if not text:
        return 0
    try:
        return max(0, int(text[0].strip()))
    except (TypeError, ValueError):
        return 0


async def normalize_gif(
    source: str, *, workdir: str, max_media_bytes: int
) -> GifResult:
    """把 GIF 归一成 mp4（三道闸 + 产物校验，SPEC-v0.4 §5.7 顺序固定）。

    闸①字节（归一前，与 `classify` 同口径）→ 闸③超时（`GIF_CONVERT_TIMEOUT_S=10 s`，
    子进程 kill）→ 闸④产物（非空 + 不超 `max_media_bytes` + **时长可度量**）。
    闸②时长在**调用方**（`analyze` 的 `[4-pre]`，因为闸要卡在"归一之前"、用的是**源**的时长）。

    闸④ 的"时长"一侧有一个**唯一放行口**（真机 F2 裁决）：产物**有视频流但没有可度量时长**
    ⇒ 按 `0.0` 收（一行 LOG，不加 note）；产物缺失/0 字节/ffprobe 真坏仍 `GIF_CONVERT_FAILED`。

    **argv 按帧数分派**（真机 F2/F2-R/F2-S 的最终修法）：
    - `frames == 1` ⇒ `gif_static_argv`（在冻结滤镜前加 `tpad=stop_mode=clone:
      stop_duration=1`，把唯一一帧克隆成 1 s 再按 `fps=12` 取样）⇒ 1.0–1.083 s /
      12–13 帧的 mp4，覆盖**全部**单帧形态（含**没有 GCE** 的老式 GIF87a）。冻结 argv 的
      `fps=12` 会把 0.04 s 的唯一一帧**整帧丢掉**（262 字节 / 0 流）；早先试过的 `-r 1`
      只有 1 帧（上游回空文本、抽帧 0/6），`-r 12` 不复制帧，两者都被真机否决；
    - `frames > 1` ⇒ 冻结的 `gif_argv` **一字不动**（`tpad` 只对单帧使用，多帧走原路径）。
    产物时长报**真实值**（单帧 ≈1.0 s），`static=True` 由源帧数决定（`GifResult.static`）。

    产物落 `<workdir>/gif.mp4`：`workdir` 是 `analyze` 的 `/tmp/media_analysis_*`，
    `finally` 整目录删；源文件**只读**（属于平台/事件，绝不删、绝不写回）。
    半成品（失败/超时/空产物）在这里 `unlink`，不留残渣。

    **失败不静默**：`GIF_CONVERT_FAILED` 是终态码（走"投递一条失败消息"），
    绝不回退成"把原始 GIF 直接送给模型"——那正是 v0.3 `File .gif` 的缺陷形态。
    """
    source_size = _size_of(source) or 0
    if source_size <= 0:
        raise envelope.ToolError(
            envelope.ERR_GIF_CONVERT_FAILED, log_detail="source missing or empty"
        )
    if max_media_bytes > 0 and source_size > max_media_bytes:
        raise envelope.ToolError(
            envelope.ERR_MEDIA_TOO_LARGE,
            log_detail=f"gif size={source_size} max_media_bytes={max_media_bytes}",
        )

    frames = await gif_frame_count(source)
    out_path = os.path.join(workdir, "gif.mp4")
    # 单帧 ⇒ `tpad` 静止变体；多帧 ⇒ 冻结的 `gif_argv` 一字不动（SPEC-v0.4 §5.6）
    argv = (
        gif_static_argv(source, out_path)
        if frames == 1
        else gif_argv(source, out_path)
    )
    rc, err, timed_out = await _run_capture(argv, timeout=GIF_CONVERT_TIMEOUT_S)
    if timed_out:
        _unlink(out_path)
        raise envelope.ToolError(
            envelope.ERR_GIF_CONVERT_FAILED,
            log_detail=f"ffmpeg timeout {GIF_CONVERT_TIMEOUT_S}s",
        )
    if rc != 0:
        _unlink(out_path)
        raise envelope.ToolError(
            envelope.ERR_GIF_CONVERT_FAILED,
            log_detail=f"ffmpeg rc={rc} stderr={err.decode('utf-8', 'replace')[:160]!r}",
        )

    size = _gif_product_size(out_path, rc=rc, max_media_bytes=max_media_bytes)

    duration_s = await _gif_product_duration(out_path)
    if duration_s is None:
        # 产物**一个流都没有**：对单帧源静止变体不该发生（实测三档都有 1 流），
        # 对多帧源意味着"全部帧被取样丢掉" ⇒ 这是转换失败，不是"可以蒙混过去"。
        _unlink(out_path)
        raise envelope.ToolError(
            envelope.ERR_GIF_CONVERT_FAILED, log_detail="output has no video stream"
        )

    LOG.info(
        "media_analysis: gif→mp4 %d B → %d B（%.2f s / 源 %d 帧）",
        source_size,
        size,
        duration_s,
        frames,
    )
    return GifResult(
        path=out_path,
        size=size,
        source_size=source_size,
        duration_s=duration_s,
        frames=frames,
    )


def _gif_product_size(out_path: str, *, rc: int, max_media_bytes: int) -> int:
    """闸④ 前半：产物**非空**且不超 `max_media_bytes`（失败一律 `_unlink` 不留残渣）。"""
    size = _size_of(out_path) or 0
    if size <= 0:
        _unlink(out_path)
        raise envelope.ToolError(
            envelope.ERR_GIF_CONVERT_FAILED, log_detail=f"empty output (rc={rc})"
        )
    if max_media_bytes > 0 and size > max_media_bytes:
        _unlink(out_path)
        raise envelope.ToolError(
            envelope.ERR_MEDIA_TOO_LARGE,
            log_detail=f"gif output size={size} max_media_bytes={max_media_bytes}",
        )
    return size


async def _gif_product_duration(out_path: str) -> float | None:
    """闸④ 的时长一侧：`float` = 产物时长（可能 `0.0`）；`None` = 产物**一个流都没有**。

    三档（真机 F2 的分档就是这张表）：

    | 产物状态 | 返回 |
    | --- | --- |
    | 有时长 | 时长 |
    | **有视频流**但没有可度量时长（`format.duration=N/A`） | `0.0` + 一行 LOG（**唯一放行口**） |
    | **没有任何流**（`fps` 网格把唯一一帧吃掉，262 字节空 mp4） | `None`（调用方按 0 流失败处理，不重试） |
    | 其余（ffprobe 真坏 / 探不出流） | `GIF_CONVERT_FAILED` |

    `0.0` 是安全的：下游只把它用在压缩码率公式（产物小到不用压）与抽帧网格（单帧源只有
    一帧画面）；"没有可度量时长"只可能来自**极短**源，撞不上 `max_media_seconds` 这类上界闸。
    """
    try:
        return await probe_duration_s(out_path)
    except envelope.ToolError as exc:
        if exc.code != envelope.ERR_PROBE_FAILED or exc.log_detail != DURATION_UNAVAILABLE:
            _unlink(out_path)
            raise envelope.ToolError(
                envelope.ERR_GIF_CONVERT_FAILED,
                log_detail=f"output duration unavailable ({exc.log_detail})",
            ) from None

    try:
        _has_audio, has_video = await probe_streams(out_path)
    except envelope.ToolError as exc:
        _unlink(out_path)
        raise envelope.ToolError(
            envelope.ERR_GIF_CONVERT_FAILED,
            log_detail=(
                "output duration unavailable; stream probe failed "
                f"({exc.log_detail})"
            ),
        ) from None
    if not has_video:
        return None  # 连流都没有：调用方判 `GIF_CONVERT_FAILED`（这里不删产物）
    LOG.info(
        "media_analysis: gif→mp4 产物有视频流但没有可度量时长（极短帧间隔），按 0.0 处理",
    )
    return 0.0


def compress_target_kbps(budget_bytes: int, duration_s: float) -> int:
    """目标**视频**码率（SPEC-v0.3 §6.1 冻结公式）：`floor(budget*8/duration/1000) - 64`。

    减掉的 64 是音频轨的固定开销（AAC 64 kbps 单声道）。返回值可能 < 64 ⇒ 调用方判
    `video_compress_infeasible`（不硬转：那只会得到一段看不清的糊片）。
    """
    if budget_bytes <= 0 or duration_s <= 0:
        return 0
    total_kbps = int(budget_bytes * 8 / float(duration_s) / 1000)
    return total_kbps - COMPRESS_AUDIO_KBPS


def compress_argv(
    source: str, out_path: str, *, video_kbps: int, has_audio: bool
) -> list[str]:
    """§6.2 冻结的 ffmpeg argv（ABR 双向钳制 + 480p + AAC 64k 单声道）。

    源**没有音轨**时去掉 `-c:a …` 三参（否则 ffmpeg 会因为"没有音频输入"报错或产出
    纯视频却带空音轨）—— 调用方据此加 `video_compressed_noaudio` note。
    """
    argv = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-i",
        source,
        "-vf",
        f"scale=-2:{COMPRESS_HEIGHT_PX}:flags=bicubic",
        "-c:v",
        "libx264",
        "-preset",
        COMPRESS_VIDEO_PRESET,
        "-b:v",
        f"{int(video_kbps)}k",
        "-maxrate",
        f"{int(video_kbps)}k",
        "-bufsize",
        f"{int(video_kbps) * 2}k",
        "-pix_fmt",
        "yuv420p",
    ]
    if has_audio:
        argv += ["-c:a", "aac", "-b:a", f"{COMPRESS_AUDIO_KBPS}k", "-ac", "1"]
    argv += ["-movflags", "+faststart", out_path]
    return argv


async def compress_video(
    source_path: str,
    *,
    budget_bytes: int,
    duration_s: float,
    workdir: str,
    has_audio: bool,
    timeout_s: float,
) -> "CompressResult":
    """把视频压到 `budget_bytes` 以内（**最多 2 趟**，§6.3），失败只是一种"原因"，不抛。

    为什么返回原因而不是抛 `ToolError`：压缩失败**不是**终态 —— 回退链还有"抽帧"这一跳
    （§7.3-[5]）。把它抛成错误码就等于把"能看画面"的路径也弄丢了。

    三条硬约束：
    - 产物只落 `workdir`（调用方给的是 `/tmp/media_analysis_*`，`finally` 里整目录删）；
    - 源文件**只读**：绝不覆盖 `path`、绝不写回 `data/`（§6.4）；
    - 两次 pass 共享一个 `timeout_s` 预算（不是每次各一份）。
    """
    source_size = _size_of(source_path) or 0
    no_audio = not has_audio
    if source_size <= 0:
        return CompressResult(False, None, None, source_size, "ffmpeg_failed", 0, no_audio)
    if source_size <= budget_bytes:
        # 源已在预算内 ⇒ **0 次转码**（t8：8 MiB 档 SKIP，0 ms）
        return CompressResult(True, source_path, source_size, source_size, "ok", 0, no_audio)

    kbps = compress_target_kbps(budget_bytes, duration_s)
    if kbps < COMPRESS_MIN_VIDEO_KBPS:
        return CompressResult(False, None, None, source_size, "infeasible", 0, no_audio)

    deadline = time.monotonic() + max(1.0, float(timeout_s))
    out_path = os.path.join(workdir, f"video_{int(budget_bytes)}.mp4")
    passes = 0
    size: int | None = None
    reason = "ffmpeg_failed"
    while passes < COMPRESS_MAX_PASSES:
        passes += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0.2:
            return CompressResult(False, None, size, source_size, "timeout", passes, no_audio)
        argv = compress_argv(
            source_path, out_path, video_kbps=kbps, has_audio=has_audio
        )
        rc, err, timed_out = await _run_capture(argv, timeout=remaining)
        if timed_out:
            return CompressResult(False, None, size, source_size, "timeout", passes, no_audio)
        if rc != 0:
            LOG.warning(
                "media_analysis: 视频压缩失败（rc=%s, %s）",
                rc,
                err.decode("utf-8", "replace")[:160],
            )
            return CompressResult(
                False, None, size, source_size, "ffmpeg_failed", passes, no_audio
            )
        size = _size_of(out_path)
        if not size:
            return CompressResult(False, None, None, source_size, "empty", passes, no_audio)
        if size <= budget_bytes:
            reason = "ok"
            break
        if passes >= COMPRESS_MAX_PASSES:
            # 两趟仍超预算 ⇒ 判失败（不第 3 次：收益趋零、时间锯齿状上涨）
            return CompressResult(
                False, None, size, source_size, "budget_exceeded", passes, no_audio
            )
        # 第二趟：按实测体积等比修正（t8：285k ⇒ 2,098,819 B ⇒ 269k ⇒ 命中）
        kbps = max(
            COMPRESS_MIN_VIDEO_KBPS,
            int(kbps * budget_bytes / size * COMPRESS_SECOND_PASS_FACTOR),
        )
    if reason != "ok" or size is None:
        return CompressResult(False, None, size, source_size, reason, passes, no_audio)
    if size >= source_size:
        return CompressResult(False, None, size, source_size, "not_smaller", passes, no_audio)
    LOG.info(
        "media_analysis: 视频压缩 %d B → %d B（预算 %d B，%d 趟）",
        source_size,
        size,
        budget_bytes,
        passes,
    )
    return CompressResult(True, out_path, size, source_size, "ok", passes, no_audio)


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


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
async def _audio_stream_info(path: str) -> dict[str, str] | None:
    """ffprobe 第一条音轨的 `{codec_name, sample_rate, channels}`；读不出回 `None`。"""
    argv = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_name,sample_rate,channels",
        "-of",
        "json",
        path,
    ]
    try:
        returncode, out, _err = await _run(
            argv, timeout=PROBE_TIMEOUT_S, error_code=envelope.ERR_PROBE_FAILED
        )
    except envelope.ToolError:
        return None
    if returncode != 0:
        return None
    try:
        payload = json.loads(out.decode("utf-8", "replace") or "{}")
    except ValueError:
        return None
    streams = payload.get("streams") or []
    if not streams or not isinstance(streams[0], dict):
        return None
    return {str(key): str(value) for key, value in streams[0].items()}


def _is_provider_wav(info: dict[str, str] | None) -> bool:
    """是否已经是「16 kHz / 单声道 / pcm_s16le」（是就不重编码）。"""
    if not info:
        return False
    return (
        info.get("codec_name") == PROVIDER_WAV_CODEC
        and info.get("sample_rate") == PROVIDER_WAV_SAMPLE_RATE
        and info.get("channels") == PROVIDER_WAV_CHANNELS
    )


async def _ffmpeg_to_wav(source: str, out_wav: str) -> str:
    """`ffmpeg -vn -ac 1 -ar 16000 -c:a pcm_s16le`；失败/空产物 ⇒ `AUDIO_EXTRACT_FAILED`。"""
    argv = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-i",
        source,
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


def read_bytes(path: str) -> bytes:
    """一次读入整个文件（**已过 stat 闸**之后才允许调；`video_native` 在 base64 后立刻释放）。

    单独成函数的两条理由：① 让"有没有先读再判"变成可测的形状（monkeypatch 它即可
    断言"超标时根本没读"）；② 同步实现交给 `asyncio.to_thread`，不阻塞事件循环。
    """
    with open(path, "rb") as handle:
        return handle.read()


async def _run_capture(argv: list[str], *, timeout: float) -> tuple[int, bytes, bool]:
    """跑一次 argv 并返回 `(rc, stderr, timed_out)`；**不抛**（压缩要自己决定怎么降级）。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError):
        return -1, b"spawn failed", False
    try:
        _out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        with contextlib.suppress(Exception):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return -1, b"timeout", True
    return (proc.returncode if proc.returncode is not None else -1), err, False


def _size_of(path: str) -> int | None:
    try:
        return os.path.getsize(path)
    except OSError:
        return None


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


def _unlink(path: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(path)


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
