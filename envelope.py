"""错误码 / 静态提示 / JSON 信封 / `ToolError`。

本模块是**叶子**：不 import 任何其它包内模块（谁都依赖它，它不依赖谁）。
16 条错误码与 16 条 `hint` 是 SPEC §5 表的**唯一事实源**（表是它的视图）：
`hint` 必须是**常量字符串**，禁止拼接运行时数据（路径、异常文本、时长、provider id）
——信封会被框架按 INFO 全量写进生产日志（`tool_loop_agent_runner.py:1439`），
hint 是唯一可能夹带运行时数据的位置，钉成常量就整条链干净。
"""

from __future__ import annotations

import json

ERR_BAD_TARGET = "BAD_TARGET"
ERR_MEDIA_NOT_FOUND = "MEDIA_NOT_FOUND"
ERR_MEDIA_UNRESOLVABLE = "MEDIA_UNRESOLVABLE"
ERR_PROBE_FAILED = "PROBE_FAILED"
ERR_MEDIA_TOO_LONG = "MEDIA_TOO_LONG"
ERR_NO_AUDIO_TRACK = "NO_AUDIO_TRACK"
ERR_NO_VIDEO_TRACK = "NO_VIDEO_TRACK"
ERR_AUDIO_EXTRACT_FAILED = "AUDIO_EXTRACT_FAILED"
ERR_FRAME_EXTRACT_FAILED = "FRAME_EXTRACT_FAILED"
ERR_STT_UNAVAILABLE = "STT_UNAVAILABLE"
ERR_STT_FAILED = "STT_FAILED"
ERR_PROVIDER_NOT_FOUND = "PROVIDER_NOT_FOUND"
ERR_PROVIDER_CAPABILITY_MISMATCH = "PROVIDER_CAPABILITY_MISMATCH"
ERR_VISION_UNAVAILABLE = "VISION_UNAVAILABLE"
ERR_VISION_FAILED = "VISION_FAILED"
ERR_INTERNAL_ERROR = "INTERNAL_ERROR"

# 唯一事实源：16 条逐字静态文案（SPEC §5 是它的视图，C9 逐字比对）。
HINTS: dict[str, str] = {
    "BAD_TARGET": "target 只接受 current 或 quoted。",
    "MEDIA_NOT_FOUND": "这条消息或它引用的消息里没有语音/视频；请用户直接发送或引用一条语音/视频。",
    "MEDIA_UNRESOLVABLE": "媒体组件在，但取不到本地文件（可能已被清理，或平台没给可用地址）。",
    "PROBE_FAILED": "读不出媒体时长（ffprobe 失败），无法判定上限也无法抽帧。",
    "MEDIA_TOO_LONG": "媒体时长超过配置上限；请用户发更短的片段，或让管理员调高 limits.max_media_seconds。",
    "NO_AUDIO_TRACK": "这条视频没有音轨；把 include_frames 设为 true 改走抽帧。",
    "NO_VIDEO_TRACK": "这条视频没有视频流；把 include_frames 设为 false 只做转写。",
    "AUDIO_EXTRACT_FAILED": "从视频抽音轨失败（转码错误）；请重试或让用户换一条媒体。",
    "FRAME_EXTRACT_FAILED": "抽帧全部失败；请重试或让用户换一条媒体。",
    "STT_UNAVAILABLE": "没有可用的语音识别 provider；请在插件配置里指定 stt_provider_id，或在后台添加 speech_to_text 类型的 provider。",
    "STT_FAILED": "语音识别调用失败或超时；请稍后重试。",
    "PROVIDER_NOT_FOUND": "配置里的 provider id 在 AstrBot 中不存在；请让管理员在插件配置里重新选择。",
    "PROVIDER_CAPABILITY_MISMATCH": "所选 provider 不支持该能力（语音识别 / 图像输入）；请让管理员改选，或在后台给该 provider 的「模型能力」勾上「图像」。",
    "VISION_UNAVAILABLE": "没有可用的对话 provider 来读图；请配置 vision_provider_id。",
    "VISION_FAILED": "视觉模型调用全部失败或超时；请稍后重试。",
    "INTERNAL_ERROR": "插件内部错误；已记录日志，请让管理员查看插件日志。",
}

# 成功信封的 9 个键，**顺序固定**（SPEC §6.1）；失败信封恰好 3 键（§6.2）。
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

    刻意不带 `elapsed_ms` / `notes` / `kind`：信封形状要能被一次断言，
    多一个键就是多一个契约面。未知 code 归一到 `INTERNAL_ERROR`（不留悬空字符串）。
    """
    if code not in HINTS:
        code = ERR_INTERNAL_ERROR
    return {"ok": False, "error": code, "hint": HINTS[code]}


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


def dumps(envelope: dict[str, object]) -> str:
    """序列化成**字符串**返回给模型（工具的返回值必须是 `str`）。"""
    return json.dumps(envelope, ensure_ascii=False)
