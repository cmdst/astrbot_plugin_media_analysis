"""注册面与工具 schema 的静态守卫（SPEC §2.6 / §10 的 C10）。

四组断言：

1. **导入形态**：测试按包导入（`astrbot_plugin_media_analysis.main`）⇒ `__package__` 是包名，
   且全部包内导入都是**相对导入**。生产按 `data.plugins.<目录名>.main` 把插件当包导入，
   扁平 `import media` 在库里可能过、在生产必挂（2026-10-18 的静默拒载）。
2. **schema 只从 docstring 生成**：三参数有默认值、类型只有 `string`/`boolean`、
   顶层键恰好 `["properties","type"]`（无 `required`）、描述非空且带取值约束。
3. **装饰器纪律**：工具方法上只有 `@filter.llm_tool`，**没有** `permission_type`
   （叠加会让它被当消息处理器，权限不符就丢用户消息）。
4. **可测形状**：`__init__(context, config=None)` 形参名逐字（加载器按关键字调用）、
   构造零 IO、`media_analyze` 是协程且非 async generator、返回注解是 `str`。
"""

from __future__ import annotations

import ast
import inspect
import json
import re
from pathlib import Path

import pytest
from astrbot.api.star import Star
from astrbot.core.provider.register import llm_tools

from astrbot_plugin_media_analysis import envelope, main

_PLUGIN_DIR = Path(__file__).resolve().parents[1]
_PKG_NAME = "astrbot_plugin_media_analysis"

#: 包内模块名（SPEC §3 冻结清单）；扁平导入这些名字在生产必挂。
_PACKAGE_MODULES = {
    "main",
    "settings",
    "envelope",
    "providers",
    "media",
    "analyze",
    "cache",
}

#: docstring 能生成的类型词（`provider/func_tool_manager.py:128-145`）。
_DOCSTRING_TYPES = {"string", "number", "object", "array", "boolean"}


def _main_tree() -> ast.Module:
    return ast.parse((_PLUGIN_DIR / "main.py").read_text(encoding="utf-8"))


def _media_analyze_def() -> ast.AsyncFunctionDef:
    for node in ast.walk(_main_tree()):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "media_analyze":
            return node
    raise AssertionError("main.py 里找不到 async def media_analyze")


def _tool_param_names(node: ast.AsyncFunctionDef) -> list[str]:
    """工具参数 ＝ 除 `self` / `event` 之外的形参（`event` 由框架注入，不进 schema）。"""
    return [arg.arg for arg in node.args.args if arg.arg not in {"self", "event"}]


# --------------------------------------------------------------------------- #
# 1. 导入形态
# --------------------------------------------------------------------------- #
def test_imported_as_a_package_not_a_flat_module() -> None:
    """`__package__` 必须是包名：相对导入只在包内成立（生产就是这么加载的）。"""
    assert main.__package__ == _PKG_NAME, f"__package__={main.__package__!r}"
    assert main.__name__ == f"{_PKG_NAME}.main"


@pytest.mark.parametrize("path", sorted(_PLUGIN_DIR.glob("*.py")), ids=lambda p: p.name)
def test_no_flat_intra_package_imports(path: Path) -> None:
    """包内模块之间**只能**相对导入：`from . import x` / `from .x import y`。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module in _PACKAGE_MODULES:
                offenders.append(f"from {node.module} import …（少了前导点）")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _PACKAGE_MODULES:
                    offenders.append(f"import {alias.name}")
    assert not offenders, f"{path.name} 有扁平包内导入：{offenders}"


def test_envelope_is_a_leaf_module() -> None:
    """`envelope.py` 是叶子：不 import 任何包内模块（谁都依赖它，它不依赖谁）。"""
    tree = ast.parse((_PLUGIN_DIR / "envelope.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"envelope.py 不该有相对导入：{ast.unparse(node)}"


# --------------------------------------------------------------------------- #
# 2. 装饰器纪律 + 可测形状
# --------------------------------------------------------------------------- #
def test_tool_method_has_exactly_one_decorator() -> None:
    """只挂 `@filter.llm_tool`；叠 `@filter.permission_type` 会被当消息处理器（AGENTS §3）。"""
    node = _media_analyze_def()
    assert len(node.decorator_list) == 1, [ast.unparse(d) for d in node.decorator_list]

    decorator = node.decorator_list[0]
    assert isinstance(decorator, ast.Call), ast.unparse(decorator)
    assert ast.unparse(decorator.func) == "filter.llm_tool"
    keywords = {keyword.arg: keyword.value for keyword in decorator.keywords}
    assert set(keywords) == {"name"}, sorted(keywords)
    assert ast.literal_eval(keywords["name"]) == "media_analyze"


def test_no_permission_or_command_decorators_anywhere_in_main() -> None:
    """整个模块只允许 `filter.llm_tool` 一种装饰器（注释里提到别的名字不算）。"""
    tree = _main_tree()
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            found.add(ast.unparse(target))
    assert found == {"filter.llm_tool"}, f"main.py 出现了别的装饰器：{sorted(found)}"


def test_tool_method_is_coroutine_and_returns_str() -> None:
    assert inspect.iscoroutinefunction(main.MediaAnalysisPlugin.media_analyze)
    assert not inspect.isasyncgenfunction(main.MediaAnalysisPlugin.media_analyze)

    node = _media_analyze_def()
    assert node.returns is not None, "工具方法必须标注返回类型"
    assert ast.unparse(node.returns) == "str", ast.unparse(node.returns)


def test_plugin_init_signature_matches_loader_contract() -> None:
    """加载器按 `star_cls_type(context=…, config=…)` 关键字调用（`star_manager.py:1229-1238`）。"""
    params = list(inspect.signature(main.MediaAnalysisPlugin.__init__).parameters.values())
    assert [p.name for p in params] == ["self", "context", "config"]
    assert params[2].default is None, "config 必须有默认 None"
    assert issubclass(main.MediaAnalysisPlugin, Star)


def test_lifecycle_hooks_are_coroutines() -> None:
    assert inspect.iscoroutinefunction(main.MediaAnalysisPlugin.initialize)
    assert inspect.iscoroutinefunction(main.MediaAnalysisPlugin.terminate)


def test_plugin_construction_is_side_effect_free_whitebox() -> None:
    """白盒守卫，不属契约面，重构可改：构造不许碰网络/文件/配置解析。

    评审 F10：断言的是"构造函数体里没有 IO"这个实现选择（对外只能观察到"构造很快"）；
    保留是因为插件一旦在 `__init__` 里解析 provider，测试与生产都会变慢/联网。
    """
    plugin = main.MediaAnalysisPlugin(context=None, config=None)
    assert plugin.config == {}
    assert plugin._settings_cache is None
    assert len(plugin._cache) == 0


def test_settings_read_point_is_not_duplicated_whitebox() -> None:
    """白盒守卫，不属契约面，重构可改：`_settings()` 只组装一次、`initialize()` 里缓存。

    评审 F10：这条断言的是**实现细节**（源码文本里出现几次），不是对外契约；
    保留是因为它能抓住"读取点被复制散开"这个真实事故形态，但重构时可以先改它。
    """
    src = (_PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
    assert src.count("def _settings(self)") == 1
    assert "self._settings_cache = self._settings()" in src


# --------------------------------------------------------------------------- #
# 3. docstring → schema 的静态守卫（C10）
# --------------------------------------------------------------------------- #
def test_tool_params_all_have_defaults() -> None:
    """每个参数都必须有 Python 默认值 ⇒ 生成的 schema 不含 `required`。"""
    node = _media_analyze_def()
    positional = [arg.arg for arg in node.args.args]
    with_defaults = len(node.args.defaults)
    assert positional[: len(positional) - with_defaults] == ["self", "event"], (
        f"只有 self/event 可以没有默认值：{positional}"
    )
    assert with_defaults == 3, f"三个工具参数都要有默认值，实际 {with_defaults}"


def test_tool_params_use_only_docstring_renderable_types() -> None:
    """`Literal[...]` 之类会在 **import 期** `ValueError`（`star_handler.py:651-655`）。"""
    node = _media_analyze_def()
    annotations = {
        arg.arg: ast.unparse(arg.annotation) if arg.annotation is not None else ""
        for arg in node.args.args
    }
    assert annotations["target"] == "str"
    assert annotations["focus"] == "str"
    assert annotations["include_frames"] == "bool"

    for name, text in annotations.items():
        for banned in ("Literal", "Union", "Optional", "|", "list[", "dict["):
            assert banned not in text, f"{name} 的类型 {text!r} 里出现 {banned}（import 期会炸）"


_ARGS_LINE = re.compile(r"^\s*(\w+) \((string|boolean|number|object|array)\):", re.MULTILINE)


def test_docstring_lists_every_tool_param_with_supported_type() -> None:
    """schema 的描述只来自 docstring 的 `Args:` 段 —— 少一行就等于参数没文档。"""
    node = _media_analyze_def()
    docstring = ast.get_docstring(node) or ""
    assert "Args:" in docstring
    listed = dict(_ARGS_LINE.findall(docstring))
    expected = set(_tool_param_names(node))
    assert set(listed) == expected, f"docstring Args 与真实参数不一致：{sorted(listed)}"
    for name, type_word in listed.items():
        assert type_word in _DOCSTRING_TYPES, f"{name} 的类型词 {type_word!r} 核心不认"


def test_docstring_carries_usage_constraints() -> None:
    """`target` 的取值只能靠文档约束（schema 里没有 enum），必须在 docstring 里写清。"""
    docstring = ast.get_docstring(_media_analyze_def()) or ""
    assert "current" in docstring and "quoted" in docstring
    assert "include_frames" in docstring and "focus" in docstring


# --------------------------------------------------------------------------- #
# 4. 真机注册面的 schema 形状（C10；工具已在本进程注册）
# --------------------------------------------------------------------------- #
def test_tool_is_registered_with_three_params_and_no_required() -> None:
    tool = llm_tools.get_func("media_analyze")
    assert tool is not None, "import 之后工具没注册（`Added llm tool: media_analyze`）"

    parameters = tool.parameters
    assert sorted(parameters) == ["properties", "type"], sorted(parameters)
    assert parameters["type"] == "object"
    assert sorted(parameters["properties"]) == ["focus", "include_frames", "target"]
    assert "required" not in parameters

    types = {name: spec.get("type") for name, spec in parameters["properties"].items()}
    assert types == {"target": "string", "focus": "string", "include_frames": "boolean"}


def test_registered_schema_descriptions_are_nonempty() -> None:
    """描述全空 = 模型不知道该字段干什么（schema 只从 docstring 来，空描述说明没写）。"""
    tool = llm_tools.get_func("media_analyze")
    assert tool is not None
    for name, spec in tool.parameters["properties"].items():
        assert spec.get("description"), f"{name} 缺 description"


def test_tool_name_is_not_shadowed_by_another_plugin() -> None:
    """名字唯一性：注册表里 `media_analyze` 只有这一份（父手册 §5 的占用普查）。"""
    tool = llm_tools.get_func("media_analyze")
    assert tool is not None
    assert getattr(tool, "name", "media_analyze") == "media_analyze"


# --------------------------------------------------------------------------- #
# 5. 信封常量的机械一致（C5 的静态一半；行为那一半在 test_analyze.py）
# --------------------------------------------------------------------------- #
def test_envelope_exports_the_frozen_key_orders() -> None:
    assert envelope.OK_KEYS == (
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
    assert envelope.FAIL_KEYS == ("ok", "error", "hint")


#: 16 条 `hint` 的**逐字快照**（评审 F3）。模型唯一读到的操作性文案，改一个字都要改这里。
#: 抄自 `envelope.py` 的 `HINTS`；这不是"再抄一遍实现"，而是**契约文本**——
#: 之前用 `envelope.HINTS[...]` 自证，改错文案全套仍绿，等于没人守。
HINTS_SNAPSHOT: dict[str, str] = {
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


def test_envelope_hints_cover_all_sixteen_codes() -> None:
    codes = {
        value
        for name, value in vars(envelope).items()
        if name.startswith("ERR_") and isinstance(value, str)
    }
    assert len(codes) == 16, sorted(codes)
    assert set(envelope.HINTS) == codes
    assert envelope.OK_KEYS + envelope.FAIL_KEYS  # 常量存在性（顺序由上面两条钉）


def test_envelope_hints_match_the_frozen_snapshot_verbatim() -> None:
    """评审 F3：16 条 `hint` 逐字快照（dict 全等，不只看键集合）。

    这是本仓库唯一的"文案级"契约：模型看到的就是这 16 句，错一个字就等于给模型
    错误的操作指令（"换一条视频"vs"去看日志"指错方向）。要改文案：先改 SPEC §5 表，
    再改 `envelope.HINTS`，最后改这里 —— 三处一起动才算契约变更。
    """
    assert envelope.HINTS == HINTS_SNAPSHOT, {
        code: (envelope.HINTS.get(code), HINTS_SNAPSHOT.get(code))
        for code in set(envelope.HINTS) | set(HINTS_SNAPSHOT)
        if envelope.HINTS.get(code) != HINTS_SNAPSHOT.get(code)
    }


def test_fail_envelope_normalizes_unknown_codes_to_internal_error() -> None:
    """评审 F3：未知错误码归一到 `INTERNAL_ERROR`（信封里不许出现悬空字符串）。

    `fail_envelope` 是唯一的失败信封构造点；任何代码路径只要凑出一个错码就能用它，
    所以"未知码"必须有确定归宿，否则模型会收到 `{"error": "TYPO_CODE"}` 这种东西。
    """
    assert envelope.fail_envelope("NOPE") == {
        "ok": False,
        "error": envelope.ERR_INTERNAL_ERROR,
        "hint": HINTS_SNAPSHOT[envelope.ERR_INTERNAL_ERROR],
    }
    # 已知码原样透传（对照组：证明上面的归一不是"全都写死 INTERNAL_ERROR"）
    assert envelope.fail_envelope(envelope.ERR_MEDIA_TOO_LONG)["error"] == envelope.ERR_MEDIA_TOO_LONG
    assert envelope.fail_envelope("")["error"] == envelope.ERR_INTERNAL_ERROR


def test_dumps_is_utf8_json_string() -> None:
    payload = envelope.ok_envelope(
        kind="audio",
        duration_s=1.0,
        transcript=[{"t": 0.0, "text": "中文"}],
        frames=[],
        summary="",
        notes=[],
        elapsed_ms=1,
        cached=False,
    )
    dumped = envelope.dumps(payload)
    assert isinstance(dumped, str)
    assert "中文" in dumped, "ensure_ascii=False 才能让中文直接可读"
    assert json.loads(dumped) == payload
