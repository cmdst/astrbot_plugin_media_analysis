"""核心补丁的**逐字模拟**：把 `_PermissionGuardedTool` 换成"已补一行镜像"的子类。

为什么需要它（SPEC-v0.4.2 §1.2 / §11.1）：路线 C 的唯一前置条件是核心在
`_PermissionGuardedTool.__init__` 的镜像区补**一行**：

```python
self.is_background_task = getattr(tool, "is_background_task", False)   # ← 核心 4.29.1 缺这一行
```

`get_full_tool_set()` 每次调用都按**模块级名字** `func_tool_manager._PermissionGuardedTool`
新建包装器（`core/provider/func_tool_manager.py:496-517`）⇒ 用**子类**替换那个模块属性即
**逐字模拟**核心补丁，不改核心源码、不动生产树（本 lane 的红线）。

两条纪律：

1. 一律用 `pytest.MonkeyPatch`（`patch_core(monkeypatch)`）替换，**测试结束自动复原**；
   `unpatch()` 只作为手工复原的逃生门。
2. 本模块是 `tests/` 的 helper（**0 格**）：它自己不做断言，只提供 `patch_core` /
   `unpatch` / `make_tool` / `FakeManager`，让"未修核心"与"已修核心"两个世界可切换。
"""

from __future__ import annotations

from astrbot.core.agent.tool import FunctionTool
from astrbot.core.provider import func_tool_manager as ftm
from astrbot.core.provider.func_tool_manager import FunctionToolManager

#: 核心原装的包装器类（手工复原用；`monkeypatch` 路径不需要它）
_ORIGINAL = ftm._PermissionGuardedTool

#: `handler_module_path` 的**生产形态**（核心在激活循环里按 `data.plugins.<目录名>` 写，
#: 见 `core/star/star_manager.py:1289-1297`）。测试用同一个字符串，才能覆盖
#: `core_mode._is_ours()` 的真实判据。
MODULE_PATH = "data.plugins.astrbot_plugin_media_analysis.main"


class PatchedGuardedTool(_ORIGINAL):  # type: ignore[misc,valid-type]
    """`_PermissionGuardedTool` + **一行镜像**（= 核心补丁的确切形状）。"""

    def __init__(self, tool, manager) -> None:
        super().__init__(tool, manager)
        # ↓↓↓ 这一行就是核心补丁的全部内容（镜像，不改权限判定）↓↓↓
        self.is_background_task = getattr(tool, "is_background_task", False)


def patch_core(monkeypatch=None, *, fm_module=None) -> None:
    """让 `get_full_tool_set()` 用"已修"的包装器。

    两种用法（两种都受守卫，选一种即可）：

    - `patch_core(monkeypatch)`：推荐。`monkeypatch` 在用例结束时**自动复原**；
    - `patch_core(fm_module=...)`：显式指定 `func_tool_manager` 模块，**手工复原**
      （用 `unpatch()`）—— 只在没有 `monkeypatch` 的场合（脚本/探针）用。
    """
    module = fm_module if fm_module is not None else ftm
    if monkeypatch is not None:
        monkeypatch.setattr(module, "_PermissionGuardedTool", PatchedGuardedTool)
        return
    module._PermissionGuardedTool = PatchedGuardedTool


def unpatch() -> None:
    """手工复原（只在没有 `monkeypatch` 的场合用）。"""
    ftm._PermissionGuardedTool = _ORIGINAL


def is_patched() -> bool:
    """当前 `func_tool_manager` 用的是不是"已修"的包装器（诊断用）。"""
    return ftm._PermissionGuardedTool is PatchedGuardedTool


async def _noop_handler(event, **kwargs):  # pragma: no cover —— 从不被调用
    return "noop"


def make_tool(
    name: str = "media_analyze",
    *,
    module_path: str | None = MODULE_PATH,
    flag: bool = False,
    active: bool = True,
) -> FunctionTool:
    """造一个**真** `FunctionTool`（不碰全局 `llm_tools`，每个用例自己一套）。"""
    tool = FunctionTool(
        name=name,
        description="test tool",
        parameters={"type": "object", "properties": {}},
        handler=_noop_handler,
    )
    tool.handler_module_path = module_path
    tool.is_background_task = flag
    tool.active = active
    return tool


class FakeManager(FunctionToolManager):
    """**真** `FunctionToolManager` + 预置若干 `FuncTool`（不读全局注册表）。"""

    def __init__(self, tools=()) -> None:
        super().__init__()
        for tool in tools:
            self.func_list.append(tool)


class ExplodingManager(FakeManager):
    """`get_full_tool_set()` 抛异常的管理器（测 `probe_error`：探测不许把插件的加载带崩）。"""

    def get_full_tool_set(self):  # noqa: D401 —— 故意抛
        raise RuntimeError("boom from get_full_tool_set")
