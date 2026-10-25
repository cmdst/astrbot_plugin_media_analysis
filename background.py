"""后台任务表 + 结果渲染 + 确定性单次投递（SPEC-v0.4 §2.6 / §3 / §4）。

**为什么是 A2（插件自管）而不是核心的 `is_background_task`**（裁决见 SPEC-v0.4 §2）：
核心 4.29.1 的 `FunctionTool.is_background_task` 在派发层读的是
`_PermissionGuardedTool` **包装器**（`core/provider/func_tool_manager.py:214-245` 只镜像
`name/description/parameters/active/handler_module_path`，**没有** `__getattr__`），
而 `get_full_tool_set()` 每次重建包装器（`:496-512`）⇒ 在 `llm_tools.get_func(...)` 上置
flag **恒为 False**（`/tmp` 探针实测：仍同步执行）。即便把 flag 置到包装器实例上（A1b），
回执文案被核心写死、多一次整段历史的 agent 回合、而且**投递由模型决定**
（`core/astr_agent_tool_exec.py:677-684` 只记 WARNING）⇒ 拿不到"恰好一次"。

**v0.4.1 复核**（路线 C 调研，`research/20261025-native-core-background-route.md`）：核心侧
只要在 `_PermissionGuardedTool.__init__` 里补一行镜像就能让 flag 生效，但 v0.4.1 **仍不
adopt**。**v0.4.2 把口径精确化为"默认不采用、可显式选择"**（`background.mode=core`，
实验模式，实现在 `core_mode.py`；本模块**不导入**它，依赖方向是 `main → core_mode →
{analyze, envelope, background.render}`）。默认 `plugin` 下本模块仍是唯一引擎。

默认不开 C 的三条理由（**v0.4.2 的事实更正**，见 SPEC-v0.4.2 §1.4 / §9.3）：
① **投递不确定** —— C 的发声靠唤醒轮模型主动调 `send_message_to_user`，没调只有核心一条
WARNING ⇒ 从"恰好一次"退化为"大概率一次"；② **多一轮主 Agent** —— 每条后台任务多一轮
完整模型调用；③ **3600 s 硬编码 + 无闸** —— 核心那层超时改不了，且 C 模式下去重/并发闸/
预算闸**全部不存在**。

**（作废的说法）"本插件的成果是带媒体 ⇒ C 模式下没有可靠媒体出口"**：本插件的交付面
**只有一条 `Plain` 文本**（`_deliver` ⇒ `MessageChain([Plain(text)])`，信封里的 `frames`
是**帧数计数**不是图片）⇒ 报告 §5.2 / §6.8 的"媒体无可靠出口"对本插件**不成立**。
未来的禁令见 SPEC-v0.4.2 §9.2：若 `_deliver` 哪天开始投媒体组件，C 模式必须先被移除。
`tests/test_registration.py` 的 `test_tool_is_not_a_core_background_task` 继续把
"**默认模式**下不置 flag"钉死，防止"顺手开原生"造成同一条任务两条消息。

本模块的三条纪律（每条都在评审清单上）：

1. **受理即返回**：`submit()` 只做类型判定（`media.find_media`，至多 6 字节本地读）
   + 去重 + 并发闸 + `create_task`，**不碰 ffmpeg、不碰 provider**；
2. **唯一投递（at-most-once）**：一次 `context.send_message`；**只有**它返回 `False`
   （= 没找到平台，什么都没发）时才 `event.send` 兜底一次；任何异常只 WARNING、
   **不重试**（重复消息比缺一条消息更伤）；
3. **notes 缓存中立**：任务生命周期信息**只进日志**，绝不写进信封（信封整体入缓存，
   一条"后台"标记会被复用给别的调用）。

依赖方向（DAG）：`background → analyze / envelope`；只有 `main` 导入本模块。
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field

from astrbot.api import logger as LOG
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain

from . import analyze, envelope

# ---- 护栏（**代码常量，0 新配置键**，SPEC-v0.4 §6.1）----
BG_MAX_PER_SESSION = 3  # 单会话同时进行的任务上限
BG_MAX_TOTAL = 6  # 单实例全局上限
DELIVERY_MAX_CHARS = 1200  # 一条群消息能舒服读完的长度
CANCEL_WAIT_S = 2.0  # `terminate()` 等任务收尾的时长（不阻塞卸载）
_RECENT_MAX = 8  # 已完成结果的诊断缓冲（只给 `wait()`/排障用）

# ---- 状态机（SPEC-v0.4 §4；`accepted|running` 是受理态，其余是终态）----
STATE_ACCEPTED = "accepted"
STATE_RUNNING = "running"
STATE_OK = "ok"
STATE_FAILED = "failed"
STATE_DELIVERED = "delivered"
STATE_UNDELIVERED = "undelivered"
STATE_CANCELLED = "cancelled"

_KIND_CN = analyze._KIND_CN  # 唯一事实源（`audio→语音` / `video→视频`），不另抄一份
_TOKEN_SHAPED = re.compile(r"[A-Za-z0-9_\-]{24,}")
_EMPTY_BODY = "（本次没有可用的文字内容）"

#: 三张表（key 都是不可变小对象，便于幂等摘除）。
_TASKS: dict[str, "BackgroundTask"] = {}  # task_id -> 任务记录
_BY_KEY: dict[str, str] = {}  # dedupe_key -> task_id
_BY_UMO: dict[str, set[str]] = {}  # umo -> {task_id}
_RECENT: "OrderedDict[str, dict[str, object]]" = OrderedDict()


@dataclass
class BackgroundTask:
    """一条后台任务（可变：`state` 与 `payload` 由任务体推进）。

    `message_id` 拿不到时是 `""` ⇒ **关掉去重**（同一会话连续发同一条媒体的两个副本
    本来就是两次请求，硬合并会给用户一条"少了一半"的结果）。
    """

    task_id: str
    umo: str
    target: str
    mode: str
    focus: str
    message_id: str = ""
    state: str = STATE_ACCEPTED
    started_at: float = 0.0
    payload: dict[str, object] | None = None
    task: asyncio.Task | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)


# --------------------------------------------------------------------------- #
# 受理
# --------------------------------------------------------------------------- #
async def submit(
    *,
    context,
    event,
    target: str,
    focus: str,
    mode: str,
    settings,
    cache,
) -> dict[str, object]:
    """登记一条后台任务，返回**受理信封**（4 键）；受控拒绝抛 `envelope.ToolError`。

    调用方（工具门面）**已经**用 `media.find_media` 做完了类型判定（含 GIF 魔数），
    所以这里只剩"去重 + 并发闸 + 起任务"三件事 —— 受理路径因此没有下载、没有 ffmpeg、
    没有 provider 调用（SPEC-v0.4 §2.6 第 1–6 步）。

    三张表 + 去重 + 两级并发闸。去重键是
    `umo|target|message_id|mode|focus`：同一条消息、同一组参数、同一个会话再调一次
    ⇒ 返回**同一个 `task_id`**（`state="running"`），不新建任务（模型偶尔会重复调用）。
    """
    umo = _umo(event)
    message_id = _message_id(event)
    dedupe_key = f"{umo}|{target}|{message_id}|{mode}|{focus}"

    if message_id:
        existing = _BY_KEY.get(dedupe_key)
        record = _TASKS.get(existing) if existing else None
        if record is not None:
            LOG.info(
                "media_analysis bg accepted task=%s state=%s target=%s mode=%s concurrent=%d",
                _short(record.task_id),
                STATE_RUNNING,
                target,
                mode,
                len(_TASKS),
            )
            return envelope.accept_envelope(envelope.STATE_RUNNING, record.task_id)

    per_session = len(_BY_UMO.get(umo, ()))
    if per_session >= BG_MAX_PER_SESSION or len(_TASKS) >= BG_MAX_TOTAL:
        raise envelope.ToolError(
            envelope.ERR_TOO_MANY_TASKS,
            log_detail=(
                f"session={per_session}/{BG_MAX_PER_SESSION} "
                f"total={len(_TASKS)}/{BG_MAX_TOTAL}"
            ),
        )

    record = BackgroundTask(
        task_id=uuid.uuid4().hex,
        umo=umo,
        target=target,
        mode=mode,
        focus=focus,
        message_id=message_id,
        started_at=time.monotonic(),
    )
    _register(record, dedupe_key if message_id else "")
    record.task = asyncio.create_task(
        _run(
            record,
            context=context,
            event=event,
            settings=settings,
            cache=cache,
        )
    )
    record.task.add_done_callback(_consume)
    LOG.info(
        "media_analysis bg accepted task=%s state=%s target=%s mode=%s concurrent=%d",
        _short(record.task_id),
        STATE_ACCEPTED,
        target,
        mode,
        len(_TASKS),
    )
    return envelope.accept_envelope(envelope.STATE_ACCEPTED, record.task_id)


# --------------------------------------------------------------------------- #
# 任务体
# --------------------------------------------------------------------------- #
async def _run(
    record: BackgroundTask,
    *,
    context,
    event,
    settings,
    cache,
) -> None:
    """任务体：跑 `analyze` → 收信封 → **一次**投递 → 记终态（异常不逃逸）。

    三条失败面各有归宿（SPEC-v0.4 §2.6 / §4 注 1-2）：
    - `ToolError` ⇒ 失败信封（**用户看得到**，不是静默）；
    - 其它任何异常 ⇒ `INTERNAL_ERROR` 信封 + 一条 ERROR 日志；
    - `CancelledError` ⇒ 记 `cancelled`、**不投递**、继续上抛（取消语义不许吞）。
    """
    record.state = STATE_RUNNING
    started = time.monotonic()
    payload: dict[str, object]
    try:
        payload = await analyze.analyze(
            context=context,
            event=event,
            target=record.target,
            focus=record.focus,
            include_frames=record.mode,
            settings=settings,
            cache=cache,
        )
    except asyncio.CancelledError:
        # 取消：**不投递**、只记一行、继续上抛（取消语义不许被吞，评审 F5 同口径）
        record.state = STATE_CANCELLED
        LOG.info("media_analysis bg cancelled task=%s", _short(record.task_id))
        _finish(record)
        raise
    except envelope.ToolError as exc:
        LOG.info(
            "media_analyze rejected: code=%s detail=%s",
            exc.code,
            _redact(exc.log_detail)[:200],
        )
        payload = envelope.fail_envelope(exc.code)
        record.state = STATE_FAILED
    except BaseException as exc:  # noqa: BLE001 —— 后台任务绝不把异常抛给事件循环
        LOG.error(
            "media_analysis: bg internal error task=%s type=%s",
            _short(record.task_id),
            type(exc).__name__,
        )
        payload = envelope.fail_envelope(envelope.ERR_INTERNAL_ERROR)
        record.state = STATE_FAILED
    else:
        record.state = STATE_OK

    record.payload = payload
    try:
        delivered = await _deliver(context, event, record.umo, render(payload))
    except asyncio.CancelledError:
        # 投递期间被取消（`terminate()` 恰好撞上）：记 cancelled，不发第二条
        record.state = STATE_CANCELLED
        LOG.info(
            "media_analysis bg cancelled during delivery task=%s",
            _short(record.task_id),
        )
        raise
    finally:
        # 无论成败/取消：三张表照样要摘干净、`done` 照样要放行
        _finish(record)
    record.state = STATE_DELIVERED if delivered else STATE_UNDELIVERED
    LOG.info(
        "media_analysis: bg finished task=%s state=%s elapsed_ms=%d delivered=%s",
        _short(record.task_id),
        record.state,
        int((time.monotonic() - started) * 1000),
        "yes" if delivered else "no",
    )


# --------------------------------------------------------------------------- #
# 投递（**恰好一次**；失败不重试）
# --------------------------------------------------------------------------- #
async def _deliver(context, event, umo: str, text: str) -> bool:
    """主动投递一次结果文本，返回是否真的发出去了（`at-most-once`）。

    决策表（SPEC-v0.4 §2.6 + 队长裁决第 5 条）：

    | 情形 | 动作 |
    | --- | --- |
    | `context.send_message` 返回 `True` | 完成（`delivered`） |
    | 返回 `False`（= 没找到平台，什么都没发） | `event.send` 兜底**一次** |
    | 抛异常 | 只 WARNING（脱敏、不含正文），**不重试、不兜底**（可能已发出去一半） |
    | `context` 没有该方法（测试桩/老核心） | 视为 `False` ⇒ 兜底一次 |

    `False → 兜底` 不会产生第二条消息：核心那条路径在"找不到平台"时会**直接 return
    False 且什么都没发**（`core/star/context.py:710-713`）。
    """
    chain = MessageChain([Plain(text)])
    sender = getattr(context, "send_message", None)
    if callable(sender):
        try:
            if await sender(umo, chain):
                return True
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 —— 投递失败只记，不重试
            LOG.warning(
                "media_analysis: 结果投递失败（%s），不重试以免重复发消息",
                type(exc).__name__,
            )
            return False

    fallback = getattr(event, "send", None)
    if callable(fallback):
        try:
            await fallback(MessageChain([Plain(text)]))
            return True
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 —— 同口径：只记不重试
            LOG.warning(
                "media_analysis: 结果兜底投递失败（%s），不重试", type(exc).__name__
            )
    return False


# --------------------------------------------------------------------------- #
# 渲染（纯函数、不读配置、可单测）
# --------------------------------------------------------------------------- #
def render(payload: dict[str, object], *, max_chars: int = DELIVERY_MAX_CHARS) -> str:
    """把信封渲染成**给用户看的一条文本**（SPEC-v0.4 §3.4 冻结）。

    只读信封，不读配置、不碰网络、**不带**路径 / `task_id` / provider id / 异常文本 /
    note 原文（note 只进日志与信封）。成功三形态：`summary` → `transcript` 拼接 →
    "（本次没有可用的文字内容）"。

    `max_chars` 是 v0.4.2 为 C 模式（核心原生后台）加的**唯一**改动（SPEC-v0.4.2 §6.2）：
    C 模式的正文要塞进唤醒轮的 system prompt，比一条群消息更紧（`CORE_BODY_MAX`）。
    **默认值 == `DELIVERY_MAX_CHARS` ⇒ A2 的输出逐字节不变**；
    两种模式的用户可见文本必须同源（同一份摘要/转写/页脚/`…` 规则），否则"同一个结果在
    两种模式下长得不一样"会变成第二个事实源。
    """
    if payload.get("ok") is False:
        code = str(payload.get("error") or envelope.ERR_INTERNAL_ERROR)
        hint = str(payload.get("hint") or envelope.HINTS.get(code, ""))
        return f"媒体分析失败：{code}。{hint}"

    frames = payload.get("frames")
    frame_count = len(frames) if isinstance(frames, list) else 0
    duration_s = _as_float(payload.get("duration_s"))
    elapsed_ms = _as_int(payload.get("elapsed_ms"))
    kind = str(payload.get("kind") or "")
    kind_cn = _KIND_CN.get(kind, kind)
    transcript = payload.get("transcript")
    chars = _transcript_chars(transcript)

    body = str(payload.get("summary") or "").strip()
    if not body:
        body = _transcript_body(transcript)
    if not body:
        body = _EMPTY_BODY

    footer = (
        f"—— 媒体分析：{kind_cn}｜时长 {duration_s:.2f}s｜画面帧 {frame_count} 张｜"
        f"转写 {chars} 字｜耗时 {elapsed_ms / 1000:.1f}s"
    )
    if payload.get("cached"):
        footer += "｜缓存"

    limit = max(0, int(max_chars))
    truncated = len(body) > limit
    if truncated:
        body = body[:limit]
    return f"{body}{'…' if truncated else ''}\n{footer}"


def _transcript_body(transcript: object) -> str:
    """`transcript` 条目拼成一段文本（`[听不清]` 之类的占位**原样保留**）。"""
    if not isinstance(transcript, list):
        return ""
    pieces = [
        str(item.get("text") or "").strip()
        for item in transcript
        if isinstance(item, dict)
    ]
    return "\n".join(piece for piece in pieces if piece)


def _transcript_chars(transcript: object) -> int:
    if not isinstance(transcript, list):
        return 0
    total = 0
    for item in transcript:
        if isinstance(item, dict):
            value = item.get("text")
            if isinstance(value, str):
                total += len(value)
    return total


def _as_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- #
# 生命周期 / 诊断面
# --------------------------------------------------------------------------- #
async def cancel_all(*, wait_s: float = CANCEL_WAIT_S) -> int:
    """取消全部任务并清空三张表（`terminate()` 调用；**幂等**）。

    为什么必须等一小会儿再清表：`_run` 的 `finally` 会摘自己，若我们清完表就走，
    被取消的任务仍可能持着 `event`/`context` 引用活到下一轮 —— 那就是"孤儿 task"。
    这里 `cancel` 之后最多等 `wait_s` 秒让它们收尾（超时也照样清表，不阻塞卸载）。
    """
    records = list(_TASKS.values())
    for record in records:
        if record.task is not None and not record.task.done():
            record.task.cancel()
    for record in records:
        if record.task is not None:
            with contextlib.suppress(BaseException):
                await asyncio.wait({record.task}, timeout=max(0.0, float(wait_s)))
    _TASKS.clear()
    _BY_KEY.clear()
    _BY_UMO.clear()
    if records:
        LOG.info("media_analysis: bg cancel_all count=%d", len(records))
    return len(records)


async def wait(task_id: str, *, timeout: float | None = None) -> dict[str, object] | None:
    """等一条任务收尾并返回它的信封（**诊断/测试面**，生产不调用）。

    取消或进程退出时返回 `None`（没有信封）。超时抛 `asyncio.TimeoutError`
    —— 调用方自己决定怎么处理，这里不造第二套"超时=失败"的语义。
    """
    record = _TASKS.get(task_id)
    if record is None:
        return _RECENT.get(task_id)
    waiter = record.done.wait()
    if timeout is None:
        await waiter
    else:
        await asyncio.wait_for(waiter, timeout=timeout)
    return record.payload


def stats() -> dict[str, int]:
    """当前任务表计数（排障一眼看并发，日志之外的只读视图）。"""
    return {
        "tasks": len(_TASKS),
        "sessions": len(_BY_UMO),
        "dedupe": len(_BY_KEY),
    }


def _register(record: BackgroundTask, dedupe_key: str) -> None:
    _TASKS[record.task_id] = record
    if dedupe_key:
        _BY_KEY[dedupe_key] = record.task_id
    _BY_UMO.setdefault(record.umo, set()).add(record.task_id)


def _release(record: BackgroundTask) -> None:
    """把任务从三张表里摘掉（幂等：`cancel_all` 与 `_run` 都可能先摘一步）。"""
    _TASKS.pop(record.task_id, None)
    for key, task_id in list(_BY_KEY.items()):
        if task_id == record.task_id:
            _BY_KEY.pop(key, None)
    bucket = _BY_UMO.get(record.umo)
    if bucket is not None:
        bucket.discard(record.task_id)
        if not bucket:
            _BY_UMO.pop(record.umo, None)


def _finish(record: BackgroundTask) -> None:
    """任务收尾的唯一出口：摘表 + 记诊断缓冲 + 放行 `wait()`（**幂等**）。"""
    _release(record)
    _remember(record)
    record.done.set()


def _remember(record: BackgroundTask) -> None:
    """把终态信封放进有界诊断缓冲（只给 `wait()` 用，最多 `_RECENT_MAX` 条）。"""
    if record.payload is None:
        return
    _RECENT[record.task_id] = record.payload
    while len(_RECENT) > _RECENT_MAX:
        _RECENT.popitem(last=False)


def _consume(task: asyncio.Task) -> None:
    """吞掉任务的终态异常（取消/已处理过的异常），避免"never retrieved"告警。"""
    with contextlib.suppress(BaseException):
        task.exception()


def _umo(event) -> str:
    value = getattr(event, "unified_msg_origin", "")
    return value if isinstance(value, str) else ""


def _message_id(event) -> str:
    """`event.message_obj.message_id`（拿不到就是 `""` ⇒ 去重关闭）。"""
    message_obj = getattr(event, "message_obj", None)
    value = getattr(message_obj, "message_id", "")
    return value if isinstance(value, str) else ""


def _short(task_id: str) -> str:
    """日志里只打 8 位前缀（全量 `task_id` 不进日志）。"""
    return str(task_id or "")[:8]


def _redact(text: str) -> str:
    """脱敏：把 token 形状的长串（≥24 位）替换成 `[REDACTED]`。

    为什么在这里**不**复用 `main.redact_sensitive_text`：那会形成 `background → main`
    的反向依赖，而 DAG 是 `main → background`（SPEC-v0.4 §2.6）。规则本身与核心函数同形
    （核心 `error_redaction` 也主要是这条），而本模块打的只有 `log_detail`
    （错误码 / 体积 / 异常类名），不含用户文案与媒体内容。
    """
    return _TOKEN_SHAPED.sub("[REDACTED]", str(text or ""))
