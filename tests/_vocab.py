"""`notes` 词表（v0.3 的 41 条 + **v0.4 的 3 条** = **44** 条）—— 主链路测试共用的唯一副本。

为什么单独一份：v0.3 把 notes 从 20 条扩到 39 条，而"词表是封闭的"这条守卫要在**两个**
主链路测试文件里都成立（`test_analyze.py` 守 v0.2 语义骨架、`test_video_chain.py` 守
v0.3 的原生/压缩/回退链）。各抄一份的结果必然漂：一边加了新词、另一边还按旧词表断言，
于是"新分支的 note 不合规"只在某个文件里被抓到。

词表本身**仍是实现的事实的照抄**（`analyze.py` + `probe.py` 的 `notes.append(...)`），
它在这里被写死成字面量，是为了让"多出一个没评审过的词"变成红灯 —— 不是为了让实现自证。

41 条 = v0.2 的 20 条（逐字不变）+ v0.3 的 19 条 + 评审的 2 条：
- F7 `video_unavailable_no_provider`（`auto` 且未配视频模型 ⇒ 不抽帧、只转写）；
- R1 `audio_timeout`（要画面时音轨超时只降级：记这条、继续走画面）。

v0.4 再加 3 条（SPEC-v0.4 §7.2，只加不改）⇒ **44** 条：`source:gif` /
`gif_converted:<in>-><out>` / `gif_static`。

**纪律（v0.4 起）：notes 必须"缓存中立"** —— 不得因"这次是后台任务/第几次调用"而变。
理由：信封整体入缓存，一条"后台"标记会被缓存复用给别的调用；任务生命周期信息
**只进日志**（SPEC-v0.4 §7.2 / §4）。
"""

from __future__ import annotations

import re

#: v0.2 的 20 条（`SPEC-v0.2.md` §8.2，逐字不变）
V02_NOTES: frozenset[str] = frozenset(
    {
        "audio_provider",
        "vision_provider",
        "source",
        "audio_extracted",
        "audio_normalized",
        "audio_empty",
        "no_audio_track",
        "frames_skipped_audio_only",
        "frames_truncated",
        "frame_extract_failed",
        "frame_desc_failed",
        "summary_skipped_budget",
        "summary_failed",
        "focus_trimmed",
        "prompt_trimmed",
        "focus_appended",
        "material_trimmed",
        "cache_hit",
        "cache_age_s",
        "file_downloaded",
    }
)

#: v0.3 新增 19 条（`SPEC-v0.3.md` §11.2）+ 评审 F7 的 1 条
V03_NOTES: frozenset[str] = frozenset(
    {
        # 评审 F7：`mode=auto` 且未配视频模型 ⇒ 不抽帧（成本回归修复）
        "video_unavailable_no_provider",
        # 评审 R1：要画面时音轨超时 ⇒ 只降级（不判死整次调用）
        "audio_timeout",
        "video_provider",
        "video_asserted",
        "video_probe_cached",
        "video_probe_l1_ok",
        "video_probe_l1_failed",
        "video_probe_disabled",
        "video_native_unavailable",
        "video_native",
        "video_native_failed",
        "video_native_empty",
        "video_compressed",
        "video_compress_skipped",
        "video_compress_failed",
        "video_compress_infeasible",
        "video_compressed_noaudio",
        "video_fallback_frames",
        "video_frames_forbidden",
        "video_no_vision_provider",
        "video_skip_budget",
    }
)

#: v0.4 新增 **2 个词**（`SPEC-v0.4.md` §7.2 新增 3 条 note 形态，其中 `source:gif` 是
#: 既有词 `source` 的**带参形态**，与 `source:file` 同构 ⇒ 不需要新词）：
#: `gif_converted:<in>-><out>`（归一字节）与 `gif_static`（源 GIF 单帧）。
V04_NOTES: frozenset[str] = frozenset({"gif_converted", "gif_static"})

NOTE_WORDS: frozenset[str] = V02_NOTES | V03_NOTES | V04_NOTES

#: 带参形态：`<word>:<param>`（`audio_provider:<id>` / `video_compressed:<from>-><to>` /
#: `cache_age_s:<n>` / `frames_truncated:<kept>/<want>` …）。参数里**不许**有空白或换行。
NOTE_PATTERN = re.compile(
    r"^(?:" + "|".join(sorted(NOTE_WORDS, key=len, reverse=True)) + r")(?::\S+)?$"
)


def split_note(note: str) -> tuple[str, str]:
    """`note` → `(词, 参数)`；无参数时参数是 `""`。"""
    word, _, param = note.partition(":")
    return word, param


def bad_notes(notes: list[str]) -> list[str]:
    """返回不合规的 note 列表（空列表 = 全部合规）。"""
    return [note for note in notes if not NOTE_PATTERN.match(note)]
