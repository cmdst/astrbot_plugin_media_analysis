"""`@filter.on_llm_request` 注入钩子的**执行级**断言（SPEC-v0.5.2 §5.4–§5.7）。

为什么这个文件不放在 `test_result_context.py` 里：那个文件守**纯逻辑**（不碰 astrbot），
这个文件守**接线**（真调 `plugin._inject_media_results(event, req)`，用假 event/req）。

**禁止**的写法（§8.2）：`assert "req.system_prompt" in inspect.getsource(hook)` ——
注释里出现该串就假绿。这里的每一条都造真对象、真 `await` 钩子、只看对象状态与日志。

三条"不写"断言（B1/B2/B3）用**同一性 + 快照**双判据：`is` 抓"换了个对象"，`==` 抓
"内容被就地改"，两者缺一都会漏（就地 append 只有 `==` 抓得到，重新赋值只有 `is` 抓得到）。
"""

from __future__ import annotations

import types
from typing import Any

import pytest

from astrbot_plugin_media_analysis import main, result_context as rc
from _fakes import FakeEvent, RecordingLogger


@pytest.fixture(autouse=True)
def clean_pending():
    """待注入表是**进程级**状态：前后都清，避免用例互相串味。"""
    rc.clear()
    yield
    rc.clear()


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> RecordingLogger:
    """把 `main.LOG` 换成记录器（钩子的 INFO / WARNING 都走它）。"""
    recorder = RecordingLogger()
    monkeypatch.setattr(main, "LOG", recorder)
    return recorder


class FakeRequest:
    """最小 `ProviderRequest`：只具备本插件真正会读写的四个面。

    `conversation.history` 刻意用 `SimpleNamespace` 并在断言里比**同一对象** ——
    核心那个字段是**落库**字段（`astrbot.core.db.po.Conversation.history`），
    我们连碰都不该碰（§5.6 B1）。
    """

    def __init__(
        self,
        *,
        prompt: str = "刚才那个视频里说了什么",
        system_prompt: Any = "",
        contexts: list[dict] | None = None,
        history: str = "[原始历史]",
    ) -> None:
        self.prompt = prompt
        self.system_prompt = system_prompt
        self.contexts = [] if contexts is None else contexts
        self.conversation = types.SimpleNamespace(history=history)


def _plugin() -> main.MediaAnalysisPlugin:
    return main.MediaAnalysisPlugin(context=None, config={})


async def _hook(plugin: main.MediaAnalysisPlugin, event: Any, req: Any) -> None:
    """真调钩子（一次 `await`）。"""
    await plugin._inject_media_results(event, req)


def _warnings(log: RecordingLogger) -> list[str]:
    return [message for level, message in log.records if level == "WARNING"]


def _errors(log: RecordingLogger) -> list[str]:
    return [message for level, message in log.records if level == "ERROR"]


# --------------------------------------------------------------------------- #
# A1 一次性
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_hook_injects_once_into_system_prompt(log: RecordingLogger) -> None:
    """A1：`remember` 后第一轮注入 —— 抬头 + 正文都进 `system_prompt`。"""
    rc.remember("aiocqhttp:GroupMessage:1000", "视频里报的数字是 8093")
    req = FakeRequest(system_prompt="你是助手。")
    event = FakeEvent(umo="aiocqhttp:GroupMessage:1000")

    await _hook(_plugin(), event, req)

    assert req.system_prompt.startswith("你是助手。\n\n"), "注入必须追加在原 system_prompt 之后"
    assert rc._HEADER in req.system_prompt
    assert "视频里报的数字是 8093" in req.system_prompt
    assert req.system_prompt.endswith("视频里报的数字是 8093")


@pytest.mark.asyncio
async def test_hook_second_request_gets_nothing(log: RecordingLogger) -> None:
    """A1 后半：同 UMO 的**第二轮**请求 ⇒ `system_prompt` 逐字不变（一次性）。"""
    rc.remember("aiocqhttp:GroupMessage:1000", "结果正文")
    plugin = _plugin()
    event = FakeEvent(umo="aiocqhttp:GroupMessage:1000")

    first = FakeRequest(system_prompt="")
    await _hook(plugin, event, first)
    assert rc._HEADER in first.system_prompt

    second = FakeRequest(system_prompt="")
    await _hook(plugin, event, second)
    assert second.system_prompt == "", "第二轮不许再注入（一次性）"


@pytest.mark.asyncio
async def test_hook_without_pending_leaves_system_prompt_untouched(
    log: RecordingLogger,
) -> None:
    """无事可注入 ⇒ 一个字符都不动，且**不打日志**（§5.7：这种情况没有日志行）。"""
    req = FakeRequest(system_prompt="原生系统提示词")
    await _hook(_plugin(), FakeEvent(), req)
    assert req.system_prompt == "原生系统提示词"
    assert log.records == []


# --------------------------------------------------------------------------- #
# A7 system_prompt=None
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_hook_handles_none_system_prompt(log: RecordingLogger) -> None:
    """A7：`system_prompt=None` ⇒ 不抛，结果恰好是 `"\\n\\n" + 抬头 + "\\n" + 正文`。"""
    rc.remember("aiocqhttp:GroupMessage:1000", "正文")
    req = FakeRequest(system_prompt=None)

    await _hook(_plugin(), FakeEvent(), req)

    assert req.system_prompt == "\n\n" + rc._HEADER + "\n" + "正文"


# --------------------------------------------------------------------------- #
# A3 跨 UMO 隔离
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_hook_isolates_by_umo(log: RecordingLogger) -> None:
    """A3：A 会话的结果**绝不**注入到 B 会话（`unified_msg_origin` 是唯一隔离键）。"""
    rc.remember("aiocqhttp:GroupMessage:1000", "A 群的结果")
    plugin = _plugin()

    other = FakeRequest(system_prompt="")
    await _hook(plugin, FakeEvent(umo="aiocqhttp:GroupMessage:2000"), other)
    assert other.system_prompt == ""

    same = FakeRequest(system_prompt="")
    await _hook(plugin, FakeEvent(umo="aiocqhttp:GroupMessage:1000"), same)
    assert "A 群的结果" in same.system_prompt


# --------------------------------------------------------------------------- #
# A4 用户引用原文 ⇒ 不注入；A5 两条上限；A6 截断
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_hook_skips_when_the_prompt_quotes_the_text(log: RecordingLogger) -> None:
    """A4：用户把结果原文贴进 prompt ⇒ 不注入（他已经看得见）。"""
    rc.remember("aiocqhttp:GroupMessage:1000", "数字是 8093")
    req = FakeRequest(prompt="数字是 8093 是什么意思", system_prompt="")

    await _hook(_plugin(), FakeEvent(), req)

    assert req.system_prompt == ""
    assert log.records == []


@pytest.mark.asyncio
async def test_hook_injects_both_entries_under_a_single_header(
    log: RecordingLogger,
) -> None:
    """A5 + §5.4：两条结果共用**一个**抬头，正文按插入序用空行分隔。"""
    rc.remember("aiocqhttp:GroupMessage:1000", "第一条")
    rc.remember("aiocqhttp:GroupMessage:1000", "第二条")
    req = FakeRequest(system_prompt="")

    await _hook(_plugin(), FakeEvent(), req)

    assert req.system_prompt.count(rc._HEADER) == 1, "抬头每轮只出现一次"
    assert req.system_prompt.endswith(rc._HEADER + "\n" + "第一条\n\n第二条")


@pytest.mark.asyncio
async def test_hook_truncates_the_block_to_max_chars(log: RecordingLogger) -> None:
    """A6：正文块按 `_MAX_CHARS` 截断（**抬头不计入** ⇒ 整串长度可以 > 2400）。"""
    first = "甲" * 2000
    second = "乙" * 2000
    rc.remember("aiocqhttp:GroupMessage:1000", first)
    rc.remember("aiocqhttp:GroupMessage:1000", second)
    req = FakeRequest(system_prompt="")

    await _hook(_plugin(), FakeEvent(), req)

    expected_block = "\n\n".join([first, second])[: rc._MAX_CHARS]
    assert req.system_prompt.endswith(expected_block)
    assert len(expected_block) == rc._MAX_CHARS
    assert len(req.system_prompt) == len("\n\n" + rc._HEADER + "\n") + rc._MAX_CHARS
    assert len(req.system_prompt) > rc._MAX_CHARS, "抬头不计入 2400（越界是**正确**的）"


# --------------------------------------------------------------------------- #
# B1 / B2 / B3 —— 三条"不写"
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_hook_does_not_touch_prompt_contexts_or_conversation_history(
    log: RecordingLogger,
) -> None:
    """B1/B2/B3：`prompt`、`contexts`（同一列表 + 内容）、`conversation.history` 全不动。

    `contexts` 用"同一对象 + 快照相等"双判据；`conversation` 也比同一对象 ——
    换掉整个 `conversation` 同样是回归（核心会拿它落库）。
    """
    rc.remember("aiocqhttp:GroupMessage:1000", "结果正文")
    contexts = [{"role": "user", "content": "上一轮"}]
    req = FakeRequest(prompt="本轮用户问题", contexts=contexts, history="[原始历史]")
    before_contexts = list(contexts)
    before_conversation = req.conversation
    before_history = req.conversation.history

    await _hook(_plugin(), FakeEvent(), req)

    assert req.prompt == "本轮用户问题", "注入只碰 system_prompt"
    assert req.contexts is contexts, "contexts 的**列表对象**不许被替换"
    assert req.contexts == before_contexts, "contexts 的内容不许被就地改"
    assert req.conversation is before_conversation
    assert req.conversation.history == before_history == "[原始历史]", "history 是落库字段，碰不得"
    assert "结果正文" in req.system_prompt, "该注入的仍然注入了"


# --------------------------------------------------------------------------- #
# B4 异常安全（吞掉、只 WARNING、不外抛）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_hook_never_raises_and_logs_warning_not_error(
    monkeypatch: pytest.MonkeyPatch, log: RecordingLogger
) -> None:
    """B4：`take` 抛 `RuntimeError` ⇒ 钩子正常返回、`system_prompt` 不变、0 ERROR / 1 WARNING。"""

    def boom(*_args: Any, **_kwargs: Any):
        raise RuntimeError("模拟取队列失败")

    monkeypatch.setattr(rc, "take", boom)
    req = FakeRequest(system_prompt="原生系统提示词")

    await _hook(_plugin(), FakeEvent(), req)  # 不许抛

    assert req.system_prompt == "原生系统提示词"
    warnings = _warnings(log)
    assert len(warnings) == 1, log.records
    assert "result_context 注入失败" in warnings[0]
    assert "RuntimeError" in warnings[0]
    assert _errors(log) == [], "吞异常不许升级成 ERROR（验收查级别）"


@pytest.mark.asyncio
async def test_hook_survives_an_event_without_umo(log: RecordingLogger) -> None:
    """事件上没有 `unified_msg_origin`（合成事件/老核心）⇒ 当作"没有会话"，不抛。"""
    rc.remember("aiocqhttp:GroupMessage:1000", "结果")
    req = FakeRequest(system_prompt="")

    await _hook(_plugin(), object(), req)  # type: ignore[arg-type]

    assert req.system_prompt == ""
    assert log.records == []


# --------------------------------------------------------------------------- #
# §5.7 日志契约
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_hook_logs_one_info_line_with_counts_only(log: RecordingLogger) -> None:
    """成功注入只留一行 INFO：`items=` / `chars=`，**不含正文与 umo**（可安全进生产日志）。"""
    rc.remember("aiocqhttp:GroupMessage:1000", "秘密正文不许进日志")
    rc.remember("aiocqhttp:GroupMessage:1000", "第二条")
    req = FakeRequest(system_prompt="")

    await _hook(_plugin(), FakeEvent(), req)

    infos = [message for level, message in log.records if level == "INFO"]
    assert len(infos) == 1, log.records
    line = infos[0]
    assert line.startswith("media_analysis: result_context inject items=2 chars=")
    assert "秘密正文不许进日志" not in line
    assert "aiocqhttp" not in line and "GroupMessage" not in line
    assert line.endswith(str(len("秘密正文不许进日志\n\n第二条")))
    assert _errors(log) == []


# --------------------------------------------------------------------------- #
# A8 terminate 清空
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_terminate_clears_pending(log: RecordingLogger) -> None:
    """A8：`await plugin.terminate()` 之后待注入表为空，且再注入为 0（执行级）。"""
    plugin = _plugin()
    rc.remember("aiocqhttp:GroupMessage:1000", "卸载前的残留")
    assert rc._pending, "前置：确有残留"

    await plugin.terminate()

    assert rc._pending == {}, "terminate 必须清空待注入结果（不跨生命周期复活）"
    req = FakeRequest(system_prompt="")
    await _hook(plugin, FakeEvent(), req)
    assert req.system_prompt == "", "清空之后还注入 ＝ 跨生命周期串味"
