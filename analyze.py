"""主链路编排（SPEC-v0.2 §9 冻结顺序）：缓存 → 音轨转写 → 按需抽帧+描述 → 摘要 → 信封。

v0.1 → v0.2 的四处结构性变化：
1. **音频改走多模态 chat**：`text_chat(prompt=<提示词>, audio_urls=[16k/mono wav])`
   （不再有 `STTProvider` 链路），异常一律映射 `AUDIO_FAILED`（§2.3 冻结表）；
2. **提示词可配置**：4 份（audio / video / frame / summary），空串回退内置默认，
   坏值回退 + 告警，超长截断（§7.1）；
3. **结果落盘缓存带 TTL**：`cache.build_key` 的 10 个分量键 + `singleflight` 同键串行化，
   命中回 `cached=true` + **本次**真实耗时 + `cache_hit` / `cache_age_s:<n>`（§6.3）；
4. **`File` 段进主流程**：`media.classify` 一次拿 `(kind, duration, has_audio, has_video)`
   （`File` 走三段梯子），音轨阶段不再重复 ffprobe。

三个阶段都可能降级，但**降级的形状是固定的**：
- 用户主诉求（转写 / 看画面）无法满足 ⇒ `ToolError`（错误码见 `envelope`）；
- 附加项失败（某帧描述、摘要）⇒ 成功信封 + `notes` 词表里的一条（§8.2，20 条）。

本模块**自己建/删工作目录**（`tempfile.mkdtemp(prefix="media_analysis_")`，落在 `/tmp`），
调用方不需要关心临时目录；中间产物（wav / 抽帧 jpg / 群文件下载物）用完即删，
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
from . import envelope, media, providers
from . import settings as settings_mod

# ---- 预算（SPEC-v0.2 §2.2 / SPEC.md §7.1）----
DEADLINE_S = 100.0  # 全局预算；框架 tool_call_timeout=120 s，留 20 s 余量
PROBE_TIMEOUT_S = 10.0  # 单次 ffprobe（实测 0.019 s，只防病态文件）
AUDIO_TIMEOUT_S = 60.0  # 单次多模态调用（含上传：60 s 音频 2.44 MiB base64）
FRAME_EXTRACT_TIMEOUT_S = 15.0  # 单帧 ffmpeg（实测 6 帧合计 0.206 s）
FRAME_CALL_TIMEOUT_S = 15.0  # 单帧 text_chat
SUMMARY_TIMEOUT_S = 15.0  # 摘要 text_chat；剩余预算少于它就不做摘要
FRAME_WIDTH_PX = 768  # 与 media.FRAME_WIDTH_PX 同值（SPEC.md §3.2 冻结常量）
FOCUS_MAX_CHARS = 200
WORKDIR_PREFIX = "media_analysis_"  # 中间产物只落 /tmp（SPEC.md §9-4）

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

_KIND_CN = {"audio": "语音", "video": "视频"}
# 提示词在 `settings.prompts` 4 元组里的下标（顺序冻结：audio, video, frame, summary）
_PROMPT_INDEX = {"audio": 0, "video": 1, "frame": 2, "summary": 3}


async def analyze(
    *,
    context,
    event,
    target: str,
    focus: str,
    include_frames: bool,
    settings: settings_mod.Settings,
    cache: cache_mod.DiskCache,
) -> dict[str, object]:
    """跑完整条链路，返回**成功信封**（9 键）；受控失败抛 `envelope.ToolError`。

    步骤号与 SPEC-v0.2 §9 一致（0–13），便于对着规格评审。
    """
    started = time.monotonic()  # 0
    deadline = started + DEADLINE_S
    notes: list[str] = []
    workdir = tempfile.mkdtemp(prefix=WORKDIR_PREFIX)
    try:
        # 1 —— focus 去空白 + 截断；配置里的上限已钳位
        focus_text, trimmed = _prepare_focus(focus)
        if trimmed:
            notes.append("focus_trimmed")
        max_frames = max(1, int(settings.max_frames))
        max_seconds = int(settings.max_media_seconds)
        max_bytes = int(settings.max_media_bytes)

        # 2 —— 定位媒体组件（Record → Video → File）
        handle = media.find_media(event, target)
        if handle.from_file:
            notes.append("source:file")

        # 3 —— 拿到本地路径（File 走 §3.2 决策树；有界下载落本工作目录）
        download_needed = media.file_download_needed(handle)
        path = await media.resolve_media_path(
            handle, workdir=workdir, max_media_bytes=max_bytes
        )
        if download_needed:
            notes.append("file_downloaded")

        # 4 —— 判型 + 时长 + 流（File 走三段梯子；Record/Video 的时长失败仍 PROBE_FAILED）
        kind, duration_s, has_audio, has_video = await media.classify(
            handle, path, max_media_bytes=max_bytes, workdir=workdir
        )
        if duration_s > max_seconds:
            raise envelope.ToolError(
                envelope.ERR_MEDIA_TOO_LONG,
                log_detail=f"duration_s={duration_s} max_media_seconds={max_seconds}",
            )

        # 5 —— 内容哈希 + 缓存键
        digest = await media.content_digest(path)
        key = cache_mod.build_key(
            digest=digest,
            kind=kind,
            include_frames=include_frames,
            max_frames=max_frames,
            focus=focus_text,
            prompts_fp=settings.prompts_fp,
            audio_provider_id=settings.audio_provider_id,
            vision_provider_id=settings.vision_provider_id,
            plugin_version=settings_mod.PLUGIN_VERSION,
        )

        # 6 —— 同键串行化 + 查缓存（命中即回放，0 token）
        async with cache.singleflight(key):
            hit = await cache.get(key)
            if hit is not None:
                return _replay(hit, started)

            # 7 —— 音频 provider（仅当真要走音轨阶段才解析：无音轨 + 只抽帧时不该
            #       被"没配音频模型"连累；这与 §9 第 10 步"视觉解析挪进抽帧阶段"同一口径）
            needs_audio = not (kind == "video" and not has_audio)
            audio_provider = None
            if needs_audio:
                audio_provider = await providers.resolve_audio_provider(
                    context, settings.audio_provider_id, _umo(event)
                )

            # 8 —— 流标志已在第 4 步拿到（File 已在 classify 内探过，不重复 ffprobe）

            # 9 —— 音轨阶段
            transcript: list[dict[str, object]] = []
            if not needs_audio:
                if not include_frames:
                    # 主诉求就是转写 ⇒ 无音轨是错误，不是降级（§8.1）
                    raise envelope.ToolError(
                        envelope.ERR_NO_AUDIO_TRACK,
                        log_detail=f"duration_s={duration_s} include_frames=false",
                    )
                notes.append("no_audio_track")
            else:
                wav_path = await media.ensure_provider_wav(
                    path, workdir=workdir, extracted=(kind == "video")
                )
                if kind == "video":
                    notes.append("audio_extracted")
                    media.track_temporary_local_file(event, wav_path)
                if wav_path != path:
                    notes.append("audio_normalized")
                prompt = _audio_prompt(kind, focus_text, settings, notes)
                text = await transcribe(
                    audio_provider, wav_path, prompt, deadline=deadline, notes=notes
                )
                if text:
                    transcript.append({"t": 0.0, "text": text})
                notes.append(
                    f"audio_provider:{providers.provider_id_of(audio_provider)}"
                )

            # 10 —— 抽帧阶段（仅 include_frames 且视频；§7 口径逐字沿用 v0.1）
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
                    # 预算不足：抽帧降级为 0 帧，**不**判死整次调用（手上的转写照常交付）
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
                        vision,
                        extracted,
                        focus=focus_text,
                        deadline=deadline,
                        notes=notes,
                        settings=settings,
                    )
                    notes.append(f"vision_provider:{providers.provider_id_of(vision)}")

            # 11 —— 摘要阶段（材料拼法不变 + §7.3 的尾部截断）
            material = _build_material(transcript, frames)
            if len(material) > settings_mod.MATERIAL_MAX_CHARS:
                material = material[: settings_mod.MATERIAL_MAX_CHARS]
                notes.append("material_trimmed")
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
                        # 摘要失败**不**把整次调用判死（§9 第 11 步）
                        LOG.warning(
                            "media_analysis: 摘要 provider 不可用（%s）", exc.code
                        )
                        chat = None
                if chat is None:
                    notes.append("summary_failed")
                else:
                    summary = await build_summary(
                        chat,
                        material=material,
                        focus=focus_text,
                        notes=notes,
                        kind=kind,
                        settings=settings,
                    )

            # 12 —— 信封 + 入缓存 + 结构化日志（只打结构/计数/耗时）
            if include_frames and kind == "video" and not transcript and not frames:
                # 诚实兜底：要画面却一帧没拿到、转写也没有 ⇒ 别给模型一个空成功
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
            await cache.put(key, payload)
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
        # 13 —— 中间产物只落 /tmp，用完即删（SPEC.md §9-4）
        shutil.rmtree(workdir, ignore_errors=True)


async def transcribe(
    provider,
    wav_path: str,
    prompt: str,
    *,
    deadline: float,
    notes: list[str],
) -> str:
    """一次多模态 `text_chat(audio_urls=[wav])`，返回**整段**文本（SPEC-v0.2 §2.1）。

    形状冻结：`image_urls` 一律不传、`audio_urls` 只放 1 个元素、不传
    `contexts`/`system_prompt`/`func_tool`（一次调用＝一次独立转写请求）。

    返回值可能为空串或哨兵 `[无语音]`：那是**成功但没内容**，写 `audio_empty` note、
    `transcript=[]`，不是错误。超时/抛错才是 `AUDIO_FAILED`（§2.3 冻结表）。
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise envelope.ToolError(
            envelope.ERR_AUDIO_FAILED, log_detail="budget exhausted"
        )
    timeout = min(AUDIO_TIMEOUT_S, remaining)
    try:
        resp = await asyncio.wait_for(
            provider.text_chat(prompt=prompt, audio_urls=[wav_path]),
            timeout=timeout,
        )
    except (asyncio.TimeoutError, TimeoutError):
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

    模板来自 `settings.prompts.summary`（§5.1），`{task}` 是"通篇概括"或"定向回答"的
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
    """渲染 audio/video/frame 模板，并保证**焦点永不被静默丢弃**（§7.1 补偿规则）。"""
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
