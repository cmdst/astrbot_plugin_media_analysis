"""配置键的三方一致守卫 + `ConfigView` 行为（SPEC §10 的 C1 / C2）。

为什么单开一个文件：本插件的配置键要同时出现在**三处** ——
`_conf_schema.json`（WebUI 渲染 + `check_config_integrity()` 的合法键集合）、
`settings.KEY_MAP`（点号路径 ⇄ 字段名的映射）、`main._settings()`（显式白名单读取点）。
漏任何一处都是**静默故障**：父手册 §3 第 8 条那次事故里，两个旋钮只写进了 schema 与消费方，
漏了 `main` 的白名单，生产链路上设置被丢掉一整天。

本文件把三处钉成双射，并且**证明守卫本身会咬人**（planted-key 反例），
否则"守卫永远绿"和"没有守卫"是同一件事。
"""

from __future__ import annotations

import ast
import dataclasses
import json
from pathlib import Path

import pytest

from astrbot_plugin_media_analysis import main, settings

_PLUGIN_DIR = Path(__file__).resolve().parents[1]

#: SPEC §4.2 冻结的 4 个点号键（写死在这里 = 契约快照；要加键必须先改 SPEC）。
EXPECTED_LEAVES: frozenset[str] = frozenset(
    {
        "provider_settings.stt_provider_id",
        "provider_settings.vision_provider_id",
        "limits.max_media_seconds",
        "limits.max_frames",
    }
)


# --------------------------------------------------------------------------- #
# 取样工具
# --------------------------------------------------------------------------- #
def _schema() -> dict:
    return json.loads((_PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))


def _schema_leaves(schema: dict) -> dict[str, dict]:
    """递归取叶子（`type: object` 且有 `items` 的是分组）。"""
    leaves: dict[str, dict] = {}

    def walk(node: dict, prefix: str = "") -> None:
        for key, spec in node.items():
            path = f"{prefix}{key}"
            items = spec.get("items") if isinstance(spec, dict) else None
            if isinstance(items, dict) and items:
                walk(items, path + ".")
            else:
                leaves[path] = spec

    walk(schema)
    return leaves


def _main_src() -> str:
    return (_PLUGIN_DIR / "main.py").read_text(encoding="utf-8")


def _read_points(source: str) -> set[str]:
    """AST 扫 `main._settings()` 里 `settings.KEY_*` 的引用，解析成点号键。"""
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or not node.attr.startswith("KEY_"):
            continue
        base = node.value
        if isinstance(base, ast.Name) and base.id == "settings":
            found.add(getattr(settings, node.attr))
    return found


def _three_way_diff(
    schema_leaves: set[str], key_map: dict[str, str], read_points: set[str]
) -> dict[str, list[str]]:
    """三方差集；全空表示三处完全一致。守卫的"咬人"能力由它体现。"""
    return {
        "schema_only": sorted(schema_leaves - set(key_map) - read_points),
        "key_map_only": sorted(set(key_map) - schema_leaves - read_points),
        "read_only": sorted(read_points - schema_leaves - set(key_map)),
        "schema_not_read": sorted(schema_leaves - read_points),
        "key_map_not_read": sorted(set(key_map) - read_points),
    }


# --------------------------------------------------------------------------- #
# C1：schema 叶子 ⇄ KEY_MAP 键 ⇄ main._settings() 读取点（三处 4 条，双射）
# --------------------------------------------------------------------------- #
def test_schema_has_exactly_the_frozen_four_leaves() -> None:
    """契约：SPEC §4.2 的 4 个键；多一个少一个都红（要加键先改 SPEC）。"""
    leaves = _schema_leaves(_schema())
    assert set(leaves) == EXPECTED_LEAVES, f"schema 叶子漂了：{sorted(leaves)}"
    assert len(leaves) == 4


def test_schema_groups_are_object_with_description_and_hint() -> None:
    """WebUI 渲染前提：分组必须是 object，且带 description / hint。"""
    schema = _schema()
    assert list(schema) == ["provider_settings", "limits"], "分组顺序即渲染顺序"
    for group, spec in schema.items():
        assert spec["type"] == "object", f"{group} 必须是 object"
        assert spec.get("description"), f"{group} 缺 description"
        assert spec.get("hint"), f"{group} 缺 hint"


def test_schema_leaves_match_key_map_exactly() -> None:
    """C1：schema 叶子集合 == `settings.KEY_MAP` 键集合。"""
    leaves = set(_schema_leaves(_schema()))
    assert leaves == set(settings.KEY_MAP), "schema 叶子与 KEY_MAP 必须双射"


def test_main_read_points_match_key_map_exactly() -> None:
    """C1：`main._settings()` 里出现的 `settings.KEY_*` == KEY_MAP 键集合（漏读＝生产静默丢键）。"""
    read_points = _read_points(_main_src())
    assert read_points == set(settings.KEY_MAP), f"读取点漂了：{sorted(read_points)}"
    assert len(read_points) == 4


def test_three_way_diff_is_empty_for_the_real_tree() -> None:
    """三方差集全空；顺带把四条视图都打出来，红的时候一眼看出是哪一边多了/少了。"""
    diff = _three_way_diff(
        set(_schema_leaves(_schema())), settings.KEY_MAP, _read_points(_main_src())
    )
    assert diff == {
        "schema_only": [],
        "key_map_only": [],
        "read_only": [],
        "schema_not_read": [],
        "key_map_not_read": [],
    }, diff


def test_guard_bites_when_a_key_is_added_in_only_one_place() -> None:
    """守卫必须会咬人：三种"漏一处"都要能被差集抓到（否则等于没有守卫）。"""
    schema = set(EXPECTED_LEAVES)
    key_map = dict(settings.KEY_MAP)
    read = set(_read_points(_main_src()))

    # ① 只在 schema 里加了键
    diff = _three_way_diff(schema | {"limits.brand_new"}, key_map, read)
    assert diff["schema_not_read"] == ["limits.brand_new"]
    assert diff["schema_only"] == ["limits.brand_new"]

    # ② 只在 KEY_MAP 里加了键
    diff = _three_way_diff(schema, {**key_map, "limits.brand_new": "brand_new"}, read)
    assert diff["key_map_not_read"] == ["limits.brand_new"]
    assert diff["key_map_only"] == ["limits.brand_new"]

    # ③ 只在 main 里读了键（schema 与 KEY_MAP 都没有）
    diff = _three_way_diff(schema, key_map, read | {"limits.brand_new"})
    assert diff["read_only"] == ["limits.brand_new"]

    # ④ 从 main 里删掉一个读取点（最危险的一种：schema 与 KEY_MAP 还在）
    diff = _three_way_diff(schema, key_map, read - {settings.KEY_MAX_FRAMES})
    assert diff["schema_not_read"] == [settings.KEY_MAX_FRAMES]
    assert diff["key_map_not_read"] == [settings.KEY_MAX_FRAMES]


def test_key_map_values_are_exactly_the_settings_fields() -> None:
    """C2：`KEY_MAP` 的值集合 == `Settings` 的字段集合（4 个，双射）。"""
    fields = {field.name for field in dataclasses.fields(settings.Settings)}
    assert fields == set(settings.KEY_MAP.values())
    assert len(fields) == 4


def test_settings_dataclass_is_frozen_and_defaults_match_schema() -> None:
    """schema 的 `default` 必须与代码默认值一致 —— 否则 WebUI 显示的默认值与实际行为不符。"""
    leaves = _schema_leaves(_schema())
    assert leaves[settings.KEY_MAX_MEDIA_SECONDS]["default"] == settings.DEFAULT_MAX_MEDIA_SECONDS
    assert leaves[settings.KEY_MAX_FRAMES]["default"] == settings.DEFAULT_MAX_FRAMES
    assert leaves[settings.KEY_STT_PROVIDER_ID]["default"] == ""
    assert leaves[settings.KEY_VISION_PROVIDER_ID]["default"] == ""

    defaults = settings.Settings()
    assert defaults.max_media_seconds == settings.DEFAULT_MAX_MEDIA_SECONDS
    assert defaults.max_frames == settings.DEFAULT_MAX_FRAMES
    assert defaults.stt_provider_id == "" and defaults.vision_provider_id == ""

    with pytest.raises(dataclasses.FrozenInstanceError):
        defaults.max_frames = 3  # type: ignore[misc]


def test_schema_leaf_types_are_renderable_by_core() -> None:
    """核心 `DEFAULT_VALUE_MAP` 只认那几种 `type`；`int`/`string` 都在白名单内。"""
    allowed = {"int", "float", "bool", "string", "text", "list", "file", "object", "template_list", "dict"}
    for path, spec in _schema_leaves(_schema()).items():
        assert spec.get("type") in allowed, f"{path} 的 type={spec.get('type')!r} 核心渲染不了"
        assert spec.get("description"), f"{path} 缺 description"
        assert spec.get("hint"), f"{path} 缺 hint"
        assert "default" in spec, f"{path} 缺 default"


def test_provider_id_leaves_declare_webui_special() -> None:
    """两个 provider id 必须带 `_special`，否则 WebUI 只给一个裸输入框（SPEC §2.7）。"""
    leaves = _schema_leaves(_schema())
    assert leaves[settings.KEY_STT_PROVIDER_ID]["_special"] == "select_provider_stt"
    assert leaves[settings.KEY_VISION_PROVIDER_ID]["_special"] == "select_provider"


def test_only_settings_module_reads_config_keys() -> None:
    """唯一读取点的守卫：`settings.KEY_*` 只允许出现在 `main._settings()` 里。

    散落的读取点会让三方守卫失效（加键时容易漏），也会让"配置改动需重启"的假设失效。
    """
    offenders: list[str] = []
    for py in sorted(_PLUGIN_DIR.glob("*.py")):
        source = py.read_text(encoding="utf-8")
        if "KEY_" not in source:
            continue
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Attribute)
                    and inner.attr.startswith("KEY_")
                    and isinstance(inner.value, ast.Name)
                    and inner.value.id == "settings"
                ):
                    if not (py.name == "main.py" and node.name == "_settings"):
                        offenders.append(f"{py.name}:{node.name}:{inner.attr}")
    assert not offenders, f"配置读取点散落到了别处：{offenders}"


# --------------------------------------------------------------------------- #
# ConfigView 行为（点号读取 / 钳位；`AstrBotConfig` 自己没有点号 get）
# --------------------------------------------------------------------------- #
def _view(raw):
    return settings.ConfigView(raw)


def test_configview_reads_dotted_paths() -> None:
    view = _view({"limits": {"max_frames": 4}, "provider_settings": {"stt_provider_id": "x"}})
    assert view.get("limits.max_frames") == 4
    assert view.get("provider_settings.stt_provider_id") == "x"
    assert view.get("limits.missing", "fallback") == "fallback"


def test_configview_treats_empty_containers_as_legal_values() -> None:
    """空 dict / 空 list 是**合法值**，不能被 default 顶掉；只有 None 才回 default。"""
    view = _view({"a": {"b": {}}, "c": {"d": []}, "e": {"f": None}})
    assert view.get("a.b", "default") == {}
    assert view.get("c.d", "default") == []
    assert view.get("e.f", "default") == "default"


def test_configview_tolerates_odd_shapes() -> None:
    view = _view(None)
    assert view.get("anything") is None
    assert view.text("anything") == ""
    assert _view({"a": 3}).get("a.b", "x") == "x"  # 中间层不是 mapping
    assert _view([]).get("a", "x") == "x"  # 非 Mapping 的根


def test_configview_text_strips_and_rejects_non_str() -> None:
    view = _view({"s": "  hello  ", "n": 3, "none": None})
    assert view.text("s") == "hello"
    assert view.text("n") == ""
    assert view.text("none") == ""


def test_configview_bounded_int_clamps_and_falls_back() -> None:
    view = _view(
        {
            "limits": {
                "low": 1,
                "high": 9999,
                "ok": 42,
                "str": "7",
                "bad": "abc",
                "flag": True,
                "f": 6.9,
            }
        }
    )
    assert view.bounded_int("limits.low", default=60, lo=5, hi=300) == 5
    assert view.bounded_int("limits.high", default=60, lo=5, hi=300) == 300
    assert view.bounded_int("limits.ok", default=60, lo=5, hi=300) == 42
    assert view.bounded_int("limits.str", default=60, lo=5, hi=300) == 7
    assert view.bounded_int("limits.bad", default=60, lo=5, hi=300) == 60
    assert view.bounded_int("limits.flag", default=60, lo=5, hi=300) == 60
    assert view.bounded_int("limits.f", default=6, lo=1, hi=12) == 6
    assert view.bounded_int("limits.missing", default=6, lo=1, hi=12) == 6


def test_main_settings_assembles_all_four_keys() -> None:
    """`main._settings()` 的四个字段都能从一份配置里取到（含钳位）。"""
    plugin = main.MediaAnalysisPlugin(
        context=None,
        config={
            "provider_settings": {"stt_provider_id": "  s1 ", "vision_provider_id": "v1"},
            "limits": {"max_media_seconds": 1, "max_frames": 99},
        },
    )
    snapshot = plugin._settings()
    assert snapshot == settings.Settings(
        stt_provider_id="s1",
        vision_provider_id="v1",
        max_media_seconds=settings.MIN_MAX_MEDIA_SECONDS,
        max_frames=settings.MAX_MAX_FRAMES,
    )


def test_main_settings_on_empty_config_returns_defaults() -> None:
    plugin = main.MediaAnalysisPlugin(context=None, config=None)
    assert plugin._settings() == settings.Settings()
