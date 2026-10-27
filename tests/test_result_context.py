"""`result_context` 的纯逻辑断言（SPEC-v0.5.2 §5.3 / §5.5 A1–A6）。

口径两条（跟 `tests/test_background.py` 的 `clean_background_tables` 同姿势）：

1. **进程级状态必须前后清**：`_pending` 是模块级字典，用例之间会串味（A5 的"2 条上限"
   尤其怕残留）；
2. **时钟只 patch `result_context._now`**：TTL 用例靠它推进，**不许** sleep，也不许把
   300 / 2400 抄成字面量（§10：常量只有一份事实源，测试引用常量）。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from astrbot_plugin_media_analysis import result_context as rc

PLUGIN_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def clean_pending():
    """每个用例前后都清空待注入表（模块级字典是进程级状态）。"""
    rc.clear()
    yield
    rc.clear()


class _Clock:
    """可推进的假时钟：`rc._now` 被 patch 成它 ⇒ TTL 判定完全可控。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(rc, "_now", fake)
    return fake


# --------------------------------------------------------------------------- #
# 结构与常量（§5.1 / §5.2）
# --------------------------------------------------------------------------- #
def test_constants_are_the_frozen_values() -> None:
    """三个常量逐值冻结；`_MAX_UMOS` 是**有界性护栏**（D3 的"不永久污染"落到内存上）。"""
    assert rc._TTL_S == 300.0
    assert rc._MAX_PER_UMO == 2
    assert rc._MAX_CHARS == 2400
    assert rc._MAX_UMOS == 512
    assert isinstance(rc._HEADER, str) and "media_analyze" in rc._HEADER


def test_module_is_pure_logic_and_imports_nothing_from_astrbot() -> None:
    """**结构守卫**（§5.1）：本模块只许 import `time` / `collections` / `__future__`。

    为什么执行级断言之外还要这条 AST 守卫：一旦有人为了"顺手读个配置"把
    `from . import settings` 塞进来，本模块就不再是纯逻辑，`test_core_imports.py` 的
    加固面与"0 新配置键"的承诺同时失效 —— 而这种回归**不会有任何行为测试变红**。
    """
    tree = ast.parse((PLUGIN_DIR / "result_context.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"纯逻辑模块不该有包内相对导入：{ast.unparse(node)}"
            if node.module:
                imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "time", "collections"}, imported
    assert "astrbot" not in imported


# --------------------------------------------------------------------------- #
# A1 一次性
# --------------------------------------------------------------------------- #
def test_remember_then_take_returns_the_text_once(clock: _Clock) -> None:
    rc.remember("umoA", "视频里报的数字是 8093")
    assert rc.take("umoA", "") == ["视频里报的数字是 8093"]


def test_take_is_destructive_and_second_call_returns_empty(clock: _Clock) -> None:
    """§5.3 第 1 条：`take` 先整体出队 ⇒ 第二次拿不到（哪怕上次一条都没返回）。"""
    rc.remember("umoA", "第一条")
    assert rc.take("umoA", "") == ["第一条"]
    assert rc.take("umoA", "") == []
    assert rc.take("umoA", "第一条") == []


def test_take_of_an_unknown_umo_returns_empty(clock: _Clock) -> None:
    rc.remember("umoA", "只有 A 有")
    assert rc.take("umoB", "") == []
    assert rc.take("", "") == []
    assert rc.take(None, "") == []  # type: ignore[arg-type] —— 事件上没有 umo 时要退化成"没有"


# --------------------------------------------------------------------------- #
# A2 TTL（闭区间）
# --------------------------------------------------------------------------- #
def test_ttl_expiry_drops_the_entry(clock: _Clock) -> None:
    rc.remember("umoA", "五分钟前的分析")
    clock.advance(rc._TTL_S + 1)
    assert rc.take("umoA", "") == []


def test_ttl_boundary_is_inclusive(clock: _Clock) -> None:
    """恰好 `_TTL_S` 仍算有效（闭区间）：差 0.001 s 就丢 ⇒ 边界两侧都要断言。"""
    rc.remember("umoA", "边界")
    clock.advance(rc._TTL_S)
    assert rc.take("umoA", "") == ["边界"], "恰好 300.0 s 仍算有效（闭区间）"

    # 相对它**自己的登记时刻**再多 0.001 s ⇒ 过期
    rc.remember("umoA", "过界")
    clock.advance(rc._TTL_S + 0.001)
    assert rc.take("umoA", "") == []


# --------------------------------------------------------------------------- #
# A3 跨 UMO 隔离
# --------------------------------------------------------------------------- #
def test_cross_umo_isolation(clock: _Clock) -> None:
    rc.remember("umoA", "A 的结果")
    rc.remember("umoB", "B 的结果")
    assert rc.take("umoB", "") == ["B 的结果"]
    assert rc.take("umoA", "") == ["A 的结果"]


# --------------------------------------------------------------------------- #
# A4 用户引用原文去重
# --------------------------------------------------------------------------- #
def test_prompt_containing_the_text_is_skipped(clock: _Clock) -> None:
    """用户把结果原文贴进 prompt ⇒ 不必再注入（他已经看得见）。"""
    rc.remember("umoA", "数字是 8093")
    assert rc.take("umoA", "刚才那段『数字是 8093』是什么意思") == []


def test_skipped_entries_are_still_consumed(clock: _Clock) -> None:
    """§5.5 A4 后半：被"已引用"丢掉的条目**也已出队** ⇒ 下一轮不会复活。"""
    rc.remember("umoA", "数字是 8093")
    assert rc.take("umoA", "数字是 8093 是什么意思") == []
    assert rc.take("umoA", "") == [], "一次性：上一轮过滤掉的条目不许在下一轮冒出来"


def test_partial_skip_keeps_the_other_entry(clock: _Clock) -> None:
    """两条里只有一条被引用 ⇒ 另一条照样注入（过滤是逐条，不是整批）。"""
    rc.remember("umoA", "旧的：里面有 1111")
    rc.remember("umoA", "新的：里面有 2222")
    # 用户把**旧那条的原文整段**贴进 prompt（判据是"文本 in prompt"，不是"片段命中"）
    asked = "旧的：里面有 1111 是什么意思"
    assert rc.take("umoA", asked) == ["新的：里面有 2222"]


def test_take_treats_non_string_prompt_as_empty(clock: _Clock) -> None:
    """`req.prompt` 可能是 `None`（核心某些轮次没有文本提示）⇒ 退化成"没有引用"。"""
    rc.remember("umoA", "结果")
    assert rc.take("umoA", None) == ["结果"]


# --------------------------------------------------------------------------- #
# A5 每 UMO 2 条上限（丢最旧）
# --------------------------------------------------------------------------- #
def test_per_umo_cap_keeps_the_latest_two(clock: _Clock) -> None:
    rc.remember("umoA", "第一条 1111")
    rc.remember("umoA", "第二条 2222")
    rc.remember("umoA", "第三条 3333")
    texts = rc.take("umoA", "")
    assert len(texts) == rc._MAX_PER_UMO == 2
    assert texts == ["第二条 2222", "第三条 3333"], "保留最新两条、插入序（旧→新）"
    assert all("第一条" not in text for text in texts)


# --------------------------------------------------------------------------- #
# 有界性（§5.2 _MAX_UMOS）+ 惰性清扫
# --------------------------------------------------------------------------- #
def test_total_umo_keys_are_bounded(clock: _Clock) -> None:
    """键数到达上限后逐出"最旧条目 ts 最小"的会话 ⇒ 字典不会无限增长。"""
    for index in range(rc._MAX_UMOS):
        rc.remember(f"umo{index}", f"结果 {index}")
        clock.advance(0.01)
    assert len(rc._pending) == rc._MAX_UMOS
    rc.remember("umo-new", "最新一条")
    assert len(rc._pending) == rc._MAX_UMOS
    assert "umo0" not in rc._pending, "最旧的那个会话必须被逐出"
    assert rc.take("umo-new", "") == ["最新一条"]


def test_expired_keys_are_swept_on_remember(clock: _Clock) -> None:
    """`remember` 的惰性清扫：条目全部过期的会话键会消失（不需要等逐出）。"""
    rc.remember("umo-old", "旧结果")
    clock.advance(rc._TTL_S + 1)
    rc.remember("umo-new", "新结果")
    assert set(rc._pending) == {"umo-new"}


# --------------------------------------------------------------------------- #
# A6 2400 字截断（前缀）
# --------------------------------------------------------------------------- #
def test_format_block_joins_with_a_blank_line_and_truncates_to_max_chars() -> None:
    first = "甲" * 2000
    second = "乙" * 2000
    block = rc.format_block([first, second])
    assert len(block) == rc._MAX_CHARS == 2400
    assert block == "\n\n".join([first, second])[: rc._MAX_CHARS], "截断必须是拼接串的前缀"
    assert block.startswith(first + "\n\n")


def test_remember_truncates_an_overlong_single_text(clock: _Clock) -> None:
    """单条超长在 `remember` 就截断（存进去的已经是前缀）⇒ 内存不会攒大文本。"""
    rc.remember("umoA", "丙" * (rc._MAX_CHARS + 500))
    texts = rc.take("umoA", "")
    assert len(texts) == 1
    assert len(texts[0]) == rc._MAX_CHARS
    assert texts[0] == "丙" * rc._MAX_CHARS


def test_format_block_is_empty_without_texts() -> None:
    assert rc.format_block([]) == ""
    assert rc.format_block(None) == ""  # type: ignore[arg-type] —— 调用方据此跳过整个注入


# --------------------------------------------------------------------------- #
# clear（A8 的纯逻辑半边）
# --------------------------------------------------------------------------- #
def test_clear_empties_everything(clock: _Clock) -> None:
    rc.remember("umoA", "A")
    rc.remember("umoB", "B")
    rc.clear()
    assert rc._pending == {}
    assert rc.take("umoA", "") == []


# --------------------------------------------------------------------------- #
# §5.3 第 4 条：remember 是总函数
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("umo", "text"),
    [
        ("umoA", ""),
        ("umoA", "   \n\t "),
        ("umoA", None),
        ("umoA", 123),
        ("", "非空文本"),
        (None, "非空文本"),
        (123, "非空文本"),
        (object(), ["列表也不是文本"]),
    ],
    ids=[
        "empty-text",
        "whitespace-text",
        "none-text",
        "int-text",
        "empty-umo",
        "none-umo",
        "int-umo",
        "object-umo",
    ],
)
def test_remember_is_a_total_function(umo: object, text: object, clock: _Clock) -> None:
    """**任何输入都不抛**（它在投递成功路径上，抛了会污染 `bg finished` 的状态机）。"""
    rc.remember(umo, text)  # type: ignore[arg-type]
    assert rc._pending == {}, "非法输入一条都不许进表"


def test_remember_strips_surrounding_whitespace(clock: _Clock) -> None:
    rc.remember("umoA", "  结果正文  ")
    assert rc.take("umoA", "") == ["结果正文"]
