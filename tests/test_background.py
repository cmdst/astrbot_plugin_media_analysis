"""后台异步契约：受理信封 / 去重 / 两级并发闸 / 唯一投递 / 取消 / 状态机（SPEC-v0.4 §2.6–§4）。

本文件守的是 **v0.4 的第二条主契约**：工具调用"受理即返回、解析在后台完成、结果**恰好一次**
投递"。四条最容易退化成"看起来对"的地方各有一组用例（SPEC-v0.4 §9 的"必测防静默三条"）：

1. **"受理即返回"退化成"先跑完再说"**：`submit()` 返回时后台任务必须**还没完成**，
   且受理路径上 `classify` / 抽帧 / provider 的调用计数必须是 **0**（spy，不是读注释）；
2. **"唯一投递"退化成"零次或两次"**：`context.send_message` 的调用次数用 spy 数，
   `False` 才兜底一次、抛错只 WARNING 不重试、取消**不投递**、完成后不再发第二条；
3. **"取消"退化成"清表但任务还在跑"**：断言 `task.cancelled()` 为真（不是只看表空了）；
4. **notes 缓存中立**：后台跑一次与同步跑一次的 notes 必须相等（任务生命周期信息只进日志）。

时间纪律：本文件**不用真 sleep 等结果**（`asyncio.Event` 当闸门），唯一一次
`asyncio.sleep(0.2)` 是"事件循环没被阻塞"的对照，整个文件的真实 sleep 合计 < 1 s（实测整文件 < 1 s）。
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest

from astrbot.api.message_components import Plain

from astrbot_plugin_media_analysis import analyze as analyze_mod
from astrbot_plugin_media_analysis import background, envelope, media, settings
from _fakes import FakeContext, RecordingLogger, make_event
from _rig import build_rig

#: `task_id` 的形状（SPEC-v0.4 §3.2：`uuid4().hex` = 32 位小写十六进制）
TASK_ID = re.compile(r"^[0-9a-f]{32}$")


# --------------------------------------------------------------------------- #
# 隔离：模块级三张表是全局状态（每个用例一套干净的表）
# --------------------------------------------------------------------------- #
def _reset_tables() -> None:
    background._TASKS.clear()
    background._BY_KEY.clear()
    background._BY_UMO.clear()
    background._RECENT.clear()


@pytest.fixture(autouse=True)
def clean_background_tables():
    """清三张表 + 诊断缓冲（不跨用例泄漏并发计数与去重键）。"""
    _reset_tables()
    yield
    for record in list(background._TASKS.values()):
        if record.task is not None:
            record.task.cancel()
    _reset_tables()


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _ok_payload(**overrides: object) -> dict[str, object]:
    base = envelope.ok_envelope(
        kind="video",
        duration_s=8.0,
        transcript=[],
        frames=[],
        summary="动图里是一个来回跳动的方块。",
        notes=["source:gif", "gif_converted:4837->14933", "video_native"],
        elapsed_ms=42,
        cached=False,
    )
    base.update(overrides)
    return base


class Gate:
    """手动放行的闸门：`release()` 之前后台任务一直挂着（控制"还没完成"的时点）。"""

    def __init__(self) -> None:
        self.event = asyncio.Event()
        self.calls: list[dict[str, object]] = []

    def release(self) -> None:
        self.event.set()


def _stub_analyze(
    monkeypatch: pytest.MonkeyPatch,
    *,
    gate: Gate | None = None,
    payload: dict[str, object] | None = None,
    error: BaseException | None = None,
    delay: float = 0.0,
) -> list[dict[str, object]]:
    """把 `analyze.analyze` 换成可控假身，返回调用记录（每次一个 kwargs dict）。"""
    calls: list[dict[str, object]] = []

    async def _analyze(**kwargs: object):
        calls.append(dict(kwargs))
        if delay:
            await asyncio.sleep(delay)
        if gate is not None:
            await gate.event.wait()
        if error is not None:
            raise error
        return payload if payload is not None else _ok_payload()

    monkeypatch.setattr(analyze_mod, "analyze", _analyze)
    return calls


async def _submit(
    *,
    event=None,
    context=None,
    target: str = "current",
    focus: str = "",
    mode: str = "auto",
) -> dict[str, object]:
    """直接调 `background.submit`（绕开媒体判定，专测任务表语义）。"""
    return await background.submit(
        context=context if context is not None else FakeContext(),
        event=event if event is not None else make_event(message_id="m-1"),
        target=target,
        focus=focus,
        mode=mode,
        settings=settings.Settings(),
        cache=None,
    )


def _texts(chain) -> list[str]:
    """`MessageChain` 里的纯文本段（投递内容的断言面）。"""
    return [comp.text for comp in chain.chain if isinstance(comp, Plain)]


def _sent_texts(context) -> list[str]:
    out: list[str] = []
    for call in context.calls.named("send_message"):
        out.extend(_texts(call.args[1]))
    return out


# --------------------------------------------------------------------------- #
# §3.2 受理信封：4 键、顺序固定、枚举封闭
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_accept_envelope_has_exactly_the_four_frozen_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§3.2：受理信封**恰好** 4 键（顺序固定），`state`/`task_id` 形状合规。"""
    gate = Gate()
    _stub_analyze(monkeypatch, gate=gate)

    payload = await _submit()

    assert list(payload) == list(envelope.ACCEPT_KEYS)
    assert payload["ok"] is True
    assert payload["state"] in envelope.ACCEPT_STATES
    assert TASK_ID.match(str(payload["task_id"]))
    assert payload["hint"] == envelope.ACCEPT_HINT
    # 三种形状可以**一次断言**判定（键数 4/9/3 + `state` 只在受理态出现）。
    # 注：与失败 3 键的公共键是 {ok, hint}（SPEC-v0.4 §3.1 的括注写的是"除 ok"，实际
    # `hint` 两者都有）⇒ 判别靠 `state` 与键数，不靠"键名零重叠"。
    assert set(envelope.ACCEPT_KEYS) & set(envelope.OK_KEYS) == {"ok"}
    assert set(envelope.ACCEPT_KEYS) & set(envelope.FAIL_KEYS) == {"ok", "hint"}
    assert set(envelope.OK_KEYS) & set(envelope.FAIL_KEYS) == {"ok"}
    assert len(envelope.ACCEPT_KEYS) == 4 and len(envelope.OK_KEYS) == 9
    assert len(envelope.FAIL_KEYS) == 3
    assert "state" not in envelope.OK_KEYS and "state" not in envelope.FAIL_KEYS

    gate.release()
    await background.wait(str(payload["task_id"]), timeout=5.0)


@pytest.mark.asyncio
async def test_accept_returns_before_the_job_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    """§2.6 第 1–6 步：`submit()` 返回时任务**必须还没跑完**（受理即返回）。

    这是"受理即返回"最直接的可执行定义：闸门没放开 ⇒ 信封已经拿到、`analyze` 还没返回。
    """
    gate = Gate()
    calls = _stub_analyze(monkeypatch, gate=gate)

    payload = await _submit()
    await asyncio.sleep(0)  # 让 `create_task` 的任务真正开跑（一个循环 tick）

    assert len(calls) == 1, "任务体应该已经启动（走到 analyze 的挂起点）"
    record = background._TASKS[str(payload["task_id"])]
    assert record.task is not None and not record.task.done()
    assert payload["state"] == "accepted"

    gate.release()
    assert isinstance(await background.wait(str(payload["task_id"]), timeout=5.0), dict)


@pytest.mark.asyncio
async def test_accept_latency_upper_bound(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """受理延迟上界（< 100 ms）：真实链路（媒体判定 + 登记 + create_task），不 sleep。"""
    gate = Gate()
    _stub_analyze(monkeypatch, gate=gate)
    rig = build_rig(monkeypatch, tmp_path, kind="audio")

    started = asyncio.get_running_loop().time()
    payload = await rig.tool_expect_accept()
    elapsed = asyncio.get_running_loop().time() - started

    assert payload["state"] == "accepted"
    assert elapsed < 0.1, f"受理耗时 {elapsed * 1000:.1f} ms，超过 100 ms 上界"

    gate.release()
    await background.wait(str(payload["task_id"]), timeout=5.0)


@pytest.mark.asyncio
async def test_accept_path_touches_no_heavy_io(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """§9 防静默第 3 条：受理路径上 `classify`/抽帧/provider 的调用计数必须是 **0**。

    打桩计数而不是读注释：`build_rig` 的假媒体层会记录每次调用。受理之后（任务体里）
    这些计数才应该变成 1 —— 这条同时证明"计数探针本身是通的"（否则 0 没有意义）。
    """
    rig = build_rig(monkeypatch, tmp_path, kind="audio")
    started = asyncio.Event()
    gate = asyncio.Event()
    real_analyze = analyze_mod.analyze

    async def _wrapped(**kwargs: object):
        started.set()
        await gate.wait()
        return await real_analyze(**kwargs)

    monkeypatch.setattr(analyze_mod, "analyze", _wrapped)

    payload = await rig.tool_expect_accept()
    await asyncio.sleep(0)  # 一个循环 tick：任务体开跑到闸门

    assert started.is_set(), "任务体应该已经启动（否则下面这组 0 没有意义）"
    assert rig.media_calls == {
        "classify": 0,
        "ensure_provider_wav": 0,
        "extract_wav": 0,
        "extract_frames": 0,
        "content_digest": 0,
    }
    assert rig.prov_calls == {"audio": 0, "vision": 0, "chat": 0}

    gate.set()
    await background.wait(str(payload["task_id"]), timeout=5.0)
    # 闸门放开、真 analyze 跑完 ⇒ 计数才有值（证明上面那组 0 不是"探针根本没接上"）
    assert rig.media_calls["classify"] == 1
    assert rig.prov_calls["audio"] == 1


# --------------------------------------------------------------------------- #
# §2.6 投递：一次、兜底一次、失败不重试
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_completion_is_delivered_exactly_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """§3.4：完成后**恰好**一条用户消息（`context.send_message` 数 = 1，`event.send` = 0）。"""
    rig = build_rig(monkeypatch, tmp_path, kind="audio")

    payload = await rig.tool()
    text = _sent_texts(rig.context)

    assert payload["ok"] is True
    assert len(rig.context.calls.named("send_message")) == 1
    assert rig.event.sent == []
    assert len(text) == 1
    assert payload["summary"] in text[0]
    assert "媒体分析：语音" in text[0]
    # 结果文本里**不许**出现路径 / task_id / provider id 原文
    assert "/tmp/" not in text[0] and "source:" not in text[0]


@pytest.mark.asyncio
async def test_send_message_false_falls_back_once_and_total_stays_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§2.6 投递表：`send_message` 返回 `False`（没找到平台）⇒ `event.send` 兜底**一次**。

    `False` 的语义是"什么都没发"（`core/star/context.py:710-713`），所以兜底不会造成
    第二条消息：总数仍然是 1。
    """
    _stub_analyze(monkeypatch)
    context = FakeContext(send_ok=False)
    event = make_event(message_id="m-fallback")
    payload = await _submit(event=event, context=context)

    finished = await background.wait(str(payload["task_id"]), timeout=5.0)

    assert finished is not None and finished["ok"] is True
    assert len(context.calls.named("send_message")) == 1
    assert len(event.sent) == 1
    assert "媒体分析" in _texts(event.sent[0])[0]


@pytest.mark.asyncio
async def test_send_message_exception_warns_and_never_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§2.6：投递抛异常 ⇒ 只 WARNING、**不重试、不兜底**（可能已发出去一半）。

    重复消息比缺一条消息更伤 ⇒ 这里同时断言"没有第二条"与"日志里没有正文"。
    """
    log = RecordingLogger()
    monkeypatch.setattr(background, "LOG", log, raising=False)
    _stub_analyze(monkeypatch)
    context = FakeContext(raise_on_send=RuntimeError("platform exploded"))
    event = make_event(message_id="m-raise")

    payload = await _submit(event=event, context=context)
    finished = await background.wait(str(payload["task_id"]), timeout=5.0)

    assert finished is not None
    assert len(context.calls.named("send_message")) == 1
    assert event.sent == [], "抛错后不许兜底（可能已经发出去了一半）"
    assert "WARNING" in log.levels()
    assert "RuntimeError" in log.text()
    assert "platform exploded" not in log.text(), "异常文本不进日志（脱敏口径）"
    assert any("state=undelivered" in message for _lvl, message in log.records)


@pytest.mark.asyncio
async def test_completion_does_not_deliver_again_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重复完成不二次投递：完成后再 `wait()`/`cancel_all()` 都不会补发第二条。"""
    _stub_analyze(monkeypatch)
    context = FakeContext()
    payload = await _submit(context=context)
    task_id = str(payload["task_id"])

    assert (await background.wait(task_id, timeout=5.0)) is not None
    assert (await background.wait(task_id, timeout=5.0)) is not None  # 从诊断缓冲回放
    assert await background.cancel_all() == 0

    assert len(context.calls.named("send_message")) == 1


# --------------------------------------------------------------------------- #
# §4 任务体的三条失败面
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_tool_error_inside_the_job_becomes_a_failure_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """任务内 `ToolError`（如 `GIF_CONVERT_FAILED`）⇒ 失败信封 + 一条用户可见消息。"""
    _stub_analyze(
        monkeypatch,
        error=envelope.ToolError(
            envelope.ERR_GIF_CONVERT_FAILED, log_detail="ffmpeg rc=187"
        ),
    )
    context = FakeContext()
    payload = await _submit(context=context)
    finished = await background.wait(str(payload["task_id"]), timeout=5.0)

    assert list(finished) == list(envelope.FAIL_KEYS)
    assert finished["error"] == envelope.ERR_GIF_CONVERT_FAILED
    texts = _sent_texts(context)
    assert len(texts) == 1
    assert "媒体分析失败：GIF_CONVERT_FAILED。" in texts[0]
    assert envelope.HINTS[envelope.ERR_GIF_CONVERT_FAILED] in texts[0]


@pytest.mark.asyncio
async def test_unexpected_exception_inside_the_job_becomes_internal_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """其它异常 ⇒ `INTERNAL_ERROR` 信封 + ERROR 日志（只打类型名）+ 不逃逸出任务体。"""
    log = RecordingLogger()
    monkeypatch.setattr(background, "LOG", log, raising=False)
    _stub_analyze(monkeypatch, error=RuntimeError("boom-secret-body"))
    context = FakeContext()
    payload = await _submit(context=context)
    task_id = str(payload["task_id"])

    finished = await background.wait(task_id, timeout=5.0)

    assert finished is not None and finished["error"] == envelope.ERR_INTERNAL_ERROR
    assert "ERROR" in log.levels()
    assert "RuntimeError" in log.text()
    assert "boom-secret-body" not in log.text()
    assert len(context.calls.named("send_message")) == 1


@pytest.mark.asyncio
async def test_cancelled_job_is_never_delivered(monkeypatch: pytest.MonkeyPatch) -> None:
    """`CancelledError` ⇒ 记 `cancelled`、**不投递**、继续上抛（取消语义不许被吞）。"""
    log = RecordingLogger()
    monkeypatch.setattr(background, "LOG", log, raising=False)
    _stub_analyze(monkeypatch, error=asyncio.CancelledError())
    context = FakeContext()
    payload = await _submit(context=context)
    task_id = str(payload["task_id"])
    record = background._TASKS[task_id]

    finished = await background.wait(task_id, timeout=5.0)

    assert finished is None, "取消没有信封"
    assert record.task is not None and record.task.cancelled() is True
    assert context.calls.named("send_message") == []
    assert any("bg cancelled" in message for _lvl, message in log.records)


@pytest.mark.asyncio
async def test_cancel_during_delivery_sends_no_second_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """投递期间被取消（`terminate()` 恰好撞上）⇒ 记 cancelled、不发第二条。"""

    class _CancelDuringSend(FakeContext):
        async def send_message(self, session, message_chain):  # type: ignore[override]
            self.calls.record("send_message", session, message_chain)
            raise asyncio.CancelledError()

    _stub_analyze(monkeypatch)
    context = _CancelDuringSend()
    event = make_event(message_id="m-cancel-deliver")
    payload = await _submit(event=event, context=context)
    task_id = str(payload["task_id"])
    record = background._TASKS[task_id]

    await background.wait(task_id, timeout=5.0)

    assert record.task is not None and record.task.cancelled() is True
    assert len(context.calls.named("send_message")) == 1
    assert event.sent == [], "取消期间不许走兜底（会变成第二条消息）"


# --------------------------------------------------------------------------- #
# 去重键
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_dedupe_same_message_same_params_returns_same_task_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§2.6 第 4 步：同一条消息 + 同一组参数再调一次 ⇒ 同一个 `task_id`、`state=running`、只跑 1 个任务。"""
    gate = Gate()
    calls = _stub_analyze(monkeypatch, gate=gate)
    event = make_event(message_id="m-dedupe")

    first = await _submit(event=event)
    await asyncio.sleep(0)
    second = await _submit(event=event)

    assert first["task_id"] == second["task_id"]
    assert first["state"] == "accepted" and second["state"] == "running"
    assert len(calls) == 1
    assert background.stats()["tasks"] == 1

    gate.release()
    await background.wait(str(first["task_id"]), timeout=5.0)


@pytest.mark.asyncio
async def test_dedupe_key_includes_mode_and_focus(monkeypatch: pytest.MonkeyPatch) -> None:
    """去重键含 `mode` / `focus`：同一消息但参数不同 ⇒ 两个任务（不许误合并）。"""
    gate = Gate()
    _stub_analyze(monkeypatch, gate=gate)
    event = make_event(message_id="m-key")

    first = await _submit(event=event, mode="auto")
    second = await _submit(event=event, mode="false")
    third = await _submit(event=event, mode="auto", focus="他在说什么")

    assert len({first["task_id"], second["task_id"], third["task_id"]}) == 3
    gate.release()
    for payload in (first, second, third):
        await background.wait(str(payload["task_id"]), timeout=5.0)


@pytest.mark.asyncio
async def test_dedupe_is_disabled_without_message_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """`message_id` 拿不到 ⇒ **关掉去重**（同一会话连发两条相同媒体本来就是两次请求）。"""
    gate = Gate()
    _stub_analyze(monkeypatch, gate=gate)
    event = make_event(message_id="")

    first = await _submit(event=event)
    second = await _submit(event=event)

    assert first["task_id"] != second["task_id"]
    assert background.stats()["dedupe"] == 0
    gate.release()
    for payload in (first, second):
        await background.wait(str(payload["task_id"]), timeout=5.0)


# --------------------------------------------------------------------------- #
# 两级并发闸
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_session_cap_is_three(monkeypatch: pytest.MonkeyPatch) -> None:
    """会话闸 3：同会话第 4 个任务 ⇒ `TOO_MANY_TASKS`（同步拒绝，不建任务）。"""
    gate = Gate()
    _stub_analyze(monkeypatch, gate=gate)
    accepted = [
        await _submit(event=make_event(message_id=f"m{i}")) for i in range(3)
    ]

    with pytest.raises(envelope.ToolError) as exc_info:
        await _submit(event=make_event(message_id="m3"))
    assert exc_info.value.code == envelope.ERR_TOO_MANY_TASKS
    assert "session=3/3" in exc_info.value.log_detail
    assert background.stats()["tasks"] == 3

    # 另一个会话不受影响（闸是**按会话**的）
    other = await _submit(event=make_event(umo="aiocqhttp:GroupMessage:2000", message_id="o1"))
    assert other["state"] == "accepted"

    gate.release()
    for payload in accepted + [other]:
        await background.wait(str(payload["task_id"]), timeout=5.0)


@pytest.mark.asyncio
async def test_global_cap_is_six(monkeypatch: pytest.MonkeyPatch) -> None:
    """全局闸 6：6 个会话各 1 个任务之后，第 7 个 ⇒ `TOO_MANY_TASKS`。"""
    gate = Gate()
    _stub_analyze(monkeypatch, gate=gate)
    accepted = [
        await _submit(event=make_event(umo=f"aiocqhttp:GroupMessage:{1000 + i}", message_id=f"m{i}"))
        for i in range(6)
    ]

    with pytest.raises(envelope.ToolError) as exc_info:
        await _submit(event=make_event(umo="aiocqhttp:GroupMessage:9999", message_id="m-x"))
    assert exc_info.value.code == envelope.ERR_TOO_MANY_TASKS
    assert "total=6/6" in exc_info.value.log_detail

    gate.release()
    for payload in accepted:
        await background.wait(str(payload["task_id"]), timeout=5.0)


@pytest.mark.asyncio
async def test_concurrency_limits_are_code_constants() -> None:
    """两级闸是**代码常量**（0 新配置键；改它等于改版本，必须过评审）。"""
    assert background.BG_MAX_PER_SESSION == 3
    assert background.BG_MAX_TOTAL == 6
    assert background.DELIVERY_MAX_CHARS == 1200


# --------------------------------------------------------------------------- #
# 生命周期：terminate / cancel_all
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_terminate_cancels_running_jobs_and_delivers_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """§2.6：`terminate()` ⇒ 全部任务 `cancel()`、表清空、**一条消息都不发**（幂等）。"""
    gate = Gate()
    _stub_analyze(monkeypatch, gate=gate)
    rig = build_rig(monkeypatch, tmp_path, kind="audio")
    payload = await rig.tool_expect_accept()
    record = background._TASKS[str(payload["task_id"])]

    await rig.plugin.terminate()

    assert record.task is not None and record.task.cancelled() is True
    assert background.stats() == {"tasks": 0, "sessions": 0, "dedupe": 0}
    assert rig.context.calls.named("send_message") == []
    assert rig.event.sent == []
    # 幂等：第二次 terminate 不抛、不重复取消
    await rig.plugin.terminate()
    gate.release()


@pytest.mark.asyncio
async def test_cancel_all_returns_count_and_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """`cancel_all()` 返回取消条数；第二次是 0（幂等，且不会二次投递）。"""
    gate = Gate()
    _stub_analyze(monkeypatch, gate=gate)
    for index in range(2):
        await _submit(event=make_event(message_id=f"m{index}"))

    assert await background.cancel_all() == 2
    assert await background.cancel_all() == 0
    assert background.stats()["tasks"] == 0


# --------------------------------------------------------------------------- #
# 状态机与诊断面
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_finished_state_is_delivered_or_undelivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§4 状态表：投递成功后是 `delivered`，`False` 兜底后仍是 `delivered`，抛错是 `undelivered`。

    断言面是**日志**（`bg finished … state=…`）：任务完成后记录已从表里摘掉，
    state 只在这里可见（也是运维唯一能看到的终态）。
    """
    log = RecordingLogger()
    monkeypatch.setattr(background, "LOG", log, raising=False)
    _stub_analyze(monkeypatch)

    await background.wait(str((await _submit(context=FakeContext()))["task_id"]), timeout=5.0)
    await background.wait(
        str((await _submit(context=FakeContext(send_ok=False)))["task_id"]), timeout=5.0
    )
    await background.wait(
        str(
            (
                await _submit(
                    context=FakeContext(raise_on_send=RuntimeError("nope")),
                )
            )["task_id"]
        ),
        timeout=5.0,
    )

    states = [message.split("state=")[1].split()[0] for message in log.text().splitlines() if "bg finished" in message]
    assert states == ["delivered", "delivered", "undelivered"]


@pytest.mark.asyncio
async def test_diagnostics_buffer_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """诊断缓冲有界（8 条）：只服务 `wait()`/排障，绝不无界增长。"""
    _stub_analyze(monkeypatch)
    task_ids = []
    for index in range(10):
        payload = await _submit(event=make_event(message_id=f"m{index}"))
        task_ids.append(str(payload["task_id"]))
        await background.wait(task_ids[-1], timeout=5.0)

    assert len(background._RECENT) == 8
    assert await background.wait(task_ids[0], timeout=1.0) is None  # 最早的已被挤掉
    assert isinstance(await background.wait(task_ids[-1], timeout=1.0), dict)


@pytest.mark.asyncio
async def test_wait_times_out_with_timeout_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """`wait()` 的超时是 `asyncio.TimeoutError`（不造第二套"超时=失败"的语义）。"""
    gate = Gate()
    _stub_analyze(monkeypatch, gate=gate)
    payload = await _submit()

    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        await background.wait(str(payload["task_id"]), timeout=0.01)

    gate.release()
    await background.wait(str(payload["task_id"]), timeout=5.0)


@pytest.mark.asyncio
async def test_logs_carry_only_short_task_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """§4 日志纪律：日志里只打 `task_id` 的 8 位前缀（全量 32 位不进日志）。"""
    log = RecordingLogger()
    monkeypatch.setattr(background, "LOG", log, raising=False)
    _stub_analyze(monkeypatch)
    payload = await _submit()
    task_id = str(payload["task_id"])
    await background.wait(task_id, timeout=5.0)

    text = log.text()
    assert task_id[:8] in text
    assert task_id not in text


# --------------------------------------------------------------------------- #
# 停止信号 / 上层取消：`CancelledError` 必须继续上抛（v0.3 评审 F5 的同一口径）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_stop_signal_cancelling_the_tool_call_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """取消打到**工具协程**上时，`CancelledError` 必须继续上抛（不许收成失败信封）。

    这是 v0.3 评审 F5 的同一口径（`main.py` 的 `except asyncio.CancelledError: raise`）：
    把取消收成信封，上层会以为"工具正常返回了"，既吞掉了停止信号，也可能顺手发一条与本轮
    无关的消息。这里在**受理之前**（`find_media` 挂起）取消，同时断言没建任务、没投递。
    """
    rig = build_rig(monkeypatch, tmp_path, kind="audio")
    gate = asyncio.Event()
    real_find = media.find_media

    async def _slow_find(event, target):  # type: ignore[no-untyped-def]
        await gate.wait()
        return await real_find(event, target)

    monkeypatch.setattr(media, "find_media", _slow_find)
    task = asyncio.ensure_future(rig.plugin.media_analyze(rig.event))
    await asyncio.sleep(0)  # 让工具协程走到挂起点
    assert not task.done()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert background.stats()["tasks"] == 0, "取消发生在受理之前 ⇒ 不该留任务"
    assert rig.context.calls.named("send_message") == []
    gate.set()


# --------------------------------------------------------------------------- #
# 事件循环不被阻塞
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_event_loop_keeps_running_while_a_job_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """后台任务在工作时**事件循环照常转**（不是把会话线程占住）。

    做法：让 analyze `sleep(0.2)`，在它还没完成时连续 50 次 `await sleep(0)`；
    每次让出都会回到事件循环 ⇒ 计数器必须走满，且总耗时远小于任务时长（< 0.15 s）。
    """
    _stub_analyze(monkeypatch, delay=0.2)
    payload = await _submit()
    task_id = str(payload["task_id"])
    loop = asyncio.get_running_loop()
    started = loop.time()

    ticks = 0
    while ticks < 50 and not background._TASKS[task_id].task.done():
        await asyncio.sleep(0)
        ticks += 1
    elapsed = loop.time() - started

    assert ticks == 50
    assert elapsed < 0.15, f"循环被占了 {elapsed:.3f}s（应远小于任务的 0.2s）"
    assert isinstance(await background.wait(task_id, timeout=5.0), dict)


# --------------------------------------------------------------------------- #
# facade 的"同步降级路径"：受理前的拒绝直接返回，不建任务
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_synchronous_rejections_create_no_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """§3.1 / §4：`BAD_TARGET` / `MEDIA_NOT_FOUND` / `TOO_MANY_TASKS` 都在**同步**侧返回。

    这三条是"同步降级路径"的全部：模型当场拿到 3 键失败信封，任务表里没有条目
    （`GIF_CONVERT_FAILED` 反而**不**在这里 —— 它在取件之后，恒在任务内）。
    """
    rig = build_rig(monkeypatch, tmp_path, kind="audio")

    bad_target = json.loads(
        await rig.plugin.media_analyze(rig.event, target="some/path.wav")
    )
    assert list(bad_target) == list(envelope.FAIL_KEYS)
    assert bad_target["error"] == envelope.ERR_BAD_TARGET
    assert background.stats()["tasks"] == 0

    text_only = make_event(current=[Plain("今天天气不错")], message_id="text")
    empty = await rig.plugin.media_analyze(text_only)
    assert json.loads(empty)["error"] == envelope.ERR_MEDIA_NOT_FOUND
    assert background.stats()["tasks"] == 0

    # TOO_MANY_TASKS 也在同步侧（受理第 5 步）：媒体判定通过、但会话闸已满
    gate = Gate()
    _stub_analyze(monkeypatch, gate=gate)
    media_chain = list(rig.event.get_messages())
    for index in range(3):
        accepted = await rig.plugin.media_analyze(
            make_event(current=media_chain, message_id=f"cap{index}")
        )
        assert json.loads(accepted)["state"] == "accepted"
    too_many = await rig.plugin.media_analyze(
        make_event(current=media_chain, message_id="cap3")
    )
    assert json.loads(too_many)["error"] == envelope.ERR_TOO_MANY_TASKS
    assert background.stats()["tasks"] == 3
    gate.release()
    await background.cancel_all()


# --------------------------------------------------------------------------- #
# notes 缓存中立（§7.2 / §4 注）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_notes_are_cache_neutral_between_sync_and_background(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """同步跑一次 vs 后台跑一次：**去掉 `cache_*` 后 notes 必须逐字相等**。

    任务生命周期信息（accepted/running/delivered）只进日志 —— 信封整体入缓存，
    一条"后台"标记会被复用给别的调用。
    """
    rig = build_rig(monkeypatch, tmp_path, kind="audio")
    sync_payload = await rig.run(include_frames=True)
    background_payload = await rig.tool(include_frames=True)

    def _stable(notes: list[str]) -> list[str]:
        return [note for note in notes if not note.startswith(("cache_hit", "cache_age_s"))]

    assert _stable(list(sync_payload["notes"])) == _stable(list(background_payload["notes"]))
    assert background_payload["cached"] is True  # 第二次是缓存命中（同一份字节）
    assert sync_payload["cached"] is False
    for payload in (sync_payload, background_payload):
        assert list(payload) == list(envelope.OK_KEYS)
        assert not any("bg" in note or "task" in note for note in payload["notes"])


# --------------------------------------------------------------------------- #
# render()：纯函数三形态（§3.4 冻结文案）
# --------------------------------------------------------------------------- #
def test_render_uses_summary_then_transcript_then_placeholder() -> None:
    """§3.4：`summary` → `transcript` 拼接 → "（本次没有可用的文字内容）"三级退化。"""
    with_summary = background.render(_ok_payload())
    assert with_summary.startswith("动图里是一个来回跳动的方块。")
    assert "—— 媒体分析：视频｜时长 8.00s｜画面帧 0 张｜转写 0 字｜耗时 0.0s" in with_summary

    transcript = _ok_payload(
        summary="", transcript=[{"text": "第一句"}, {"text": "第二句"}]
    )
    assert background.render(transcript).startswith("第一句\n第二句")
    assert "转写 6 字" in background.render(transcript)  # 第一句(3) + 第二句(3)

    empty = background.render(_ok_payload(summary="", transcript=[]))
    assert background.render(_ok_payload(summary="   ", transcript=[])).startswith(
        "（本次没有可用的文字内容）"
    )
    assert empty.startswith("（本次没有可用的文字内容）")


def test_render_marks_cache_hits_and_audio_kind() -> None:
    """`cached=True` ⇒ 页脚追加"｜缓存"；`kind=audio` ⇒ "语音"。"""
    rendered = background.render(_ok_payload(kind="audio", cached=True))
    assert "媒体分析：语音" in rendered
    assert rendered.endswith("｜缓存")


def test_render_truncates_the_body_but_keeps_the_footer() -> None:
    """正文超 `DELIVERY_MAX_CHARS` ⇒ 截断 + `…`，页脚完整保留（用户要能看到耗时/帧数）。"""
    long_summary = "字" * (background.DELIVERY_MAX_CHARS + 120)
    rendered = background.render(_ok_payload(summary=long_summary))

    body, _, footer = rendered.partition("\n")
    assert body == "字" * background.DELIVERY_MAX_CHARS + "…"
    assert "媒体分析：视频" in footer


def test_render_respects_an_explicit_max_chars_and_keeps_the_default() -> None:
    """`max_chars` 关键字（v0.4.2 为 C 模式加的**唯一**改动，SPEC-v0.4.2 §6.2）。

    两条都要：
    ① 显式给 `max_chars=200` ⇒ 正文按 200 截断（C 模式的正文要塞进唤醒轮 system prompt，
       比一条群消息更紧）；
    ② **默认参数输出与 v0.4.1 逐字节相同**（A2 的 1200 语义一个字都没改）。
    """
    long_summary = "字" * (background.DELIVERY_MAX_CHARS + 120)
    payload = _ok_payload(summary=long_summary)

    tight = background.render(payload, max_chars=200)
    body, _, footer = tight.partition("\n")
    assert body == "字" * 200 + "…"
    assert "媒体分析：视频" in footer, "截断只切正文，页脚照旧"

    # 默认参数 == 显式给出冻结常量 == v0.4.1 的语义（1200 + "…"）
    assert background.render(payload) == background.render(
        payload, max_chars=background.DELIVERY_MAX_CHARS
    )
    assert background.render(payload).partition("\n")[0] == (
        "字" * background.DELIVERY_MAX_CHARS + "…"
    )


def test_render_failure_is_code_plus_static_hint() -> None:
    """§3.4 失败形态：`媒体分析失败：{error}。{hint}`（hint 来自常量表，无运行时数据）。"""
    rendered = background.render(envelope.fail_envelope(envelope.ERR_GIF_CONVERT_FAILED))
    assert rendered == (
        f"媒体分析失败：{envelope.ERR_GIF_CONVERT_FAILED}。"
        f"{envelope.HINTS[envelope.ERR_GIF_CONVERT_FAILED]}"
    )
    # 未知错误码由 `fail_envelope` 归一到 INTERNAL_ERROR（渲染器只读信封，不做归一）
    assert "INTERNAL_ERROR" in background.render(envelope.fail_envelope("NOPE"))


def test_render_never_injects_runtime_details() -> None:
    """渲染器只读信封：**不**输出 notes 原文 / 路径 / task_id / provider id。"""
    rendered = background.render(
        _ok_payload(notes=["source:gif", "video_provider:cpa_Gemini/x", "cache_hit"])
    )
    assert "source:gif" not in rendered
    assert "cpa_Gemini" not in rendered
    assert "cache_hit" not in rendered
    assert "task_id" not in rendered


# --------------------------------------------------------------------------- #
# 受理信封不入缓存（0 新键的副作用守卫）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_accept_envelope_never_enters_the_result_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """受理信封**绝不入缓存**：缓存目录里只应该有分析信封（9 键），没有 `"state"`。"""
    rig = build_rig(monkeypatch, tmp_path, kind="audio")

    await rig.tool()
    payload = await rig.tool()  # 第二次命中缓存

    entries = list((tmp_path / "plugin_data").rglob("*.json"))
    assert entries, "缓存应该落了至少一条"
    for entry in entries:
        text = entry.read_text(encoding="utf-8")
        assert '"state"' not in text
        assert '"task_id"' not in text
        assert payload["cached"] is True
