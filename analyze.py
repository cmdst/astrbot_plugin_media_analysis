"""主链路编排（SPEC §7 冻结顺序）：音轨转写 → 按需抽帧+描述 → 摘要 → 信封。

三个阶段都可能降级，但**降级的形状是固定的**：
- 用户主诉求（转写 / 看画面）无法满足 ⇒ `ToolError`（错误码见 `envelope`）；
- 附加项失败（某帧描述、摘要）⇒ 成功信封 + `notes` 词表里的一条（SPEC §6.4，12 条）。

本模块**自己建/删工作目录**（`tempfile.mkdtemp(prefix="media_analysis_")`，落在 `/tmp`），
调用方不需要关心临时目录；中间产物（wav / 抽帧 jpg）用完即删，不落库、不落工作区。
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time

from astrbot.api import logger as LOG

# 别名：冻结签名里 `settings` / `cache` 是**形参名**，模块名直接 import 会被遮住。
from . import cache as cache_mod
from . import envelope, media, providers
from . import settings as settings_mod

# ---- 预算（SPEC §7.1 / §3.2）----
DEADLINE_S = 100.0  # 全局预算；框架 tool_call_timeout=120 s，留 20 s 余量
PROBE_TIMEOUT_S = 10.0  # 单次 ffprobe（实测 0.019 s，只防病态文件）
STT_TIMEOUT_S = 60.0  # 生产唯一 STT 是远端 API，网络未知
FRAME_EXTRACT_TIMEOUT_S = 15.0  # 单帧 ffmpeg（实测 6 帧合计 0.206 s）
FRAME_CALL_TIMEOUT_S = 15.0  # 单帧 text_chat
SUMMARY_TIMEOUT_S = 15.0  # 摘要 text_chat；剩余预算少于它就不做摘要
FRAME_WIDTH_PX = 768  # 与 media.FRAME_WIDTH_PX 同值（SPEC §3.2 冻结常量）
FOCUS_MAX_CHARS = 200
WORKDIR_PREFIX = "media_analysis_"  # 中间产物只落 /tmp（SPEC §9-4）

# ---- 提示词（SPEC §7.5 冻结常量；改动即契约变更，要同步 SPEC）----
FRAME_PROMPT = (
    "这是一段视频里截取的一帧画面。请用一到两句话客观描述画面里能看到的内容"
    "（人物/动作/场景/画面上的文字）。不要猜测视频主题，不要评价，不要补充画面里没有的信息。"
    "{focus_clause}"
)
FRAME_FOCUS_CLAUSE = (
    "另外，用户关心的是「{focus}」：如果画面上有与之相关的信息，请优先说这部分。"
)
SUMMARY_PROMPT = (
    "下面的材料来自一段{kind_cn}的分析结果。请用不超过三句中文概括它的主要内容。"
    "如果材料里没有可用的内容，只回答：没有可用的内容。\n\n材料：\n{material}"
)
SUMMARY_FOCUS_PROMPT = (
    "下面的材料来自一段{kind_cn}的分析结果。请只用不超过三句中文回答这个问题：「{focus}」。"
    "材料里没有相关信息就直说没有。\n\n材料：\n{material}"
)

_KIND_CN = {"audio": "语音", "video": "视频"}


async def analyze(
    *,
    context,
    event,
    target: str,
    focus: str,
    include_frames: bool,
    settings: settings_mod.Settings,
    cache: cache_mod.ResultCache,
) -> dict[str, object]:
    """跑完整条链路，返回**成功信封**（9 键）；受控失败抛 `envelope.ToolError`。

    步骤号与 SPEC §7 一致（0–11），便于对着规格评审。
    """
    started = time.monotonic()  # 0
    deadline = started + DEADLINE_S
    notes: list[str] = []
    workdir = tempfile.mkdtemp(prefix=WORKDIR_PREFIX)
    try:
        # 1 —— focus 去空白 + 截断
        focus_text, trimmed = _prepare_focus(focus)
        if trimmed:
            notes.append("focus_trimmed")
        max_frames = max(1, int(settings.max_frames))
        max_seconds = int(settings.max_media_seconds)

        # 2 —— 定位媒体组件
        handle = media.find_media(event, target)
        kind = handle.kind

        # 3 —— 拿到本地路径
        path = await media.resolve_media_path(handle)

        # 4 —— 时长 + 上限
        duration_s = await media.probe_duration_s(path)
        if duration_s > max_seconds:
            raise envelope.ToolError(
                envelope.ERR_MEDIA_TOO_LONG,
                log_detail=f"duration_s={duration_s} max_media_seconds={max_seconds}",
            )

        # 5 —— 内容哈希 + 缓存
        digest = await media.content_digest(path)
        key = cache_mod.ResultCache.make_key(
            digest,
            kind=kind,
            focus=focus_text,
            include_frames=include_frames,
            max_frames=max_frames,
        )
        hit = cache.get(key)
        if hit is not None:
            return _replay(hit, started)

        # 6 —— 判流（语音天然有音轨、无视频流）
        if kind == "video":
            has_audio, has_video = await media.probe_streams(path)
        else:
            has_audio, has_video = True, False

        # 7 —— 音轨阶段
        transcript: list[dict[str, object]] = []
        if kind == "video" and not has_audio:
            if not include_frames:
                # 主诉求就是转写 ⇒ 无音轨是错误，不是降级（SPEC §7.2）
                raise envelope.ToolError(
                    envelope.ERR_NO_AUDIO_TRACK,
                    log_detail=f"duration_s={duration_s} include_frames=false",
                )
            notes.append("no_audio_track")
        else:
            wav_path = path
            if kind == "video":
                wav_path = await media.extract_wav(path, workdir=workdir)
                media.track_temporary_local_file(event, wav_path)
                notes.append("audio_extracted")
            stt = await providers.resolve_stt_provider(
                context, settings.stt_provider_id, _umo(event)
            )
            text = await transcribe(stt, wav_path, notes=notes)
            if text:
                transcript.append({"t": 0.0, "text": text})
            notes.append(f"stt_provider:{providers.provider_id_of(stt)}")

        # 8 —— 抽帧阶段（只有 include_frames=True 才跑；语音忽略开关）
        frames: list[dict[str, object]] = []
        vision = None
        if include_frames and kind == "audio":
            notes.append("frames_skipped_audio_only")
        elif include_frames and kind == "video":
            if not has_video:
                raise envelope.ToolError(
                    envelope.ERR_NO_VIDEO_TRACK,
                    log_detail=f"duration_s={duration_s} include_frames=true",
                )
            extracted, truncated = await _extract_frames_budgeted(
                path, max_frames, duration_s, workdir, deadline, notes
            )
            if truncated:
                # 预算不足：抽帧降级为 0 帧，**不**判死整次调用（手上的转写照常交付，SPEC §7.1）
                LOG.info(
                    "media_analysis: 抽帧因预算不足被截断（0/%d），继续走摘要",
                    max_frames,
                )
            elif not extracted:
                raise envelope.ToolError(
                    envelope.ERR_FRAME_EXTRACT_FAILED,
                    log_detail=f"0/{max_frames} frames",
                )
            else:
                vision = await providers.resolve_vision_provider(
                    context, settings.vision_provider_id, _umo(event)
                )
                frames = await describe_frames(
                    vision, extracted, focus=focus_text, deadline=deadline, notes=notes
                )
                notes.append(f"vision_provider:{providers.provider_id_of(vision)}")

        # 9 —— 摘要阶段
        material = _build_material(transcript, frames)
        summary = ""
        if not material:
            summary = ""
        elif deadline - time.monotonic() < SUMMARY_TIMEOUT_S:
            notes.append("summary_skipped_budget")
        else:
            chat = vision
            if chat is None:
                try:
                    chat = await providers.resolve_chat_provider(
                        context,
                        settings.vision_provider_id,
                        _umo(event),
                        require_image=False,
                    )
                except envelope.ToolError as exc:
                    # 摘要失败**不**把整次调用判死（SPEC §7 第 9 步）
                    LOG.warning("media_analysis: 摘要 provider 不可用（%s）", exc.code)
                    chat = None
            if chat is None:
                notes.append("summary_failed")
            else:
                summary = await build_summary(
                    chat, material=material, focus=focus_text, notes=notes, kind=kind
                )

        # 10 —— 信封 + 入缓存
        if include_frames and kind == "video" and not transcript and not frames:
            # 诚实兜底：用户要的是画面，而画面一帧没拿到、转写也没有 ⇒ 别给模型一个空成功
            raise envelope.ToolError(
                envelope.ERR_FRAME_EXTRACT_FAILED,
                log_detail="include_frames=true 但 transcript 与 frames 都为空",
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
        cache.put(key, payload)
        # 只打结构与计数，**不打**转写正文 / 画面描述（SPEC §9-1）
        LOG.info(
            "media_analysis: ok kind=%s duration_s=%.2f frames=%d "
            "transcript_chars=%d notes=%d elapsed_ms=%d",
            kind,
            duration_s,
            len(frames),
            len(transcript[0]["text"]) if transcript else 0,
            len(notes),
            payload["elapsed_ms"],
        )
        return payload
    finally:
        # 11 —— 中间产物只落 /tmp，用完即删（SPEC §9-4）
        shutil.rmtree(workdir, ignore_errors=True)


async def transcribe(stt, wav_path: str, *, notes: list[str]) -> str:
    """调 STT provider 把 wav 转成**整段**文本（SPEC §2.3：唯一入口 `get_text`）。

    返回值可能为空串（whisper 类实现会这样）：那是**成功但没内容**，写
    `stt_empty` note、`transcript=[]`，不是错误。超时/抛错才是 `STT_FAILED`。
    """
    try:
        raw = await asyncio.wait_for(
            stt.get_text(audio_url=wav_path), timeout=STT_TIMEOUT_S
        )
    except (asyncio.TimeoutError, TimeoutError):
        raise envelope.ToolError(
            envelope.ERR_STT_FAILED, log_detail=f"get_text timeout {STT_TIMEOUT_S}s"
        ) from None
    except Exception as exc:  # noqa: BLE001 —— provider 侧什么都可能抛
        raise envelope.ToolError(
            envelope.ERR_STT_FAILED,
            log_detail=f"get_text {type(exc).__name__}: {str(exc)[:120]}",
        ) from None
    text = raw.strip() if isinstance(raw, str) else ""
    if not text:
        notes.append("stt_empty")
    return text


async def describe_frames(
    provider,
    frames: list[tuple[float, str]],
    *,
    focus: str,
    deadline: float,
    notes: list[str],
) -> list[dict[str, object]]:
    """逐帧 `text_chat`（每帧 15 s）；单帧失败记 note 继续，**全**失败抛 `VISION_FAILED`。

    两种"一帧都没描述成"要分清（SPEC §7.1「预算截断是降级、不是失败」）：
    - 循环因 `remaining <= 0` 在**第 0 帧前**就 break ⇒ 返回 `[]` **不抛**（预算截断，已记
      `frames_truncated:0/<want>`）；
    - **发起过**调用但全部失败/超时 ⇒ 保持 `VISION_FAILED`（模型侧真失败，语义不变）。
    """
    want = len(frames)
    described: list[dict[str, object]] = []
    attempted = 0
    prompt = _frame_prompt(focus)
    for index, (at_s, jpg_path) in enumerate(frames):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            notes.append(f"frames_truncated:{index}/{want}")
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
    if attempted and not described:
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
) -> str:
    """1 次纯文本 `text_chat`（15 s）：`focus` 非空→回答该问题，空→通篇摘要。

    `kind` 是 SPEC §3.2 签名之外**追加的带默认值**参数：提示词里的 `{kind_cn}`
    需要它，而冻结签名没带（默认 `""` → 「媒体」）。失败/超时/空结果都返回 `""`
    并记 `summary_failed`，**不**影响 `ok`。
    """
    if not material:
        return ""
    kind_cn = _KIND_CN.get(kind, "媒体")
    if focus:
        prompt = SUMMARY_FOCUS_PROMPT.format(
            kind_cn=kind_cn, focus=focus, material=material
        )
    else:
        prompt = SUMMARY_PROMPT.format(kind_cn=kind_cn, material=material)
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
# 内部工具
# --------------------------------------------------------------------------- #


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
    `([], True)`，**不抛** —— 预算截断是降级不是失败（SPEC §7.1「信封照常返回」），
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


def _frame_prompt(focus: str) -> str:
    """单帧描述提示词；`focus` 非空时追加定向从句（SPEC §7.5）。"""
    clause = FRAME_FOCUS_CLAUSE.format(focus=focus) if focus else ""
    return FRAME_PROMPT.format(focus_clause=clause)


def _prepare_focus(focus: str) -> tuple[str, bool]:
    """去空白 + 截断到 `FOCUS_MAX_CHARS`；返回 `(文本, 是否被截断)`。"""
    text = focus.strip() if isinstance(focus, str) else ""
    if len(text) > FOCUS_MAX_CHARS:
        return text[:FOCUS_MAX_CHARS], True
    return text, False


def _build_material(
    transcript: list[dict[str, object]], frames: list[dict[str, object]]
) -> str:
    """摘要材料拼法（SPEC §7.5 冻结）：先转写，再逐行帧描述，换行连接。"""
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


def _replay(hit: dict[str, object], started: float) -> dict[str, object]:
    """缓存命中：回填 `cached=True` 与**本次**真实耗时，其余字段原样（含原始 notes）。"""
    payload = dict(hit)
    payload["cached"] = True
    payload["elapsed_ms"] = _elapsed_ms(started)
    return payload


def _umo(event) -> str:
    """会话来源（用于 provider 会话偏好解析）；拿不到就空串。"""
    value = getattr(event, "unified_msg_origin", "")
    return value if isinstance(value, str) else ""


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
