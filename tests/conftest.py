"""tests/ 地基：真核心优先 + 最小桩兜底 + 合成夹具 + 假对象。

本文件**不 import 任何插件模块**（插件模块由各测试文件按包名导入），所以这一层可以与
实现并行开发：`pytest tests/` 在插件源码还不存在时也能跑（`test_fixtures.py` 就是证据）。

四件事，按执行顺序：

1. **隔离 `ASTRBOT_ROOT`（必须在 import astrbot 之前）**。核心 `AstrBotConfig` 构造时会跑
   `migrate_config_on_load()`（`astrbot/core/config/astrbot_config.py:18`
   → `astrbot/core/utils/migra_helper.py:317`），它会**回写** `<root>/data/cmd_config.json`。
   本 lane 的红线是不写生产树 `/root/AstrBot`，所以测试根默认钉在
   `/tmp/media_analysis_testroot`；已显式设了 `ASTRBOT_ROOT` 就尊重它。
   实测（2026-10-22）：**只 import 核心**不写任何文件（同一份配置 sha256 前后一致，
   对照实验见 `tests/SEAMS.md` §3）；这条隔离是兜住"测试里构造 `AstrBotConfig` /
   加载插件配置"这种情形，以及别人机器上 cwd 恰好是生产树的情形。

2. **`sys.path` 只注入两处**：仓库的**父目录**（`…/projects`，让测试按
   `<PKG>.main` 导入插件 —— 复刻核心 `data.plugins.<目录名>.main` 的包导入形态，
   父手册 §3 的 2026-10-18 拒载事故就出在这）与 `tests/` 自己（让测试 `import _fakes`）。
   **不注入仓库根、更不注入插件目录**：那会放过扁平导入（SPEC §11.3 两条铁律之一）。

3. **后端判定：真核心优先**。`/root/AstrBot/.venv/bin/python` 里 `astrbot` 就是部署树本身
   （SPEC §11.1 实测）⇒ 插件需要的 `Record.fromFileSystem` / `Video.fromFileSystem` /
   `isinstance(prov, STTProvider)` 都是真类型面，**不需要桩**。只有核心不可用
   （拿系统 `python3` 跑）时才装最小桩，保证 `tests/` 至少能 collect；桩永不覆盖真核心。

4. **夹具**：合成媒体默认生成到 `/tmp/media_analysis_fixtures`（`$MA_FIXTURES_DIR` 可覆盖），
   首次使用时生成，缺 ffmpeg 且文件不齐 ⇒ `skip`（附原因），不是 `fail`。

没有 `tests/__init__.py`：加了它，pytest 会把 `tests/` 的**父目录**当 basedir，
`import _fakes` 就失效了（GMI 与 AtTool 两处先例也都没有）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# --------------------------------------------------------------------------- #
# 0. 路径常量
# --------------------------------------------------------------------------- #
TESTS_DIR = Path(__file__).resolve().parent
REPO_DIR = TESTS_DIR.parent  # …/projects/astrbot_plugin_media_analysis
PROJECTS_DIR = REPO_DIR.parent  # …/projects（注入 sys.path 的那一层）
PKG_NAME = REPO_DIR.name  # astrbot_plugin_media_analysis

# --------------------------------------------------------------------------- #
# 1. ASTRBOT_ROOT 隔离 —— 必须早于任何 `import astrbot`
# --------------------------------------------------------------------------- #
TEST_ROOT = Path(os.environ.get("ASTRBOT_ROOT") or "/tmp/media_analysis_testroot").expanduser().resolve()
os.environ["ASTRBOT_ROOT"] = str(TEST_ROOT)
(TEST_ROOT / "data").mkdir(parents=True, exist_ok=True)

# --------------------------------------------------------------------------- #
# 2. sys.path：仓库父目录（按包导入）+ tests 目录（import _fakes）
# --------------------------------------------------------------------------- #
for _path in (TESTS_DIR, PROJECTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


# --------------------------------------------------------------------------- #
# 3. 后端判定（真核心优先；桩只兜底）
# --------------------------------------------------------------------------- #
def _real_core_available() -> bool:
    """插件与测试真正需要的那几个面是否都在（不只是 `import astrbot` 成功）。"""
    try:
        import astrbot.api  # noqa: F401
        from astrbot.api.message_components import Record, Reply, Video  # noqa: F401
        from astrbot.api.provider import Provider, STTProvider  # noqa: F401
        from astrbot.core.utils.error_redaction import redact_sensitive_text  # noqa: F401
        from astrbot.core.utils.media_utils import get_media_duration  # noqa: F401
    except Exception:  # pragma: no cover - 正常门禁永远走真核心
        return False
    return True


def _purge_astrbot_modules() -> None:
    """清掉半成品导入，免得桩复用到"导到一半"的模块。"""
    for name in [n for n in sys.modules if n == "astrbot" or n.startswith("astrbot.")]:
        del sys.modules[name]


if _real_core_available():
    CORE_BACKEND = "real-core"
else:  # pragma: no cover - 只有拿没有 astrbot 的解释器跑才会走到
    _purge_astrbot_modules()
    from _astrbot_stub import install as _install_stub

    _install_stub()
    CORE_BACKEND = "stub"


# --------------------------------------------------------------------------- #
# 4. 夹具
# --------------------------------------------------------------------------- #
def _fixtures_root() -> Path:
    import make_fixtures

    return make_fixtures.default_dir()


@pytest.fixture(scope="session")
def core_backend() -> str:
    """'real-core' 或 'stub'。断言"必须真核心"的用例用它 `skip`。"""
    return CORE_BACKEND


@pytest.fixture(scope="session")
def media_fixtures() -> dict[str, Path]:
    """合成夹具 `{stem: Path}`（首次调用时生成；幂等）。

    键：`audio_30s` / `video_30s` / `video_70s` / `video_5s` / `video_5s_noaudio` /
    `video_audio_only`。缺 ffmpeg 且文件不齐 ⇒ `skip`（附原因，不是失败）。
    """
    import make_fixtures

    directory = _fixtures_root()
    try:
        return make_fixtures.ensure_all(directory, quiet=True)
    except make_fixtures.FixtureError as exc:
        pytest.skip(f"合成夹具不可用：{exc}")


def _one(name: str):
    @pytest.fixture(scope="session")
    def _fixture(media_fixtures: dict[str, Path]) -> Path:
        return media_fixtures[name]

    _fixture.__name__ = name
    return _fixture


audio_30s = _one("audio_30s")
video_30s = _one("video_30s")
video_70s = _one("video_70s")
video_5s = _one("video_5s")
video_5s_noaudio = _one("video_5s_noaudio")
video_audio_only = _one("video_audio_only")


@pytest.fixture
def make_event():
    """一行造假事件（`_fakes.make_event` 的别名，省掉每个文件重复 import）。"""
    from _fakes import make_event as _make_event

    return _make_event


@pytest.fixture
def make_record():
    from _fakes import make_record as _make_record

    return _make_record


@pytest.fixture
def make_video():
    from _fakes import make_video as _make_video

    return _make_video


@pytest.fixture
def fake_stt():
    """`FakeSTTProvider` 的构造器（每次调用造一个新的，互不串调用记录）。"""
    from _fakes import FakeSTTProvider

    return FakeSTTProvider


@pytest.fixture
def fake_chat():
    """`FakeChatProvider` 的构造器（`modalities` 三态见类文档）。"""
    from _fakes import FakeChatProvider

    return FakeChatProvider


# --------------------------------------------------------------------------- #
# 5. 报告头：跑门禁时一眼看出发的是哪套后端、夹具在哪
# --------------------------------------------------------------------------- #
def pytest_report_header(config: pytest.Config) -> list[str]:
    return [
        f"media_analysis tests: backend={CORE_BACKEND} package={PKG_NAME}",
        f"  ASTRBOT_ROOT={os.environ.get('ASTRBOT_ROOT')} (测试根，不触生产树)",
        f"  fixtures={_fixtures_root()}",
    ]
