"""配置面的**四处**一致守卫 + schema 渲染纪律 + 加载期告警 + `ready` 行
（SPEC-v0.5.1 §2 / §7 / §8 / §10；前身见 SPEC-v0.3 §8.2、v0.4.2 §3.1）。

为什么单开一个文件：本插件的配置键要同时出现在**四处** ——
`_conf_schema.json`（WebUI 渲染 + `check_config_integrity()` 的合法键集合）、
`settings.KEY_MAP`（点号路径 ⇄ 字段名的映射）、`main._settings()`（显式白名单读取点）、
`tools/migrate_config_v05.py:KEY_ORDER`（运维侧迁移器的报告顺序；**v0.5.1 起第四处换成
它**，v0.4.2 与 v0.3 的脚本**冻结为历史快照**，改由"22 键写死元组 + 21 键前缀"两条断言守住）。
漏任何一处都是**静默故障**：父手册 §3 第 8 条那次事故里，两个旋钮只写进了 schema 与消费方，
漏了 `main` 的白名单，生产链路上设置被丢掉一整天（v0.2 = 4 处 × 11 键，v0.3 = 4 处 × 21 键，
v0.4.2 = 4 处 × 22 键，**v0.5.1 = 4 处 × 21 键**）。

本文件钉五组东西：

1. **G1 键表**：21 叶快照 / 6 组组序 / 9 个数值叶的 `slider` 与「（允许范围：X–Y）」hint /
   4 个隐藏叶 / 四处互钉（集合 + 顺序）；
2. **G2 冻结**：`migrate_config_v042.py:KEY_ORDER` == **写死的 22 键元组**（不再比
   `settings.KEY_MAP`），v0.3 的 21 键仍是 v042 的 21 键前缀；
3. **MB 语义**：`bytes = MB × 1048576`、默认 8 / 6 / 64、half-up 换算边界表；
4. **`ready` 行有效值**（§7）：新 token 全在、旧字节字段全无、两个 cap 由**公式**算出；
5. **加载期告警三条 + 启动期零噪声**（§8）：固定 id / 固定文案 / 每 id 每进程一次 /
   provider 不可解析只打 DEBUG 且顺延到首次调用 / 加载期**不调** `get_provider_by_id`。

迁移器刻意**不 import 插件模块**（它是独立脚本，由 ops/fix 在插件树之外跑），所以它那份
`KEY_ORDER` / 默认值 / `bytes_to_mb` 只能在这里与 `tests/test_migrate_config_v05.py` 做交叉
检查 —— 这也是"第四处"没有别的守卫的原因。
"""

from __future__ import annotations

import ast
import dataclasses
import importlib.util
import json
import logging
import re
from pathlib import Path

import pytest

from astrbot_plugin_media_analysis import analyze, main, settings, video_native

_PLUGIN_DIR = Path(__file__).resolve().parents[1]
_MIGRATOR_V05 = _PLUGIN_DIR / "tools" / "migrate_config_v05.py"
_MIGRATOR_V042 = _PLUGIN_DIR / "tools" / "migrate_config_v042.py"
_MIGRATOR_V03 = _PLUGIN_DIR / "tools" / "migrate_config_v03.py"

#: SPEC-v0.5.1 §2.3 冻结的 **21** 叶，**顺序即契约**（面板渲染序 = `KEY_MAP` 序 =
#: 迁移器 `KEY_ORDER` 序）。其中 `provider_settings.stt_provider_id` 是**弃用键**：
#: 它必须继续留在 schema 里（删了核心会把用户旧值当 stale 删掉并落盘），只是面板隐藏。
EXPECTED_ORDER: tuple[str, ...] = (
    "background.mode",
    "provider_settings.audio_provider_id",
    "provider_settings.video_provider_id",
    "provider_settings.vision_provider_id",
    "provider_settings.stt_provider_id",
    "video.max_direct_mb",
    "video.compress_enabled",
    "video.compress_target_mb",
    "video.compress_timeout_seconds",
    "video.probe_enabled",
    "video.probe_ttl_seconds",
    "limits.max_media_seconds",
    "limits.max_frames",
    "limits.max_media_mb",
    "limits.audio_timeout_seconds",
    "cache.ttl_seconds",
    "prompts.audio",
    "prompts.video",
    "prompts.frame",
    "prompts.summary",
    "prompts.video_native",
)
EXPECTED_LEAVES: frozenset[str] = frozenset(EXPECTED_ORDER)

#: 组序（SPEC-v0.5.1 §2.3 冻死）：`background` 置顶（引擎开关不许藏最后一组）、
#: `prompts` 置末（五段长文本不再插在中间）。
EXPECTED_GROUPS = ["background", "provider_settings", "video", "limits", "cache", "prompts"]

#: 4 个**隐藏**叶（item 级 `invisible: true`）：键与值都保留，只是面板不渲染。
EXPECTED_HIDDEN = frozenset(
    {
        "provider_settings.stt_provider_id",
        "video.compress_timeout_seconds",
        "video.probe_enabled",
        "video.probe_ttl_seconds",
    }
)

#: 9 个数值叶的 `(slider.min, slider.max, slider.step)` —— 与 `settings` 的钳位常量
#: **逐值相等**（由 `test_range_hint_matches_slider_and_settings_clamps` 断言）。
EXPECTED_SLIDERS: dict[str, tuple[int, int, int]] = {
    "video.max_direct_mb": (1, 32, 1),
    "video.compress_target_mb": (1, 32, 1),
    "video.compress_timeout_seconds": (10, 60, 5),
    "video.probe_ttl_seconds": (0, 2592000, 86400),
    "limits.max_media_seconds": (5, 300, 5),
    "limits.max_frames": (1, 12, 1),
    "limits.max_media_mb": (1, 512, 1),
    "limits.audio_timeout_seconds": (10, 95, 5),
    "cache.ttl_seconds": (0, 2592000, 3600),
}

#: 面板**可见**的 MB 键（这些叶的 description/hint 必须按 MB 措辞，§2.5-5）
VISIBLE_MB_KEYS = ("video.max_direct_mb", "video.compress_target_mb", "limits.max_media_mb")

#: v0.5.1 已删除的**点号**旧键（`grep` 级静态守卫用；正文不许再出现）
LEGACY_KEY_PATHS: tuple[str, ...] = (
    "video.max_direct_bytes",
    "video.compress_target_bytes",
    "video.native_max_tokens",
    "video.native_timeout_seconds",
    "limits.max_media_bytes",
)


# --------------------------------------------------------------------------- #
# 夹具：加载期告警的去重集合是**进程级**的（§8.1），每个用例前后清空
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _reset_config_check_dedup():
    main._CONFIG_CHECK_DONE.clear()
    yield
    main._CONFIG_CHECK_DONE.clear()


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


def _read_points(source: str, function_name: str = "_settings") -> set[str]:
    """AST 扫**指定函数体**里 `settings.KEY_*` 的引用，解析成点号键。

    t5-F6：`ast.walk(整棵模块)` 会把函数体之外的同形引用也算进来 —— 最典型的是
    `_config_check` 里的 `settings.KEY_AUDIO_TIMEOUT_SECONDS`（拼日志用的**派生用法**，
    不读值）。那会让"读点集合 == 键表"这条断言被一处日志文案悄悄加固/放宽：
    加一条日志就可能让漏读的键看起来"被读了"。
    做法与 `test_only_settings_module_reads_config_keys` 一致：**先定位 `FunctionDef`，
    只在它的子树里扫**（同名的重载/嵌套函数不影响：`ast.walk` 会覆盖到）。
    """
    tree = ast.parse(source)
    found: set[str] = set()
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef) or func.name != function_name:
            continue
        for node in ast.walk(func):
            if not isinstance(node, ast.Attribute) or not node.attr.startswith("KEY_"):
                continue
            base = node.value
            if isinstance(base, ast.Name) and base.id == "settings":
                found.add(getattr(settings, node.attr))
    return found


def _load_migrator(path, name: str):
    """按路径加载迁移器（独立脚本，不属于包 ⇒ 不能 `from astrbot_plugin_media_analysis`）。"""
    assert path.is_file(), f"迁移器不见了：{path}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _migrator_v05():
    """当前迁移器（21 键）：**第四处**的事实源（v0.5.1 起）。"""
    return _load_migrator(_MIGRATOR_V05, "migrate_config_v05_under_test")


def _migrator_v042():
    """v0.4.2 迁移器：**冻结为历史快照**（22 键）。"""
    return _load_migrator(_MIGRATOR_V042, "migrate_config_v042_frozen_under_test")


def _migrator_v03():
    """v0.2→v0.3 的**历史快照**（21 键）：只用于"前缀关系"那条断言。"""
    return _load_migrator(_MIGRATOR_V03, "migrate_config_v03_frozen_under_test")


def _four_way_diff(
    schema_leaves: set[str],
    key_map: dict[str, str],
    read_points: set[str],
    key_order: set[str],
) -> dict[str, list[str]]:
    """四方差集；全空表示四处完全一致。守卫的"咬人"能力由它体现。"""
    return {
        "schema_only": sorted(schema_leaves - set(key_map) - read_points - key_order),
        "key_map_only": sorted(set(key_map) - schema_leaves - read_points - key_order),
        "read_only": sorted(read_points - schema_leaves - set(key_map) - key_order),
        "key_order_only": sorted(key_order - schema_leaves - set(key_map) - read_points),
        "schema_not_read": sorted(schema_leaves - read_points),
        "key_map_not_read": sorted(set(key_map) - read_points),
        "key_order_not_read": sorted(key_order - read_points),
        "key_map_not_in_key_order": sorted(set(key_map) - key_order),
    }


# --------------------------------------------------------------------------- #
# G1：schema 叶子 ⇄ KEY_MAP ⇄ main._settings() ⇄ 迁移器 KEY_ORDER（四处 21 键）
# --------------------------------------------------------------------------- #
def test_schema_has_exactly_the_frozen_twenty_one_leaves() -> None:
    """契约：SPEC-v0.5.1 §2.3 的 21 个键；多一个少一个都红（先改 SPEC）。"""
    leaves = _schema_leaves(_schema())
    assert set(leaves) == EXPECTED_LEAVES, f"schema 叶子漂了：{sorted(leaves)}"
    assert len(leaves) == 21


def test_schema_groups_are_object_with_description_and_hint() -> None:
    """WebUI 渲染前提：6 组、组序冻结、每组分组的 `description` / `hint` 齐全。"""
    schema = _schema()
    assert list(schema) == EXPECTED_GROUPS, "分组顺序即渲染顺序（background 置顶 / prompts 置末）"
    for group, spec in schema.items():
        assert spec["type"] == "object", f"{group} 必须是 object"
        assert spec.get("description"), f"{group} 缺 description"
        assert spec.get("hint"), f"{group} 缺 hint"
        assert "invisible" not in spec, f"{group} 是分组，隐藏只许写在**叶子**上（§2.5-3）"


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
    """第四处：`tools/migrate_config_v05.py:KEY_ORDER` 的键集合 == `settings.KEY_MAP`。

    迁移器不 import 插件模块 ⇒ 这份 `KEY_ORDER` 是与 `KEY_MAP` 的**手工对偶**。少一个键
    意味着 `--report` 不会报告它（运维以为"这个键已经就位"），多一个键则会在 `plan()` 里
    补出一个 schema 不认识的键（核心下次加载会把它当 stale 删掉并落盘）。
    """
    assert {key for key, _type in _migrator_v05().KEY_ORDER} == set(settings.KEY_MAP)


def test_key_order_shares_the_schema_writing_order() -> None:
    """四处不仅**集合**相等，**顺序**也要一致：schema 书写序 == `KEY_ORDER` == `KEY_MAP`。

    顺序是 WebUI 渲染顺序 + 迁移器报告顺序，而且**核心在加载期会按 schema 序重排配置并再存
    一次盘**（SPEC-v0.5.1 §1.3-3）⇒ 迁移器落盘序错位，就不可能满足「重载后 sha 不变」。
    """
    schema_order = _schema_leaf_order(_schema())
    key_order = [key for key, _type in _migrator_v05().KEY_ORDER]
    assert schema_order == list(EXPECTED_ORDER)
    assert schema_order == key_order
    assert schema_order == list(settings.KEY_MAP)


def test_four_way_diff_is_empty_for_the_real_tree() -> None:
    """四方差集全空；顺带把视图都打出来，红的时候一眼看出是哪一边多了/少了。"""
    diff = _four_way_diff(
        set(_schema_leaves(_schema())),
        settings.KEY_MAP,
        _read_points(_main_src()),
        {key for key, _type in _migrator_v05().KEY_ORDER},
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
    """守卫必须会咬人：**五种**"漏一处"都要能被差集抓到（否则等于没有守卫）。"""
    schema = set(EXPECTED_LEAVES)
    key_map = dict(settings.KEY_MAP)
    read = set(_read_points(_main_src()))
    key_order = {key for key, _type in _migrator_v05().KEY_ORDER}

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

    # ⑥ v0.5.1 的**新键**同样四处在册（挑最容易漏的：只有 schema 与 KEY_MAP 认识它）
    only_key = settings.KEY_AUDIO_TIMEOUT_SECONDS
    diff = _four_way_diff(
        schema,
        key_map,
        read - {only_key},
        key_order - {only_key},
    )
    assert diff["schema_not_read"] == [only_key]
    assert diff["key_order_only"] == []


# --------------------------------------------------------------------------- #
# G1：schema 渲染纪律（slider / 范围 hint / 隐藏 / MB 措辞）
# --------------------------------------------------------------------------- #
def _numeric_leaves() -> list[tuple[str, dict]]:
    return sorted(
        (path, spec)
        for path, spec in _schema_leaves(_schema()).items()
        if spec.get("type") in ("int", "float")
    )


def test_numeric_leaves_are_exactly_the_nine_frozen_ones() -> None:
    """数值叶恰好 9 个（三个 MB 键 + 新增 audio_timeout + 五个既有 int）。"""
    assert [path for path, _spec in _numeric_leaves()] == sorted(EXPECTED_SLIDERS)


def test_every_numeric_leaf_has_a_slider_and_no_top_level_min_max() -> None:
    """面板**只认** `slider`：顶层 `min`/`max` 零效果（SPEC-v0.5.1 §1.4 / §2.5-1）。"""
    for path, spec in _numeric_leaves():
        slider = spec.get("slider")
        assert isinstance(slider, dict), f"{path} 缺 slider（面板不认顶层 min/max）"
        assert set(slider) == {"min", "max", "step"}, f"{path} 的 slider 只认 min/max/step"
        assert not ({"min", "max"} & set(spec)), f"{path} 用了顶层 min/max（面板无效）"


def test_range_hint_matches_slider_and_settings_clamps() -> None:
    """每个数值叶的 hint 都以「（允许范围：X–Y）」结尾，且 X/Y == slider 区间。

    再加一层：`slider` 的区间必须与 `settings` 的**钳位常量**逐值相等 —— 面板给的是
    1–32，服务端却钳到 1–512 时，用户能填的值与真正生效的值就分家了。
    """
    range_re = re.compile(r"（允许范围：([-\d.]+)–([-\d.]+)）$")
    clamps = {
        "video.max_direct_mb": (
            settings.MIN_VIDEO_MAX_DIRECT_MB,
            settings.MAX_VIDEO_MAX_DIRECT_MB,
        ),
        "video.compress_target_mb": (
            settings.MIN_VIDEO_COMPRESS_TARGET_MB,
            settings.MAX_VIDEO_COMPRESS_TARGET_MB,
        ),
        "video.compress_timeout_seconds": (
            settings.MIN_VIDEO_COMPRESS_TIMEOUT_SECONDS,
            settings.MAX_VIDEO_COMPRESS_TIMEOUT_SECONDS,
        ),
        "video.probe_ttl_seconds": (
            settings.MIN_VIDEO_PROBE_TTL_SECONDS,
            settings.MAX_VIDEO_PROBE_TTL_SECONDS,
        ),
        "limits.max_media_seconds": (
            settings.MIN_MAX_MEDIA_SECONDS,
            settings.MAX_MAX_MEDIA_SECONDS,
        ),
        "limits.max_frames": (settings.MIN_MAX_FRAMES, settings.MAX_MAX_FRAMES),
        "limits.max_media_mb": (
            settings.MIN_MAX_MEDIA_MB,
            settings.MAX_MAX_MEDIA_MB,
        ),
        "limits.audio_timeout_seconds": (
            settings.MIN_AUDIO_TIMEOUT_SECONDS,
            settings.MAX_AUDIO_TIMEOUT_SECONDS,
        ),
        "cache.ttl_seconds": (
            settings.MIN_CACHE_TTL_SECONDS,
            settings.MAX_CACHE_TTL_SECONDS,
        ),
    }
    assert set(clamps) == set(EXPECTED_SLIDERS)
    for path, spec in _numeric_leaves():
        slider = spec["slider"]
        assert (slider["min"], slider["max"], slider["step"]) == EXPECTED_SLIDERS[path], path
        assert (slider["min"], slider["max"]) == clamps[path], f"{path}: slider 与钳位常量漂了"
        match = range_re.search(str(spec.get("hint") or ""))
        assert match, f"{path} 的 hint 未以「（允许范围：X–Y）」结尾"
        assert float(match.group(1)) == float(slider["min"]), path
        assert float(match.group(2)) == float(slider["max"]), path


def test_hidden_leaves_are_exactly_the_four_and_keep_their_full_specs() -> None:
    """隐藏 = **item 级** `invisible: true`，且 `type`/`default`/`slider`/`hint` 全套保留。

    "隐藏 ≠ 删除"是这一版最容易做错的取舍：删键会让核心把用户旧值当 stale 删掉并落盘
    （§1.3-2），而隐藏只是不渲染（`AstrBotConfig.vue:218/236/270`）。
    """
    leaves = _schema_leaves(_schema())
    hidden = {path for path, spec in leaves.items() if spec.get("invisible") is True}
    assert hidden == EXPECTED_HIDDEN
    for path in hidden:
        spec = leaves[path]
        assert "default" in spec and spec["type"] in ("int", "float", "bool", "string", "text")
        assert spec.get("description") and spec.get("hint"), f"{path} 隐藏但缺文档字段"
        if spec["type"] in ("int", "float"):
            assert isinstance(spec.get("slider"), dict), f"{path} 是隐藏的数值叶，仍要有 slider"


def test_mb_leaves_are_documented_in_mb() -> None:
    """三个 MB 键的措辞统一：「（MB）」+「1 MB = 1,048,576 字节」+ 参考值句（§2.5-5）。"""
    leaves = _schema_leaves(_schema())
    for path in VISIBLE_MB_KEYS:
        spec = leaves[path]
        assert "（MB）" in spec["description"], f"{path} 的 description 必须标明（MB）"
        hint = spec["hint"]
        assert "1 MB = 1,048,576 字节" in hint, f"{path} 的 hint 必须写死换算口径"
        assert "默认" in hint, f"{path} 的 hint 必须给参考值"
        assert path.endswith("_mb"), path
    assert "32 MB 同时是内存硬上限" in leaves["video.max_direct_mb"]["hint"]
    # 三个字节键的旧名字一个都不许出现在 schema 里（U1：不保留字节别名）
    raw_schema = (_PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8")
    for legacy in LEGACY_KEY_PATHS:
        assert legacy not in raw_schema, f"schema 里还留着旧键 {legacy}"


def test_background_mode_enum_matches_settings() -> None:
    """`background.mode` 的三处枚举互钉：schema `options`/`default` ⇄ `settings` 常量。

    WebUI 的下拉框、`Settings` 的默认值、以及"合法取值集合"必须是同一份：任一处漂了，
    用户在下拉框里选出的值都可能被运行期归一成**另一个**（`plugin`），且没有报错。
    """
    leaf = _schema_leaves(_schema())["background.mode"]
    assert leaf["type"] == "string"
    assert tuple(leaf["options"]) == settings.BACKGROUND_MODES
    assert leaf["default"] == settings.DEFAULT_BACKGROUND_MODE
    assert settings.DEFAULT_BACKGROUND_MODE == settings.Settings().background_mode
    assert settings.BACKGROUND_MODES == (settings.MODE_PLUGIN, settings.MODE_CORE)
    assert len(leaf["labels"]) == len(leaf["options"]), "每个 option 都要有人读的 label"
    assert "invisible" not in leaf, "引擎开关必须可见（派单 C4）"


def test_schema_leaf_types_are_renderable_by_core() -> None:
    """核心 `DEFAULT_VALUE_MAP` 只认那几种 `type`；`int`/`string` 都在白名单内。"""
    allowed = {
        "int",
        "float",
        "bool",
        "string",
        "text",
        "list",
        "file",
        "object",
        "template_list",
        "dict",
    }
    for path, spec in _schema_leaves(_schema()).items():
        assert spec.get("type") in allowed, f"{path} 的 type={spec.get('type')!r} 核心渲染不了"
        assert spec.get("description"), f"{path} 缺 description"
        assert spec.get("hint"), f"{path} 缺 hint"
        assert "default" in spec, f"{path} 缺 default"


def test_provider_id_leaves_declare_webui_special() -> None:
    """四个 provider id 必须带 `_special`，否则 WebUI 只给一个裸输入框（SPEC §5.2）。

    弃用键保留 `select_provider_stt`：它与真实类型一致（那个位置历史上存的就是 STT id）。
    """
    leaves = _schema_leaves(_schema())
    assert leaves[settings.KEY_AUDIO_PROVIDER_ID]["_special"] == "select_provider"
    assert leaves[settings.KEY_VIDEO_PROVIDER_ID]["_special"] == "select_provider"
    assert leaves[settings.KEY_VISION_PROVIDER_ID]["_special"] == "select_provider"
    assert leaves[settings.KEY_LEGACY_STT_PROVIDER_ID]["_special"] == "select_provider_stt"


def test_schema_numeric_defaults_are_the_mb_ones() -> None:
    """三个 MB 键的 schema 默认值 **8 / 6 / 64**（U1；D-3 已把 compress_target 从 8 改 6）。"""
    leaves = _schema_leaves(_schema())
    assert leaves["video.max_direct_mb"]["default"] == 8
    assert leaves["video.compress_target_mb"]["default"] == 6
    assert leaves["limits.max_media_mb"]["default"] == 64
    assert leaves["limits.audio_timeout_seconds"]["default"] == 60


# --------------------------------------------------------------------------- #
# G2：冻结的旧脚本（v0.4.2 = 写死 22 键；v0.3 = 它的 21 键前缀）
# --------------------------------------------------------------------------- #
#: v0.4.2 迁移器的 **22** 键（写死快照；SPEC-v0.5.1 §10 不变式 4）
KEY_ORDER_V042: tuple[str, ...] = (
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
    "background.mode",
)


def test_v042_migrator_is_the_frozen_twenty_two_key_snapshot() -> None:
    """G2-⑦：`migrate_config_v042.py:KEY_ORDER` == **写死的** 22 键元组。

    v0.5.1 把"第四处"换成了 `migrate_config_v05.py` ⇒ v0.4.2 那份冻结为历史快照。
    再拿它比 `settings.KEY_MAP` 会让"每加一版就顺手对齐旧脚本"变成惯例，旧证据立刻失效
    （`VERIFY-v0.4.2.md` 与旧派单都引用过它的 22 行报告）。
    """
    keys = tuple(key for key, _type in _migrator_v042().KEY_ORDER)
    assert keys == KEY_ORDER_V042
    assert len(keys) == 22
    assert keys[-1] == "background.mode", "v0.4.2 的新键只许在末尾"


def test_v03_migrator_is_the_frozen_twenty_one_key_prefix_of_v042() -> None:
    """G2-⑧：v0.3 迁移器**恰好**是 v0.4.2 的 21 键前缀（顺序 + 类型标签都一致）。

    不钉住这层关系，v0.3 脚本就会**悄悄变成半个事实源**：有人往它里面补键，
    "哪一格在守哪个版本"立刻变成谜，且旧证据失效。
    """
    v03 = list(_migrator_v03().KEY_ORDER)
    v042 = list(_migrator_v042().KEY_ORDER)
    assert len(v03) == 21 and len(v042) == 22
    assert v03 == v042[:21]
    assert [key for key, _t in v03] == list(KEY_ORDER_V042)[:21]


# --------------------------------------------------------------------------- #
# 键表 ⇄ Settings 字段 ⇄ 默认值 ⇄ MB 语义
# --------------------------------------------------------------------------- #
def test_key_map_values_are_exactly_the_settings_fields() -> None:
    """C2：`KEY_MAP` 的值集合 == `Settings` 的字段集合（21 个，双射）+ 白名单非键字段。

    派生量（`prompts` / `prompts_trimmed` / `prompts_fp` / `video_fp` /
    `compress_budget_bytes` / 三个 `*_bytes`）走 `property` / `cached_property`，**不是**字段
    —— 它们不是配置键，不该出现在 `KEY_MAP` 里，也不该混进 dataclass 字段。
    """
    fields = {field.name for field in dataclasses.fields(settings.Settings)}
    assert fields - settings.NON_KEY_FIELDS == set(settings.KEY_MAP.values())
    assert len(fields - settings.NON_KEY_FIELDS) == 21
    assert settings.NON_KEY_FIELDS == {"raw_audio_timeout"}
    derived = {
        "prompts",
        "prompts_trimmed",
        "prompts_fp",
        "video_fp",
        "compress_budget_bytes",
        "video_max_direct_bytes",
        "video_compress_target_bytes",
        "max_media_bytes",
        "_effective_prompts",
    }
    assert derived & fields == set(), "派生量混进了 dataclass 字段"


def test_settings_defaults_match_schema() -> None:
    """schema 的 `default` 必须与代码默认值一致 —— 否则 WebUI 显示的默认值与实际行为不符。"""
    leaves = _schema_leaves(_schema())

    int_defaults = {
        settings.KEY_MAX_MEDIA_SECONDS: settings.DEFAULT_MAX_MEDIA_SECONDS,
        settings.KEY_MAX_FRAMES: settings.DEFAULT_MAX_FRAMES,
        settings.KEY_MAX_MEDIA_MB: settings.DEFAULT_MAX_MEDIA_MB,
        settings.KEY_AUDIO_TIMEOUT_SECONDS: settings.DEFAULT_AUDIO_TIMEOUT_SECONDS,
        settings.KEY_CACHE_TTL_SECONDS: settings.DEFAULT_CACHE_TTL_SECONDS,
        settings.KEY_VIDEO_PROBE_TTL_SECONDS: settings.DEFAULT_VIDEO_PROBE_TTL_SECONDS,
        settings.KEY_VIDEO_MAX_DIRECT_MB: settings.DEFAULT_VIDEO_MAX_DIRECT_MB,
        settings.KEY_VIDEO_COMPRESS_TARGET_MB: settings.DEFAULT_VIDEO_COMPRESS_TARGET_MB,
        settings.KEY_VIDEO_COMPRESS_TIMEOUT_SECONDS: (
            settings.DEFAULT_VIDEO_COMPRESS_TIMEOUT_SECONDS
        ),
    }
    assert set(int_defaults) == {
        key for key in settings.KEY_MAP if leaves[key]["type"] == "int"
    } | {settings.KEY_MAX_MEDIA_SECONDS}, "int 默认值表必须覆盖 schema 的每个 int 叶"
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
    assert defaults.max_media_mb == settings.DEFAULT_MAX_MEDIA_MB
    assert defaults.audio_timeout_seconds == settings.DEFAULT_AUDIO_TIMEOUT_SECONDS
    assert defaults.cache_ttl_seconds == settings.DEFAULT_CACHE_TTL_SECONDS
    assert defaults.audio_provider_id == "" and defaults.vision_provider_id == ""
    assert defaults.video_provider_id == ""
    assert defaults.legacy_stt_provider_id == ""
    assert (
        defaults.video_probe_enabled,
        defaults.video_probe_ttl_seconds,
        defaults.video_max_direct_mb,
        defaults.video_compress_enabled,
        defaults.video_compress_target_mb,
        defaults.video_compress_timeout_seconds,
    ) == (
        settings.DEFAULT_VIDEO_PROBE_ENABLED,
        settings.DEFAULT_VIDEO_PROBE_TTL_SECONDS,
        settings.DEFAULT_VIDEO_MAX_DIRECT_MB,
        settings.DEFAULT_VIDEO_COMPRESS_ENABLED,
        settings.DEFAULT_VIDEO_COMPRESS_TARGET_MB,
        settings.DEFAULT_VIDEO_COMPRESS_TIMEOUT_SECONDS,
    )
    assert (defaults.prompt_audio, defaults.prompt_video) == ("", "")
    assert (defaults.prompt_frame, defaults.prompt_summary) == ("", "")
    assert defaults.prompt_video_native == ""
    # 原值字段的默认与钳位值一致（缺键时两者都回默认）
    assert defaults.raw_audio_timeout == settings.DEFAULT_AUDIO_TIMEOUT_SECONDS

    with pytest.raises(dataclasses.FrozenInstanceError):
        defaults.max_frames = 3  # type: ignore[misc]


def test_mb_semantics_defaults_and_derived_bytes() -> None:
    """U1 的机械判据：`有效字节 = MB × 1048576`，默认值仍是 **8 / 6 / 64**（MiB 口径）。

    判据不是"键名里有没有 mb"，而是**运行期拿到的字节数与 v0.4.2 逐字相同**：
    8 MiB / 6 MiB（D-3 改了默认，见 §9.3 声明清单）/ 64 MiB。
    """
    base = settings.Settings()
    assert settings.BYTES_PER_MB == 1048576
    assert (
        base.video_max_direct_mb,
        base.video_compress_target_mb,
        base.max_media_mb,
    ) == (8, 6, 64)
    assert base.video_max_direct_bytes == 8388608
    assert base.video_compress_target_bytes == 6291456
    assert base.max_media_bytes == 67108864
    # 生产值（32 / 29 / 64）换算后就是现网跑着的字节数（29 MiB = 30408704，±半个 MB 内）
    live = settings.Settings(
        video_max_direct_mb=32, video_compress_target_mb=29, max_media_mb=64
    )
    assert live.video_max_direct_bytes == 33554432
    assert live.video_compress_target_bytes == 30408704
    assert live.max_media_bytes == 67108864
    # MB 域钳位是纯乘法；不改字段就不会有任何浮点
    assert settings.mb_to_bytes(1) == 1048576
    assert settings.mb_to_bytes(0) == 0


def test_bytes_to_mb_boundary_table_is_half_up() -> None:
    """`bytes_to_mb` 的 half-up 边界表（§2.1 写死；迁移器的同名函数必须同值）。"""
    table = {
        0: 0,
        524287: 0,
        524288: 1,
        1048575: 1,
        1048576: 1,
        1500000: 1,
        1572864: 2,
        30000000: 29,
        30408704: 29,
        33554432: 32,
        67108864: 64,
    }
    for raw, expected in table.items():
        assert settings.bytes_to_mb(raw) == expected, f"{raw} 字节 ≠ {expected} MB"
    # 往返：`mb_to_bytes(bytes_to_mb(x))` 与 x 相差不超过半个 MB（E2 的容差）
    for raw in table:
        assert abs(settings.mb_to_bytes(settings.bytes_to_mb(raw)) - raw) <= 524288


def test_audio_timeout_constants_are_derived_not_hand_written() -> None:
    """`MAX_AUDIO_TIMEOUT_SECONDS` 是 `int(AUDIO_HONOR_MAX_S)`，后者 = `DEADLINE_S − 5`。

    captain 裁决（2026-10-26）：slider 上界 120 → **95** —— 96–120 永远不可能被兑现
    （与派单 C2 抱怨的"填 110 与填 61 等价"同一个病）。两个数都是**派生量**，
    测试里也不许手写 95（写死就变成第三份事实源）。
    """
    assert settings.AUDIO_HONOR_MAX_S == analyze.DEADLINE_S - 5.0
    assert settings.MAX_AUDIO_TIMEOUT_SECONDS == int(settings.AUDIO_HONOR_MAX_S)
    assert settings.AUDIO_TIMEOUT_DEFAULT_S == float(
        settings.DEFAULT_AUDIO_TIMEOUT_SECONDS
    )
    assert settings.MIN_AUDIO_TIMEOUT_SECONDS < settings.MAX_AUDIO_TIMEOUT_SECONDS


def test_removed_native_keys_are_constants_now() -> None:
    """两个已删键的生效值落在代码常量里（§2.2-B）；`settings` 不再有这两个字段。"""
    fields = {field.name for field in dataclasses.fields(settings.Settings)}
    assert "video_native_max_tokens" not in fields
    assert "video_native_timeout_seconds" not in fields
    assert video_native.NATIVE_MAX_TOKENS == 4096
    assert video_native.NATIVE_MAX_TOKENS > video_native.MAX_TOKENS_FLOOR
    # `native_timeout_s` 的唯一事实源在运行面（analyze）；settings 那份是配置面镜像
    assert settings.NATIVE_TIMEOUT_S == 60.0
    runtime = getattr(analyze, "NATIVE_TIMEOUT_S", None)
    if runtime is not None:  # 运行面常量落地后必须相等（受守卫对偶）
        assert float(runtime) == settings.NATIVE_TIMEOUT_S
    # `main.py` 里不许再出现这两个**属性读**（`ready` 行打的是标签，不是属性）
    src = _main_src()
    assert "video_native_max_tokens" not in src
    assert "video_native_timeout_seconds" not in src


def test_native_timeout_invariant_holds() -> None:
    """加载期不变式（§8.1）：`NATIVE_TIMEOUT_S + NATIVE_RESERVE_S <= DEADLINE_S` 等。

    派生关系一律从 `analyze` 的既有常量算，不手写 35 / 65 / 95；`AUDIO_RETRY_MIN_S` 属运行面
    的 D1 接线点（随 `analyze.py` 一起落地），这里只在它已存在时校验 —— 不替运行面任务造常量。
    """
    assert (
        analyze.NATIVE_RESERVE_S
        == analyze.FRAMES_MIN_BUDGET_S + analyze.NATIVE_RESERVE_MARGIN_S
    )
    assert settings.NATIVE_TIMEOUT_S + analyze.NATIVE_RESERVE_S <= analyze.DEADLINE_S
    retry_min = getattr(analyze, "AUDIO_RETRY_MIN_S", None)
    if retry_min is not None:
        assert float(retry_min) < settings.AUDIO_HONOR_MAX_S


def test_direct_mb_clamp_ceiling_matches_the_memory_hard_cap() -> None:
    """钳位上限 32 MB **必须**等于 `video_native.HARD_MAX_DIRECT_BYTES`（SPEC-v0.3 §10.2-③）。

    两个数字漂开会出现两种坏法：钳位比硬上限大 ⇒ 用户配到 32 MB 之外，每调用一次都在
    `call_native` 里 stat 失败（多一次白跑）；钳位比硬上限小 ⇒ 硬上限永远碰不到，
    内存护栏就成了摆设。
    """
    assert settings.mb_to_bytes(settings.MAX_VIDEO_MAX_DIRECT_MB) == (
        video_native.HARD_MAX_DIRECT_BYTES
    )
    assert settings.DEFAULT_VIDEO_MAX_DIRECT_MB <= settings.MAX_VIDEO_MAX_DIRECT_MB


def test_only_settings_module_reads_config_keys() -> None:
    """唯一读取点的守卫：`settings.KEY_*` 只允许出现在 `main._settings()` 里。

    散落的读取点会让四方守卫失效（加键时容易漏），也会让"配置改动需重启"的假设失效。
    例外是 `main` 的两处**派生用法**：`_config_check` 用 `KEY_AUDIO_TIMEOUT_SECONDS` 拼日志
    里的键名（不是读值），仍算配置面代码；它读的是 `Settings` 字段而不是原始配置。
    """
    allowed = {("main.py", "_settings"), ("main.py", "_config_check")}
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
                    if (py.name, node.name) not in allowed:
                        offenders.append(f"{py.name}:{node.name}:{inner.attr}")
    assert not offenders, f"配置读取点散落到了别处：{offenders}"


def test_legacy_key_names_are_gone_from_the_config_surface() -> None:
    """静态守卫（§9.4 附加）：旧**点号**键在**配置面三份**文件里一处都不许出现。

    覆盖 `_conf_schema.json` / `settings.py` / `main.py`（本任务的 inScope 三份）。
    `analyze.py` / `envelope.py` 里仍有旧键名（既有 docstring 与 hint 文案），它们随
    **运行面任务**一起改 —— 那两份的守卫挂在运行面测试上（`test_video_chain.py` 等），
    这里不越界断言（否则本任务一落地就红，且红的原因不在本任务的文件里）。
    """
    targets = ["_conf_schema.json", "settings.py", "main.py"]
    for name in targets:
        text = (_PLUGIN_DIR / name).read_text(encoding="utf-8")
        for legacy in LEGACY_KEY_PATHS:
            assert legacy not in text, f"{name} 里还留着旧键 {legacy}"
    # 旧的 `KEY_*` 常量与 `Settings` 字段也一并消失（不能留"影子读取点"）
    assert not hasattr(settings, "KEY_VIDEO_MAX_DIRECT_BYTES")
    assert not hasattr(settings, "KEY_VIDEO_NATIVE_TIMEOUT_SECONDS")
    assert not hasattr(settings, "KEY_VIDEO_NATIVE_MAX_TOKENS")
    assert not hasattr(settings, "KEY_MAX_MEDIA_BYTES")
    assert not hasattr(settings, "DEFAULT_VIDEO_NATIVE_MAX_TOKENS")


def test_error_codes_hints_and_tool_parameters_are_unchanged() -> None:
    """本版**不动**的三张表：错误码 23 / `HINTS` 23 / 工具参数 3（SPEC-v0.5.1 §15-2）。"""
    import inspect

    from astrbot_plugin_media_analysis import envelope

    codes = {
        value
        for name, value in vars(envelope).items()
        if name.startswith("ERR_") and isinstance(value, str)
    }
    assert len(codes) == 23
    assert set(envelope.HINTS) == codes
    params = list(inspect.signature(main.MediaAnalysisPlugin.media_analyze).parameters)
    assert params == ["self", "event", "target", "focus", "include_frames"]


# --------------------------------------------------------------------------- #
# ConfigView 行为（点号读取 / 钳位 / 原值；`AstrBotConfig` 自己没有点号 get）
# --------------------------------------------------------------------------- #
def _view(raw):
    return settings.ConfigView(raw)


def test_configview_reads_dotted_paths() -> None:
    view = _view(
        {
            "limits": {"max_frames": 4},
            "provider_settings": {"stt_provider_id": "x"},
            "cache": {"ttl_seconds": 0},
            "video": {"max_direct_mb": 4},
        }
    )
    assert view.get("limits.max_frames") == 4
    assert view.get("provider_settings.stt_provider_id") == "x"
    assert view.get("cache.ttl_seconds") == 0, "0 是合法值（＝关闭缓存），不能回 default"
    assert view.get("video.max_direct_mb") == 4
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


def test_configview_raw_int_does_not_clamp(monkeypatch: pytest.MonkeyPatch) -> None:
    """`raw_int()`：读**原值**、不钳位、不告警（越界真相由它交给加载期告警①）。"""
    from _fakes import RecordingLogger

    log = RecordingLogger()
    monkeypatch.setattr(settings, "LOG", log)
    view = _view({"limits": {"audio_timeout_seconds": 120, "bad": "x", "flag": True}})
    assert view.raw_int("limits.audio_timeout_seconds", default=60) == 120
    assert view.raw_int("limits.missing", default=60) == 60
    assert view.raw_int("limits.bad", default=60) == 60
    assert view.raw_int("limits.flag", default=60) == 60, "布尔不许当整数原值"
    assert log.records == [], "原值读取不该告警（告警归 _config_check）"


def test_configview_flag_accepts_only_real_booleans(monkeypatch: pytest.MonkeyPatch) -> None:
    """`flag()`：真布尔原样；`0/1`/`"true"` 一律回默认 + 一条告警（不猜方向）。

    两个布尔键（`video.probe_enabled` / `video.compress_enabled`）都会**花钱**
    （自动探测一次调用 / 自动压缩一次转码），所以"猜成 true"的代价很实在。
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


# --------------------------------------------------------------------------- #
# `main._settings()`：21 键逐条显式读（含 MB 域钳位与原值）
# --------------------------------------------------------------------------- #
def test_main_settings_assembles_all_twenty_one_keys() -> None:
    """`main._settings()` 的 21 个字段都能从一份配置里取到（含钳位、去空白与枚举归一）。

    这份配置**故意把每个数值键喂到边界外**：钳位值必须落到边界上，而
    `raw_audio_timeout` 必须保留原值 9999（钳位后恒 ≤ 95，拿钳位值判越界是死代码）。
    """
    plugin = main.MediaAnalysisPlugin(
        context=None,
        config={
            "background": {"mode": "core"},
            "provider_settings": {
                "audio_provider_id": "  a1 ",
                "video_provider_id": " vid1 ",
                "vision_provider_id": "v1",
                "stt_provider_id": "  old_stt ",
            },
            "video": {
                "max_direct_mb": 0,
                "compress_enabled": False,
                "compress_target_mb": 9999,
                "compress_timeout_seconds": 1,
                "probe_enabled": False,
                "probe_ttl_seconds": 0,
            },
            "limits": {
                "max_media_seconds": 1,
                "max_frames": 99,
                "max_media_mb": 0,
                "audio_timeout_seconds": 9999,
            },
            "cache": {"ttl_seconds": -5},
            "prompts": {
                "audio": "  A{focus_clause}  ",
                "summary": "S{material}",
                "video_native": "  N{focus_clause}  ",
            },
        },
    )
    snapshot = plugin._settings()
    assert snapshot == settings.Settings(
        background_mode=settings.MODE_CORE,
        audio_provider_id="a1",
        video_provider_id="vid1",
        vision_provider_id="v1",
        legacy_stt_provider_id="old_stt",
        video_max_direct_mb=settings.MIN_VIDEO_MAX_DIRECT_MB,
        video_compress_enabled=False,
        video_compress_target_mb=settings.MAX_VIDEO_COMPRESS_TARGET_MB,
        video_compress_timeout_seconds=settings.MIN_VIDEO_COMPRESS_TIMEOUT_SECONDS,
        video_probe_enabled=False,
        video_probe_ttl_seconds=settings.MIN_VIDEO_PROBE_TTL_SECONDS,
        max_media_seconds=settings.MIN_MAX_MEDIA_SECONDS,
        max_frames=settings.MAX_MAX_FRAMES,
        max_media_mb=settings.MIN_MAX_MEDIA_MB,
        audio_timeout_seconds=settings.MAX_AUDIO_TIMEOUT_SECONDS,
        cache_ttl_seconds=settings.MIN_CACHE_TTL_SECONDS,
        prompt_audio="A{focus_clause}",
        prompt_summary="S{material}",
        prompt_video_native="N{focus_clause}",
        raw_audio_timeout=9999,
    )
    # 未给的键回空串（`prompts.video` / `prompts.frame` 没配）
    assert snapshot.prompt_video == "" and snapshot.prompt_frame == ""
    # 钳位在 MB 域，字节只在派生 property 里出现
    assert snapshot.video_max_direct_bytes == 1048576
    assert snapshot.max_media_bytes == 1048576
    assert snapshot.video_compress_target_bytes == 32 * 1048576
    # 越界原值仍被保留给告警①（钳位值 95 ≠ 原值 9999）
    assert snapshot.raw_audio_timeout == 9999
    assert snapshot.audio_timeout_seconds == 95


def test_main_settings_on_empty_config_returns_defaults() -> None:
    plugin = main.MediaAnalysisPlugin(context=None, config=None)
    assert plugin._settings() == settings.Settings()


# --------------------------------------------------------------------------- #
# 派生量：压缩预算与视频指纹（都是"配置的函数"，但**不是**配置键）
# --------------------------------------------------------------------------- #
def test_compress_budget_is_the_min_of_target_and_direct_limit() -> None:
    """`compress_budget_bytes = min(compress_target_bytes, max_direct_bytes)`（SPEC-v0.3 §6.1）。"""
    assert (
        settings.Settings(video_compress_target_mb=8, video_max_direct_mb=4).compress_budget_bytes
        == 4 * 1048576
    )
    assert (
        settings.Settings(video_compress_target_mb=2, video_max_direct_mb=8).compress_budget_bytes
        == 2 * 1048576
    )
    # 默认对 = (6, 8) ⇒ 预算 6 MiB（D-3 把 compress_target 默认从 8 改成 6 的直接后果）
    assert settings.Settings().compress_budget_bytes == 6 * 1048576


def test_video_fp_is_a_stable_function_of_three_config_values() -> None:
    """`video_fp` = `sha256("max_direct|compress_enabled|target")[:8]`（冻结公式，哈希**字节**）。

    公式 v0.5.1 **逐字未变**（§2.4）：仍哈希派生字节 ⇒ 与 v0.4.2 同源，
    缓存失效的唯一原因是 `PLUGIN_VERSION` bump，不是公式变了。
    """
    import hashlib

    base = settings.Settings()
    material = (
        f"{base.video_max_direct_bytes}|{base.video_compress_enabled}|"
        f"{base.video_compress_target_bytes}"
    )
    assert base.video_fp == hashlib.sha256(material.encode("utf-8")).hexdigest()[:8]
    assert len(base.video_fp) == 8
    # 同一组字节值的不变式：v0.4.2 的生产对 (32 MiB, 29 MiB) 与本版的 (32, 29) 同指纹
    live = settings.Settings(video_max_direct_mb=32, video_compress_target_mb=29)
    legacy_material = f"{33554432}|True|{30408704}"
    assert live.video_fp == hashlib.sha256(legacy_material.encode("utf-8")).hexdigest()[:8]

    variants = [
        settings.Settings(video_max_direct_mb=4),
        settings.Settings(video_compress_enabled=False),
        settings.Settings(video_compress_target_mb=2),
    ]
    assert len({base.video_fp, *(v.video_fp for v in variants)}) == 4

    # 不影响产物内容的键：指纹不变
    assert (
        settings.Settings(
            video_probe_enabled=False,
            video_probe_ttl_seconds=0,
            video_compress_timeout_seconds=60,
            audio_timeout_seconds=95,
        ).video_fp
        == base.video_fp
    )


def test_suggest_direct_bytes_table_is_frozen() -> None:
    """内置建议表（SPEC-v0.3 §4）：Gemini flash 8 MiB / mimo 4 MiB / 未知 4 MiB。

    这张表**不参与运行期判定**（运行期只认 `video.max_direct_mb`，否决项 §16.2-6），
    它只用于加载期告警③与校准工具；mimo 口述的 "<50 MB" 按 §16.3-4 **不进表**。
    """
    assert settings.suggest_direct_bytes("gemini-3-flash-preview") == 8 * 1048576
    assert settings.suggest_direct_bytes("Gemini-3-FLASH") == 8 * 1048576
    assert settings.suggest_direct_bytes("mimo-v2.6-chat") == 4 * 1048576
    assert settings.suggest_direct_bytes("mimo-anything") == 4 * 1048576
    assert settings.suggest_direct_bytes("gpt-4o") == 4 * 1048576
    assert settings.suggest_direct_bytes("") == 4 * 1048576, "空模型名也要给保守值"
    assert max(value for _pattern, value in settings.VIDEO_DIRECT_BYTES_SUGGESTIONS) == 8 * 1048576


# --------------------------------------------------------------------------- #
# §7：`ready` 行打**有效值**（单行、前缀不变、可 grep）
# --------------------------------------------------------------------------- #
async def _run_initialize(logger, config: dict, *, context=None) -> main.MediaAnalysisPlugin:
    plugin = main.MediaAnalysisPlugin(context=context, config=config)
    await plugin.initialize()
    return plugin


@pytest.mark.asyncio
async def test_ready_line_reports_effective_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """㉚㉛：`ready` 行含 6 个新 token、无旧字节字段，两个 cap 由**公式**算出。

    真调 `initialize()`（不是自己拼字符串），用 `RecordingLogger` 抓那一行 —— 这是本仓库
    既有的日志断言姿势（`astrbot.api.logger` 是包装器，`caplog` 抓不到 INFO 级）。
    两个 cap **不许写死 65 / 95**：它们必须分别等于 `int(DEADLINE_S − NATIVE_RESERVE_S)`
    与 `int(AUDIO_HONOR_MAX_S)`，否则测试自己就成了第三份事实源。
    """
    from _fakes import RecordingLogger

    log = RecordingLogger()
    monkeypatch.setattr(main, "LOG", log)
    config = {
        "provider_settings": {"video_provider_id": "vid-x"},
        "video": {
            "max_direct_mb": 32,
            "compress_target_mb": 29,
            "compress_enabled": True,
            "probe_enabled": True,
            "probe_ttl_seconds": 604800,
        },
        "limits": {"max_media_seconds": 300, "max_frames": 6, "max_media_mb": 64},
        "cache": {"ttl_seconds": 0},  # 关闭缓存 ⇒ 不建目录、不碰磁盘
    }
    plugin = await _run_initialize(log, config)
    await plugin.terminate()

    ready = [message for level, message in log.records if level == "INFO" and "ready:" in message]
    assert len(ready) == 1, log.records
    line = ready[0]
    assert line.startswith("media_analysis ready: ")
    assert "\n" not in line, "ready 行必须单行（可 grep）"

    # ① 新 token 全在（三个 MB 键 + 两个 cap + 音频超时）
    for token in (
        "max_media_mb=64",
        "video.max_direct_mb=32",
        "video.compress_target_mb=29",
        "audio_timeout_s=60",
        f"native_timeout_cap_s={int(analyze.DEADLINE_S - analyze.NATIVE_RESERVE_S)}",
        f"audio_timeout_cap_s={int(settings.AUDIO_HONOR_MAX_S)}",
        f"native_max_tokens={video_native.NATIVE_MAX_TOKENS}",
        f"native_timeout_s={int(settings.NATIVE_TIMEOUT_S)}",
    ):
        assert token in line, f"ready 行缺 {token}：{line}"

    # ② 旧字节字段已删（并存会让"字节"与"MB"同时出现，正是 C1 抱怨的根源）
    for legacy in ("max_media_bytes=", "max_direct_bytes=", "compress_target_bytes=", " target="):
        assert legacy not in line, f"ready 行还有旧字段 {legacy}：{line}"
    assert " ttl=" not in line, "旧 ttl= 字段应改名为 probe_ttl="
    assert "probe_ttl=604800" in line

    # ③ 既有 grep 口径保留
    assert "video_provider_id='vid-x'" in line
    assert "compress=on" in line and "probe=on" in line


@pytest.mark.asyncio
async def test_ready_line_audio_timeout_is_the_clamped_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """越界的 `audio_timeout_seconds` 在 `ready` 行打**钳位后**的值（95），不是原值。"""
    from _fakes import RecordingLogger

    log = RecordingLogger()
    monkeypatch.setattr(main, "LOG", log)
    config = {
        "limits": {"audio_timeout_seconds": 120},
        "cache": {"ttl_seconds": 0},
    }
    plugin = await _run_initialize(log, config)
    await plugin.terminate()
    ready = [m for level, m in log.records if level == "INFO" and "ready:" in m]
    assert len(ready) == 1
    assert f"audio_timeout_s={settings.MAX_AUDIO_TIMEOUT_SECONDS}" in ready[0]
    assert "audio_timeout_s=120" not in ready[0]


# --------------------------------------------------------------------------- #
# §8：加载期一致性告警三条 + 启动期 provider 噪声
# --------------------------------------------------------------------------- #
class _StubProvider:
    """只有 `provider_config["model"]` 的最小假 provider（告警③需要 model 名）。"""

    def __init__(self, model: str) -> None:
        self.provider_config = {"model": model}


class _StubManager:
    def __init__(self, inst_map: dict) -> None:
        self.inst_map = inst_map


class _StubContext:
    """`provider_manager.inst_map` 可读的假 context；**`get_provider_by_id` 一调就炸**。

    这正是 §8.2 的判据：加载期**禁止**调用任何"目标缺失就打 WARN"的核心 API。
    真核心里那条 `Provider <id> was not found.` 就是 `context.get_provider_by_id()`
    自己打的（`star/context.py:466`）—— 所以只要它没被调用，冷启动就不可能有那条噪声。
    """

    def __init__(self, inst_map: dict | None = None, *, has_manager: bool = True) -> None:
        self.provider_manager = _StubManager(inst_map or {}) if has_manager else None

    def get_provider_by_id(self, provider_id):  # pragma: no cover - 调到这里就是回归
        raise AssertionError(
            "加载期不许用 context.get_provider_by_id（会打核心的 was not found 噪声）"
        )


def _alerts(log) -> list[str]:
    return [
        message
        for level, message in log.records
        if level == "WARNING" and "config-check warn=" in message
    ]


@pytest.mark.asyncio
async def test_config_check_timeout_over_budget_bites_only_above_the_honorable_max(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """告警①：判据是 **raw 配置值 > 95**（钳位后恒 ≤ 95，拿钳位值比就是死代码）。

    96 / 120 触发；95（恰好等于上界）与 60 不触发。文案逐字冻结（§8.1）。
    """
    from _fakes import RecordingLogger

    for raw, expected in ((96, True), (120, True), (95, False), (60, False)):
        main._CONFIG_CHECK_DONE.clear()
        log = RecordingLogger()
        monkeypatch.setattr(main, "LOG", log)
        plugin = await _run_initialize(
            log, {"limits": {"audio_timeout_seconds": raw}, "cache": {"ttl_seconds": 0}}
        )
        await plugin.terminate()
        hits = [m for m in _alerts(log) if "warn=timeout_over_budget" in m]
        assert bool(hits) is expected, f"raw={raw} 期望 {expected}：{_alerts(log)}"
        if expected:
            assert hits[0] == (
                "media_analysis: config-check warn=timeout_over_budget "
                "key=limits.audio_timeout_seconds "
                f"configured_s={raw} honorable_s={int(settings.AUDIO_HONOR_MAX_S)}"
            )


@pytest.mark.asyncio
async def test_config_check_compress_target_ratio(monkeypatch: pytest.MonkeyPatch) -> None:
    """告警②：`4 × compress_target_mb > 3 × max_direct_mb`（**严格大于**，整数比较）。

    生产对 (29, 32) 命中（116 > 96）；默认对 (6, 8) **不**命中（24 > 24 为假）；
    (24, 32) 恰好 75% 也不命中 —— 默认对一出生就告警＝把告警变成噪音。
    """
    from _fakes import RecordingLogger

    for target_mb, direct_mb, expected in (
        (29, 32, True),
        (32, 32, True),
        (6, 8, False),
        (24, 32, False),
        (1, 32, False),
    ):
        main._CONFIG_CHECK_DONE.clear()
        log = RecordingLogger()
        monkeypatch.setattr(main, "LOG", log)
        plugin = await _run_initialize(
            log,
            {
                "video": {
                    "max_direct_mb": direct_mb,
                    "compress_target_mb": target_mb,
                },
                "cache": {"ttl_seconds": 0},
            },
        )
        await plugin.terminate()
        hits = [m for m in _alerts(log) if "warn=compress_target_ge_075_direct" in m]
        assert bool(hits) is expected, f"({target_mb},{direct_mb}) 期望 {expected}：{_alerts(log)}"
        if expected:
            assert hits[0] == (
                "media_analysis: config-check warn=compress_target_ge_075_direct "
                f"compress_target_mb={target_mb} max_direct_mb={direct_mb}"
            )


@pytest.mark.asyncio
async def test_config_check_max_direct_over_suggested_warns_when_provider_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """告警③：能**静默**读到 provider 时，门槛高于该模型建议值就 WARNING（带 `suggested_mb`）。"""
    from _fakes import RecordingLogger

    log = RecordingLogger()
    monkeypatch.setattr(main, "LOG", log)
    context = _StubContext({"vid-gemini": _StubProvider("gemini-3-flash-preview")})
    plugin = await _run_initialize(
        log,
        {
            "provider_settings": {"video_provider_id": "vid-gemini"},
            "video": {"max_direct_mb": 32, "compress_target_mb": 1},
            "cache": {"ttl_seconds": 0},
        },
        context=context,
    )
    await plugin.terminate()
    hits = [m for m in _alerts(log) if "warn=max_direct_over_suggested" in m]
    assert len(hits) == 1, _alerts(log)
    suggested_mb = settings.suggest_direct_bytes("gemini-3-flash-preview") // 1048576
    assert hits[0] == (
        "media_analysis: config-check warn=max_direct_over_suggested "
        f"max_direct_mb=32 suggested_mb={suggested_mb}"
    )


@pytest.mark.asyncio
async def test_config_check_skips_quietly_then_retries_on_first_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider 不可解析 ⇒ **0 条 WARNING** + 一条 DEBUG skip；首次调用再补一次。

    §8.2 的顺延规则：冷启动时 provider 尚未注册，不许因此丢告警，也不许污染日志。
    """
    from _fakes import RecordingLogger

    log = RecordingLogger()
    monkeypatch.setattr(main, "LOG", log)
    context = _StubContext({})  # 注册表里还没有那个 provider
    plugin = await _run_initialize(
        log,
        {
            "provider_settings": {"video_provider_id": "vid-late"},
            "video": {"max_direct_mb": 32, "compress_target_mb": 1},
            "cache": {"ttl_seconds": 0},
        },
        context=context,
    )
    assert _alerts(log) == [], f"不可解析时不该有 WARNING：{_alerts(log)}"
    assert any(
        level == "DEBUG"
        and "config-check skip=max_direct_over_suggested reason=provider_unresolved" in message
        for level, message in log.records
    ), log.records

    # provider 上线后，首次调用把这条检查补齐（每 id 每进程至多一条 WARNING）
    context.provider_manager.inst_map["vid-late"] = _StubProvider("gemini-3-flash")
    async def _boom(event, target):  # noqa: ANN001 - 只为让门面走到 except 分支
        raise main.envelope.ToolError(main.envelope.ERR_MEDIA_NOT_FOUND)

    monkeypatch.setattr(main.media, "find_media", _boom)
    payload = await plugin.media_analyze(None, target="current")
    await plugin.terminate()
    assert '"ok":false' in payload or '"ok": false' in payload, payload
    hits = [m for m in _alerts(log) if "warn=max_direct_over_suggested" in m]
    assert len(hits) == 1, _alerts(log)

    # 再调一次：去重集合挡住，不再重复告警
    await plugin.media_analyze(None, target="current")
    assert len([m for m in _alerts(log) if "warn=max_direct_over_suggested" in m]) == 1


@pytest.mark.asyncio
async def test_config_check_dedupes_each_id_once_per_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """每 id 每进程**至多一条** WARNING（两次 `initialize()` 也只打一条）。"""
    from _fakes import RecordingLogger

    log = RecordingLogger()
    monkeypatch.setattr(main, "LOG", log)
    config = {
        "video": {"max_direct_mb": 32, "compress_target_mb": 29},
        "limits": {"audio_timeout_seconds": 120},
        "cache": {"ttl_seconds": 0},
    }
    first = await _run_initialize(log, config)
    await first.terminate()
    second = await _run_initialize(log, config)
    await second.terminate()
    warnings = _alerts(log)
    assert len(warnings) == 2, warnings  # ①+② 各一条，第二次加载不再重复
    assert sum("timeout_over_budget" in m for m in warnings) == 1
    assert sum("compress_target_ge_075_direct" in m for m in warnings) == 1


@pytest.mark.asyncio
async def test_config_check_tolerates_missing_provider_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """㉞ `provider_manager` 不存在 ⇒ 不抛、0 条 WARNING、DEBUG skip（fail-open）。"""
    from _fakes import RecordingLogger

    log = RecordingLogger()
    monkeypatch.setattr(main, "LOG", log)
    context = _StubContext(has_manager=False)
    plugin = await _run_initialize(
        log,
        {
            "provider_settings": {"video_provider_id": "vid-x"},
            "video": {"max_direct_mb": 32, "compress_target_mb": 1},
            "cache": {"ttl_seconds": 0},
        },
        context=context,
    )
    await plugin.terminate()
    assert _alerts(log) == []
    assert any(
        "config-check skip=max_direct_over_suggested reason=provider_unresolved" in message
        for _level, message in log.records
    )


@pytest.mark.asyncio
async def test_initialize_never_calls_get_provider_by_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """②' 启动期零噪声的机械判据：加载期**不调用** `context.get_provider_by_id`。

    真核心那条 `Provider <id> was not found.` 正是它自己打的（`star/context.py:466`），
    而且只在**冷启动**出现（插件先于 provider 注册）。`_StubContext` 的这个方法一调就炸。
    """
    from _fakes import RecordingLogger

    log = RecordingLogger()
    monkeypatch.setattr(main, "LOG", log)
    context = _StubContext({"vid-gemini": _StubProvider("gemini-3-flash-preview")})
    plugin = await _run_initialize(
        log,
        {
            "provider_settings": {"video_provider_id": "vid-gemini"},
            "video": {"max_direct_mb": 4, "compress_target_mb": 1},
            "cache": {"ttl_seconds": 0},
        },
        context=context,
    )
    await plugin.terminate()
    # 顺带：门槛在建议值内 ⇒ 不告警，但"查过了"要记住（不重复读注册表）
    assert _alerts(log) == []
    assert "max_direct_over_suggested" in main._CONFIG_CHECK_DONE


@pytest.mark.asyncio
async def test_config_check_alerts_reach_the_python_logging_pipeline(caplog) -> None:
    """三条告警走的是标准 `logging`（`caplog` 能抓到）—— 运维的 grep 与 tester 的断言同源。"""
    caplog.set_level(logging.WARNING)
    config = {
        "video": {"max_direct_mb": 32, "compress_target_mb": 29},
        "limits": {"audio_timeout_seconds": 120},
        "cache": {"ttl_seconds": 0},
    }
    main._CONFIG_CHECK_DONE.clear()
    plugin = main.MediaAnalysisPlugin(context=None, config=config)
    await plugin.initialize()
    await plugin.terminate()
    messages = [record.getMessage() for record in caplog.records]
    assert any("config-check warn=timeout_over_budget" in m for m in messages), messages
    assert any("config-check warn=compress_target_ge_075_direct" in m for m in messages), messages


def test_compression_hop_threshold_pins_the_d2_arithmetic() -> None:
    """t5-F3：压缩跳门槛 = `compress_timeout + NATIVE_TIMEOUT_S/2`，两个代表值 + 可达性。

    为什么这条值得单独钉：`NATIVE_TIMEOUT_S` 在这个式子里是**加性**用法，所以 D-2
    （`native_timeout_seconds` 110 → 常量 60）**不是"语义不变"**，而是把这个门槛从
    `60 + 55 = 115 s` 降到 `60 + 30 = 90 s`。而进入这一跳时起步剩余 ≈ 96.4 s
    （SPEC-v0.5.1 §9.2 第 6 行 / §2.2-B 的"起步时 remaining≈96.4"）：

    - 旧串 `115 s > 96.4 s` ⇒ **超门槛视频的压缩跳从来没有跑过**（恒 `video_skip_budget`）；
    - 新串 `90 s < 96.4 s` ⇒ 压缩跳**重新可达**。

    这条算术是行为变更的判据，写在 `analyze.min_actionable_s()` 一处，测试只钉它。
    """
    from astrbot_plugin_media_analysis import analyze

    assert analyze.NATIVE_TIMEOUT_S == 60.0
    assert analyze.min_actionable_s(
        settings.Settings(video_compress_timeout_seconds=30)
    ) == pytest.approx(60.0), "默认压缩上限(30) + 原生半程(30)"
    assert analyze.min_actionable_s(
        settings.Settings(video_compress_timeout_seconds=60)
    ) == pytest.approx(90.0), "生产能力值(60，隐藏键保号) + 原生半程(30)"

    start_remaining_s = 96.4  # 实测起步剩余（SPEC-v0.5.1 §2.2-B / §9.2 #6）
    new_threshold = analyze.min_actionable_s(
        settings.Settings(video_compress_timeout_seconds=60)
    )
    old_threshold = 60.0 + 110.0 * 0.5  # D-2 之前的串（native_timeout_seconds=110）
    assert new_threshold < start_remaining_s, "新串必须让压缩跳可达（否则白删这两个键）"
    assert old_threshold > start_remaining_s, "旧串的门槛在剩余之外 —— 这正是 D-2 修掉的那半边"


def test_read_points_walks_only_the_settings_function_body() -> None:
    """t5-F6 的反向测试：函数体**之外**的 `settings.KEY_*` 必须**不被计入**读点。

    为什么需要这条反向格：`_read_points` 的旧实现 `ast.walk` 整棵树，于是
    `_config_check` 里那句 `settings.KEY_AUDIO_TIMEOUT_SECONDS`（拼日志的派生用法）
    会被当成"读了这个键" ⇒ 真出现"漏读"时，那处日志会替它挡枪。用一段合成源码把
    收窄的行为钉住（改回整树 walk，这条立刻红）。
    """
    source = (
        "from . import settings\n"
        "\n"
        "def _some_helper(self):\n"
        "    return settings.KEY_CACHE_TTL_SECONDS  # 派生用法：函数体外\n"
        "\n"
        "def _settings(self):\n"
        "    view = settings.ConfigView(self.config)\n"
        "    return view.text(settings.KEY_AUDIO_PROVIDER_ID)\n"
    )
    assert _read_points(source) == {settings.KEY_AUDIO_PROVIDER_ID}

    # 对照组：真的 `main._settings()` 读的键数仍等于键表（收窄没有把实现里的读点漏掉）
    live = _read_points(_main_src())
    assert live == set(settings.KEY_MAP), sorted(set(settings.KEY_MAP) - live)


def test_moving_a_key_read_out_of_settings_makes_the_four_way_guard_red() -> None:
    """t5-L2 的**反向判据**：把某个 `settings.KEY_*` 读点移出 `_settings()` ⇒ 守卫必须红。

    为什么必须做"移出去"这个动作：只测"扫描器忽略函数体外引用"是**必要但不充分**的
    —— 真正要防的是"新键漏读时四方守卫不报错"。这里取**真** `main.py` 源码，把
    `_settings()` 里 `settings.KEY_CACHE_TTL_SECONDS` 那处读点改写成字面量
    （等价于"忘了从配置读这个键"），断言读点集合立刻少一个 ⇒ 与键表相等的断言必然失败。
    """
    source = _main_src()
    assert source.count("settings.KEY_CACHE_TTL_SECONDS") == 1, "这条用例假定它在 main.py 里只出现一次"

    without = source.replace("settings.KEY_CACHE_TTL_SECONDS", "0", 1)
    assert without != source
    assert _read_points(without) == set(settings.KEY_MAP) - {
        settings.KEY_CACHE_TTL_SECONDS
    }, "漏读一个键之后读点集合必须少一个（否则守卫根本不会红）"
