"""C 模式的**接线面**（`initialize()` → `activate()` → 工具门面 → C handler）。

为什么单开一个文件、而不是塞进 `tests/test_core_mode.py`（SPEC-v0.4.2 §11.2 的 26 格）：
那个文件是**单元面**（逐函数：`activate()` / `render_core()` / `run()`）；本文件是**接线面**
—— 同样的性质换成"从 `initialize()` 出发、经 `media_analyze` 门面、到返回文本"再走一遍。

这不是重复劳动，而是本项目教训 §9-10 的直接产物：**"测死代码"会制造已接线的错觉**
（`budgeted_timeout()` 有纯函数单测却生产零调用，一路绿到评审用 spy 才发现）。单元面绿了
不等于 `initialize()` 里真的调了 `activate()`、也不等于门面的 C 分支真的不会掉进 A2。
因此本文件对每一条关键接线都用 **spy 计数**或**打桩抛错**断言"运行期调到了/没调到"，而不是
"代码里写了/没写"。

四组（15 个用例函数，参数化后 23 个 item）：

1. **探测三态（5）**：未修核心 ⇒ `wrapper_no_mirror` + flag 还原 + 内存回落 + **不写配置**；
   已修 ⇒ PASS；显式 `plugin` ⇒ **不探测**（`get_full_tool_set()` 0 次）且不置 flag；
   大小写/空白/坏值在**探测之前**归一；`activate()` 在 `initialize()` 里**恰好一次**。
2. **置位与重载重放（3）**：注册 → 置位 → `terminate()` → 模拟重载（新 `FuncTool` 对象）→
   再置位；上次 core、这次重载成 `plugin` ⇒ flag 归 False；置位**只命中本插件**的工具。
3. **互斥（2）**：C 模式三张表恒空、`send_message` 0 次、`event.send` 兜底也不碰；
   `plugin` 模式表生命周期不变（受理后 1 条 / 完成后 0 条 / 投递恰好 1 次）。
4. **C handler 契约（5）**：长正文 ≤1200 + 前缀 + 截断标记 + 页脚；四类异常都返回**文本**
   且表为空；`CancelledError` 是**唯一**上抛的；两条静态结构守卫（不引用 A2 投递面、
   不做任何落盘/写配置）。

两个"世界"仍由 `_core_shim` 提供（未修 = 原装包装器；已修 = 补一行镜像的子类），
与 `test_core_mode.py` 同一套 —— 本文件不新造世界，只新造**路径**。
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest

from astrbot.core.provider import func_tool_manager as ftm

from astrbot_plugin_media_analysis import analyze as analyze_mod
from astrbot_plugin_media_analysis import background, cache as cache_mod, core_mode
from astrbot_plugin_media_analysis import envelope, main, settings
from _core_shim import FakeManager, make_tool, patch_core
from _fakes import FakeContext, RecordingLogger, make_event
from _rig import build_rig

_CORE_MODE_FILE = Path(core_mode.__file__).resolve()
_CORE_CONFIG = {"background": {"mode": "core"}, "cache": {"ttl_seconds": 0}}
_PLUGIN_CONFIG = {"background": {"mode": "plugin"}, "cache": {"ttl_seconds": 0}}

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


# --------------------------------------------------------------------------- #
# 零件
# --------------------------------------------------------------------------- #
class _ReadOnlyConfig(dict):
    """任何**写**操作直接炸：用来证明 `initialize()`（含探测失败回落）不写配置。

    生产里这个位置是 `AstrBotConfig`，它的 `__setitem__` 会顺手 `save_config()` 落盘
    （`astrbot_config.py` 的完整性检查就用这条路）。"内存级回落"必须**不**走那条路：
    用户选的 `core` 要原样留在磁盘上，否则下次加载就变成"插件偷偷把配置改了"。
    """

    def _readonly(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("initialize() 写配置了（回落必须是内存级，见 SPEC-v0.4.2 §4.3）")

    __setitem__ = _readonly  # type: ignore[assignment]
    __delitem__ = _readonly  # type: ignore[assignment]
    update = _readonly  # type: ignore[assignment]
    setdefault = _readonly  # type: ignore[assignment]
    pop = _readonly  # type: ignore[assignment]
    popitem = _readonly  # type: ignore[assignment]
    clear = _readonly  # type: ignore[assignment]

    def save_config(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("initialize() 调了 save_config（回落必须不落盘）")


class _CountingManager(FakeManager):
    """`get_full_tool_set()` 计数的管理器：证明 `plugin` 模式**一次都没探测**。"""

    def __init__(self, tools: object = ()) -> None:
        super().__init__(tools)  # type: ignore[arg-type]
        self.probe_calls = 0

    def get_full_tool_set(self):  # noqa: D401 —— 只为计数
        self.probe_calls += 1
        return super().get_full_tool_set()


def _new_plugin(manager, config) -> main.MediaAnalysisPlugin:
    return main.MediaAnalysisPlugin(context=FakeContext(manager=manager), config=config)


def _c_plugin(*, manager=None) -> main.MediaAnalysisPlugin:
    """已处于 core 生效态的实例（手工置 `_bg`，不跑探测）—— C handler 用例的台子。"""
    plugin = _new_plugin(manager, _CORE_CONFIG)
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
    """把 `analyze.analyze` 换成固定结果/固定异常（`core_mode` 与本文件共享同一模块对象）。"""

    async def _fake(**kwargs):
        if isinstance(outcome, BaseException):
            raise outcome
        return dict(outcome)

    monkeypatch.setattr(analyze_mod, "analyze", _fake)


class _UnpatchedGuardedTool(ftm._PermissionGuardedTool):  # type: ignore[misc,valid-type]
    """核心 4.29.1 的**原装**包装器形态：镜像区**没有** `is_background_task`。

    为什么自己造"未修真身"：本机核心树可能已被（部署单/运营方）打过那一行镜像补丁 ——
    那时"未修世界"在真机上就不存在了，这一格会随宿主状态假红/假绿。这里在真类之上把那个
    **实例属性**摘掉，读数回落到 `FunctionTool.is_background_task` 的类默认 `False`，
    与 4.29.1 原装行为逐字等价，且与宿主树是否被打过补丁无关
    （与 `tests/test_core_mode.py::unpatched_core` 同一套做法）。
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


def _levels(logger: RecordingLogger, level: str) -> list[str]:
    return [message for _level, message in logger.records if _level == level]


# --------------------------------------------------------------------------- #
# 1. 探测三态（5）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_initialize_rejects_core_when_the_wrapper_does_not_mirror(monkeypatch) -> None:
    """① 未修核心 + 配置写 `core` ⇒ REJECT + flag 还原 + 回落 plugin + **一个键都不写**。

    这是本版最重要的**生产**性质（不是单元性质）：运维在面板里选了 `core`、核心还没补那
    一行镜像时，插件必须"响亮失败 + 按 A2 跑"，而不是"看起来正常、实际同步阻塞 30–100 s"。
    同时用户的**选择**必须原样留在磁盘上（`_ReadOnlyConfig` 写一下就炸）—— 否则下次加载
    会变成"插件自己把配置改回了 plugin"，运维会以为"开了没生效"。

    "未修核心"由 `unpatched_core(monkeypatch)` **显式**构造：宿主核心树在 2026-10-26 02:01
    已被打上那一行镜像 ⇒ 直接依赖环境就等于让本格随宿主状态漂（t5 复议里的那条红）。
    """
    unpatched_core(monkeypatch)  # "未修核心"要**显式**装（宿主树可能自己已经带那一行镜像）
    logger = _log(monkeypatch, core_mode)
    main_log = _log(monkeypatch, main)  # `ready:` 行由 `main` 打（引擎判据走 core_mode）
    tool = make_tool()
    tmgr = FakeManager([tool])
    config = _ReadOnlyConfig(_CORE_CONFIG)
    plugin = _new_plugin(tmgr, config)

    await plugin.initialize()

    # —— 激活态：请求 core、生效 plugin、原因是包装器没镜像 —— #
    assert plugin._bg.requested == core_mode.MODE_CORE, "用户的选择要记住（只在内存里）"
    assert plugin._bg.effective == core_mode.MODE_PLUGIN
    assert plugin._bg.reason == core_mode.REASON_WRAPPER_NO_MIRROR
    assert plugin._bg.raw_flag is True, "探测期间确实置过位"
    assert plugin._bg.wrapper_flag is False, "未修核心的包装器上没有这个字段"
    assert plugin._bg_recheck_pending is False, "回落态没有复检（复检只属于 core）"

    # —— fail-closed：flag 必须还原，不留半开状态 —— #
    assert tool.is_background_task is False, "失败必须还原（半开 = 静默前台阻塞）"

    # —— 内存级回落：配置快照读到 core，磁盘上的配置一个键都没动 —— #
    assert plugin._settings_cache is not None
    assert plugin._settings_cache.background_mode == "core"
    assert dict(plugin.config) == _CORE_CONFIG, "回落不许改配置（写操作会直接炸）"

    # —— 日志：一条 ERROR（带 reason 与指引）+ 恰好一行 ready: —— #
    errors = _levels(logger, "ERROR")
    assert len(errors) == 1, "一次加载一条 ERROR（不是每步一条）"
    assert "reason=wrapper_no_mirror" in errors[0]
    assert "已回落 plugin" in errors[0]
    ready = [message for message in _levels(main_log, "INFO") if "ready:" in message]
    assert len(ready) == 1, "ready: 行逐字不动（引擎判据走独立日志）"
    assert "background.mode" not in ready[0], "ready: 行不加新字段（运维口径保持同形）"

    await plugin.terminate()


@pytest.mark.asyncio
async def test_initialize_activates_core_when_the_probe_passes(monkeypatch) -> None:
    """② 已修核心 + 配置 `core` ⇒ PASS：生效 core、raw/wrapper 都为 True、INFO 一条。

    这条证明 ① 的失败不是"永远失败"：同一份代码在补了那一行镜像的世界里确实会启用 C。
    """
    patch_core(monkeypatch)
    logger = _log(monkeypatch, core_mode)
    tool = make_tool()
    tmgr = FakeManager([tool])
    plugin = _new_plugin(tmgr, dict(_CORE_CONFIG))

    await plugin.initialize()

    assert plugin._bg.effective == core_mode.MODE_CORE
    assert plugin._bg.reason == core_mode.REASON_WRAPPER_MIRRORS_RAW
    assert plugin._bg.raw_flag is True and plugin._bg.wrapper_flag is True
    assert plugin._bg_recheck_pending is True, "core 生效态要保留一次复检"
    assert tool.is_background_task is True
    assert core_mode.wrapper_flag(tmgr) is True

    marks = [message for message in _levels(logger, "INFO") if "effective=" in message]
    assert len(marks) == 1
    assert "effective=core" in marks[0] and "probe=wrapper_mirrors_raw" in marks[0]
    assert _levels(logger, "ERROR") == []

    await plugin.terminate()
    assert tool.is_background_task is False


@pytest.mark.asyncio
async def test_explicit_plugin_mode_never_probes(monkeypatch) -> None:
    """③ 显式 `plugin` ⇒ **根本不探测**（`get_full_tool_set()` 0 次），也不置 flag。

    为什么单列"不探测"：探测要 `get_full_tool_set()`，那在生产里是**每次新建一整套包装器**
    （4.29.1 的实现），默认模式跑它等于给每次加载加一份无用的对象垃圾；更重要的是
    "默认模式零噪音/零副作用"是 v0.4.2 的承诺（默认行为与 v0.4.1 逐字一致）。
    """
    patch_core(monkeypatch)  # 就算核心已修，默认模式也不许去动 flag
    logger = _log(monkeypatch, core_mode)
    tmgr = _CountingManager([make_tool()])
    plugin = _new_plugin(tmgr, dict(_PLUGIN_CONFIG))

    await plugin.initialize()

    assert tmgr.probe_calls == 0, "plugin 模式不许探测（spy：一次都没调 get_full_tool_set）"
    tool = tmgr.func_list[0]
    assert tool.is_background_task is False, "plugin 模式不许置核心 flag"
    assert plugin._bg == core_mode.INACTIVE, "默认态就是 INACTIVE（与 v0.4.1 同形）"
    assert plugin._bg_recheck_pending is False

    assert len([m for m in _levels(logger, "DEBUG") if "probe=config_plugin" in m]) == 1
    assert [m for m in _levels(logger, "INFO") if "effective=" in m] == [], "默认零噪音"
    assert _levels(logger, "ERROR") == []

    await plugin.terminate()


@pytest.mark.parametrize(
    "raw,expected_mode,expect_warning",
    [
        ("core", "core", False),
        ("CORE", "core", False),  # 大小写手滑可容忍
        (" core ", "core", False),  # 空白手滑可容忍
        ("on", "plugin", True),  # 布尔式写法：保守侧（plugin）+ 告警一次
        ("", "plugin", False),  # 空串 = 缺键 ⇒ 默认，不告警
        (None, "plugin", False),  # null ⇒ 默认，不告警
    ],
)
@pytest.mark.asyncio
async def test_initialize_normalizes_the_mode_before_probing(
    monkeypatch, raw, expected_mode, expect_warning
) -> None:
    """④ 归一发生在**探测之前**：`CORE`/` core ` 也真启用；坏值回 plugin 且只告警一次。

    顺序很重要：`activate()` 自己也归一一次（防调用方传坏值），但**配置面**的归一在
    `settings.normalize_background_mode`，它负责"告警一次 + 不打值"。这里走完整的
    `initialize()`，断言两条归一没有互相拆台（例如 `CORE` 若在 `_settings()` 阶段被吞成
    `plugin`，本格会红）。
    """
    patch_core(monkeypatch)
    monkeypatch.setattr(core_mode, "LOG", RecordingLogger())
    monkeypatch.setattr(settings, "_WARNED", set(), raising=False)
    settings_log = _log(monkeypatch, settings)
    tmgr = FakeManager([make_tool()])
    plugin = _new_plugin(tmgr, {"background": {"mode": raw}, "cache": {"ttl_seconds": 0}})

    await plugin.initialize()

    assert plugin._settings_cache is not None
    assert plugin._settings_cache.background_mode == expected_mode
    assert plugin._bg.effective == expected_mode
    warnings = _levels(settings_log, "WARNING")
    if expect_warning:
        assert len(warnings) == 1
        assert "background.mode" in warnings[0] and "plugin" in warnings[0]
    else:
        assert warnings == []

    await plugin.terminate()


@pytest.mark.asyncio
async def test_initialize_calls_activate_exactly_once_with_the_normalized_mode(monkeypatch) -> None:
    """⑤ `initialize()` 里 `activate()` **恰好一次**，且传的是**已归一**的请求值。

    为什么用 spy：本项目教训 §9-10 —— 纯函数单测绿了不等于生产路径调了它，也不等于**只**调了
    一次。加载期重复探测在生产里的代价是"每次加载多建一整套包装器对象"，且第二次探测的结果
    会覆盖第一次（如果两次之间发生了 `deactivate`，就会在一次加载里抖出 plugin 态）。
    """
    patch_core(monkeypatch)
    monkeypatch.setattr(core_mode, "LOG", RecordingLogger())
    calls: list[tuple[object, str]] = []
    real_activate = core_mode.activate

    def _spy(tmgr, *, requested):
        calls.append((tmgr, requested))
        return real_activate(tmgr, requested=requested)

    monkeypatch.setattr(core_mode, "activate", _spy)
    tmgr = FakeManager([make_tool()])
    plugin = _new_plugin(
        tmgr, {"background": {"mode": " core "}, "cache": {"ttl_seconds": 0}}
    )

    await plugin.initialize()

    assert len(calls) == 1, f"activate() 应恰好一次，实际 {len(calls)} 次"
    assert calls[0][0] is tmgr, "传进去的必须是 context 给的那个管理器"
    assert calls[0][1] == "core", "配置面的归一必须先做完（CORE/空白都不许漏给探测）"
    assert plugin._bg.effective == core_mode.MODE_CORE

    await plugin.terminate()


# --------------------------------------------------------------------------- #
# 2. 置位与重载重放（3）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_flag_is_replayed_across_a_simulated_reload(monkeypatch) -> None:
    """⑥ 注册 → 置位 → 卸载 → **模拟重载（全新 `FuncTool` 对象）** → 再置位。

    为什么必须每次加载重放（SPEC-v0.4.2 §8.1）：`FuncTool` 对象跟着 `import` 走 —— 重载会
    重建注册表里的工具对象，旧对象上的 `True` 谁也不会再读。所以"上次开过 C"这件事
    **不能记住**，只能在 `initialize()` 里重做一遍（本格用两个插件实例 + 一次对象替换模拟）。
    """
    patch_core(monkeypatch)
    monkeypatch.setattr(core_mode, "LOG", RecordingLogger())
    first = make_tool()
    tmgr = FakeManager([first])
    config = dict(_CORE_CONFIG)

    plugin1 = _new_plugin(tmgr, config)
    await plugin1.initialize()
    assert plugin1._bg.effective == core_mode.MODE_CORE
    assert first.is_background_task is True, "首次加载置位"

    await plugin1.terminate()
    assert first.is_background_task is False, "卸载不许留半开状态"
    assert plugin1._bg == core_mode.INACTIVE

    # 模拟重载：模块重新 import ⇒ 注册表里是**全新的 FuncTool 对象**（旧对象作废）
    second = make_tool()
    tmgr.func_list.clear()
    tmgr.func_list.append(second)
    assert second.is_background_task is False

    plugin2 = _new_plugin(tmgr, config)
    await plugin2.initialize()
    assert plugin2._bg.effective == core_mode.MODE_CORE
    assert second.is_background_task is True, "flag 必须在新对象上重放"
    assert core_mode.locate(tmgr) == [second]

    await plugin2.terminate()


@pytest.mark.asyncio
async def test_reload_into_plugin_mode_clears_the_flag(monkeypatch) -> None:
    """⑦ 上次 core、这次重载成 `plugin` ⇒ 一律归 False（"关掉实验模式"必须真的关干净）。

    只改配置不重载时 flag 仍是 True 的窗口属于运维口径（改配置 → 重载，见派单）；
    但**只要重载发生**，就必须回到 `plugin` 的干净状态 —— 否则同一份配置在磁盘上是
    `plugin`、运行期却还在派发原生后台，排查会完全对不上。
    """
    patch_core(monkeypatch)
    monkeypatch.setattr(core_mode, "LOG", RecordingLogger())
    tool = make_tool()
    tmgr = FakeManager([tool])
    plugin1 = _new_plugin(tmgr, dict(_CORE_CONFIG))
    await plugin1.initialize()
    assert tool.is_background_task is True
    await plugin1.terminate()

    second = make_tool()
    tmgr.func_list.clear()
    tmgr.func_list.append(second)
    plugin2 = _new_plugin(tmgr, dict(_PLUGIN_CONFIG))

    await plugin2.initialize()

    assert plugin2._bg.effective == core_mode.MODE_PLUGIN
    assert plugin2._bg.reason == core_mode.REASON_CONFIG_PLUGIN
    assert second.is_background_task is False, "重载成 plugin 后不许还有 flag"
    assert plugin2._bg_recheck_pending is False

    await plugin2.terminate()


@pytest.mark.asyncio
async def test_initialize_only_flags_our_own_tool(monkeypatch) -> None:
    """⑧ `initialize()` 的置位**只命中本插件**的工具：别的插件的同名工具保持 False。

    工具名是**全局命名空间**（`add_func` 先 `remove_func` 同名再追加），所以"按名字找"会
    误伤隔壁插件的 `media_analyze` —— 给它置 True 等于让**别人的**工具被核心当后台任务派发。
    判据是 `handler_module_path`（核心在激活循环里写成 `data.plugins.<目录名>`）。本格从
    `initialize()` 走一遍，确保这条过滤真的接在**加载路径**上（单元面见
    `test_core_mode.py::test_activation_only_touches_our_own_tool`）。
    """
    patch_core(monkeypatch)
    monkeypatch.setattr(core_mode, "LOG", RecordingLogger())
    foreign = make_tool(module_path="data.plugins.other_plugin.main")
    ours = make_tool()  # 默认 module_path == 生产形态（本插件）
    # 我们的放**末位**：`get_full_tool_set()` 按后加载覆盖同名，包装器读到的必须是我们的
    tmgr = FakeManager([foreign, ours])
    plugin = _new_plugin(tmgr, dict(_CORE_CONFIG))

    await plugin.initialize()

    assert plugin._bg.effective == core_mode.MODE_CORE
    assert ours.is_background_task is True
    assert foreign.is_background_task is False, "别的插件的同名工具不许被置位"

    await plugin.terminate()
    assert ours.is_background_task is False
    assert foreign.is_background_task is False


# --------------------------------------------------------------------------- #
# 3. 互斥（2）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_core_mode_keeps_every_background_table_empty(monkeypatch) -> None:
    """⑨ C 模式跑完：**三张表全空**、`stats()` 全 0、`send_message` 0 次、`event.send` 也不碰。

    `background.stats()` 只覆盖三张表的**计数**；这里直接把三个 dict 拿出来比空（连"曾经
    有过又删掉"都不允许 —— C 模式不该产生 `task_id`）。`event.send` 那一半是防"兜底投递"
    被顺手接进 C 分支（A2 的 `_deliver` 才会用它）。
    """
    await background.cancel_all()
    _patch_analyze(monkeypatch, dict(_OK_PAYLOAD))
    monkeypatch.setattr(core_mode, "LOG", RecordingLogger())
    plugin = _c_plugin()
    event = make_event(current=[])

    text = await plugin.media_analyze(event, target="current")

    assert text.startswith(core_mode.CORE_TEXT_PREFIX)
    assert background._TASKS == {}, "C 模式不建任务表（任务所有权在核心 registry）"
    assert background._BY_KEY == {}, "C 模式没有去重键（没有第二个 owner）"
    assert background._BY_UMO == {}
    assert background.stats() == {"tasks": 0, "sessions": 0, "dedupe": 0}
    assert plugin.context.calls.named("send_message") == [], "C 模式不投递（交付归唤醒轮）"
    assert event.sent == [], "兜底投递也不许碰"


@pytest.mark.asyncio
async def test_plugin_mode_task_table_lifecycle_is_unchanged(monkeypatch, tmp_path) -> None:
    """⑩ `plugin` 模式逐字不变：受理后 **1 条**、完成后 **0 条**、投递恰好 **1 次**。

    v0.4.2 只加了"另一个引擎"，没有动 A2 的任何一格。这里从**表**的角度再钉一遍
    （`test_core_mode.py` ㉑ 钉的是信封形状与 send 计数），并断言兜底 `event.send` 没被用到
    （主路成功时它必须保持 0 —— 否则就是双发）。
    """
    await background.cancel_all()
    rig = build_rig(monkeypatch, tmp_path, kind="audio")

    raw = await rig.plugin.media_analyze(rig.event, target="current")

    payload = json.loads(raw)
    assert set(payload) == set(envelope.ACCEPT_KEYS), payload
    # 受理返回时任务已入表（`submit` 里 create_task 后立即返回，本行没有让出事件循环）。
    # `dedupe=0` 是因为 rig 的事件没有 `message_id` ⇒ 去重键为空（`submit` 只在有 id 时登记）；
    # 去重面的行为在 `tests/test_background.py`，这里只钉"受理后表里有且只有 1 条"。
    assert background.stats() == {"tasks": 1, "sessions": 1, "dedupe": 0}

    finished = await background.wait(str(payload["task_id"]), timeout=60.0)
    assert finished is not None and finished["ok"] is True
    assert background.stats() == {"tasks": 0, "sessions": 0, "dedupe": 0}
    assert len(rig.context.calls.named("send_message")) == 1, "at-most-once 投递"
    assert rig.event.sent == [], "主路成功时不该走 event.send 兜底"


# --------------------------------------------------------------------------- #
# 4. C handler 契约（5）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_core_handler_success_text_is_bounded_and_prefixed(monkeypatch) -> None:
    """⑪ 经门面的成功正文：不可信前缀 + ≤1200 + 截断 `…` + 页脚仍在 + 不建表。

    单元面（`render_core`）已经钉过截断公式，这里钉的是**门面没绕过它**：
    C 分支必须走 `render_core` 这一条出口（不能"顺手"把 `envelope.dumps` 的 JSON 返回给
    唤醒轮模型 —— 那会把 `notes` 内部标记外泄并诱导模型复述结构）。
    """
    payload = dict(_OK_PAYLOAD)
    payload["summary"] = "长" * 5000
    _patch_analyze(monkeypatch, payload)
    monkeypatch.setattr(core_mode, "LOG", RecordingLogger())
    plugin = _c_plugin()

    text = await plugin.media_analyze(make_event(current=[]), target="current")

    assert text.startswith(core_mode.CORE_TEXT_PREFIX + "\n\n")
    assert len(text) <= core_mode.CORE_TEXT_MAX_CHARS
    assert "…" in text, "超长正文必须留截断标记"
    assert "长" * core_mode.CORE_BODY_MAX in text
    assert "长" * (core_mode.CORE_BODY_MAX + 1) not in text
    assert "—— 媒体分析：" in text, "页脚与 A2 同源（截断只切正文）"
    assert not text.lstrip().startswith("{"), "C 模式返回**人读文本**，不是 JSON 信封"
    assert background._TASKS == {}


@pytest.mark.parametrize(
    "outcome,code",
    [
        (envelope.ToolError(envelope.ERR_MEDIA_TOO_LONG, log_detail="duration=999.0"), envelope.ERR_MEDIA_TOO_LONG),
        (RuntimeError("SECRET-MARKER-SHOULD-NOT-APPEAR"), envelope.ERR_INTERNAL_ERROR),
        (ValueError("x" * 50), envelope.ERR_INTERNAL_ERROR),
        (OSError("EIO"), envelope.ERR_INTERNAL_ERROR),
    ],
)
@pytest.mark.asyncio
async def test_core_handler_failure_matrix_never_raises(monkeypatch, outcome, code) -> None:
    """⑫ 四类失败都收成**短文本**（不抛、不泄漏、不建表），每一类的正文逐字可对。

    为什么逐字比对而不只是"含错误码"：C 模式的正文会进**唤醒轮 system prompt**
    （SPEC-v0.4.2 §6.3），任何自由拼接都可能把 `str(exc)`、路径、`notes` 带进去。
    所以这里把整段文本钉成 `前缀 + \\n\\n + 失败行`，多一个字符都会红。
    """
    logger = _log(monkeypatch, core_mode)
    _patch_analyze(monkeypatch, outcome)
    plugin = _c_plugin()

    text = await plugin.media_analyze(make_event(current=[]), target="current")

    expected = (
        core_mode.CORE_TEXT_PREFIX
        + "\n\n"
        + f"媒体分析失败：{code}。{envelope.HINTS[code]}"
    )
    assert text == expected
    assert len(text) < core_mode.CORE_TEXT_MAX_CHARS
    assert "SECRET-MARKER-SHOULD-NOT-APPEAR" not in text
    assert "Traceback" not in text
    assert background._TASKS == {} and background._BY_KEY == {} and background._BY_UMO == {}
    assert plugin.context.calls.named("send_message") == []
    # `ToolError` 是"可预期失败"⇒ INFO；其余是"没人想到的异常"⇒ ERROR（且只打类名）
    if isinstance(outcome, envelope.ToolError):
        assert [m for m in _levels(logger, "INFO") if "rejected" in m], "受控失败要留 rejected 行"
        assert _levels(logger, "ERROR") == []
    else:
        errors = _levels(logger, "ERROR")
        assert len(errors) == 1 and type(outcome).__name__ in errors[0]


@pytest.mark.asyncio
async def test_cancelled_error_is_the_only_exception_that_escapes(monkeypatch) -> None:
    """⑬ `CancelledError` ⇒ **上抛**（核心接不住它 ⇒ 任务取消 ⇒ 不唤醒、不投递）。

    这是 C handler 唯一的"上抛"出口（⑫ 的失败矩阵已经证明其余四类都收成文本）。语义上它等价于
    用户按了停止：取消应该**静默**，而不是变成一条"媒体分析失败：INTERNAL_ERROR"发到群里。
    """
    logger = _log(monkeypatch, core_mode)
    _patch_analyze(monkeypatch, asyncio.CancelledError())
    plugin = _c_plugin()

    with pytest.raises(asyncio.CancelledError):
        await plugin.media_analyze(make_event(current=[]), target="current")

    assert any("core-mode call cancelled" in message for message in _levels(logger, "INFO"))
    assert _levels(logger, "ERROR") == []
    assert background._TASKS == {}
    assert plugin.context.calls.named("send_message") == []


def test_core_mode_module_never_references_the_a2_delivery_surface() -> None:
    """⑭ **静态结构互斥**：`core_mode.py` 只从 `background` 借 `render`，不碰投递/任务面。

    运行期的证明在 `test_core_mode.py::test_core_mode_never_touches_the_a2_tables`
    （把 `submit` / `_deliver` 打桩成抛错仍跑通）与本文件的 ⑨（三张表全空）；这一格是"将来别顺手加回来"的守卫：
    `submit` / `_deliver` / 三张表 / `create_task` 一旦出现在本模块里，C 模式就有了第二个
    任务 owner（＝被否决的"混合模式"，SPEC-v0.4.2 §6.4 / §7.1）。
    """
    source = _CORE_MODE_FILE.read_text(encoding="utf-8")
    tree = ast.parse(source)

    background_attrs = sorted(
        {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "background"
        }
    )
    assert background_attrs == ["render"], f"core_mode 只许用 background.render，实际 {background_attrs}"

    # 属性/调用面里不许出现投递与任务表（docstring 里提 `send_message_to_user` 是**说明**，
    # 不是调用 —— 所以按 AST 判，不按子串判）
    touched = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert "send_message" not in touched, "C 模式不投递（交付归唤醒轮模型）"
    for token in ("_TASKS", "_BY_KEY", "_BY_UMO", "create_task", "submit", "_deliver"):
        assert token not in touched, f"core_mode 不许碰 A2 的 {token}"


def test_core_mode_module_never_persists_anything() -> None:
    """⑮ **静态不落盘守卫**：`core_mode.py` 不写文件、不读配置文件、不碰 JSON/路径库。

    "内存级回落"的机械含义是：这个模块**没有**任何持久化能力。它不需要 `json`/`os`/
    `pathlib`/`tempfile`，也没有 `save_config` 之类的方法调用 —— 只要这几样不出现，
    "探测结果被记住 7 天"这种退化就必须先显式加一个 import，评审一眼能看见
    （SPEC-v0.4.2 §4.3：**不写配置、不持久化、不改用户选择**）。
    """
    tree = ast.parse(_CORE_MODE_FILE.read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
    forbidden_modules = imported & {"json", "os", "pathlib", "shutil", "tempfile", "pickle"}
    assert not forbidden_modules, f"core_mode 不许有持久化能力，却 import 了 {sorted(forbidden_modules)}"

    forbidden_calls = {"open", "write_text", "write_bytes", "dump", "dumps", "unlink", "remove", "save_config"}
    called: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            called.add(func.id)
        elif isinstance(func, ast.Attribute):
            called.add(func.attr)
    assert not (called & forbidden_calls), f"core_mode 不许落盘，却调用了 {sorted(called & forbidden_calls)}"
