"""路线 C（核心原生后台）**可选实验模式** + 能力探测（SPEC-v0.4.2 §4–§7 / §11.2）。

26 格，按规格 §11.2 的七组排列：

1. **探测（6）**：未修核心 ⇒ `plugin` + ERROR + 还原；shim 已修 ⇒ `core` + INFO；
   `no_manager` / `no_tool` / `probe_error`（**失败不留半开**）/ 幂等。
2. **置位 / 重放（4）**：只命中自己的工具；重载后 flag 重放；`deactivate` 往返；
   `terminate()` 把引擎退回 `plugin`。
3. **渲染（6）**：不可信前缀 + ≤ 1200；超长截断；失败文本；禁词扫描；
   **默认参数输出与 v0.4.1 逐字节相同**；两个 1200 常量互钉。
4. **互斥（4）**：C 分支不碰 A2 的表/投递（结构性证明）；`background.stats()` 全 0；
   plugin 模式不碰核心 flag；A2 路径逐字回归（受理 4 键 + 投递恰好 1 次）。
5. **异常 / 取消（3）**：`ToolError` ⇒ 文本；未知异常 ⇒ `INTERNAL_ERROR` 且**不泄漏**；
   `CancelledError` ⇒ **上抛**。
6. **复检（2）**：首次调用重跑探测；变 reject ⇒ 本次仍走 C、后续回落 A2；
   复检通过 ⇒ 标志清掉、不再复检。
7. **初始化集成（1）**：`context=None` / 没有 `get_llm_tool_manager` 时 `initialize()`
   不抛、`effective=plugin`、`ready:` 行仍恰好 1 行。

两个"世界"由 `_core_shim` 提供：**未修核心** = 原装 `_PermissionGuardedTool`（不 patch）；
**已修核心** = `patch_core(monkeypatch)` 换成补了一行镜像的子类。后者与核心那行补丁
**逐字等价**，因此本文件既证明"未修时的失败是响亮的"，也证明"修好后探测会 PASS"。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from astrbot.core.provider import func_tool_manager as ftm

from astrbot_plugin_media_analysis import analyze as analyze_mod
from astrbot_plugin_media_analysis import background, core_mode, envelope, main, settings
from astrbot_plugin_media_analysis import cache as cache_mod
from _core_shim import (
    MODULE_PATH,
    ExplodingManager,
    FakeManager,
    is_patched as core_shim_is_patched,
    make_tool,
    patch_core,
    unpatch as core_shim_unpatch,
)
from _fakes import FakeContext, RecordingLogger, make_event
from _rig import build_rig

# --------------------------------------------------------------------------- #
# 夹具与零件
# --------------------------------------------------------------------------- #
_OK_PAYLOAD: dict[str, object] = {
    "ok": True,
    "kind": "video",
    "duration_s": 1.0,
    "transcript": [{"start": 0.0, "end": 1.0, "text": "你好世界"}],
    "frames": [{"at_s": 0.0}],
    "summary": "画面里有人在说话。",
    "notes": ["video_native"],
    "elapsed_ms": 100,
    "cached": False,
}


class _UnpatchedGuardedTool(ftm._PermissionGuardedTool):  # type: ignore[misc,valid-type]
    """核心 4.29.1 的**原装**包装器形态：镜像区**没有** `is_background_task`。

    为什么自己造一个"未修真身"，而不是直接用宿主核心树里的那个类：本机核心树**可能**
    （由部署单/运营方）已经打过那一行镜像补丁 —— 那时"未修世界"在真机上就不存在了，
    这三格会随宿主状态假红/假绿。这里在真类之上把那个**实例属性**摘掉，读数自然回落到
    `FunctionTool.is_background_task` 的类默认 `False`，与 4.29.1 原装行为逐字等价，
    且与宿主树是否被打过补丁**无关**（判据仍是 `wrapper_flag() is False`）。
    """

    def __init__(self, tool, manager) -> None:  # noqa: D107 —— 与父类同签名
        super().__init__(tool, manager)
        self.__dict__.pop("is_background_task", None)


def unpatched_core(monkeypatch) -> None:
    """把"未修核心"这个世界**显式**装好（`monkeypatch` 结束时自动复原）。"""
    monkeypatch.setattr(ftm, "_PermissionGuardedTool", _UnpatchedGuardedTool)


def _log(monkeypatch, module) -> RecordingLogger:
    logger = RecordingLogger()
    monkeypatch.setattr(module, "LOG", logger)
    return logger


def _c_plugin(*, manager=None, config=None) -> main.MediaAnalysisPlugin:
    """造一个**已处于 core 生效态**的插件实例（手工置 `_bg`，不跑探测）。"""
    plugin = main.MediaAnalysisPlugin(
        context=FakeContext(manager=manager), config=config or {}
    )
    plugin._settings_cache = settings.Settings()
    plugin._cache = cache_mod.DiskCache(plugin_name=settings.PLUGIN_NAME, ttl_seconds=0)
    plugin._bg = core_mode.Activation(
        core_mode.MODE_CORE,
        core_mode.MODE_CORE,
        core_mode.REASON_WRAPPER_MIRRORS_RAW,
        True,
        True,
    )
    return plugin


def _patch_analyze(monkeypatch, outcome) -> None:
    """把 `analyze.analyze` 换成固定结果/固定异常（`core_mode` 与测试共享同一模块对象）。"""

    async def _fake(**kwargs):
        if isinstance(outcome, BaseException):
            raise outcome
        return dict(outcome)

    monkeypatch.setattr(analyze_mod, "analyze", _fake)


def _levels(logger: RecordingLogger, level: str) -> list[str]:
    return [message for _level, message in logger.records if _level == level]


def _stub_analyze_calls(monkeypatch) -> list[dict[str, object]]:
    """把 `analyze.analyze` 换成"只记录参数"的假身（短路时它**一次都不该被调用**）。"""
    calls: list[dict[str, object]] = []

    async def _fake(**kwargs):
        calls.append(dict(kwargs))
        return dict(_OK_PAYLOAD)

    monkeypatch.setattr(analyze_mod, "analyze", _fake)
    return calls


def _cron_event():
    """核心 `CronMessageEvent` 的最小同形桩：`get_platform_name()=="cron"`、无消息链。"""
    event = make_event(current=[])
    event.get_platform_name = lambda: "cron"  # type: ignore[assignment]
    return event


# --------------------------------------------------------------------------- #
# 1. 探测（6 格）
# --------------------------------------------------------------------------- #
def test_unpatched_core_is_rejected_loudly_and_rolled_back(monkeypatch) -> None:
    """① 未修核心（**4.29.1 原装形态**，无镜像行）⇒ plugin + ERROR + **flag 已还原**。

    这条是本版的核心安全性质：配置写了 `core`，但核心没补那一行镜像时，插件必须
    **响亮地拒绝**（而不是"看起来正常、实际同步阻塞 30–100 s"）。
    "原装形态"由 `unpatched_core()` **显式**构造（不依赖宿主核心树的补丁状态）。
    """
    unpatched_core(monkeypatch)
    logger = _log(monkeypatch, core_mode)
    tool = make_tool()
    tmgr = FakeManager([tool])

    activation = core_mode.activate(tmgr, requested="core")

    assert activation.requested == core_mode.MODE_CORE
    assert activation.effective == core_mode.MODE_PLUGIN, "未修核心不许生效"
    assert activation.reason == core_mode.REASON_WRAPPER_NO_MIRROR
    assert core_mode.REASONS == (
        "config_plugin",
        "wrapper_mirrors_raw",
        "no_manager",
        "no_tool",
        "no_wrapper",
        "wrapper_no_mirror",
        "probe_error",
    ), "七个 reason 是 SPEC-v0.4.2 §4.2 的冻结枚举"
    assert activation.reason in core_mode.REASONS
    assert activation.raw_flag is True, "探测期间确实置过位"
    assert activation.wrapper_flag is False
    assert tool.is_background_task is False, "失败必须还原，不留半开状态"
    errors = _levels(logger, "ERROR")
    assert len(errors) == 1, "一次加载只打一条 ERROR"
    assert "reason=wrapper_no_mirror" in errors[0]
    assert "已回落 plugin" in errors[0]
    assert _levels(logger, "INFO") == []


def test_patched_core_passes_and_both_flags_are_on(monkeypatch) -> None:
    """② shim 已修（= 核心补了那一行）⇒ core + INFO + 两侧 flag 都为 True。"""
    patch_core(monkeypatch)
    logger = _log(monkeypatch, core_mode)
    tool = make_tool()
    tmgr = FakeManager([tool])

    activation = core_mode.activate(tmgr, requested="core")

    assert activation.requested == core_mode.MODE_CORE
    assert activation.effective == core_mode.MODE_CORE
    assert activation.reason == core_mode.REASON_WRAPPER_MIRRORS_RAW
    assert activation.raw_flag is True and activation.wrapper_flag is True
    assert tool.is_background_task is True
    assert core_mode.wrapper_flag(tmgr) is True
    infos = _levels(logger, "INFO")
    assert len(infos) == 1
    assert "effective=core" in infos[0] and "probe=wrapper_mirrors_raw" in infos[0]
    assert _levels(logger, "ERROR") == []


def test_missing_manager_is_rejected(monkeypatch) -> None:
    """③ 拿不到管理器（`context is None` / 桩环境）⇒ `no_manager` + plugin + ERROR。"""
    logger = _log(monkeypatch, core_mode)

    for tmgr in (None, object()):
        activation = core_mode.activate(tmgr, requested="core")
        assert activation.effective == core_mode.MODE_PLUGIN
        assert activation.reason == core_mode.REASON_NO_MANAGER
        assert activation.raw_flag is False

    errors = _levels(logger, "ERROR")
    assert len(errors) == 2, "每次 activate 各一条（不同加载）"
    assert all("reason=no_manager" in message for message in errors)


def test_missing_tool_is_rejected(monkeypatch) -> None:
    """④ `func_list` 里找不到自己的工具 ⇒ `no_tool` + plugin + ERROR。"""
    logger = _log(monkeypatch, core_mode)
    activation = core_mode.activate(FakeManager([]), requested="core")

    assert activation.effective == core_mode.MODE_PLUGIN
    assert activation.reason == core_mode.REASON_NO_TOOL
    assert "reason=no_tool" in _levels(logger, "ERROR")[0]


def test_probe_error_restores_the_flag(monkeypatch) -> None:
    """⑤ `get_full_tool_set()` 抛 ⇒ `probe_error` + plugin + ERROR + **raw 已还原 False**。

    关键不是"探测失败"，而是"失败后不留半开"：半开的后果是**静默前台阻塞**。
    """
    logger = _log(monkeypatch, core_mode)
    tool = make_tool()
    tmgr = ExplodingManager([tool])

    activation = core_mode.activate(tmgr, requested="core")

    assert activation.effective == core_mode.MODE_PLUGIN
    assert activation.reason == core_mode.REASON_PROBE_ERROR
    assert activation.raw_flag is True
    assert tool.is_background_task is False, "probe_error 也必须还原"
    assert "reason=probe_error" in _levels(logger, "ERROR")[0]


def test_activate_is_idempotent(monkeypatch) -> None:
    """⑥ `activate()` 连跑两次 ⇒ 结果相同、不抛、flag 仍 True（每次加载/复检都会重跑）。"""
    patch_core(monkeypatch)
    monkeypatch.setattr(core_mode, "LOG", RecordingLogger())
    tool = make_tool()
    tmgr = FakeManager([tool])

    first = core_mode.activate(tmgr, requested="core")
    second = core_mode.activate(tmgr, requested="core")

    assert first == second
    assert second.effective == core_mode.MODE_CORE
    assert tool.is_background_task is True

    # 手工逃生门同样可用（没有 `monkeypatch` 的脚本场景）：复原后回到宿主树的包装器
    assert core_shim_is_patched() is True
    core_shim_unpatch()
    assert core_shim_is_patched() is False
    # "未修世界"要**显式**装（宿主核心树可能自己已经带那一行镜像；见 `unpatched_core`）
    unpatched_core(monkeypatch)
    assert core_mode.activate(tmgr, requested="core").reason == core_mode.REASON_WRAPPER_NO_MIRROR


# --------------------------------------------------------------------------- #
# 2. 置位 / 重放（4 格）
# --------------------------------------------------------------------------- #
def test_activation_only_touches_our_own_tool(monkeypatch) -> None:
    """⑦ 只给自己名下的工具置位：**别的插件**的同名 `media_analyze` 保持 False。

    判据是 `handler_module_path`（工具名是全局命名空间，只按名字会误伤别人的工具）。
    这里把"我们的"放在 `func_list` 末位，保证 `get_full_tool_set()` 的**后加载覆盖**
    规则下包装器也是我们的（否则包装器读到的是别人的 False）。
    """
    patch_core(monkeypatch)
    monkeypatch.setattr(core_mode, "LOG", RecordingLogger())
    foreign = make_tool(name="media_analyze", module_path="data.plugins.other_plugin.main")
    ours = make_tool(name="media_analyze", module_path=MODULE_PATH)
    tmgr = FakeManager([foreign, ours])

    activation = core_mode.activate(tmgr, requested="core")

    assert activation.effective == core_mode.MODE_CORE
    assert ours.is_background_task is True
    assert foreign.is_background_task is False, "别人的同名工具不许被置位"
    assert {tool.name for tool in core_mode.locate(tmgr)} == {"media_analyze"}
    assert core_mode.locate(tmgr) == [ours]


def test_flag_is_replayed_after_a_reload(monkeypatch) -> None:
    """⑧ 重载 ⇒ **全新的 `FuncTool` 对象** ⇒ 必须重放 flag（旧对象上的 True 无意义）。"""
    patch_core(monkeypatch)
    monkeypatch.setattr(core_mode, "LOG", RecordingLogger())
    first_tool = make_tool()
    tmgr = FakeManager([first_tool])
    assert core_mode.activate(tmgr, requested="core").effective == core_mode.MODE_CORE
    assert first_tool.is_background_task is True

    # 模拟重载：核心 `add_func` 先 remove_func 同名再加，然后 initialize() 再跑一遍
    second_tool = make_tool()
    tmgr.func_list.clear()
    tmgr.func_list.append(second_tool)

    assert core_mode.activate(tmgr, requested="core").effective == core_mode.MODE_CORE
    assert second_tool.is_background_task is True
    # 旧对象已不在 `func_list` 里 ⇒ `locate()` 找不到它（谁也不会再读它的 flag；
    # 这正是"flag 必须重放"的原因：对象跟着 import 走）
    assert core_mode.locate(tmgr) == [second_tool]


def test_deactivate_then_activate_round_trip(monkeypatch) -> None:
    """⑨ `deactivate()` 幂等置 False；再 `activate()` 恢复 True。"""
    patch_core(monkeypatch)
    monkeypatch.setattr(core_mode, "LOG", RecordingLogger())
    tool = make_tool()
    tmgr = FakeManager([tool])

    assert core_mode.activate(tmgr, requested="core").effective == core_mode.MODE_CORE
    core_mode.deactivate(tmgr)
    core_mode.deactivate(tmgr)  # 幂等
    assert tool.is_background_task is False
    assert core_mode.activate(tmgr, requested="core").effective == core_mode.MODE_CORE
    assert tool.is_background_task is True
    core_mode.deactivate(None)  # 空管理器不抛


@pytest.mark.asyncio
async def test_terminate_returns_the_engine_to_plugin(monkeypatch) -> None:
    """⑩ `initialize()` 进 core，`terminate()` 后 `effective == plugin` 且 flag 归 False。"""
    patch_core(monkeypatch)
    monkeypatch.setattr(core_mode, "LOG", RecordingLogger())
    tool = make_tool()
    tmgr = FakeManager([tool])
    plugin = main.MediaAnalysisPlugin(
        context=FakeContext(manager=tmgr),
        config={"background": {"mode": "core"}, "cache": {"ttl_seconds": 0}},
    )

    await plugin.initialize()
    assert plugin._bg.effective == core_mode.MODE_CORE
    assert plugin._bg_recheck_pending is True
    assert tool.is_background_task is True

    await plugin.terminate()
    assert plugin._bg.effective == core_mode.MODE_PLUGIN
    assert plugin._bg_recheck_pending is False
    assert tool.is_background_task is False


# --------------------------------------------------------------------------- #
# 3. 渲染（6 格）
# --------------------------------------------------------------------------- #
def test_rendered_text_starts_with_the_untrusted_prefix() -> None:
    """⑪ 成功正文 ≤ 1200 且以 `CORE_TEXT_PREFIX + "\\n\\n"` 开头（指令级边界在最前面）。"""
    text = core_mode.render_core(dict(_OK_PAYLOAD))

    assert text.startswith(core_mode.CORE_TEXT_PREFIX + "\n\n")
    assert len(text) <= core_mode.CORE_TEXT_MAX_CHARS
    assert "画面里有人在说话。" in text
    assert "—— 媒体分析：" in text, "页脚与 A2 同源"


def test_long_body_is_truncated_to_the_frozen_limit() -> None:
    """⑫ 超长正文 ⇒ 截断 + `…`，总长**恰好** ≤ 1200（用 5000 字 summary）。"""
    payload = dict(_OK_PAYLOAD)
    payload["summary"] = "长" * 5000

    text = core_mode.render_core(payload)

    assert len(text) <= core_mode.CORE_TEXT_MAX_CHARS
    assert "…" in text
    assert "长" * core_mode.CORE_BODY_MAX in text
    assert "长" * (core_mode.CORE_BODY_MAX + 1) not in text
    assert "—— 媒体分析：" in text, "页脚必须留下（截断只切正文）"


def test_failure_envelope_renders_code_and_the_constant_hint() -> None:
    """⑬ 失败信封 ⇒ `媒体分析失败：CODE。` + `envelope.HINTS` 的**常量** hint。"""
    payload = envelope.fail_envelope(envelope.ERR_MEDIA_NOT_FOUND)

    text = core_mode.render_core(payload)

    assert text.startswith(core_mode.CORE_TEXT_PREFIX + "\n\n")
    assert "媒体分析失败：MEDIA_NOT_FOUND。" in text
    assert envelope.HINTS[envelope.ERR_MEDIA_NOT_FOUND] in text


def test_rendered_text_never_leaks_paths_urls_or_media_tokens() -> None:
    """⑭ **禁词扫描**：返回体不含路径 / URL / `data:` / 媒体后缀 / `base64`。

    C 模式的正文会进唤醒轮的 **system prompt**（不是用户消息）⇒ 它是"只交付文本"的
    边界（SPEC-v0.4.2 §9.2）。中间产物（/tmp 的帧、压缩产物、wav）**从不外发**。
    """
    forbidden = ("/tmp/", "data:", "http", ".mp4", ".wav", ".jpg", "base64")
    payloads = [dict(_OK_PAYLOAD), envelope.fail_envelope(envelope.ERR_VIDEO_TOO_LARGE)]

    for payload in payloads:
        text = core_mode.render_core(payload)
        for token in forbidden:
            assert token not in text, f"返回体里出现了 {token!r}：{text[:120]}"


def test_default_render_output_is_byte_identical_to_v041() -> None:
    """⑮ `background.render(payload)`（**默认参数**）与 v0.4.1 逐字节相同。

    这是"给 `render` 加 `max_chars` 不改变 A2 输出"的机械证明：同一 payload 双跑，
    再与冻结字面量比对（后者防"两处一起漂"）。
    """
    payload = dict(_OK_PAYLOAD)
    expected = (
        "画面里有人在说话。\n"
        "—— 媒体分析：视频｜时长 1.00s｜画面帧 1 张｜转写 4 字｜耗时 0.1s"
    )

    assert background.render(payload) == expected
    assert background.render(payload) == background.render(
        payload, max_chars=background.DELIVERY_MAX_CHARS
    )
    # A2 的 1200 截断语义不变（只截正文，不截页脚）
    long_payload = dict(payload, summary="长" * 2000)
    rendered = background.render(long_payload)
    assert rendered.startswith("长" * background.DELIVERY_MAX_CHARS + "…\n—— 媒体分析：")


def test_core_text_limit_matches_the_delivery_limit() -> None:
    """⑯ `CORE_TEXT_MAX_CHARS == background.DELIVERY_MAX_CHARS`，且 `CORE_BODY_MAX` 是公式。"""
    assert core_mode.CORE_TEXT_MAX_CHARS == background.DELIVERY_MAX_CHARS
    assert core_mode.CORE_BODY_MAX == (
        core_mode.CORE_TEXT_MAX_CHARS
        - len(core_mode.CORE_TEXT_PREFIX)
        - 2
        - core_mode.CORE_TEXT_FOOTER_RESERVE
        - 1
    )
    assert core_mode.CORE_BODY_MAX == 982, "公式的当前取值（换前缀/预留时要一起看）"
    assert 40 <= len(core_mode.CORE_TEXT_PREFIX) <= 80


# --------------------------------------------------------------------------- #
# 4. 互斥（4 格）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_core_mode_never_touches_the_a2_tables(monkeypatch) -> None:
    """⑰ **结构性证明**：把 `background.submit` / `_deliver` 换成抛错，C 模式照样跑通。

    这不是"代码里没写"，而是"运行期调不到"：C 分支只碰 `core_mode.run`，而 `core_mode`
    只从 `background` 借了 `render`。
    """
    _patch_analyze(monkeypatch, _OK_PAYLOAD)
    monkeypatch.setattr(core_mode, "LOG", RecordingLogger())

    def _boom(*args, **kwargs):  # pragma: no cover —— 被调用即失败
        raise AssertionError("C 模式碰到了 A2 的受理/投递面")

    monkeypatch.setattr(background, "submit", _boom)
    monkeypatch.setattr(background, "_deliver", _boom)
    plugin = _c_plugin()

    text = await plugin.media_analyze(make_event(current=[]), target="current")

    assert isinstance(text, str) and text.startswith(core_mode.CORE_TEXT_PREFIX)
    assert "画面里有人在说话。" in text
    # spy：C 模式下插件**一次都没**调 `send_message`（交付归唤醒轮模型，不是我们）
    assert len(plugin.context.calls.named("send_message")) == 0


@pytest.mark.asyncio
async def test_core_mode_leaves_background_stats_at_zero(monkeypatch) -> None:
    """⑱ C 模式跑完 `background.stats()` 全 0（**不建表**＝没有第二个 owner）。"""
    await background.cancel_all()
    _patch_analyze(monkeypatch, _OK_PAYLOAD)
    monkeypatch.setattr(core_mode, "LOG", RecordingLogger())
    plugin = _c_plugin()

    await plugin.media_analyze(make_event(current=[]), target="current")

    assert background.stats() == {"tasks": 0, "sessions": 0, "dedupe": 0}


def test_plugin_mode_leaves_the_core_flag_off(monkeypatch) -> None:
    """⑲ plugin 模式**不碰核心**：flag 恒 False（含"上次 core、这次重载成 plugin"的复放）。"""
    patch_core(monkeypatch)
    logger = _log(monkeypatch, core_mode)
    tool = make_tool()
    tmgr = FakeManager([tool])

    activation = core_mode.activate(tmgr, requested="plugin")
    assert activation.effective == core_mode.MODE_PLUGIN
    assert activation.reason == core_mode.REASON_CONFIG_PLUGIN
    assert tool.is_background_task is False

    # 上次是 core
    core_mode.activate(tmgr, requested="core")
    assert tool.is_background_task is True
    # 这次重载成 plugin ⇒ 必须还原
    assert core_mode.activate(tmgr, requested="plugin").effective == core_mode.MODE_PLUGIN
    assert tool.is_background_task is False

    # 默认模式零噪音：plugin 只打 DEBUG；唯一的那条 INFO 属于中间那次 core 激活
    infos = _levels(logger, "INFO")
    assert len(infos) == 1 and "effective=core" in infos[0]
    assert _levels(logger, "ERROR") == []
    assert len(_levels(logger, "DEBUG")) == 2, "两次 plugin 激活各一条 DEBUG"


@pytest.mark.asyncio
async def test_plugin_mode_still_returns_the_accept_envelope_and_delivers_once(monkeypatch, tmp_path) -> None:
    """⑳ **A2 路径逐字回归**：受理 4 键 + `send_message` 恰好 1 次（默认模式没被改）。"""
    rig = build_rig(monkeypatch, tmp_path, kind="audio")

    raw = await rig.plugin.media_analyze(rig.event, target="current")
    payload = json.loads(raw)
    assert set(payload) == set(envelope.ACCEPT_KEYS), payload
    finished = await background.wait(str(payload["task_id"]), timeout=60.0)
    assert finished is not None and finished["ok"] is True
    assert len(rig.context.calls.named("send_message")) == 1


# --------------------------------------------------------------------------- #
# 5. 异常 / 取消（3 格）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_tool_error_returns_text_and_logs_rejected(monkeypatch) -> None:
    """㉑ `analyze` 抛 `ToolError` ⇒ 返回文本（**不抛**）+ INFO `rejected`。"""
    logger = _log(monkeypatch, core_mode)
    _patch_analyze(
        monkeypatch,
        envelope.ToolError(envelope.ERR_MEDIA_TOO_LONG, log_detail="duration=999.0"),
    )
    plugin = _c_plugin()

    text = await plugin.media_analyze(make_event(current=[]), target="current")

    assert "媒体分析失败：MEDIA_TOO_LONG。" in text
    assert envelope.HINTS[envelope.ERR_MEDIA_TOO_LONG] in text
    rejected = [message for message in _levels(logger, "INFO") if "rejected" in message]
    assert len(rejected) == 1 and "code=MEDIA_TOO_LONG" in rejected[0]


@pytest.mark.asyncio
async def test_unknown_exception_becomes_internal_error_without_leaking_text(monkeypatch) -> None:
    """㉒ 未知异常 ⇒ `INTERNAL_ERROR` 文本；ERROR **只打类名**（不打 str(exc)/traceback）。

    为什么这么严：核心会把异常吞成 `"error: … internal error: <e!s>"` 再拼进唤醒轮的
    system prompt（SPEC-v0.4.2 §6.3）—— 那等于把异常正文塞进 system prompt。
    """
    logger = _log(monkeypatch, core_mode)
    marker = "SECRET-TOKEN-SHOULD-NOT-APPEAR"
    _patch_analyze(monkeypatch, RuntimeError(marker))
    plugin = _c_plugin()

    text = await plugin.media_analyze(make_event(current=[]), target="current")

    assert "媒体分析失败：INTERNAL_ERROR。" in text
    assert marker not in text
    errors = _levels(logger, "ERROR")
    assert len(errors) == 1 and "RuntimeError" in errors[0]
    assert marker not in errors[0]
    assert "Traceback" not in errors[0]


@pytest.mark.asyncio
async def test_cancelled_error_is_re_raised(monkeypatch) -> None:
    """㉓ `CancelledError` ⇒ **上抛**（核心不会接住它 ⇒ 任务取消 ⇒ 不唤醒、不投递）。"""
    logger = _log(monkeypatch, core_mode)
    _patch_analyze(monkeypatch, asyncio.CancelledError())
    plugin = _c_plugin()

    with pytest.raises(asyncio.CancelledError):
        await plugin.media_analyze(make_event(current=[]), target="current")

    assert any("core-mode call cancelled" in message for message in _levels(logger, "INFO"))
    assert _levels(logger, "ERROR") == []


# --------------------------------------------------------------------------- #
# 6. 复检（2 格）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_first_call_recheck_demotes_later_calls_to_a2(monkeypatch) -> None:
    """㉔ 复检变 reject ⇒ **本次仍走 C**（入口快照）、**后续回落 A2**、ERROR 一条。

    本次立刻改走 A2 会造成"核心把它当后台任务 + 我们又回受理信封"⇒ 受理文案进唤醒轮
    system prompt，紧接 A2 再发真实结果（SPEC-v0.4.2 §4.5）。
    """
    _patch_analyze(monkeypatch, _OK_PAYLOAD)
    logger = _log(monkeypatch, core_mode)
    unpatched_core(monkeypatch)  # 显式"未修核心"世界 ⇒ 复检会判 wrapper_no_mirror
    tool = make_tool()
    tmgr = FakeManager([tool])
    plugin = _c_plugin(manager=tmgr)
    plugin._bg_recheck_pending = True

    first = await plugin.media_analyze(make_event(current=[]), target="current")
    assert first.startswith(core_mode.CORE_TEXT_PREFIX), "本次仍按 C 跑"
    assert plugin._bg.effective == core_mode.MODE_PLUGIN, "复检只影响后续"
    assert plugin._bg.reason == core_mode.REASON_WRAPPER_NO_MIRROR
    assert tool.is_background_task is False
    assert len(_levels(logger, "ERROR")) == 1

    second = await plugin.media_analyze(make_event(current=[]), target="current")
    assert set(json.loads(second)) == set(envelope.FAIL_KEYS), "后续应走 A2（同步失败信封）"
    assert json.loads(second)["error"] == envelope.ERR_MEDIA_NOT_FOUND


@pytest.mark.asyncio
async def test_recheck_runs_once_and_is_cleared_on_pass(monkeypatch) -> None:
    """㉕ 复检 PASS ⇒ 标志被清、不再复检（第二次调用**不调** `activate`，spy 计数）。"""
    _patch_analyze(monkeypatch, _OK_PAYLOAD)
    monkeypatch.setattr(core_mode, "LOG", RecordingLogger())
    calls: list[str] = []
    real_activate = core_mode.activate

    def _spy(tmgr, *, requested):
        calls.append(requested)
        return real_activate(tmgr, requested=requested)

    monkeypatch.setattr(core_mode, "activate", _spy)
    patch_core(monkeypatch)
    tool = make_tool()
    plugin = _c_plugin(manager=FakeManager([tool]))
    plugin._bg_recheck_pending = True

    await plugin.media_analyze(make_event(current=[]), target="current")
    assert calls == [core_mode.MODE_CORE]
    assert plugin._bg_recheck_pending is False
    assert plugin._bg.effective == core_mode.MODE_CORE
    assert tool.is_background_task is True

    await plugin.media_analyze(make_event(current=[]), target="current")
    assert calls == [core_mode.MODE_CORE], "复检只做一次"


# --------------------------------------------------------------------------- #
# 7. 初始化集成（1 格）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_initialize_never_throws_when_the_manager_is_missing(monkeypatch) -> None:
    """㉖ `context=None` / 没有 `get_llm_tool_manager` ⇒ 不抛、plugin、`ready:` 仍 1 行。

    这条守住的是"加载期探测绝不把插件加载带崩"：`initialize()` 里任何异常都会让插件
    变成**静默拒载**（目录在、工具不注册、界面上像"没装"）。
    """
    logger = _log(monkeypatch, main)
    config = {"background": {"mode": "core"}, "cache": {"ttl_seconds": 0}}

    for context in (None, FakeContext(manager=None)):
        plugin = main.MediaAnalysisPlugin(context=context, config=config)
        await plugin.initialize()
        assert plugin._bg.effective == core_mode.MODE_PLUGIN
        assert plugin._bg.reason == core_mode.REASON_NO_MANAGER
        await plugin.terminate()

    ready = [message for message in _levels(logger, "INFO") if "ready:" in message]
    assert len(ready) == 2, "每次 initialize() 恰好一行 ready:"


# --------------------------------------------------------------------------- #
# 8. v0.5.1：C7 `route=` 行 + `engine=` 传递 + D2 唤醒轮短路（SPEC-v0.5.1 §5 / §6）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_c_passes_engine_core_to_analyze_and_emits_one_route_line(
    monkeypatch, tmp_path
) -> None:
    """㉘ C 分支**显式**传 `engine="core"`；且 `route=` 行由 `analyze` 打、`stage=analyze`。

    断言的是**真的链路**（不是桩）：`build_rig` 的真 `analyze` 会打自己那行
    `route=… engine=core … stage=analyze`，这正是"引擎名不靠 `settings` 反查"的证据。
    """
    rig = build_rig(monkeypatch, tmp_path, kind="audio")
    logger = RecordingLogger()
    monkeypatch.setattr(analyze_mod, "LOG", logger)

    text = await core_mode.run(
        context=rig.context,
        event=rig.event,
        target="current",
        focus="",
        include_frames=False,
        settings=rig.settings,
        cache=rig.cache,
    )

    assert text.startswith(core_mode.CORE_TEXT_PREFIX)
    route_lines = [
        message
        for level, message in logger.records
        if level == "INFO" and message.startswith("media_analysis: route=")
    ]
    assert len(route_lines) == 1, route_lines
    assert "stage=analyze engine=core" in route_lines[0], route_lines[0]
    assert "route=audio" in route_lines[0]


@pytest.mark.asyncio
async def test_c_wake_round_short_circuits_with_the_wake_hint(monkeypatch) -> None:
    """D2 第 4 条：C 分支在 `analyze` **之前**短路，回同一条专用 hint 的**文本**。

    C 模式下"不提交后台任务"是结构性成立的（C 根本没有任务表）⇒ 判据只能看
    "没跑链路"（`analyze` 未被调用）+ 文本里带唤醒轮 hint + 一行 `stage=accept`。
    """
    logger = _log(monkeypatch, core_mode)
    calls = _stub_analyze_calls(monkeypatch)
    plugin = _c_plugin()

    text = await plugin.media_analyze(_cron_event(), target="current")

    assert calls == [], "短路必须发生在 analyze 之前"
    assert core_mode.CORE_TEXT_PREFIX in text
    assert envelope.HINT_MEDIA_NOT_FOUND_WAKE in text
    assert envelope.HINTS[envelope.ERR_MEDIA_NOT_FOUND] not in text
    done = [message for message in _levels(logger, "INFO") if "core-mode done" in message]
    assert done and "wake_round=1" in done[0]


@pytest.mark.asyncio
async def test_c_mode_never_prints_delivered_or_a2_lines(monkeypatch, tmp_path) -> None:
    """§6.4：C 模式下**任何行**都没有 `delivered=`，也没有 A2 专属的 `bg accepted/finished`。

    这是 v0.4.2 冻结的互斥判据在**日志面**的对偶：谁投递决定谁打 `delivered=`，
    而 C 的投递归唤醒轮的模型（`send_message_to_user`）。
    """
    rig = build_rig(monkeypatch, tmp_path, kind="audio")
    core_log = RecordingLogger()
    bg_log = RecordingLogger()
    monkeypatch.setattr(core_mode, "LOG", core_log)
    monkeypatch.setattr(background, "LOG", bg_log, raising=False)

    await core_mode.run(
        context=rig.context,
        event=rig.event,
        target="current",
        focus="",
        include_frames=False,
        settings=rig.settings,
        cache=rig.cache,
    )

    joined = "\n".join(
        message for _lvl, message in core_log.records + bg_log.records
    )
    assert "delivered=" not in joined
    assert "bg accepted" not in joined
    assert "bg finished" not in joined
    assert "core-mode done" in joined
