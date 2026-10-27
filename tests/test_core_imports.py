"""核心内部面 import 的静态守卫 + 时长兜底单测（t16）。

为什么需要这一层：本插件有两处 `astrbot.core.*` 的**内部面** import ——
`main.py` 的 `redact_sensitive_text`、`media.py` 的 `get_media_duration` ——
它们都不在 `astrbot.api.*` 的公开导出里。**裸** import 一旦遇到核心升级改名/搬家，
会在**导入期**抛 `ModuleNotFoundError` ⇒ 插件**静默拒载**：目录在、工具不注册、
界面上看起来像"没装"，而且加载失败只在 `grep -a 'Failed to import plugin <目录名>'`
里能看见（父手册 §3.4 的最贵失败模式）。

两处都必须：包在 `try/except` 里 + 绑定本地兜底。本文件的 AST 守卫就是那条线 ——
以后谁再写一个裸的内部面 import，这里直接红。

第二块是兜底实现的单测：`media._fallback_duration_ms` 必须与核心
`get_media_duration` **同契约**（返回**毫秒**、失败 `None`），否则"加固"只是把
拒载换成了错数。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from astrbot_plugin_media_analysis import media

REPO_ROOT = Path(__file__).resolve().parent.parent
INTERNAL_PREFIX = "astrbot.core"
SOURCE_MODULES = sorted(REPO_ROOT.glob("*.py"))
EXPECTED_SOURCE_MODULES = [
    "__init__.py",
    "analyze.py",
    "background.py",  # v0.4：后台任务表（SPEC-v0.4 §2.6）
    "cache.py",
    "envelope.py",
    "main.py",
    "media.py",
    "probe.py",
    "providers.py",
    "settings.py",
    "video_native.py",
]


def _iter_core_imports(tree: ast.AST):
    """产出 `(import 节点, 模块名)`，只挑 `astrbot.core.*` 的内部面。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == INTERNAL_PREFIX or alias.name.startswith(
                    INTERNAL_PREFIX + "."
                ):
                    yield node, alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == INTERNAL_PREFIX or module.startswith(INTERNAL_PREFIX + "."):
                yield node, module


def _is_guarded(node: ast.AST, tree: ast.AST) -> bool:
    """`node` 是否位于某个**带 except 的** `ast.Try` 的 try 体里（finally 不算保护）。"""
    for candidate in ast.walk(tree):
        if not isinstance(candidate, ast.Try) or not candidate.handlers:
            continue
        for statement in candidate.body:
            for inner in ast.walk(statement):
                if inner is node:
                    return True
    return False


def test_scan_scope_is_the_source_modules_only() -> None:
    """守卫必须扫**源码模块**（不含 tests/）：漏扫等于没扫。"""
    assert [path.name for path in SOURCE_MODULES] == EXPECTED_SOURCE_MODULES
    assert all(path.parent == REPO_ROOT for path in SOURCE_MODULES)
    assert not any("tests" in path.parts for path in SOURCE_MODULES)


@pytest.mark.parametrize("path", SOURCE_MODULES, ids=lambda p: p.name)
def test_core_internal_imports_are_guarded(path: Path) -> None:
    """任何 `astrbot.core.*` 的 import 都必须被 `try/except` 包住（否则静默拒载）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = [
        f"{path.name}:{node.lineno} 裸 import {module}（外面没有 try/except ⇒ 核心一搬家就静默拒载）"
        for node, module in _iter_core_imports(tree)
        if not _is_guarded(node, tree)
    ]
    assert not offenders, "核心内部面必须加固：\n" + "\n".join(offenders)


def test_the_two_known_internal_surfaces_are_present_and_guarded() -> None:
    """守卫**不是空转**：必须真的扫到那两处内部面，且两处都在 `try` 里。

    否则"没有 offender"可能只是因为扫描什么都没找到（比如 glob 写错、文件改名）。
    """
    seen: dict[str, list[tuple[str, int, bool]]] = {}
    for path in SOURCE_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node, module in _iter_core_imports(tree):
            seen.setdefault(module, []).append(
                (path.name, node.lineno, _is_guarded(node, tree))
            )

    assert "astrbot.core.utils.media_utils" in seen, "media.py 的时长内部面不见了？"
    assert "astrbot.core.utils.error_redaction" in seen, "main.py 的脱敏内部面不见了？"
    assert sum(len(v) for v in seen.values()) >= 2, f"内部面扫描结果异常：{seen}"
    for module, entries in seen.items():
        for name, lineno, guarded in entries:
            assert guarded, f"{name}:{lineno} 的 {module} 没被 try 包住"


# --------------------------------------------------------------------------- #
# 兜底实现：与核心同契约（返回**毫秒**、失败 None）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_fallback_duration_matches_core_within_tolerance(
    media_fixtures: dict[str, Path], core_backend: str
) -> None:
    """同一份夹具上，兜底探测与核心 `get_media_duration` 的差 ≤ 100 ms。"""
    if core_backend != "real-core":
        pytest.skip("需要真核心的 get_media_duration 做基准（桩里没有这个面）")

    from astrbot.core.utils.media_utils import get_media_duration as core_duration

    for name in ("video_5s", "video_30s", "audio_30s"):
        path = media_fixtures[name]
        fallback = await media._fallback_duration_ms(str(path))
        reference = await core_duration(str(path))
        assert isinstance(fallback, int), f"{name}: 兜底应返回毫秒 int，实际 {fallback!r}"
        assert isinstance(reference, int), f"{name}: 核心基准异常 {reference!r}"
        assert abs(fallback - reference) <= 100, (
            f"{name}: 兜底 {fallback} ms vs 核心 {reference} ms 差超过 100 ms"
        )


@pytest.mark.asyncio
async def test_fallback_duration_returns_none_for_missing_file(tmp_path: Path) -> None:
    """路径不存在 ⇒ `None`（与核心同契约：失败不抛异常）。"""
    assert await media._fallback_duration_ms(str(tmp_path / "nope.mp4")) is None


@pytest.mark.asyncio
async def test_fallback_duration_returns_none_when_ffprobe_unavailable(
    monkeypatch: pytest.MonkeyPatch, media_fixtures: dict[str, Path]
) -> None:
    """`ffprobe` 不在 PATH 上 ⇒ `None`，不把异常抛出去。"""
    monkeypatch.setenv("PATH", "")
    assert await media._fallback_duration_ms(str(media_fixtures["video_5s"])) is None


@pytest.mark.asyncio
async def test_probe_duration_still_reports_probe_failed_without_duration(
    monkeypatch: pytest.MonkeyPatch, media_fixtures: dict[str, Path]
) -> None:
    """兜底拿不到时长时，上层仍走既有 `PROBE_FAILED` 分支（错误码不变）。"""
    from astrbot_plugin_media_analysis import envelope

    async def _no_duration(path: str) -> int | None:
        return None

    monkeypatch.setattr(media, "get_media_duration", _no_duration)
    with pytest.raises(envelope.ToolError) as exc_info:
        await media.probe_duration_s(str(media_fixtures["video_5s"]))
    assert exc_info.value.code == envelope.ERR_PROBE_FAILED
