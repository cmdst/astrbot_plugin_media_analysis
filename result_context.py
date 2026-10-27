"""一次性结果上下文：把"已投递的分析结果"挂到该会话的**下一轮**请求上（SPEC-v0.5.2 §5）。

**为什么要它（用户裁决 D3）**：工具是异步的 —— `media_analyze` 受理即返回，结果由
`background` 在后台发一条用户可见消息，**模型自己看不到那条消息**。用户紧接着追问
"刚才那个视频里说了什么"，模型只有空白记忆，于是要么让用户重发、要么（更糟）现编一段。
本模块让插件在**投递成功后**登记结果，由 `main` 的唯一 `@filter.on_llm_request` 钩子在
**下一轮同会话请求**把它拼进 `req.system_prompt`（与核心自己的"后台结果轮"同形，
SPEC-v0.5.2 §1.7：核心也是把后台结果拼进 `system_prompt`）。

四条不可协商的语义（§5.3）：

1. **一次性**：`take()` 先整体 `pop` 该 UMO 的队列 ⇒ 被"TTL 过滤"或"用户已引用原文"
   丢掉的条目**也不会**在后续请求里复活（否则会出现"隔三轮又被注入"）；
2. **每 UMO 最多 2 条、保留最新**：`deque(maxlen=_MAX_PER_UMO)` 的 append 语义（挤掉最旧）；
3. **截断是前缀**：`format_block` 返回 `"\\n\\n".join(texts)[:_MAX_CHARS]` ⇒ 结果一定是
   拼接串的前缀（测试可以断言 `block == joined[:_MAX_CHARS]`）；
4. **`remember` 是总函数**：任何输入都不抛（它在投递成功路径上，抛了会污染
   `bg finished` 那行日志的状态机）。

本模块是**纯逻辑**：不 import `astrbot`、不读配置、不碰网络与磁盘、不打日志。唯一的外部
事实是 `time.monotonic()`（`_now()` 是测试唯一需要 monkeypatch 的点）。
"""

from __future__ import annotations

import time
from collections import deque

_TTL_S = 300.0
"""待注入条目的存活时长（5 分钟）：够"看完结果再追问"，过期即不再注入。"""

_MAX_PER_UMO = 2
"""一个会话最多挂 2 条待注入结果（挤掉最旧的）。"""

_MAX_CHARS = 2400
"""注入**正文**总长上限（`len(str)` 计，code point；不含下方抬头）。"""

_MAX_UMOS = 512
"""键数上限（有界性护栏）：超出时逐出"最旧条目 ts 最小"的那个会话。"""

_HEADER = (
    "【media_analyze 工具结果】\n"
    "以下是你此前通过 media_analyze 工具得到的分析结果，已由插件代发到本会话。"
    "把它当作你自己的上下文；不要重复发送，不要声称没做过。"
)
"""注入抬头（**固定常量**，逐字冻结：不含 task_id / umo / 时间戳 / provider id）。"""

_pending: dict[str, deque[tuple[float, str]]] = {}
"""`umo -> deque(maxlen=_MAX_PER_UMO)`，元素是 `(单调时钟戳, 文本)`。"""


def _now() -> float:
    """唯一"时钟"（测试 monkeypatch 这一个点，别在别处直接读 `time.monotonic()`）。"""
    return time.monotonic()


def _sweep() -> None:
    """惰性清扫：把"所有条目都已过期"的会话键删掉（键数护栏的前置动作）。"""
    now = _now()
    dead = [
        umo
        for umo, queue in _pending.items()
        if not any(now - ts <= _TTL_S for ts, _text in queue)
    ]
    for umo in dead:
        del _pending[umo]


def _evict_oldest() -> None:
    """键数达上限时逐出"最旧条目 ts 最小"的那一个会话（按插入时间，不按文本）。"""
    if not _pending:  # pragma: no cover - 调用点已保证非空
        return
    oldest = min(_pending, key=lambda umo: _pending[umo][0][0])
    del _pending[oldest]


def remember(umo: str, text: str) -> None:
    """登记一条**已成功投递**的结果文本（总函数：任何输入都不抛、不报错）。

    | 输入 | 行为 |
    | --- | --- |
    | 非 `str` / 空 `umo` | 静默忽略（拿到的是事件对象/None 时不该炸） |
    | 空白文本（`text.strip()` 为空） | 静默忽略（没什么可注入的） |
    | 超长文本（`> _MAX_CHARS`） | 按 `_MAX_CHARS` **截断**（前缀）后再存 |
    | 该会话已有 `_MAX_PER_UMO` 条 | 挤掉**最旧**的一条 |

    时间戳取 `_now()`（登记时刻），不是投递时刻 —— 两者在同一条 `_run` 里相邻，差可忽略。
    """
    if not isinstance(umo, str) or not umo:
        return
    if not isinstance(text, str):
        return
    body = text.strip()
    if not body:
        return
    if len(body) > _MAX_CHARS:
        body = body[:_MAX_CHARS]

    _sweep()
    if umo not in _pending and len(_pending) >= _MAX_UMOS:
        _evict_oldest()
    _pending.setdefault(umo, deque(maxlen=_MAX_PER_UMO)).append((_now(), body))


def take(umo: str, prompt: str | None) -> list[str]:
    """**取走**该会话的待注入文本（一次性：无论后面过滤掉多少条，队列都不再存在）。

    过滤两条：
    - `now - ts <= _TTL_S`（**闭区间**：恰好 300.0 s 仍算有效）；
    - `text not in prompt`（用户把结果原文引用进 prompt ⇒ 不必再重复注入）。

    返回**插入序**（旧→新）。`umo` 非 `str` / 无登记 / 全部被过滤 ⇒ `[]`。
    """
    if not isinstance(umo, str) or not umo:
        return []
    queue = _pending.pop(umo, None)  # ① 先整体出队 ⇒ 一次性
    if not queue:
        return []
    now = _now()
    asked = prompt if isinstance(prompt, str) else ""
    return [
        body
        for ts, body in queue
        if now - ts <= _TTL_S and body not in asked
    ]


def format_block(texts: list[str]) -> str:
    """把多条结果拼成**注入正文块**：空 ⇒ `""`（调用方据此跳过整个注入）。"""
    if not texts:
        return ""
    return "\n\n".join(texts)[:_MAX_CHARS]


def clear() -> None:
    """清空全部待注入条目（`terminate()` 用：卸载后不留跨生命周期的记忆）。"""
    _pending.clear()
