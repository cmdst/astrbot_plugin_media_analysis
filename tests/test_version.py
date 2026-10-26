"""版本与仓库信息守卫（SPEC-v0.4 §6.2；v0.3 §14.1 / v0.2 §11 C9 的同一套）。

**为什么必须存在这一条**：`settings.PLUGIN_VERSION` 是**缓存键成分**（升级即整体失效），
而 `metadata.yaml` 的 `version` 是加载器、面板与运维（`pin-check.sh`）看到的版本面。
两者一旦漂移：缓存继续命中**旧版本**的结果（键没变），而线上显示的版本已经变了
—— 这就是"升级了但行为没变"的幽灵，且没有任何报错可查。

同一个文件里还钉着另外两处**用户明确要求补齐**的信息：
- `repo`：上游仓库 `cmdst/astrbot_plugin_media_analysis`；
- `astrbot_version`：PEP 440 range —— 不含当前核心时加载器**静默拒载**
  （目录在、工具不注册、界面上像"没装"，父手册 §3 的最贵失败模式）。

解析用正则而不是 PyYAML：本仓库刻意不引第三方依赖（无 `pyproject.toml` / 无 requirements），
而这四个键在 `metadata.yaml` 里都是**顶格单行标量**。若哪天它们被挪进块标量或嵌套结构，
正则取不到 ⇒ 本文件直接红，正好逼人回来看这份说明而不是"静默通过"。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from astrbot_plugin_media_analysis import settings

REPO_DIR = Path(__file__).resolve().parents[1]
METADATA = REPO_DIR / "metadata.yaml"
CHANGELOG = REPO_DIR / "CHANGELOG.md"

#: 用户要求的版本面（与 `metadata.yaml` / `settings.PLUGIN_VERSION` 三方一致）
EXPECTED_VERSION = "0.5.1"
#: 用户要求补齐的上游仓库（逐字）
EXPECTED_REPO = "https://github.com/cmdst/astrbot_plugin_media_analysis"


def _metadata_text() -> str:
    assert METADATA.is_file(), f"metadata.yaml 不见了：{METADATA}"
    return METADATA.read_text(encoding="utf-8")


def _scalar(key: str) -> str:
    """取 `metadata.yaml` 里的顶格单行标量（去掉成对引号）；取不到就断言失败。"""
    pattern = re.compile(rf"^{re.escape(key)}:[ \t]*(.+?)[ \t]*$", re.MULTILINE)
    match = pattern.search(_metadata_text())
    assert match, (
        f"metadata.yaml 里找不到顶格单行标量 `{key}:`"
        "（被挪进块标量 / 嵌套结构了？本测试的解析器只认单行标量）"
    )
    value = match.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def _normalize_version(raw: str) -> str:
    """容忍 `v` 前缀（`v0.2.0` 与 `0.2.0` 视为同一版本），其余原样。"""
    text = raw.strip()
    if text[:1] in {"v", "V"}:
        text = text[1:]
    return text


def test_metadata_exposes_the_four_loader_facing_scalars() -> None:
    """`name` / `version` / `repo` / `astrbot_version` 必须都能被单行解析到。"""
    for key in ("name", "version", "repo", "astrbot_version"):
        assert _scalar(key), f"{key} 是空值"


def test_plugin_version_equals_metadata_version() -> None:
    """C9：`settings.PLUGIN_VERSION` == `metadata.yaml:version`（去掉 `v` 前缀后逐字）。

    这条守的是缓存键：`PLUGIN_VERSION` 在 `cache.build_key` 的 **12 个分量**里（v0.3）
    与 probe 缓存的键（`probe.build_key(provider, model, PLUGIN_VERSION)`）里 ⇒ 版本升级
    会让结果缓存与探测记录**同时**自然失效。两边不一致时，升级不会清缓存、也不会体现在键里。
    """
    assert _normalize_version(_scalar("version")) == _normalize_version(
        settings.PLUGIN_VERSION
    )
    assert settings.PLUGIN_VERSION == EXPECTED_VERSION
    assert _normalize_version(settings.PLUGIN_VERSION) == EXPECTED_VERSION


def test_metadata_version_is_a_plain_semver() -> None:
    """版本写法只有 `vX.Y.Z` 这一种（加载器与 `pin-check.sh` 都按它读）。"""
    raw = _scalar("version")
    assert re.fullmatch(r"v?\d+\.\d+\.\d+", raw), f"版本写法不规范：{raw!r}"
    assert _normalize_version(raw) == EXPECTED_VERSION


def test_plugin_name_equals_metadata_name() -> None:
    """C9 的另一半：`settings.PLUGIN_NAME` == `metadata.yaml:name`，且是合法 Python 标识符。

    核心按 `data.plugins.<目录名>.main` 把插件当**包**导入，`name` 必须是合法标识符，
    且**部署时**要与安装目录名逐字相等（否则静默拒载）；缓存目录也用 `PLUGIN_NAME` 作
    子目录名，所以这两处必须同一个事实源。

    这里**刻意不比对 `REPO_DIR.name`**：仓库检出目录叫什么都不影响部署（`git archive` 到
    `/tmp/verify` 也要能跑测试）。"安装目录名 == name" 是部署判据，由派单的校验命令兜。
    """
    name = _scalar("name")
    assert name == settings.PLUGIN_NAME
    assert name.isidentifier(), f"name 必须是合法 Python 标识符：{name!r}"


def test_repo_points_at_the_upstream_repository() -> None:
    """用户要求补齐的上游仓库信息（`cmdst/astrbot_plugin_media_analysis`），逐字比对。"""
    assert _scalar("repo") == EXPECTED_REPO


def test_astrbot_version_range_contains_the_running_core() -> None:
    """`astrbot_version` 是 PEP 440 range，必须含当前部署的核心 `4.29.1`。

    range 不含核心时，加载器按 PEP 440 判定后**静默拒载**（不报错、不注册工具），
    是本插件历史上最贵的失败模式之一 ⇒ 这里用真 `SpecifierSet` 判，不靠肉眼。
    """
    packaging_specifiers = pytest.importorskip(
        "packaging.specifiers", reason="PEP 440 判定需要 packaging（pytest 自带依赖）"
    )
    raw = _scalar("astrbot_version")
    specifier = packaging_specifiers.SpecifierSet(raw)
    assert specifier.contains("4.29.1"), f"{raw} 不含当前核心 4.29.1（会被静默拒载）"
    # 对称面：range 也不该把整个 5.x 放进来（大版本升级需要人看一眼）
    assert not specifier.contains("5.0.0"), f"{raw} 把 5.0.0 也放行了"


def test_changelog_top_entry_mentions_the_version() -> None:
    """CHANGELOG 的**顶部条目**必须是本版本 —— 否则版本面只有一半（代码有、文档没）。"""
    assert CHANGELOG.is_file(), f"CHANGELOG.md 不见了：{CHANGELOG}"
    headings = [
        line.strip()
        for line in CHANGELOG.read_text(encoding="utf-8").splitlines()
        if line.startswith("## ")
    ]
    assert headings, "CHANGELOG.md 里没有任何 `## ` 条目"
    top = headings[0]
    assert EXPECTED_VERSION in top, f"CHANGELOG 顶部条目没提到 {EXPECTED_VERSION}：{top!r}"
