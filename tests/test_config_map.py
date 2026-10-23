"""配置键的**四处**一致守卫 + `ConfigView` 行为（SPEC-v0.3 §8.2；v0.2 §10 的 C1 / C2）。

为什么单开一个文件：本插件的配置键要同时出现在**四处** ——
`_conf_schema.json`（WebUI 渲染 + `check_config_integrity()` 的合法键集合）、
`settings.KEY_MAP`（点号路径 ⇄ 字段名的映射）、`main._settings()`（显式白名单读取点）、
`tools/migrate_config_v03.py:KEY_ORDER`（运维侧迁移器的报告顺序）。
漏任何一处都是**静默故障**：父手册 §3 第 8 条那次事故里，两个旋钮只写进了 schema 与消费方，
漏了 `main` 的白名单，生产链路上设置被丢掉一整天（v0.2 是 4 处 × 11 键，v0.3 是 4 处 × 21 键）。

本文件把四处钉成双射，并且**证明守卫本身会咬人**（planted-key 反例），
否则"守卫永远绿"和"没有守卫"是同一件事。

迁移器刻意**不 import 插件模块**（它是独立脚本，由 ops/fix 在插件树之外跑），所以它那份
`KEY_ORDER` 只能在这里做交叉检查 —— 这也是"第四处"没有别的守卫的原因。
"""

from __future__ import annotations

import ast
import dataclasses
import importlib.util
import json
from pathlib import Path

import pytest

from astrbot_plugin_media_analysis import main, settings

_PLUGIN_DIR = Path(__file__).resolve().parents[1]
_MIGRATOR_V03 = _PLUGIN_DIR / "tools" / "migrate_config_v03.py"

#: SPEC-v0.3 §8.1 冻结的 **21** 个点号键（写死在这里 = 契约快照；要加键必须先改 SPEC）。
#: 其中 `provider_settings.stt_provider_id` 是**弃用键**：它必须继续留在 schema 里，
#: 否则核心 `check_config_integrity()` 会把用户配置里的旧值当 stale 键删掉并落盘
#: （`astrbot_config.py:252-266` + `:113-121`）。代码只读不生效，迁移脚本负责搬值。
EXPECTED_LEAVES: frozenset[str] = frozenset(
    {
        "provider_settings.audio_provider_id",
        "provider_settings.video_provider_id",
        "provider_settings.vision_provider_id",
        "provider_settings.stt_provider_id",
        "video.probe_enabled",
        "video.probe_ttl_seconds",
        "video.max_direct_bytes",
        "video.native_timeout_seconds",
        "video.native_max_tokens",
        "video.compress_enabled",
        "video.compress_target_bytes",
        "video.compress_timeout_seconds",
        "prompts.audio",
        "prompts.video",
        "prompts.frame",
        "prompts.summary",
        "prompts.video_native",
        "limits.max_media_seconds",
        "limits.max_frames",
        "limits.max_media_bytes",
        "cache.ttl_seconds",
    }
)

#: 分组顺序 == WebUI 渲染顺序（v0.3 在 `provider_settings` 之后插入了 `video` 组）
EXPECTED_GROUPS = ["provider_settings", "video", "prompts", "limits", "cache"]


# --------------------------------------------------------------------------- #
# 取样工具
# --------------------------------------------------------------------------- #
def _schema() -> dict:
    return json.loads((_PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))


def _schema_leaf_order(schema: dict) -> list[str]:
    """按 schema 的书写顺序取叶子（`type: object` 且有 `items` 的是分组）。"""
    order: list[str] = []

    def walk(node: dict, prefix: str = "") -> None:
        for key, spec in node.items():
            path = f"{prefix}{key}"
            items = spec.get("items") if isinstance(spec, dict) else None
            if isinstance(items, dict) and items:
                walk(items, path + ".")
            else:
                order.append(path)

    walk(schema)
    return order


def _schema_leaves(schema: dict) -> dict[str, dict]:
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


def _load_migrator_v03():
    """按路径加载迁移器（独立脚本，不属于包 ⇒ 不能 `from astrbot_plugin_media_analysis`）。"""
    assert _MIGRATOR_V03.is_file(), f"迁移器不见了：{_MIGRATOR_V03}"
    spec = importlib.util.spec_from_file_location(
        "migrate_config_v03_under_test", _MIGRATOR_V03
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _migrator_keys() -> set[str]:
    return {key for key, _type in _load_migrator_v03().KEY_ORDER}


def _four_way_diff(
    schema_leaves: set[str],
    key_map: dict[str, str],
    read_points: set[str],
    key_order: set[str],
) -> dict[str, list[str]]:
    """四方差集；全空表示四处完全一致。守卫的"咬人"能力由它体现。"""
    return {
        "schema_only": sorted(
            schema_leaves - set(key_map) - read_points - key_order
        ),
        "key_map_only": sorted(set(key_map) - schema_leaves - read_points - key_order),
        "read_only": sorted(read_points - schema_leaves - set(key_map) - key_order),
        "key_order_only": sorted(key_order - schema_leaves - set(key_map) - read_points),
        "schema_not_read": sorted(schema_leaves - read_points),
        "key_map_not_read": sorted(set(key_map) - read_points),
        "key_order_not_read": sorted(key_order - read_points),
        "key_map_not_in_key_order": sorted(set(key_map) - key_order),
    }


# --------------------------------------------------------------------------- #
# C1：schema 叶子 ⇄ KEY_MAP ⇄ main._settings() ⇄ 迁移器 KEY_ORDER（四处 21 键）
# --------------------------------------------------------------------------- #
def test_schema_has_exactly_the_frozen_twenty_one_leaves() -> None:
    """契约：SPEC-v0.3 §8.1 的 21 个键；多一个少一个都红（要加键先改 SPEC）。"""
    leaves = _schema_leaves(_schema())
    assert set(leaves) == EXPECTED_LEAVES, f"schema 叶子漂了：{sorted(leaves)}"
    assert len(leaves) == 21


def test_schema_groups_are_object_with_description_and_hint() -> None:
    """WebUI 渲染前提：分组必须是 object，且带 description / hint（v0.3 多一个 `video` 组）。"""
    schema = _schema()
    assert list(schema) == EXPECTED_GROUPS, "分组顺序即渲染顺序"
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
    assert len(read_points) == 21


def test_migrator_key_order_matches_key_map_exactly() -> None:
    """第四处：`tools/migrate_config_v03.py:KEY_ORDER` 的键集合 == `settings.KEY_MAP`。

    迁移器不 import 插件模块 ⇒ 这份 `KEY_ORDER` 是与 `KEY_MAP` 的**手工对偶**。少一个键
    意味着 `--report` 不会报告它（运维以为"这个键已经就位"），多一个键则会在 `plan()` 里
    补出一个 schema 不认识的键（核心下次加载会把它当 stale 删掉并落盘）。
    """
    assert _migrator_keys() == set(settings.KEY_MAP)


def test_key_order_shares_the_schema_writing_order() -> None:
    """四处不仅**集合**相等，**顺序**也要一致：schema 书写顺序 == `KEY_ORDER` == `KEY_MAP`。

    顺序是 WebUI 渲染顺序 + 迁移器报告顺序；错位会让"第 7 行是什么键"在对文档时对不上，
    也容易在下一次加键时插错位置（然后靠人肉数行数发现）。
    """
    schema_order = _schema_leaf_order(_schema())
    key_order = [key for key, _type in _load_migrator_v03().KEY_ORDER]
    assert schema_order == key_order
    assert schema_order == list(settings.KEY_MAP)


def test_four_way_diff_is_empty_for_the_real_tree() -> None:
    """四方差集全空；顺带把视图都打出来，红的时候一眼看出是哪一边多了/少了。"""
    diff = _four_way_diff(
        set(_schema_leaves(_schema())),
        settings.KEY_MAP,
        _read_points(_main_src()),
        _migrator_keys(),
    )
    assert diff == {
        "schema_only": [],
        "key_map_only": [],
        "read_only": [],
        "key_order_only": [],
        "schema_not_read": [],
        "key_map_not_read": [],
        "key_order_not_read": [],
        "key_map_not_in_key_order": [],
    }, diff


def test_guard_bites_when_a_key_is_added_in_only_one_place() -> None:
    """守卫必须会咬人：**四种**"漏一处"都要能被差集抓到（否则等于没有守卫）。"""
    schema = set(EXPECTED_LEAVES)
    key_map = dict(settings.KEY_MAP)
    read = set(_read_points(_main_src()))
    key_order = _migrator_keys()

    # ① 只在 schema 里加了键
    diff = _four_way_diff(schema | {"limits.brand_new"}, key_map, read, key_order)
    assert diff["schema_not_read"] == ["limits.brand_new"]
    assert diff["schema_only"] == ["limits.brand_new"]

    # ② 只在 KEY_MAP 里加了键
    diff = _four_way_diff(
        schema, {**key_map, "limits.brand_new": "brand_new"}, read, key_order
    )
    assert diff["key_map_not_read"] == ["limits.brand_new"]
    assert diff["key_map_only"] == ["limits.brand_new"]
    assert diff["key_map_not_in_key_order"] == ["limits.brand_new"]

    # ③ 只在 main 里读了键（schema / KEY_MAP / KEY_ORDER 都没有）
    diff = _four_way_diff(schema, key_map, read | {"limits.brand_new"}, key_order)
    assert diff["read_only"] == ["limits.brand_new"]

    # ④ 只在迁移器 KEY_ORDER 里加了键
    diff = _four_way_diff(schema, key_map, read, key_order | {"limits.brand_new"})
    assert diff["key_order_only"] == ["limits.brand_new"]
    assert diff["key_order_not_read"] == ["limits.brand_new"]

    # ⑤ 从 main 里删掉一个读取点（最危险的一种：schema 与 KEY_MAP 还在）
    diff = _four_way_diff(schema, key_map, read - {settings.KEY_MAX_FRAMES}, key_order)
    assert diff["schema_not_read"] == [settings.KEY_MAX_FRAMES]
    assert diff["key_map_not_read"] == [settings.KEY_MAX_FRAMES]
    assert diff["key_order_not_read"] == [settings.KEY_MAX_FRAMES]

    # ⑥ 弃用键也不能漏读：`stt_provider_id` 若从白名单消失，旧值就再也没人读得到
    #    （它的唯一用途就是"读出来打一次弃用告警"与迁移脚本对照）
    diff = _four_way_diff(
        schema, key_map, read - {settings.KEY_LEGACY_STT_PROVIDER_ID}, key_order
    )
    assert diff["schema_not_read"] == [settings.KEY_LEGACY_STT_PROVIDER_ID]

    # ⑦ v0.3 的**新**键同样四处在册（挑一个最容易被漏掉的：只有迁移器认识它）
    only_key = settings.KEY_VIDEO_COMPRESS_TIMEOUT_SECONDS
    diff = _four_way_diff(
        schema - {only_key},
        {k: v for k, v in key_map.items() if k != only_key},
        read - {only_key},
        key_order,
    )
    assert diff["key_order_only"] == [only_key]


def test_key_map_values_are_exactly_the_settings_fields() -> None:
    """C2：`KEY_MAP` 的值集合 == `Settings` 的字段集合（21 个，双射）。

    派生量（`prompts` / `prompts_trimmed` / `prompts_fp` / `video_fp` /
    `compress_budget_bytes`）走 `property` / `cached_property`，**不是**字段 ——
    它们不是配置键，不该出现在 `KEY_MAP` 里，也不该混进 dataclass 字段。
    """
    fields = {field.name for field in dataclasses.fields(settings.Settings)}
    assert fields == set(settings.KEY_MAP.values())
    assert len(fields) == 21
    derived = {
        "prompts",
        "prompts_trimmed",
        "prompts_fp",
        "video_fp",
        "compress_budget_bytes",
        "_effective_prompts",
    }
    assert derived & fields == set(), "派生量混进了 dataclass 字段"


def test_settings_dataclass_is_frozen_and_defaults_match_schema() -> None:
    """schema 的 `default` 必须与代码默认值一致 —— 否则 WebUI 显示的默认值与实际行为不符。

    v0.3 的 10 个新键逐个比对（值、类型都是"人以为配了但其实没配"的高发区）。
    """
    leaves = _schema_leaves(_schema())

    int_defaults = {
        settings.KEY_MAX_MEDIA_SECONDS: settings.DEFAULT_MAX_MEDIA_SECONDS,
        settings.KEY_MAX_FRAMES: settings.DEFAULT_MAX_FRAMES,
        settings.KEY_MAX_MEDIA_BYTES: settings.DEFAULT_MAX_MEDIA_BYTES,
        settings.KEY_CACHE_TTL_SECONDS: settings.DEFAULT_CACHE_TTL_SECONDS,
        settings.KEY_VIDEO_PROBE_TTL_SECONDS: settings.DEFAULT_VIDEO_PROBE_TTL_SECONDS,
        settings.KEY_VIDEO_MAX_DIRECT_BYTES: settings.DEFAULT_VIDEO_MAX_DIRECT_BYTES,
        settings.KEY_VIDEO_NATIVE_TIMEOUT_SECONDS: settings.DEFAULT_VIDEO_NATIVE_TIMEOUT_SECONDS,
        settings.KEY_VIDEO_NATIVE_MAX_TOKENS: settings.DEFAULT_VIDEO_NATIVE_MAX_TOKENS,
        settings.KEY_VIDEO_COMPRESS_TARGET_BYTES: settings.DEFAULT_VIDEO_COMPRESS_TARGET_BYTES,
        settings.KEY_VIDEO_COMPRESS_TIMEOUT_SECONDS: settings.DEFAULT_VIDEO_COMPRESS_TIMEOUT_SECONDS,
    }
    for key, default in int_defaults.items():
        assert leaves[key]["default"] == default, f"{key} 的 schema 默认值与代码默认值漂了"

    bool_defaults = {
        settings.KEY_VIDEO_PROBE_ENABLED: settings.DEFAULT_VIDEO_PROBE_ENABLED,
        settings.KEY_VIDEO_COMPRESS_ENABLED: settings.DEFAULT_VIDEO_COMPRESS_ENABLED,
    }
    for key, default in bool_defaults.items():
        assert leaves[key]["default"] == default, f"{key} 的 schema 默认值与代码默认值漂了"
        assert isinstance(leaves[key]["default"], bool), f"{key} 必须是真布尔"
        assert leaves[key]["type"] == "bool", f"{key} 的 schema type 必须是 bool"

    for key in (
        settings.KEY_AUDIO_PROVIDER_ID,
        settings.KEY_VIDEO_PROVIDER_ID,
        settings.KEY_VISION_PROVIDER_ID,
        settings.KEY_LEGACY_STT_PROVIDER_ID,
    ):
        assert leaves[key]["default"] == "", f"{key} 的默认值该是空串"

    # 五份提示词的 schema 默认值 == 内置默认模板（逐字）：WebUI 里看到的就是会发出去的那份
    for name in settings.PROMPT_KEYS:
        leaf = leaves[f"prompts.{name}"]
        assert leaf["default"] == settings.DEFAULT_PROMPTS[name], f"prompts.{name} 默认值漂了"
        assert leaf["default"].strip(), f"prompts.{name} 默认值不能是空串"

    defaults = settings.Settings()
    assert defaults.max_media_seconds == settings.DEFAULT_MAX_MEDIA_SECONDS
    assert defaults.max_frames == settings.DEFAULT_MAX_FRAMES
    assert defaults.max_media_bytes == settings.DEFAULT_MAX_MEDIA_BYTES
    assert defaults.cache_ttl_seconds == settings.DEFAULT_CACHE_TTL_SECONDS
    assert defaults.audio_provider_id == "" and defaults.vision_provider_id == ""
    assert defaults.video_provider_id == ""
    assert defaults.legacy_stt_provider_id == ""
    assert (
        defaults.video_probe_enabled,
        defaults.video_probe_ttl_seconds,
        defaults.video_max_direct_bytes,
        defaults.video_native_timeout_seconds,
        defaults.video_native_max_tokens,
        defaults.video_compress_enabled,
        defaults.video_compress_target_bytes,
        defaults.video_compress_timeout_seconds,
    ) == (
        settings.DEFAULT_VIDEO_PROBE_ENABLED,
        settings.DEFAULT_VIDEO_PROBE_TTL_SECONDS,
        settings.DEFAULT_VIDEO_MAX_DIRECT_BYTES,
        settings.DEFAULT_VIDEO_NATIVE_TIMEOUT_SECONDS,
        settings.DEFAULT_VIDEO_NATIVE_MAX_TOKENS,
        settings.DEFAULT_VIDEO_COMPRESS_ENABLED,
        settings.DEFAULT_VIDEO_COMPRESS_TARGET_BYTES,
        settings.DEFAULT_VIDEO_COMPRESS_TIMEOUT_SECONDS,
    )
    assert (defaults.prompt_audio, defaults.prompt_video) == ("", "")
    assert (defaults.prompt_frame, defaults.prompt_summary) == ("", "")
    assert defaults.prompt_video_native == ""

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
    """四个 provider id 必须带 `_special`，否则 WebUI 只给一个裸输入框（SPEC §5.2）。

    弃用键保留 `select_provider_stt`：它与真实类型一致（那个位置历史上存的就是 STT id），
    改成 `select_provider` 会让 WebUI 只列出 chat provider —— 用户改不了旧值，
    却会把旧值误读成"合法的音频模型"。
    """
    leaves = _schema_leaves(_schema())
    assert leaves[settings.KEY_AUDIO_PROVIDER_ID]["_special"] == "select_provider"
    assert leaves[settings.KEY_VIDEO_PROVIDER_ID]["_special"] == "select_provider"
    assert leaves[settings.KEY_VISION_PROVIDER_ID]["_special"] == "select_provider"
    assert leaves[settings.KEY_LEGACY_STT_PROVIDER_ID]["_special"] == "select_provider_stt"


def test_direct_bytes_clamp_ceiling_matches_the_memory_hard_cap() -> None:
    """钳位上限 32 MiB **必须**等于 `video_native.HARD_MAX_DIRECT_BYTES`（SPEC-v0.3 §10.2-③）。

    两个数字漂开会出现两种坏法：钳位比硬上限大 ⇒ 用户配到 32 MiB 之外，每调用一次都在
    `call_native` 里 stat 失败（多一次白跑）；钳位比硬上限小 ⇒ 硬上限永远碰不到，
    内存护栏就成了摆设。
    """
    from astrbot_plugin_media_analysis import video_native

    assert (
        settings.MAX_VIDEO_MAX_DIRECT_BYTES == video_native.HARD_MAX_DIRECT_BYTES
    ), "配置钳位上限与内存硬上限必须同值"
    assert settings.DEFAULT_VIDEO_MAX_DIRECT_BYTES <= settings.MAX_VIDEO_MAX_DIRECT_BYTES


def test_only_settings_module_reads_config_keys() -> None:
    """唯一读取点的守卫：`settings.KEY_*` 只允许出现在 `main._settings()` 里。

    散落的读取点会让四方守卫失效（加键时容易漏），也会让"配置改动需重启"的假设失效。
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
    view = _view(
        {
            "limits": {"max_frames": 4},
            "provider_settings": {"stt_provider_id": "x"},
            "cache": {"ttl_seconds": 0},
            "video": {"max_direct_bytes": 1048576},
        }
    )
    assert view.get("limits.max_frames") == 4
    assert view.get("provider_settings.stt_provider_id") == "x"
    assert view.get("cache.ttl_seconds") == 0, "0 是合法值（＝关闭缓存），不能回 default"
    assert view.get("video.max_direct_bytes") == 1048576
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


def test_configview_flag_accepts_only_real_booleans(monkeypatch: pytest.MonkeyPatch) -> None:
    """`flag()`：真布尔原样；`0/1`/`"true"` 一律回默认 + 一条告警（不猜方向）。

    v0.3 的两个布尔键（`video.probe_enabled` / `video.compress_enabled`）都会**花钱**
    （自动探测一次调用 / 自动压缩一次转码），所以"猜成 true"的代价比 v0.2 更实在。
    """
    from _fakes import RecordingLogger

    log = RecordingLogger()
    monkeypatch.setattr(settings, "LOG", log)

    view = _view({"video": {"probe_enabled": True, "compress_enabled": "true"}})
    assert view.flag("video.probe_enabled", default=False) is True
    assert view.flag("video.compress_enabled", default=False) is False, "字符串不许当布尔"
    assert len([1 for level, _m in log.records if level == "WARNING"]) == 1

    # 缺键 ⇒ 默认值（不告警）
    log.clear()
    assert view.flag("video.missing", default=True) is True
    assert log.records == []


def test_main_settings_assembles_all_twenty_one_keys() -> None:
    """`main._settings()` 的 21 个字段都能从一份配置里取到（含钳位与去空白）。"""
    plugin = main.MediaAnalysisPlugin(
        context=None,
        config={
            "provider_settings": {
                "audio_provider_id": "  a1 ",
                "video_provider_id": " vid1 ",
                "vision_provider_id": "v1",
                "stt_provider_id": "  old_stt ",
            },
            "video": {
                "probe_enabled": False,
                "probe_ttl_seconds": 0,
                "max_direct_bytes": 1,
                "native_timeout_seconds": 9999,
                "native_max_tokens": 64,
                "compress_enabled": False,
                "compress_target_bytes": 99999999999,
                "compress_timeout_seconds": 1,
            },
            "prompts": {
                "audio": "  A{focus_clause}  ",
                "summary": "S{material}",
                "video_native": "  N{focus_clause}  ",
            },
            "limits": {"max_media_seconds": 1, "max_frames": 99, "max_media_bytes": 1},
            "cache": {"ttl_seconds": -5},
        },
    )
    snapshot = plugin._settings()
    assert snapshot == settings.Settings(
        audio_provider_id="a1",
        video_provider_id="vid1",
        vision_provider_id="v1",
        legacy_stt_provider_id="old_stt",
        video_probe_enabled=False,
        video_probe_ttl_seconds=settings.MIN_VIDEO_PROBE_TTL_SECONDS,
        video_max_direct_bytes=settings.MIN_VIDEO_MAX_DIRECT_BYTES,
        video_native_timeout_seconds=settings.MAX_VIDEO_NATIVE_TIMEOUT_SECONDS,
        video_native_max_tokens=settings.MIN_VIDEO_NATIVE_MAX_TOKENS,
        video_compress_enabled=False,
        video_compress_target_bytes=settings.MAX_VIDEO_COMPRESS_TARGET_BYTES,
        video_compress_timeout_seconds=settings.MIN_VIDEO_COMPRESS_TIMEOUT_SECONDS,
        max_media_seconds=settings.MIN_MAX_MEDIA_SECONDS,
        max_frames=settings.MAX_MAX_FRAMES,
        max_media_bytes=settings.MIN_MAX_MEDIA_BYTES,
        prompt_audio="A{focus_clause}",
        prompt_summary="S{material}",
        prompt_video_native="N{focus_clause}",
        cache_ttl_seconds=settings.MIN_CACHE_TTL_SECONDS,
    )
    # 未给的键回空串（`prompts.video` / `prompts.frame` 没配）
    assert snapshot.prompt_video == "" and snapshot.prompt_frame == ""
    # 钳位是"防手滑"，不是产品承诺：native_max_tokens 的下限是 1024（护栏①）
    assert snapshot.video_native_max_tokens == 1024


def test_main_settings_on_empty_config_returns_defaults() -> None:
    plugin = main.MediaAnalysisPlugin(context=None, config=None)
    assert plugin._settings() == settings.Settings()


# --------------------------------------------------------------------------- #
# 派生量：压缩预算与视频指纹（都是"配置的函数"，但**不是**配置键）
# --------------------------------------------------------------------------- #
def test_compress_budget_is_the_min_of_target_and_direct_limit() -> None:
    """`compress_budget_bytes = min(compress_target_bytes, max_direct_bytes)`（SPEC-v0.3 §6.1）。

    配得比门槛还大时按**小者**算：压到比门槛还大等于没压（那份产物仍会被直送闸拒掉）。
    """
    assert (
        settings.Settings(
            video_compress_target_bytes=8 * 1024 * 1024,
            video_max_direct_bytes=4 * 1024 * 1024,
        ).compress_budget_bytes
        == 4 * 1024 * 1024
    )
    assert (
        settings.Settings(
            video_compress_target_bytes=2 * 1024 * 1024,
            video_max_direct_bytes=8 * 1024 * 1024,
        ).compress_budget_bytes
        == 2 * 1024 * 1024
    )
    assert settings.Settings().compress_budget_bytes == settings.DEFAULT_VIDEO_COMPRESS_TARGET_BYTES


def test_video_fp_is_a_stable_function_of_three_config_values() -> None:
    """`video_fp` = `sha256("max_direct|compress_enabled|target")[:8]`（SPEC-v0.3 §9.1 冻结公式）。

    三个分量各改一个都必须换指纹；其它 `video.*`（探测 / 超时 / max_tokens）**不在**里面
    —— 那些不影响产物内容，进键只会让缓存无谓失效。
    """
    import hashlib

    base = settings.Settings()
    material = (
        f"{base.video_max_direct_bytes}|{base.video_compress_enabled}|"
        f"{base.video_compress_target_bytes}"
    )
    assert base.video_fp == hashlib.sha256(material.encode("utf-8")).hexdigest()[:8]
    assert len(base.video_fp) == 8

    variants = [
        settings.Settings(video_max_direct_bytes=4 * 1024 * 1024),
        settings.Settings(video_compress_enabled=False),
        settings.Settings(video_compress_target_bytes=2 * 1024 * 1024),
    ]
    assert len({base.video_fp, *(v.video_fp for v in variants)}) == 4

    # 不影响产物内容的三个键：指纹不变
    assert (
        settings.Settings(
            video_probe_enabled=False,
            video_probe_ttl_seconds=0,
            video_native_timeout_seconds=110,
            video_native_max_tokens=4096,
        ).video_fp
        == base.video_fp
    )


def test_suggest_direct_bytes_table_is_frozen() -> None:
    """内置建议表（SPEC-v0.3 §4）：Gemini flash 8 MiB / mimo 4 MiB / 未知 4 MiB。

    这张表**不参与运行期判定**（运行期只认 `video.max_direct_bytes`，否决项 §16.2-6），
    它只用于 `ready` 日志提示与校准工具；mimo 口述的 "<50 MB" 按 §16.3-4 **不进表**。
    """
    assert settings.suggest_direct_bytes("gemini-3-flash-preview") == 8 * 1024 * 1024
    assert settings.suggest_direct_bytes("Gemini-3-FLASH") == 8 * 1024 * 1024
    assert settings.suggest_direct_bytes("mimo-v2.6-chat") == 4 * 1024 * 1024
    assert settings.suggest_direct_bytes("mimo-anything") == 4 * 1024 * 1024
    assert settings.suggest_direct_bytes("gpt-4o") == 4 * 1024 * 1024
    assert settings.suggest_direct_bytes("") == 4 * 1024 * 1024, "空模型名也要给保守值"
    # 表里没有 >8 MiB 的条目：t8 实测无体积上限，建议值只由**延迟**决定
    assert max(value for _pattern, value in settings.VIDEO_DIRECT_BYTES_SUGGESTIONS) == 8 * 1024 * 1024


# --------------------------------------------------------------------------- #
# 评审 F5：`native_max_tokens` 默认 2048（mimo 在 8 MiB + 长提示词下要 4096 才有正文）
# --------------------------------------------------------------------------- #
def test_native_max_tokens_default_is_2048_and_the_floor_stays_1024() -> None:
    """默认值翻倍、**下限不动**：护栏① 是"不许低于 1024"，不是"默认就该 1024"。"""
    snapshot = settings.Settings()
    assert snapshot.video_native_max_tokens == 2048
    assert settings.DEFAULT_VIDEO_NATIVE_MAX_TOKENS == 2048
    assert settings.MIN_VIDEO_NATIVE_MAX_TOKENS == 1024, "护栏① 的下限不许被顺手改掉"
    assert settings.MAX_VIDEO_NATIVE_MAX_TOKENS == 4096
    # 三处默认值互钉（schema / settings / 迁移器）——F5 改的是这三个地方
    leaves = _schema_leaves(_schema())
    assert leaves["video.native_max_tokens"]["default"] == 2048
    assert "4096" in leaves["video.native_max_tokens"].get("hint", ""), (
        "schema 的 hint 要点明 mimo 场景建议 4096（否则运维只会看到 2048）"
    )


@pytest.mark.asyncio
async def test_ready_line_reports_both_the_direct_gate_and_max_tokens(monkeypatch) -> None:
    """`ready` 行同时打直送门槛与 `native_max_tokens`（评审 F5 的运维要求）。

    真调 `initialize()`（不是自己拼字符串），用 `RecordingLogger` 抓那一行 —— 这是本仓库
    既有的日志断言姿势（`astrbot.api.logger` 是包装器，caplog 抓不到）。
    """
    from _fakes import RecordingLogger

    from astrbot_plugin_media_analysis import main as main_mod

    log = RecordingLogger()
    monkeypatch.setattr(main_mod, "LOG", log)
    config = {
        "provider_settings": {"video_provider_id": "vid-x"},
        "video": {"max_direct_bytes": 4 * 1024 * 1024, "native_max_tokens": 2048},
        "cache": {"ttl_seconds": 0},  # 关闭缓存 ⇒ 不建目录、不碰磁盘
    }
    plugin = main_mod.MediaAnalysisPlugin(context=None, config=config)
    await plugin.initialize()
    await plugin.terminate()

    ready = [message for level, message in log.records if level == "INFO" and "ready:" in message]
    assert len(ready) == 1, log.records
    line = ready[0] % () if "%" not in ready[0] else ready[0]
    assert "video_provider_id='vid-x'" in line
    assert "video.max_direct_bytes=4194304" in line
    assert "native_max_tokens=2048" in line
