"""主链路编排（SPEC-v0.2 §9 + SPEC-v0.3 §7 冻结顺序）。

v0.2 → v0.3 的结构性变化（一句话：**视频主路径改成"原生视频直送"，抽帧降级为回退**）：

1. **视频原生直送**：`provider.client.chat.completions.create(messages=[…video_url…])`
   （核心 `text_chat` 放不进 video 块，`openai_source.py:1424` 硬抛 `ValueError`）；
   形状与三条护栏在 `video_native`，这里只管编排。
2. **压缩在直送之前**：超 `video.max_direct_bytes` 先用 ffmpeg 压到预算（最多 2 趟），
   压不动才轮到抽帧 —— 顺序**不许反**（§6.5：压缩 4.8 s / 0 token，抽帧是 6 次视觉调用）。
3. **抽帧只是回退**：原生不可用 / 超门槛且压不动 / 调用失败时，才按 `include_frames`
   三态决定要不要抽帧（`vision_provider_id` 是**仅回退**用的图片模型位）。
4. **`include_frames` 变三态字符串** `auto|true|false`（旧布尔值仍接受）：
   `false` + 原生失败 ⇒ **绝不静默抽帧**（那是偷偷花两份钱）。
5. **L1 可见性探测**（首次，缓存 7 天）+ **100 s 预算的 `remaining` 递减**。

v0.4 只加一段（SPEC-v0.4 §5.12）：`[3.5]` 魔数二次确认 + `[4-pre]` **GIF 归一**——
GIF（`Image` 或群文件）先用 `ffmpeg` 归一成 mp4，再走**完全不变**的视频七跳；
`kind` 仍是 `"video"`，走 GIF 分支时**跳过 `classify`**（那一步会把 `Image` 判成
`kind=audio`），缓存 digest 取**源 GIF 字节**。后台异步不在这层（`background.py`）。

三个阶段的降级形状仍然固定：
- 用户主诉求（转写 / 看画面）无法满足 ⇒ `ToolError`（错误码见 `envelope`，v0.4 起 23 条）；
- 附加项失败（某帧描述、摘要、压缩、探测、GIF 归一之外的旁支）⇒ 成功信封 + `notes` 一条
  （v0.4 起 43 个词 / 44 条形态）。

本模块**自己建/删工作目录**（`tempfile.mkdtemp(prefix="media_analysis_")`，落在 `/tmp`），
调用方不需要关心临时目录；中间产物（wav / 抽帧 jpg / 压缩产物 / 群文件下载物）用完即删，
不落库、不落工作区、不落生产 `data/`。
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time

from astrbot.api import logger as LOG

# 别名：冻结签名里 `settings` / `cache` 是**形参名**，模块名直接 import 会被遮住。
from . import cache as cache_mod
from . import envelope, media, probe, providers, video_native
from . import settings as settings_mod

# ---- 预算（SPEC-v0.2 §2.2 / SPEC-v0.3 §5.2）----
DEADLINE_S = 100.0  # 全局预算；框架 tool_call_timeout=120 s，留 20 s 余量
PROBE_TIMEOUT_S = 10.0  # 单次 ffprobe（实测 0.019 s，只防病态文件）
AUDIO_TIMEOUT_S = 60.0  # 单次多模态调用（含上传：60 s 音频 2.44 MiB base64）
FRAME_EXTRACT_TIMEOUT_S = 15.0  # 单帧 ffmpeg（实测 6 帧合计 0.206 s）
FRAME_CALL_TIMEOUT_S = 15.0  # 单帧 text_chat
SUMMARY_TIMEOUT_S = 15.0  # 摘要 text_chat；剩余预算少于它就不做摘要
FRAME_WIDTH_PX = 768  # 与 media.FRAME_WIDTH_PX 同值（SPEC.md §3.2 冻结常量）
FOCUS_MAX_CHARS = 200
WORKDIR_PREFIX = "media_analysis_"  # 中间产物只落 /tmp（SPEC.md §9-4）

# ---- v0.3 预算护栏（§5.2 推论 2/3、§7.2 护栏③）----
FRAMES_MIN_BUDGET_S = 25.0  # 抽帧这一跳最少要多少秒才值得起（推论 3）
#: 原生调用之后必须留出的余量。**评审 F6**：原来固定 15 s < `FRAMES_MIN_BUDGET_S`(25)，
#: 于是"原生超时 ⇒ 抽帧几乎必然 `video_skip_budget`"—— 主打回退在最该生效时失效。
#: 现在改成"抽帧最小预算 + 余量"，原生阶段最多吃到 `remaining - 35`。
NATIVE_RESERVE_MARGIN_S = 10.0
NATIVE_RESERVE_S = FRAMES_MIN_BUDGET_S + NATIVE_RESERVE_MARGIN_S  # 35 s
PROBE_BUDGET_RESERVE_S = 30.0  # 跑 L1 还要留的量（`remaining < L1_TIMEOUT + 30` ⇒ 不跑）
AUDIO_MIN_ATTEMPT_S = 5.0  # 给音轨至少留这么多；不足就别扣预留（后续本来也做不成）

# ---- `include_frames` 三态（SPEC-v0.3 §7.1）----
MODE_AUTO = "auto"
MODE_TRUE = "true"
MODE_FALSE = "false"
MODES: frozenset[str] = frozenset({MODE_AUTO, MODE_TRUE, MODE_FALSE})

_TRUE_LITERALS = frozenset({"1", "true", "yes", "on"})
_FALSE_LITERALS = frozenset({"", "0", "false", "no", "off"})

# ---- 原生通道的四种状态（只用于"没有回退时该报哪个码"，§7.4）----
NATIVE_OK = "ok"
NATIVE_UNAVAILABLE = "unavailable"  # 键空 / 非 openai 适配器 / 无 client / L1 未过 / 预算不足
NATIVE_FAILED = "failed"  # 调用失败或超时
NATIVE_EMPTY = "empty"  # 200 但空文本（或哨兵）
NATIVE_TOO_LARGE = "too_large"  # 门槛超限且压缩没成功/被关

# ---- 提示词的**固定片段**（模板本身可配置，见 settings.DEFAULT_PROMPTS）----
FRAME_FOCUS_CLAUSE = (
    "另外，用户关心的是「{focus}」：如果画面上有与之相关的信息，请优先说这部分。"
)
SUMMARY_TASK_SUMMARY = (
    "请用不超过三句中文概括它的主要内容。如果材料里没有可用的内容，只回答：没有可用的内容。"
)
SUMMARY_TASK_FOCUS = (
    "请只用不超过三句中文回答这个问题：「{focus}」。材料里没有相关信息就直说没有。"
)
SUMMARY_MATERIAL_PREFIX = "\n\n材料：\n"
NO_SPEECH_SENTINEL = "[无语音]"
NO_CONTENT_SENTINEL = "[无可用内容]"
EMPTY_SENTINELS: frozenset[str] = frozenset({NO_SPEECH_SENTINEL, NO_CONTENT_SENTINEL})

_KIND_CN = {"audio": "语音", "video": "视频"}
# 提示词在 `settings.prompts` 5 元组里的下标（顺序冻结：audio, video, frame, summary,
# video_native —— 前四位沿用 v0.2，新键在末位）
_PROMPT_INDEX = {
    "audio": 0,
    "video": 1,
    "frame": 2,
    "summary": 3,
    "video_native": 4,
}

# 终态码映射（§7.4）：**没有可用回退**时，按"为什么没直送"给码
_TERMINAL_CODE = {
    NATIVE_UNAVAILABLE: envelope.ERR_VIDEO_UNAVAILABLE,
    NATIVE_FAILED: envelope.ERR_VIDEO_FAILED,
    NATIVE_EMPTY: envelope.ERR_VIDEO_FAILED,
    NATIVE_TOO_LARGE: envelope.ERR_VIDEO_TOO_LARGE,
}

_PROBE_STORE: probe.ProbeStore | None = None


def is_valid_mode_value(value: object) -> bool:
    """这个值本身是否合法（决定工具门面要不要打一条 WARNING）。

    与 `normalize_mode` 分开的两个理由：① 归一函数保持**纯函数**（多次调用不产生日志噪声）；
    ② 告警是**门面**的职责（唯一的生产入口是 `main._as_mode`），不该埋在链路深处。
    """
    if isinstance(value, bool) or value is None:
        return True
    if isinstance(value, str):
        text = value.strip().lower()
        return text in MODES or text in _TRUE_LITERALS or text in _FALSE_LITERALS
    return False


def frames_possible(
    mode: str, settings: settings_mod.Settings, *, has_video: bool
) -> bool:
    """本轮**是否可能要抽帧**（决定音轨要不要为画面留预算、超时后能否降级）。

    与 `_frames_fallback` 的三道门**逐条对齐**（顺序也一致：`has_video` → `mode=false` →
    视觉位 → `auto` 且无视频模型）—— 但只回答"要不要"，不产生 note、不判终态码。
    单独抽出来的理由（评审 R1）：这条判据现在有**两个**消费方（音轨预留 + 音轨超时降级），
    再复制一份就是又一处"两份逻辑必然分叉"。
    """
    if not has_video:
        return False
    if mode == MODE_FALSE:
        return False
    if not settings.vision_provider_id:
        return False
    if mode == MODE_AUTO and not settings.video_provider_id:
        return False
    return True


def budgeted_timeout(
    remaining: float, cap: float, reserve_s: float, *, floor_s: float = AUDIO_MIN_ATTEMPT_S
) -> float:
    """在 `remaining` 里给这一步圈一段时间，同时尽量给后面留 `reserve_s`（评审 F6）。

    规则（三条，按顺序）：
    1. `remaining <= 0` ⇒ `0`（预算已尽）；
    2. `remaining - reserve_s >= floor_s` ⇒ 扣掉预留（`min(cap, remaining - reserve_s)`）；
    3. 否则不扣（`min(cap, remaining)`）—— 反正后面那跳也做不成了，别把这一步也饿死。
    """
    if remaining <= 0:
        return 0.0
    leftover = remaining - max(0.0, float(reserve_s))
    if leftover >= float(floor_s):
        return max(0.0, min(float(cap), leftover))
    return max(0.0, min(float(cap), float(remaining)))


def normalize_mode(value: object) -> str:
    """把模型/调用方给的 `include_frames` 归一成 `"auto" | "true" | "false"`（§13.1）。

    | 输入 | 输出 |
    | --- | --- |
    | `bool` | `true` / `false`（旧客户端兼容） |
    | `"auto"/"true"/"false"`（去空白、大小写不敏感） | 原样 |
    | `"1"/"yes"/"on"` | `true` |
    | `"0"/"no"/"off"/""` | `false` |
    | `None` | `auto` |
    | 其它（`2.5`、`{}`、未知字符串…） | **`auto`** + 一条 WARNING |

    为什么非法值默认 `auto` 而**不再**默认 `false`（与 v0.2 相反）：v0.3 的主路径本身
    就是"看视频"，误发值按 `auto` 走只多一次原生调用；按 `false` 会把它悄悄降级成
    "只转写"，而用户以为画面也被看了。

    **纯函数、不告警**：告警在门面（`main._as_mode`，配 `is_valid_mode_value` 判定）——
    这样本函数可以在测试里被调 20 次而不制造日志噪声。
    """
    if isinstance(value, bool):  # 注意：bool 是 int 的子类，必须先判
        return MODE_TRUE if value else MODE_FALSE
    if isinstance(value, str):
        text = value.strip().lower()
        if text in MODES:
            return text
        if text in _TRUE_LITERALS:
            return MODE_TRUE
        if text in _FALSE_LITERALS:
            return MODE_FALSE
        return MODE_AUTO
    if value is None:
        return MODE_AUTO
    return MODE_AUTO


async def analyze(
    *,
    context,
    event,
    target: str,
    focus: str,
    include_frames: object,
    settings: settings_mod.Settings,
    cache: cache_mod.DiskCache,
) -> dict[str, object]:
    """跑完整条链路，返回**成功信封**（9 键）；受控失败抛 `envelope.ToolError`。

    步骤号与 SPEC-v0.3 §7.3 的七跳一致（[0]–[7]），便于对着规格评审。
    """
    started = time.monotonic()  # [0]
    deadline = started + DEADLINE_S
    notes: list[str] = []
    mode = normalize_mode(include_frames)
    workdir = tempfile.mkdtemp(prefix=WORKDIR_PREFIX)
    try:
        # 1 —— focus 去空白 + 截断；配置里的上限已钳位
        focus_text, trimmed = _prepare_focus(focus)
        if trimmed:
            notes.append("focus_trimmed")
        max_frames = max(1, int(settings.max_frames))
        max_seconds = int(settings.max_media_seconds)
        max_bytes = int(settings.max_media_bytes)

        # 2 —— 定位媒体组件（Record → Video → Image(GIF) → File）
        #     v0.4：**协程**（`Image` 的 GIF 判定要读 6 字节魔数）；普通图片在这里就被拒
        handle = await media.find_media(event, target)
        if handle.from_file:
            notes.append("source:file")

        # 3 —— 拿到本地路径（File 走 §3.2 决策树；有界下载落本工作目录）
        download_needed = media.file_download_needed(handle)
        path = await media.resolve_media_path(
            handle, workdir=workdir, max_media_bytes=max_bytes
        )
        if download_needed:
            notes.append("file_downloaded")

        # 3.5 —— GIF 魔数**二次确认**（SPEC-v0.4 §5.3/§5.12）：File 在此转正或降回，
        #        Image 非 GIF 直接 MEDIA_NOT_FOUND（远程 `.gif` 那条路的收口点）
        #        v0.4.1：`File` **一律**验魔数（后缀只是候选线索）⇒ 无后缀 GIF 也转正；
        #        非 GIF 的图片（PNG/APNG/WebP/JPEG）显式拒绝，不再靠"ffprobe 碰巧没时长"。
        digest_source = path  # 缓存 digest 取【源】字节（GIF 归一前后都不变）
        if handle.from_file:
            handle = media.confirm_gif(handle, path)
            if not handle.gif:
                image_kind = media.image_content_kind(path)
                if image_kind is not None:
                    raise envelope.ToolError(
                        envelope.ERR_UNSUPPORTED_MEDIA_TYPE,
                        log_detail=(
                            f"file content is {image_kind}; images are not supported"
                        ),
                    )
        elif handle.gif:
            handle = media.confirm_gif(handle, path)

        # 4-pre —— GIF 分支（SPEC-v0.4 §5.12）：时长闸（**源**）→ 归一 mp4 → 产物当视频用
        #         ⚠ 必须跳过 `classify`：它会把 `Image` 判成 `kind=audio`（§1.2-M7 实测）
        if handle.gif:
            notes.append("source:gif")
            # 用 GIF 专用入口：无 GCE 的单帧 GIF 没有 `format.duration`（真 ffprobe 实测
            # `N/A`）⇒ 0.0（闸②是上界闸，未知即不可能超限）；真正的探测故障仍 PROBE_FAILED
            source_duration_s = await media.probe_gif_duration_s(path)
            if source_duration_s > max_seconds:
                raise envelope.ToolError(
                    envelope.ERR_MEDIA_TOO_LONG,
                    log_detail=(
                        f"gif duration_s={source_duration_s} "
                        f"max_media_seconds={max_seconds}"
                    ),
                )
            gif = await media.normalize_gif(
                path, workdir=workdir, max_media_bytes=max_bytes
            )
            notes.append(f"gif_converted:{gif.source_size}->{gif.size}")
            if gif.static:
                notes.append("gif_static")
            path = gif.path
            kind, duration_s, has_audio, has_video = "video", gif.duration_s, False, True
        else:
            # 4 —— 判型 + 时长 + 流（File 走三段梯子；Record/Video 的时长失败仍 PROBE_FAILED）
            kind, duration_s, has_audio, has_video = await media.classify(
                handle, path, max_media_bytes=max_bytes, workdir=workdir
            )
            if duration_s > max_seconds:
                raise envelope.ToolError(
                    envelope.ERR_MEDIA_TOO_LONG,
                    log_detail=f"duration_s={duration_s} max_media_seconds={max_seconds}",
                )

        # 5 —— 内容哈希 + 缓存键（v3：12 分量，含 mode 与 video_fp）
        #     GIF 用**源**字节（不是产物 mp4）⇒ 同一张 GIF 以 Image / File 两种形态到达时
        #     字节相同 ⇒ 同一个键 ⇒ 共享缓存（SPEC-v0.4 §5.11）
        digest = await media.content_digest(digest_source)
        key = cache_mod.build_key(
            digest=digest,
            kind=kind,
            mode=mode,
            max_frames=max_frames,
            focus=focus_text,
            prompts_fp=settings.prompts_fp,
            audio_provider_id=settings.audio_provider_id,
            video_provider_id=settings.video_provider_id,
            vision_provider_id=settings.vision_provider_id,
            video_fp=settings.video_fp,
            plugin_version=settings_mod.PLUGIN_VERSION,
        )

        # 6 —— 同键串行化 + 查缓存（命中即回放，0 token）
        async with cache.singleflight(key):
            hit = await cache.get(key)
            if hit is not None:
                return _replay(hit, started)

            if kind == "video":
                transcript, frames, summary, frames_degraded = await _video_stage(
                    context=context,
                    event=event,
                    path=path,
                    duration_s=duration_s,
                    has_audio=has_audio,
                    has_video=has_video,
                    mode=mode,
                    focus=focus_text,
                    max_frames=max_frames,
                    deadline=deadline,
                    notes=notes,
                    settings=settings,
                    workdir=workdir,
                )
            else:
                transcript, frames, summary, frames_degraded = await _audio_stage(
                    context=context,
                    event=event,
                    path=path,
                    mode=mode,
                    focus=focus_text,
                    deadline=deadline,
                    notes=notes,
                    settings=settings,
                    workdir=workdir,
                )

            # 7 —— 信封 + 入缓存 + 结构化日志（只打结构/计数/耗时）
            if (
                kind == "video"
                and not transcript
                and not frames
                and not summary
                and not frames_degraded
            ):
                # 诚实兜底：**想要画面却没拿到**且连转写都空 ⇒ 别给模型一个空成功。
                # 评审 F4：`mode=false` / `auto`（无视频模型、无视觉位）／预算截断这些
                # "按设计降级"的情形**不走这里** —— 它们该是 `ok:true` + `audio_empty`
                # （有音轨但没转出内容属正常降级），拿 `FRAME_EXTRACT_FAILED` 描述"没有
                # 画面诉求"既错码又误导用户去换媒体。
                raise envelope.ToolError(
                    envelope.ERR_FRAME_EXTRACT_FAILED,
                    log_detail=f"mode={mode} 但 transcript 与 frames 都为空",
                )
            payload = envelope.ok_envelope(
                kind=kind,
                duration_s=duration_s,
                transcript=transcript,
                frames=frames,
                summary=summary,
                notes=notes,
                elapsed_ms=_elapsed_ms(started),
                cached=False,
            )
            await cache.put(key, payload)
            LOG.info(
                "media_analysis: ok kind=%s mode=%s duration_s=%.2f frames=%d "
                "transcript_chars=%d notes=%d elapsed_ms=%d",
                kind,
                mode,
                duration_s,
                len(frames),
                len(transcript[0]["text"]) if transcript else 0,
                len(notes),
                payload["elapsed_ms"],
            )
            return payload
    finally:
        # 8 —— 中间产物（含压缩产物）只落 /tmp，用完即删
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 语音消息：v0.2 链路原样（转写 → 摘要）
# --------------------------------------------------------------------------- #
async def _audio_stage(
    *,
    context,
    event,
    path: str,
    mode: str,
    focus: str,
    deadline: float,
    notes: list[str],
    settings: settings_mod.Settings,
    workdir: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]], str]:
    """纯语音消息：只做音轨转写 + 摘要（v0.2 语义不变；`mode` 只多一条 note）。"""
    if mode != MODE_FALSE:
        notes.append("frames_skipped_audio_only")
    transcript = await _transcribe_path(
        context,
        event,
        path=path,
        kind="audio",
        focus=focus,
        deadline=deadline,
        notes=notes,
        settings=settings,
        workdir=workdir,
    )
    summary = await _summary_stage(
        context,
        event,
        transcript=transcript,
        frames=[],
        focus=focus,
        kind="audio",
        deadline=deadline,
        notes=notes,
        settings=settings,
    )
    return transcript, [], summary, True  # 语音消息没有"画面诉求"⇒ 永远算按设计降级


# --------------------------------------------------------------------------- #
# 视频：原生 → 压缩 → 抽帧（SPEC-v0.3 §7.3 七跳）
# --------------------------------------------------------------------------- #
async def _video_stage(
    *,
    context,
    event,
    path: str,
    duration_s: float,
    has_audio: bool,
    has_video: bool,
    mode: str,
    focus: str,
    max_frames: int,
    deadline: float,
    notes: list[str],
    settings: settings_mod.Settings,
    workdir: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]], str]:
    """视频七跳：[1] 视频位 → [2] L1 → [3] 直送闸/压缩 → [4] 原生 → [5] 回退 → [6] 摘要。

    返回 `(transcript, frames, summary)`：原生成功时前两项是 `[]`、`summary` 是模型整段回答
    （`transcript=[]` 是**显式取舍**：不做"原生 + 音轨"双跑，§16.2-7）。
    """
    # [1]-[4] 原生通道 + 直送闸 + 原生调用
    native_state = NATIVE_UNAVAILABLE
    if has_video:
        provider = await _native_channel(
            context, settings, deadline, notes, workdir=workdir
        )
        if provider is not None:
            text, native_state = await _native_attempt(
                provider,
                path=path,
                duration_s=duration_s,
                has_audio=has_audio,
                focus=focus,
                settings=settings,
                deadline=deadline,
                notes=notes,
                workdir=workdir,
            )
            if native_state == NATIVE_OK:
                return [], [], text, False
    elif mode == MODE_TRUE:
        # 没有视频流却点名要画面 ⇒ v0.2 的判据逐字沿用
        raise envelope.ToolError(
            envelope.ERR_NO_VIDEO_TRACK,
            log_detail=f"duration_s={duration_s} include_frames=true",
        )

    # [5] 回退：音轨（v0.2 链路）+ 按三态决定是否抽帧
    transcript: list[dict[str, object]] = []
    wants_frames = frames_possible(mode, settings, has_video=has_video)
    if has_audio:
        transcript = await _transcribe_path(
            context,
            event,
            path=path,
            kind="video",
            focus=focus,
            deadline=deadline,
            notes=notes,
            settings=settings,
            workdir=workdir,
            # 评审 R1：要画面 ⇒ 音轨给抽帧留 25 s 预留，且超时只降级不判死
            reserve_s=FRAMES_MIN_BUDGET_S if wants_frames else 0.0,
            soft_timeout=wants_frames,
        )
    else:
        notes.append("no_audio_track")

    frames, frames_degraded = await _frames_fallback(
        context,
        event,
        path=path,
        duration_s=duration_s,
        has_audio=has_audio,
        has_video=has_video,
        mode=mode,
        focus=focus,
        max_frames=max_frames,
        native_state=native_state,
        deadline=deadline,
        notes=notes,
        settings=settings,
        workdir=workdir,
    )

    # [6] 摘要（v0.2 逻辑；原生成功时上面已 return）
    summary = await _summary_stage(
        context,
        event,
        transcript=transcript,
        frames=frames,
        focus=focus,
        kind="video",
        deadline=deadline,
        notes=notes,
        settings=settings,
    )
    return transcript, frames, summary, frames_degraded


async def _frames_fallback(
    context,
    event,
    *,
    path: str,
    duration_s: float,
    has_audio: bool,
    has_video: bool,
    mode: str,
    focus: str,
    max_frames: int,
    native_state: str,
    deadline: float,
    notes: list[str],
    settings: settings_mod.Settings,
    workdir: str,
) -> tuple[list[dict[str, object]], bool]:
    """抽帧回退（§7.3-[5]）。**`mode=false` 任何组合都不许走到这里抽帧**。

    返回 `(frames, degraded)`：`degraded=True` ＝这次"没有画面"是**按设计降级**，
    不是"想要却没拿到"（评审 F4：后者才配 `FRAME_EXTRACT_FAILED`，前者必须 `ok:true`）。

    五种不抽帧的情形要分清（每一种都只降级，不判死整次调用）：
    - `mode=false`：用户的显式成本约束 ⇒ note `video_frames_forbidden`；
    - **`mode=auto` 且 `video_provider_id` 空**（评审 F7）：没有视频模型＝这条链路根本
      没有"原生优先"可谈，再拿 6 次视觉调用去补，等于把 v0.2 的 0 次变成 6 次
      ⇒ note `video_unavailable_no_provider`，退化为"只转写"。**`mode=true` 不受此限**；
    - `mode=auto` 且 `vision_provider_id` 空：不猜、不回落核心对话 provider
      （§7.1 表格）⇒ note `video_no_vision_provider`；`mode=true` 同情形 ⇒ `VISION_UNAVAILABLE`；
    - 容器里没有视频流 ⇒ 抽帧无从谈起（`mode=true` 已在 `_video_stage` 判 `NO_VIDEO_TRACK`）；
    - 剩余预算 < 25 s（或抽帧层 truncate）⇒ note `video_skip_budget`：`auto/false` 按设计
      降级，只有 `mode=true` 才算"想要却没拿到"（评审 F6 的第二半）。

    其余情形（没有音轨）按 §7.4 给终态码。
    """
    if mode == MODE_FALSE:
        notes.append("video_frames_forbidden")
        return _terminal_or_empty(native_state, has_audio, "mode=false"), True

    if not settings.vision_provider_id:
        # 先看视觉位：它空着时"配一个视觉模型"才是眼前该做的事（v0.2 语义不变）
        if mode == MODE_TRUE:
            raise envelope.ToolError(
                envelope.ERR_VISION_UNAVAILABLE,
                log_detail="mode=true 但 vision_provider_id 为空",
            )
        notes.append("video_no_vision_provider")
        return _terminal_or_empty(native_state, has_audio, "mode=auto no vision id"), True

    if mode == MODE_AUTO and not settings.video_provider_id:
        # 评审 F7：视觉位配了、但**视频模型没配**时，`auto` 不许拿 6 次视觉调用去补
        # （那是把 v0.2 的 0 次调用变成 6 次的成本回归）⇒ 退化为"只转写"。
        # `mode=true` 是用户点名要画面，不受此限（下一条分支继续走抽帧）。
        notes.append("video_unavailable_no_provider")
        return _terminal_or_empty(native_state, has_audio, "mode=auto no video provider"), True

    if not has_video:
        return _terminal_or_empty(native_state, has_audio, "no video stream"), True

    if deadline - time.monotonic() < FRAMES_MIN_BUDGET_S:
        notes.append("video_skip_budget")
        return (
            _terminal_or_empty(native_state, has_audio, "frames budget"),
            mode != MODE_TRUE,
        )

    vision = await providers.resolve_vision_provider(
        context, settings.vision_provider_id, _umo(event)
    )
    extracted, truncated = await _extract_frames_budgeted(
        path, max_frames, duration_s, workdir, deadline, notes
    )
    if truncated:
        # 预算不足：抽帧降级为 0 帧，**不**判死整次调用（手上的转写照常交付）；
        # `mode=true` 属"想要却没拿到"（交给上层诚实兜底），`auto/false` 属按设计降级
        LOG.info(
            "media_analysis: 抽帧因预算不足被截断（0/%d），继续走摘要", max_frames
        )
        return [], mode != MODE_TRUE
    if not extracted:
        raise envelope.ToolError(
            envelope.ERR_FRAME_EXTRACT_FAILED,
            log_detail=f"0/{max_frames} frames",
        )
    notes.append("video_fallback_frames")
    frames = await describe_frames(
        vision,
        extracted,
        focus=focus,
        deadline=deadline,
        notes=notes,
        settings=settings,
    )
    notes.append(f"vision_provider:{providers.provider_id_of(vision)}")
    return frames, False


def _terminal_or_empty(
    native_state: str, has_audio: bool, why: str
) -> list[dict[str, object]]:
    """没抽帧时：有音轨 ⇒ 空帧列表（只交转写）；无音轨 ⇒ 按 `native_state` 给终态码。"""
    if has_audio:
        return []
    code = _TERMINAL_CODE.get(native_state, envelope.ERR_VIDEO_UNAVAILABLE)
    raise envelope.ToolError(code, log_detail=f"no audio track; {why}")


async def _native_channel(
    context,
    settings: settings_mod.Settings,
    deadline: float,
    notes: list[str],
    *,
    workdir: str,
) -> object | None:
    """§7.3-[1]/[2]：解析视频位 + （首次）跑 L1 可见性探测。

    返回可用的 provider，或 `None`（没有原生通道）。**不抛**"没有通道"这类软失败
    —— 那是回退链的入口，不是错误；只有"配置写错"（id 不存在 / 不是 Provider）才抛。
    """
    if settings.video_provider_id:
        notes.append("video_asserted")
    provider, _reason = await providers.resolve_video_provider(
        context, settings.video_provider_id
    )
    if provider is None:
        notes.append("video_native_unavailable")
        return None
    notes.append(f"video_provider:{providers.provider_id_of(provider)}")

    ttl = int(settings.video_probe_ttl_seconds)
    if not settings.video_probe_enabled or ttl <= 0:
        # 运营方的逃生门：信任"显式声明"直接直送（§3.4；不推荐长期关，§16.1-R5）
        notes.append("video_probe_disabled")
        return provider

    if deadline - time.monotonic() < probe.L1_TIMEOUT_S + PROBE_BUDGET_RESERVE_S:
        notes.append("video_skip_budget")
        return None

    outcome = await probe.ensure_l1(
        provider,
        provider_id=providers.provider_id_of(provider),
        model=video_native.model_of(provider),
        plugin_version=settings_mod.PLUGIN_VERSION,
        ttl_seconds=ttl,
        workdir=workdir,
        store=_probe_store(),
    )
    # 注记口径（§11.2 的"本次实跑"限定 + §2.2-⑥ 的缓存未通过）：
    #   缓存命中且通过   ⇒ 只有 video_probe_cached（没有实跑，就不该说"本次 L1 通过"）
    #   缓存命中且未通过 ⇒ video_probe_cached + video_probe_l1_failed（§2.2-⑥ 明写这一条）
    #   实跑            ⇒ video_probe_l1_ok / video_probe_l1_failed
    if outcome.source == "cache":
        notes.append("video_probe_cached")
        if not outcome.ok:
            notes.append("video_probe_l1_failed")
    else:
        notes.append("video_probe_l1_ok" if outcome.ok else "video_probe_l1_failed")
    if not outcome.ok:
        LOG.info("media_analysis: L1 未通过（%s），本次走回退链", outcome.reason)
        return None
    return provider


async def _native_attempt(
    provider,
    *,
    path: str,
    duration_s: float,
    has_audio: bool,
    focus: str,
    settings: settings_mod.Settings,
    deadline: float,
    notes: list[str],
    workdir: str,
) -> tuple[str, str]:
    """§7.3-[3]/[4]：直送闸（`stat` 先判）→ 压缩 → 原生调用。返回 `(text, state)`。"""
    size = media._size_of(path) or 0
    direct_limit = min(
        int(settings.video_max_direct_bytes), video_native.HARD_MAX_DIRECT_BYTES
    )
    send_path = path
    if size > direct_limit:
        remaining = deadline - time.monotonic()
        min_actionable = (
            float(settings.video_compress_timeout_seconds)
            + float(settings.video_native_timeout_seconds) * 0.5
        )
        if remaining < min_actionable:
            notes.append("video_skip_budget")
            return "", NATIVE_FAILED
        if not settings.video_compress_enabled:
            notes.append("video_compress_skipped")
            return "", NATIVE_TOO_LARGE
        result = await media.compress_video(
            path,
            budget_bytes=settings.compress_budget_bytes,
            duration_s=duration_s,
            workdir=workdir,
            has_audio=has_audio,
            timeout_s=float(settings.video_compress_timeout_seconds),
        )
        if not result.ok:
            notes.append(
                "video_compress_infeasible"
                if result.reason == "infeasible"
                else "video_compress_failed"
            )
            LOG.info(
                "media_analysis: 压缩未成功（reason=%s passes=%d）",
                result.reason,
                result.passes,
            )
            return "", NATIVE_TOO_LARGE
        notes.append(f"video_compressed:{size}->{int(result.size or 0)}")
        if result.no_audio:
            notes.append("video_compressed_noaudio")
        send_path = result.path or path
        size = int(result.size or size)

    remaining = deadline - time.monotonic()
    timeout = min(
        float(settings.video_native_timeout_seconds), remaining - NATIVE_RESERVE_S
    )
    if timeout <= 0:
        notes.append("video_skip_budget")
        return "", NATIVE_FAILED

    prompt = _native_prompt(focus, settings, notes)
    outcome = await video_native.call_native(
        provider,
        prompt=prompt,
        video_path=send_path,
        max_tokens=settings.video_native_max_tokens,
        timeout=timeout,
    )
    if not outcome.ok:
        note = outcome.note or video_native.NOTE_FAILED
        if note == video_native.NOTE_UNAVAILABLE:
            # 解析之后 client 又没了（核心热重载）⇒ 仍然只是"没有通道"
            notes.append("video_native_unavailable")
            return "", NATIVE_UNAVAILABLE
        notes.append(note)
        LOG.info(
            "media_analysis: 原生视频未成功（chars=%d detail=%s）",
            len(outcome.text),
            outcome.detail,
        )
        return "", NATIVE_EMPTY if note == video_native.NOTE_EMPTY else NATIVE_FAILED

    text = outcome.text
    if _is_empty_sentinel(text):
        # 哨兵（[无可用内容]/[无语音]）等同空文本 ⇒ 进回退链（§12.1）
        notes.append("video_native_empty")
        return "", NATIVE_EMPTY
    notes.append("video_native")
    LOG.info(
        "media_analysis: 原生视频成功 chars=%d size=%d elapsed_ms=%d",
        len(text),
        size,
        outcome.elapsed_ms,
    )
    return text, NATIVE_OK


# --------------------------------------------------------------------------- #
# 音轨阶段（v0.2 链路原样）
# --------------------------------------------------------------------------- #
async def _transcribe_path(
    context,
    event,
    *,
    path: str,
    kind: str,
    focus: str,
    deadline: float,
    notes: list[str],
    settings: settings_mod.Settings,
    workdir: str,
    reserve_s: float = 0.0,
    soft_timeout: bool = False,
) -> list[dict[str, object]]:
    """取 provider wav → 多模态 `text_chat(audio_urls=[wav])` → 转写列表。

    `reserve_s` / `soft_timeout` 只在**视频**回退链里非默认（见 `transcribe` 的分流表）：
    有画面诉求时给抽帧留预算、超时降级为 `audio_timeout`。
    """
    audio_provider = await providers.resolve_audio_provider(
        context, settings.audio_provider_id, _umo(event)
    )
    wav_path = await media.ensure_provider_wav(
        path, workdir=workdir, extracted=(kind == "video")
    )
    if kind == "video":
        notes.append("audio_extracted")
        media.track_temporary_local_file(event, wav_path)
    if wav_path != path:
        notes.append("audio_normalized")
    prompt = _audio_prompt(kind, focus, settings, notes)
    text = await transcribe(
        audio_provider,
        wav_path,
        prompt,
        deadline=deadline,
        notes=notes,
        reserve_s=reserve_s,
        soft_timeout=soft_timeout,
    )
    notes.append(f"audio_provider:{providers.provider_id_of(audio_provider)}")
    if not text:
        return []
    return [{"t": 0.0, "text": text}]


async def _summary_stage(
    context,
    event,
    *,
    transcript: list[dict[str, object]],
    frames: list[dict[str, object]],
    focus: str,
    kind: str,
    deadline: float,
    notes: list[str],
    settings: settings_mod.Settings,
) -> str:
    """摘要阶段（v0.2 语义不变；材料为空则不做，剩余预算不足也跳过）。"""
    material = _build_material(transcript, frames)
    if len(material) > settings_mod.MATERIAL_MAX_CHARS:
        material = material[: settings_mod.MATERIAL_MAX_CHARS]
        notes.append("material_trimmed")
    if not material:
        return ""
    if deadline - time.monotonic() < SUMMARY_TIMEOUT_S:
        notes.append("summary_skipped_budget")
        return ""
    chat = None
    try:
        chat = await providers.resolve_chat_provider(
            context,
            settings.vision_provider_id,
            _umo(event),
            require_image=False,
        )
    except envelope.ToolError as exc:
        # 摘要失败**不**把整次调用判死（§9 第 11 步）
        LOG.warning("media_analysis: 摘要 provider 不可用（%s）", exc.code)
        chat = None
    if chat is None:
        notes.append("summary_failed")
        return ""
    return await build_summary(
        chat,
        material=material,
        focus=focus,
        notes=notes,
        kind=kind,
        settings=settings,
    )


async def transcribe(
    provider,
    wav_path: str,
    prompt: str,
    *,
    deadline: float,
    notes: list[str],
    reserve_s: float = 0.0,
    soft_timeout: bool = False,
) -> str:
    """一次多模态 `text_chat(audio_urls=[wav])`，返回**整段**文本（SPEC-v0.2 §2.1）。

    形状冻结：`image_urls` 一律不传、`audio_urls` 只放 1 个元素、不传
    `contexts`/`system_prompt`/`func_tool`（一次调用＝一次独立转写请求）。

    返回值可能为空串或哨兵 `[无语音]`：那是**成功但没内容**，写 `audio_empty` note、
    `transcript=[]`，不是错误。

    **预算（评审 R1 的接线点）**：超时不再写 `min(AUDIO_TIMEOUT_S, remaining)`，而是
    `budgeted_timeout(remaining, AUDIO_TIMEOUT_S, reserve_s)` —— 扣掉后面那一跳（抽帧）
    要用的预留。不扣的话，慢音轨会把抽帧预算吃光，主打回退在最该生效时 `video_skip_budget`。

    **超时后的分流（评审 R1 的裁决）**：

    | 情形 | 行为 |
    | --- | --- |
    | 本轮**要画面**（`soft_timeout=True`） | 记 note `audio_timeout`，返回 `""`，**继续走画面阶段** |
    | 不要画面（`soft_timeout=False`） | `AUDIO_FAILED`（主诉求就是转写，不能假成功） |

    「要画面」的判据是 `frames_possible()`（`mode=true`，或 `auto` + 视频模型 + 视觉位都配了）。
    **只有超时**走这条降级：provider 抛错（4xx/5xx/安全过滤…）仍一律 `AUDIO_FAILED` ——
    那说明模型侧真出问题了，不是"预算不够"。
    """
    remaining = deadline - time.monotonic()
    timeout = budgeted_timeout(remaining, AUDIO_TIMEOUT_S, reserve_s)
    if timeout <= 0:
        if soft_timeout:
            notes.append("audio_timeout")
            LOG.info("media_analysis: 音轨没有可用预算（要画面）⇒ 记 audio_timeout 继续")
            return ""
        raise envelope.ToolError(
            envelope.ERR_AUDIO_FAILED, log_detail="budget exhausted"
        )
    try:
        resp = await asyncio.wait_for(
            provider.text_chat(prompt=prompt, audio_urls=[wav_path]),
            timeout=timeout,
        )
    except (asyncio.TimeoutError, TimeoutError):
        if soft_timeout:
            # 要画面 ⇒ 别让慢音轨把整次调用拖死（评审 R1）
            notes.append("audio_timeout")
            LOG.info(
                "media_analysis: 音轨超时（%.1f s，已扣 %.0f s 画面预留）⇒ 记 audio_timeout 继续",
                timeout,
                reserve_s,
            )
            return ""
        raise envelope.ToolError(
            envelope.ERR_AUDIO_FAILED, log_detail=f"text_chat timeout {timeout:.1f}s"
        ) from None
    except Exception as exc:  # noqa: BLE001 —— provider 侧什么都可能抛
        # `asyncio.CancelledError` 是 BaseException，不在这里被吞（取消语义必须传播）
        raise envelope.ToolError(
            envelope.ERR_AUDIO_FAILED, log_detail=_exception_detail(exc)
        ) from None
    text = _response_text(resp)
    if not text or text == NO_SPEECH_SENTINEL:
        notes.append("audio_empty")
        return ""
    return text


async def describe_frames(
    provider,
    frames: list[tuple[float, str]],
    *,
    focus: str,
    deadline: float,
    notes: list[str],
    settings: settings_mod.Settings,
) -> list[dict[str, object]]:
    """逐帧 `text_chat`（每帧 15 s）；单帧失败记 note 继续，**全**失败抛 `VISION_FAILED`。

    三种"没拿到全部帧"要分清（SPEC.md §6.4 / §7.1）：
    - 单帧失败/超时 ⇒ 只降级这一帧（`frame_desc_failed:<at_s>`），其余继续；
    - 循环因 `remaining <= 0` 提前 break ⇒ **预算截断**：记
      `frames_truncated:<实际保留数>/<want>`（与 `frames` 长度一致）并返回，**不抛**；
    - **发起过**调用、全部失败/超时、且**没有**发生预算截断 ⇒ `VISION_FAILED`（模型侧真失败）。

    预算截断不判死的理由：此时手上往往已经有转写，把它随错误信封丢掉比降级更糟（评审 F4）。
    """
    want = len(frames)
    described: list[dict[str, object]] = []
    attempted = 0
    truncated = False
    prompt = _frame_prompt(focus, settings, notes)
    for at_s, jpg_path in frames:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            notes.append(f"frames_truncated:{len(described)}/{want}")
            truncated = True
            break
        timeout = min(FRAME_CALL_TIMEOUT_S, remaining)
        attempted += 1
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, image_urls=[jpg_path]),
                timeout=timeout,
            )
        except (asyncio.TimeoutError, TimeoutError):
            notes.append(f"frame_desc_failed:{media.format_at_s(at_s)}")
            continue
        except Exception as exc:  # noqa: BLE001
            LOG.warning(
                "media_analysis: 帧描述调用失败（%s）", type(exc).__name__
            )
            notes.append(f"frame_desc_failed:{media.format_at_s(at_s)}")
            continue
        desc = _response_text(resp)
        if not desc:
            notes.append(f"frame_desc_failed:{media.format_at_s(at_s)}")
            continue
        described.append({"at_s": at_s, "desc": desc})
    if attempted and not described and not truncated:
        raise envelope.ToolError(
            envelope.ERR_VISION_FAILED, log_detail=f"0/{want} frames described"
        )
    return described


async def build_summary(
    provider,
    *,
    material: str,
    focus: str,
    notes: list[str],
    kind: str = "",
    settings: settings_mod.Settings | None = None,
) -> str:
    """1 次纯文本 `text_chat`（15 s）：`focus` 非空→回答该问题，空→通篇摘要。

    模板来自 `settings.prompts.summary`，`{task}` 是"通篇概括"或"定向回答"的
    固定指令（v0.1 的两套模板合成为一套）。失败/超时/空结果都返回 `""` 并记
    `summary_failed`，**不**影响 `ok`。
    """
    if not material:
        return ""
    prompt = _summary_prompt(
        material, focus=focus, kind=kind, settings=settings, notes=notes
    )
    try:
        resp = await asyncio.wait_for(
            provider.text_chat(prompt=prompt), timeout=SUMMARY_TIMEOUT_S
        )
    except (asyncio.TimeoutError, TimeoutError):
        notes.append("summary_failed")
        return ""
    except Exception as exc:  # noqa: BLE001
        LOG.warning("media_analysis: 摘要调用失败（%s）", type(exc).__name__)
        notes.append("summary_failed")
        return ""
    text = _response_text(resp)
    if not text:
        notes.append("summary_failed")
    return text


# --------------------------------------------------------------------------- #
# 提示词渲染（§7.1 冻结规则）
# --------------------------------------------------------------------------- #
def _native_prompt(
    focus: str, settings: settings_mod.Settings, notes: list[str]
) -> str:
    """原生视频的提示词（`prompts.video_native`：一次调用同时要"画面 + 声音"）。"""
    template = settings.prompts[_PROMPT_INDEX["video_native"]]
    if "video_native" in settings.prompts_trimmed:
        notes.append("prompt_trimmed:video_native")
    clause = settings_mod.FOCUS_CLAUSE.format(focus=focus) if focus else ""
    return _render_prompt(
        template, key="video_native", notes=notes, focus=focus, clause=clause
    )


def _audio_prompt(
    kind: str,
    focus: str,
    settings: settings_mod.Settings,
    notes: list[str],
) -> str:
    """语音用 `prompts.audio`、视频音轨用 `prompts.video`（③ 的"各自独立提示词"）。"""
    key = "video" if kind == "video" else "audio"
    template = settings.prompts[_PROMPT_INDEX[key]]
    if key in settings.prompts_trimmed:
        notes.append(f"prompt_trimmed:{key}")
    clause = settings_mod.FOCUS_CLAUSE.format(focus=focus) if focus else ""
    return _render_prompt(template, key=key, notes=notes, focus=focus, clause=clause)


def _frame_prompt(
    focus: str, settings: settings_mod.Settings, notes: list[str]
) -> str:
    """单帧描述提示词（`prompts.frame`；焦点从句逐字沿用 v0.1 的 `FRAME_FOCUS_CLAUSE`）。"""
    template = settings.prompts[_PROMPT_INDEX["frame"]]
    if "frame" in settings.prompts_trimmed:
        notes.append("prompt_trimmed:frame")
    clause = FRAME_FOCUS_CLAUSE.format(focus=focus) if focus else ""
    return _render_prompt(template, key="frame", notes=notes, focus=focus, clause=clause)


def _summary_prompt(
    material: str,
    *,
    focus: str,
    kind: str,
    settings: settings_mod.Settings | None,
    notes: list[str],
) -> str:
    """摘要提示词：`{kind_cn}` / `{task}` / `{material}`（缺哪个补哪个，§7.1）。"""
    if settings is None:
        template = settings_mod.DEFAULT_PROMPTS["summary"]
    else:
        template = settings.prompts[_PROMPT_INDEX["summary"]]
        if "summary" in settings.prompts_trimmed:
            notes.append("prompt_trimmed:summary")
    kind_cn = _KIND_CN.get(kind, "媒体")
    task = SUMMARY_TASK_FOCUS.format(focus=focus) if focus else SUMMARY_TASK_SUMMARY
    missing_task = "{task}" not in template
    missing_material = "{material}" not in template
    if missing_task:
        notes.append("focus_appended:summary")
    rendered = _safe_format(
        template,
        kind_cn=kind_cn,
        task="" if missing_task else task,
        material="" if missing_material else material,
    )
    if missing_task:
        # 补在渲染**之后**：task 里可能含用户给的 focus（带花括号也不该被再解析一次）
        rendered = rendered.rstrip("\n") + "\n" + task
    if missing_material:
        rendered = rendered.rstrip("\n") + SUMMARY_MATERIAL_PREFIX + material
    return rendered


def _render_prompt(
    template: str, *, key: str, notes: list[str], focus: str, clause: str
) -> str:
    """渲染 audio/video/frame/video_native 模板，并保证**焦点永不被静默丢弃**。"""
    missing_clause = bool(focus) and "{focus_clause}" not in template
    if missing_clause:
        notes.append(f"focus_appended:{key}")
    rendered = _safe_format(template, focus_clause="" if missing_clause else clause)
    if missing_clause:
        # 补在渲染之后：clause 里含用户给的 focus，不该被再解析一次
        rendered = rendered.rstrip("\n") + "\n" + clause
    return rendered


def _safe_format(template: str, **values: str) -> str:
    """`str.format` 的安全版：坏模板只告警并原样返回，**绝不**把异常抛给模型。"""
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError, AttributeError):
        LOG.warning("media_analysis: 提示词模板渲染失败，已按原文发送（配置需修正）")
        return template


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _probe_store() -> probe.ProbeStore:
    """进程级 probe 缓存实例（`_broken` 熔断与"只告警一次"要跨调用生效）。"""
    global _PROBE_STORE
    if _PROBE_STORE is None:
        _PROBE_STORE = probe.ProbeStore(plugin_name=settings_mod.PLUGIN_NAME)
    return _PROBE_STORE


def _is_empty_sentinel(text: str) -> bool:
    """原生回答恰为哨兵（`[无可用内容]` / `[无语音]`）⇒ 等同空文本（§12.1）。"""
    return text.strip() in EMPTY_SENTINELS


async def _extract_frames_budgeted(
    video_path: str,
    want: int,
    duration_s: float,
    workdir: str,
    deadline: float,
    notes: list[str],
) -> tuple[list[tuple[float, str]], bool]:
    """在全局预算内抽帧；返回 `(frames, truncated)`。

    `truncated=True` ＝**预算不足**（一帧都没抽）：记 `frames_truncated:0/<want>` 后返回
    `([], True)`，**不抛** —— 预算截断是降级不是失败（SPEC.md §7.1「信封照常返回」），
    调用方不得因此丢掉手上的转写。真正的抽帧失败/超时**照旧**抛 `FRAME_EXTRACT_FAILED`。
    """
    remaining = deadline - time.monotonic()
    if remaining < FRAME_EXTRACT_TIMEOUT_S:
        notes.append(f"frames_truncated:0/{want}")
        return [], True
    budget = min(FRAME_EXTRACT_TIMEOUT_S * want + 5.0, remaining)
    try:
        frames = await asyncio.wait_for(
            media.extract_frames(
                video_path,
                count=want,
                duration_s=duration_s,
                workdir=workdir,
                notes=notes,
            ),
            timeout=budget,
        )
    except (asyncio.TimeoutError, TimeoutError):
        raise envelope.ToolError(
            envelope.ERR_FRAME_EXTRACT_FAILED, log_detail=f"extract timeout {budget:.1f}s"
        ) from None
    return frames, False


def _prepare_focus(focus: str) -> tuple[str, bool]:
    """去空白 + 截断到 `FOCUS_MAX_CHARS`；返回 `(文本, 是否被截断)`。"""
    text = focus.strip() if isinstance(focus, str) else ""
    if len(text) > FOCUS_MAX_CHARS:
        return text[:FOCUS_MAX_CHARS], True
    return text, False


def _build_material(
    transcript: list[dict[str, object]], frames: list[dict[str, object]]
) -> str:
    """摘要材料拼法（SPEC.md §7.5 冻结）：先转写，再逐行帧描述，换行连接。"""
    parts: list[str] = []
    if transcript:
        text = transcript[0].get("text") or ""
        if text:
            parts.append(f"转写：{text}")
    for index, frame in enumerate(frames, start=1):
        parts.append(f"第 {index} 帧（{frame.get('at_s')}s）：{frame.get('desc')}")
    return "\n".join(parts)


def _response_text(resp: object) -> str:
    """`LLMResponse.completion_text` → 去空白文本（非 str 一律空）。"""
    text = getattr(resp, "completion_text", "")
    return text.strip() if isinstance(text, str) else ""


def _exception_detail(exc: BaseException) -> str:
    """把异常压成**只进日志**的一行（§2.3 冻结表的三个特例逐字）。"""
    name = type(exc).__name__
    raw = str(exc)
    if name == "EmptyModelOutputError":
        return name
    lowered = raw.lower()
    if "内容安全过滤" in raw or "content_filter" in lowered or "content filter" in lowered:
        return "content_filter"
    return f"{name}: {raw[:120]}"


def _replay(entry: dict[str, object], started: float) -> dict[str, object]:
    """缓存命中回放（§6.3）：`cached=true` + **本次**真实耗时 + 追加两条 note。

    `envelope` 是**逐字节复用**的（`duration_s`/`transcript`/`frames`/`summary`/`kind`/
    原始 `notes` 都不改），只回填三项；`age_s` 是条目年龄（向下取整）。
    """
    stored = entry.get("envelope")
    payload = dict(stored) if isinstance(stored, dict) else {}
    payload["cached"] = True
    payload["elapsed_ms"] = _elapsed_ms(started)
    payload["notes"] = list(payload.get("notes") or []) + [
        "cache_hit",
        f"cache_age_s:{int(entry.get('age_s') or 0)}",
    ]
    return payload


def _umo(event) -> str:
    """会话来源（用于 provider 会话偏好解析）；拿不到就空串。"""
    value = getattr(event, "unified_msg_origin", "")
    return value if isinstance(value, str) else ""


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
