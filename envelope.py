"""错误码 / 静态提示 / JSON 信封 / `ToolError`。

本模块是**叶子**：不 import 任何其它包内模块（谁都依赖它，它不依赖谁）。

21 条错误码与 21 条 `hint` 是 SPEC-v0.2 §8.1 + SPEC-v0.3 §11.1 表的**唯一事实源**：
`hint` 必须是**常量字符串**，禁止拼接运行时数据（路径、异常文本、时长、provider id、文件名）
—— 信封会被框架按 INFO 全量写进生产日志（`tool_loop_agent_runner.py:1439`），
hint 是唯一可能夹带运行时数据的位置，钉成常量就整条链干净。

相对 v0.1（SPEC.md §5）的增删（SPEC-v0.2 §8.1）：
- **删** `STT_UNAVAILABLE` / `STT_FAILED`（v0.2 不再有 STT 链路）；
- **增** `MEDIA_TOO_LARGE` / `UNSUPPORTED_MEDIA_TYPE`（`File` 段取件与判型）/
  `AUDIO_UNAVAILABLE` / `AUDIO_FAILED`（音频改走多模态对话模型）。

v0.3（SPEC-v0.3 §11.1）**只加不改**：既有 18 条逐字不变，新增 3 条视频链路的终态码
`VIDEO_UNAVAILABLE` / `VIDEO_FAILED` / `VIDEO_TOO_LARGE` ⇒ 共 **21 条**。

v0.4（SPEC-v0.4 §7.1 / §3）**只加不改**：既有 21 条逐字不变，新增 2 条
`GIF_CONVERT_FAILED`（GIF 归一失败/超时/空产物）与 `TOO_MANY_TASKS`（两级并发闸）⇒ **23 条**；
同时把信封从"两种形状"扩到"**三种形状**"：新增**受理态** 4 键
`{"ok","state","task_id","hint"}`（`ACCEPT_KEYS`），与既有成功 9 键 / 失败 3 键**键名不重叠**
（除 `ok`）。这是 v0.1 以来第一次动信封形状，属**契约变更**（评审已批准）。

v0.5.1（SPEC-v0.5.1 §5.3 / §2.2-A）**码数仍是 23**，只动两处：
- `MEDIA_NOT_FOUND` 的 `hint` **分叉出一条唤醒轮变体**（`HINT_MEDIA_NOT_FOUND_WAKE`，
  由 `fail_envelope_wake()` 构造），原 `HINTS["MEDIA_NOT_FOUND"]` **一字不改**；
- 三条 hint 里的**配置键名**跟随 v0.5.1 键表改名（两个字节键变成 MB 键），
  而原生超时那个键已被删除 ⇒ 那句改成不指向任何配置键（只说"换一个更快的视频模型"）。
"""

from __future__ import annotations

import json

ERR_BAD_TARGET = "BAD_TARGET"
ERR_MEDIA_NOT_FOUND = "MEDIA_NOT_FOUND"
ERR_MEDIA_UNRESOLVABLE = "MEDIA_UNRESOLVABLE"
ERR_PROBE_FAILED = "PROBE_FAILED"
ERR_MEDIA_TOO_LONG = "MEDIA_TOO_LONG"
ERR_MEDIA_TOO_LARGE = "MEDIA_TOO_LARGE"
ERR_UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"
ERR_NO_AUDIO_TRACK = "NO_AUDIO_TRACK"
ERR_NO_VIDEO_TRACK = "NO_VIDEO_TRACK"
ERR_AUDIO_EXTRACT_FAILED = "AUDIO_EXTRACT_FAILED"
ERR_FRAME_EXTRACT_FAILED = "FRAME_EXTRACT_FAILED"
ERR_AUDIO_UNAVAILABLE = "AUDIO_UNAVAILABLE"
ERR_AUDIO_FAILED = "AUDIO_FAILED"
ERR_PROVIDER_NOT_FOUND = "PROVIDER_NOT_FOUND"
ERR_PROVIDER_CAPABILITY_MISMATCH = "PROVIDER_CAPABILITY_MISMATCH"
ERR_VISION_UNAVAILABLE = "VISION_UNAVAILABLE"
ERR_VISION_FAILED = "VISION_FAILED"
ERR_VIDEO_UNAVAILABLE = "VIDEO_UNAVAILABLE"
ERR_VIDEO_FAILED = "VIDEO_FAILED"
ERR_VIDEO_TOO_LARGE = "VIDEO_TOO_LARGE"
ERR_GIF_CONVERT_FAILED = "GIF_CONVERT_FAILED"  # v0.4 新增
ERR_TOO_MANY_TASKS = "TOO_MANY_TASKS"  # v0.4 新增
ERR_INTERNAL_ERROR = "INTERNAL_ERROR"

# 唯一事实源：23 条逐字静态文案（SPEC-v0.2 §8.1 + SPEC-v0.3 §11.1 + SPEC-v0.4 §7.1 是它的视图）。
HINTS: dict[str, str] = {
    "BAD_TARGET": (
        "target 只接受 current 或 quoted；不要传文件路径。"
        "群文件形式的视频/语音也用 current 或 quoted 取。"
    ),
    "MEDIA_NOT_FOUND": (
        "这条消息或它引用的消息里没有语音/视频/可解析的文件；"
        "请用户直接发送或引用一条语音、视频，或作为群文件发送。"
    ),
    "MEDIA_UNRESOLVABLE": "媒体组件在，但取不到本地文件（可能已被清理，或平台没给可用地址）。",
    "PROBE_FAILED": "读不出媒体时长（ffprobe 失败），无法判定上限也无法抽帧。",
    "MEDIA_TOO_LONG": (
        "媒体时长超过配置上限；请用户发更短的片段，或让管理员调高 limits.max_media_seconds。"
    ),
    "MEDIA_TOO_LARGE": (
        "文件体积超过上限；请用户发更短的片段，或让管理员调高 limits.max_media_mb。"
    ),
    "UNSUPPORTED_MEDIA_TYPE": (
        "这个文件不是可解析的音频或视频；请用户改发语音条，"
        "或发 mp4/mov/m4a/mp3/wav/amr 之类的媒体文件。"
    ),
    "NO_AUDIO_TRACK": "这条视频没有音轨；把 include_frames 设为 true 改走抽帧。",
    "NO_VIDEO_TRACK": "这条视频没有视频流；把 include_frames 设为 false 只做转写。",
    "AUDIO_EXTRACT_FAILED": "音频转码失败；请重试或让用户换一条媒体。",
    "FRAME_EXTRACT_FAILED": "抽帧全部失败；请重试或让用户换一条媒体。",
    "AUDIO_UNAVAILABLE": (
        "没有可用的音频对话模型；请让管理员在插件配置里指定「音频模型」（模型能力需勾上「音频」）。"
    ),
    "AUDIO_FAILED": "音频模型调用失败或超时；请稍后重试。",
    "PROVIDER_NOT_FOUND": "配置里的 provider id 在 AstrBot 中不存在；请让管理员在插件配置里重新选择。",
    "PROVIDER_CAPABILITY_MISMATCH": (
        "所选 provider 不具备该能力（音频输入 / 图像输入）；请让管理员改选，"
        "或在后台给该 provider 的「模型能力」勾上「音频」/「图像」。"
    ),
    "VISION_UNAVAILABLE": (
        "没有可用的对话 provider 来读图；请让管理员在插件配置里指定「视觉模型」。"
    ),
    "VISION_FAILED": "视觉模型调用全部失败或超时；请稍后重试。",
    "VIDEO_UNAVAILABLE": (
        "没有可用的视频模型，也无法回退抽帧；请让管理员在插件配置里指定「视频模型」"
        "（走原生视频），或把 include_frames 设为 true 并配置「视觉模型」。"
    ),
    "VIDEO_FAILED": (
        "视频模型调用失败或超时；请稍后重试，或让管理员换一个更快的视频模型。"
    ),
    "VIDEO_TOO_LARGE": (
        "视频体积超过直送门槛且压不到门槛内；请让管理员调大 video.max_direct_mb，"
        "或把 include_frames 设为 auto 或 true 改走抽帧。"
    ),
    # —— v0.4 新增 2 条（SPEC-v0.4 §7.1；既有 21 条逐字不变）——
    "GIF_CONVERT_FAILED": "这张动图（GIF）转换失败；请重试，或让用户改发视频。",
    "TOO_MANY_TASKS": "当前会话或实例正在分析的媒体太多；请等前面的结果发出来再试。",
    "INTERNAL_ERROR": "插件内部错误；已记录日志，请让管理员查看插件日志。",
}

#: 唤醒轮（`CronMessageEvent` / 后台任务结果轮）专用的 `MEDIA_NOT_FOUND` 文案
#: （SPEC-v0.5.1 §5.3，**不是新错误码**：`error` 仍是 `MEDIA_NOT_FOUND`，23 码不变）。
#:
#: 为什么要分叉：合成事件里**本来就不该有媒体**，把它当"用户忘了发"会让模型去劝用户重发
#: 同一条消息（下一轮还是空）。这句话把模型引到唯一有效的动作上：如实转述 + 让用户**重新**
#: 发一条新消息。与 `HINTS` 同一纪律：纯常量，无路径/异常文本/provider id/数字串。
HINT_MEDIA_NOT_FOUND_WAKE = (
    "本轮是后台任务的唤醒轮：这条消息里没有、也不会有任何媒体，重试本工具一定失败。"
    "请把后台任务的失败结果如实转述给用户，并请用户重新发送媒体。"
)

# 成功信封的 9 个键，**顺序固定**（SPEC.md §6.1 / SPEC-v0.2 §6.3）；失败信封恰好 3 键。
OK_KEYS: tuple[str, ...] = (
    "ok",
    "kind",
    "duration_s",
    "transcript",
    "frames",
    "summary",
    "notes",
    "elapsed_ms",
    "cached",
)
FAIL_KEYS: tuple[str, ...] = ("ok", "error", "hint")

#: 受理信封的 4 个键，**顺序固定**（SPEC-v0.4 §3.2）。三种形状的键集合两两不相交（除 `ok`）。
ACCEPT_KEYS: tuple[str, ...] = ("ok", "state", "task_id", "hint")

#: 受理信封 `state` 的两个取值（枚举封闭，SPEC-v0.4 §3.2）。
#: `accepted` = 新任务已登记；`running` = 同一条消息同一组参数已在跑（去重命中，`task_id` 相同）。
STATE_ACCEPTED = "accepted"
STATE_RUNNING = "running"
ACCEPT_STATES: frozenset[str] = frozenset({STATE_ACCEPTED, STATE_RUNNING})

#: 受理回执给模型的那句话。与 `HINTS` **同一纪律**：纯常量，不夹带任何运行时数据
#: （不含 `{` / 路径 / URL / 数字串）。模型看到的就是这一句，改一个字都是契约变更。
ACCEPT_HINT = (
    "已受理：分析在后台进行，完成后结果会自动发到本会话。"
    "你现在只回一句「已受理，稍后把结果发来」即可——"
    "不要编造内容，也不要再调用 media_analyze。"
)


class ToolError(Exception):
    """受控失败：`code` 进信封，`log_detail` **只**进插件日志（永不进信封）。"""

    def __init__(self, code: str, *, log_detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.log_detail = log_detail

    def __str__(self) -> str:  # 便于测试与日志读起来不刺眼
        return self.code if not self.log_detail else f"{self.code} ({self.log_detail})"


def fail_envelope(code: str) -> dict[str, object]:
    """失败信封：**恰好** `ok` / `error` / `hint` 三键（SPEC §6.2）。

    刻意不带 `elapsed_ms` / `notes` / `kind` / `cached`：信封形状要能被一次断言，
    多一个键就是多一个契约面。未知 code 归一到 `INTERNAL_ERROR`（不留悬空字符串）。
    """
    if code not in HINTS:
        code = ERR_INTERNAL_ERROR
    return {"ok": False, "error": code, "hint": HINTS[code]}


def fail_envelope_wake() -> dict[str, object]:
    """唤醒轮短路的失败信封：**恰好**三键、顺序不变，只有 `hint` 分叉（SPEC-v0.5.1 §5.3）。

    为什么单开一个构造函数而不是给 `fail_envelope` 加 `hint` 形参：失败信封的键集合与
    顺序是契约面（3 键），"哪一种失败用哪句话"只应有一个入口；把它做成显式的第二个
    构造函数，`grep fail_envelope_wake` 就能看清"哪条路径改了文案"，也不会给别的码
    开后门。
    """
    return {"ok": False, "error": ERR_MEDIA_NOT_FOUND, "hint": HINT_MEDIA_NOT_FOUND_WAKE}


def ok_envelope(
    *,
    kind: str,
    duration_s: float,
    transcript: list[dict[str, object]],
    frames: list[dict[str, object]],
    summary: str,
    notes: list[str],
    elapsed_ms: int,
    cached: bool,
) -> dict[str, object]:
    """成功信封：**9 键、顺序固定、缺一不可**（SPEC §6.1）。"""
    return {
        "ok": True,
        "kind": kind,
        "duration_s": duration_s,
        "transcript": transcript,
        "frames": frames,
        "summary": summary,
        "notes": notes,
        "elapsed_ms": int(elapsed_ms),
        "cached": bool(cached),
    }


def accept_envelope(state: str, task_id: str) -> dict[str, object]:
    """受理信封：**恰好** `ok` / `state` / `task_id` / `hint` 四键、顺序固定（SPEC-v0.4 §3.2）。

    与失败信封同一纪律：键集合能被一次断言，任何"顺手多塞一个键"都是契约变更。
    `state` 不在枚举里 ⇒ 归一到 `accepted`（不留悬空字符串，与 `fail_envelope` 同思路）；
    `task_id` 只用于日志与去重，**不进用户可见文本**。
    """
    safe_state = state if state in ACCEPT_STATES else STATE_ACCEPTED
    return {
        "ok": True,
        "state": safe_state,
        "task_id": str(task_id or ""),
        "hint": ACCEPT_HINT,
    }


def dumps(envelope: dict[str, object]) -> str:
    """序列化成**字符串**返回给模型（工具的返回值必须是 `str`）。"""
    return json.dumps(envelope, ensure_ascii=False)
