"""L1 可见性探测 `probe.py`（SPEC-v0.3 §3 / §9.2 / §15-T2）。

探测要回答一个**只有调用才能回答**的问题：这个 provider 真的能"看见"视频吗？
（`modalities` 里没有 video 选项，核心侧没有任何声明式判据，§16.2-2）

所以本文件守四件事：

1. **判据是"答对"不是"200"**：本次合成的**数字 + 单词两条都命中**才算过；
   答错 / 空文本 / 超时 / 抛错 ⇒ `ok=False`（**绝不**当成"支持"）；
2. **缓存键含 provider + model + PLUGIN_VERSION**（升版本天然失效），TTL 7 天，`0` = 关闭；
3. **成本保护**：同一键每 TTL 窗口最多自动跑 **1 次**；**落盘失败也退化为进程内记忆**
   —— 否则每次调用都白烧一次视频请求；
4. **合成样本失败不阻塞主链路**：没有字体 / 没有 ffmpeg / 编码失败 ⇒ `l1_ok=false` 占位
   （并缓存，防每轮重试），主链路照常回退抽帧。

所有用例都**不联网、不跑真 ffmpeg**（除了 `pick_font` 读一个本地字体文件路径）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from astrbot_plugin_media_analysis import cache as cache_mod
from astrbot_plugin_media_analysis import probe, video_native
from _fakes import RecordingLogger, native_video

_PLUGIN = "astrbot_plugin_media_analysis"


@pytest.fixture(autouse=True)
def _clean_memo():
    """进程内记忆是全局的 ⇒ 每个用例前后都清空（否则用例互相"命中"）。"""
    probe.reset_memo()
    yield
    probe.reset_memo()


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> probe.ProbeStore:
    monkeypatch.setattr(
        cache_mod, "_core_plugin_data_path", lambda: str(tmp_path / "plugin_data")
    )
    return probe.ProbeStore(plugin_name=_PLUGIN)


def _sample_file(tmp_path: Path, size: int = 156_234) -> probe.Sample:
    path = tmp_path / "probe_l1.mp4"
    path.write_bytes(b"\x00" * size)
    return probe.Sample(str(path), "7319", "MOVING", size)


def _patch_sample(monkeypatch: pytest.MonkeyPatch, sample: probe.Sample) -> None:
    async def _build(_workdir: str):
        return sample, probe.REASON_OK

    monkeypatch.setattr(probe, "build_sample", _build)


def _patch_sample_failure(monkeypatch: pytest.MonkeyPatch, reason: str) -> None:
    async def _build(_workdir: str):
        return None, reason

    monkeypatch.setattr(probe, "build_sample", _build)


class _FakeProc:
    """`asyncio.create_subprocess_exec` 的假身（**不跑真 ffmpeg**）。"""

    def __init__(
        self,
        *,
        rc: int = 0,
        err: bytes = b"",
        delay: float = 0.0,
        on_run=None,
    ) -> None:
        self.returncode: int | None = rc
        self._err = err
        self._delay = delay
        self._on_run = on_run
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._on_run is not None:
            self._on_run()
        return b"", self._err

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode or 0


def _patch_subprocess(monkeypatch: pytest.MonkeyPatch, factory) -> list[list[str]]:
    argv_seen: list[list[str]] = []

    async def _exec(*argv, **_kwargs):
        argv_seen.append(list(argv))
        return factory(list(argv))

    monkeypatch.setattr(probe.asyncio, "create_subprocess_exec", _exec)
    return argv_seen


# --------------------------------------------------------------------------- #
# 1. 判据（§3.2）：数字 + 单词**都**对才算看见
# --------------------------------------------------------------------------- #
def test_judge_requires_both_the_number_and_the_word() -> None:
    assert probe.judge("数字=7319,单词=MOVING", "7319", "MOVING") is True
    assert probe.judge("数字=7319", "7319", "MOVING") is False, "只答对数字不算看见"
    assert probe.judge("单词=MOVING", "7319", "MOVING") is False, "只答对单词不算看见"
    assert probe.judge("数字=0000,单词=MOVING", "7319", "MOVING") is False


def test_judge_is_case_insensitive_and_normalizes_fullwidth_digits() -> None:
    assert probe.judge("数字=7319 单词=moving", "7319", "MOVING") is True
    assert probe.judge("数字＝７３１９，单词＝MOVING", "7319", "MOVING") is True


def test_judge_rejects_empty_and_non_text() -> None:
    assert probe.judge("", "7319", "MOVING") is False
    assert probe.judge("   ", "7319", "MOVING") is False
    assert probe.judge(None, "7319", "MOVING") is False  # type: ignore[arg-type]


def test_judge_accepts_verbose_answers() -> None:
    """模型爱啰嗦：结果里多几句解释不影响判定（我们只要求那两条 token 出现）。"""
    answer = "我看到了。数字是 7319，右下角的英文单词是 MOVING。"
    assert probe.judge(answer, "7319", "MOVING") is True


# --------------------------------------------------------------------------- #
# 2. 缓存键（§3.3）：provider + model + PLUGIN_VERSION
# --------------------------------------------------------------------------- #
def test_build_key_is_64_hex_and_deterministic() -> None:
    key = probe.build_key("vid1", "gemini-3-flash", "0.3.0")
    assert len(key) == probe.KEY_HEX_LEN == 64
    assert set(key) <= set("0123456789abcdef")
    assert key == probe.build_key("vid1", "gemini-3-flash", "0.3.0")


@pytest.mark.parametrize(
    ("field", "value"),
    [("provider_id", "vid2"), ("model", "mimo-v2.6"), ("plugin_version", "0.3.1")],
)
def test_build_key_changes_with_every_component(field: str, value: str) -> None:
    base = {"provider_id": "vid1", "model": "gemini-3-flash", "plugin_version": "0.3.0"}
    assert probe.build_key(**{**base, field: value}) != probe.build_key(**base), (
        f"{field} 没进 probe 缓存键"
    )


def test_build_key_carries_the_probe_schema_version() -> None:
    """键前缀是 `probe-v1`（换判据/样本就换它 ⇒ 旧结论整体作废）。"""
    import hashlib

    material = "|".join((probe.PROBE_SCHEMA_VERSION, "vid1", "m", "0.3.0"))
    assert probe.build_key("vid1", "m", "0.3.0") == hashlib.sha256(
        material.encode("utf-8")
    ).hexdigest()
    assert probe.PROBE_SCHEMA_VERSION == "probe-v1"
    assert probe.PROBE_DIR_VERSION == "v1"


# --------------------------------------------------------------------------- #
# 3. 样本合成（§3.1）：任何失败都只是"没通过"，不阻塞主链路
# --------------------------------------------------------------------------- #
def test_pick_font_returns_empty_when_no_candidate_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(probe, "FONT_CANDIDATES", (str(tmp_path / "nope.ttf"),))
    assert probe.pick_font() == ""


def test_pick_font_returns_a_non_empty_existing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    font = tmp_path / "fake.ttf"
    font.write_bytes(b"\x00\x01")
    monkeypatch.setattr(probe, "FONT_CANDIDATES", (str(tmp_path / "missing.ttf"), str(font)))
    assert probe.pick_font() == str(font)


@pytest.mark.asyncio
async def test_build_sample_without_font_is_not_retried_as_a_crash(tmp_path: Path) -> None:
    """没有字体 ⇒ `no_font`（**不**抛异常）。调用方据此记占位并回退抽帧。"""
    import unittest.mock

    with unittest.mock.patch.object(probe, "pick_font", return_value=""):
        sample, reason = await probe.build_sample(str(tmp_path))
    assert sample is None
    assert reason == probe.REASON_NO_FONT


@pytest.mark.asyncio
async def test_build_sample_without_ffmpeg_reports_ffmpeg_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(probe, "pick_font", lambda: "/usr/share/fonts/x.ttf")

    async def _no_ffmpeg(*_argv, **_kwargs):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(probe.asyncio, "create_subprocess_exec", _no_ffmpeg)
    sample, reason = await probe.build_sample(str(tmp_path))
    assert sample is None
    assert reason == probe.REASON_FFMPEG_MISSING


@pytest.mark.asyncio
async def test_build_sample_with_nonzero_rc_reports_build_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(probe, "pick_font", lambda: "/usr/share/fonts/x.ttf")
    _patch_subprocess(monkeypatch, lambda _argv: _FakeProc(rc=1, err=b"drawtext failed"))
    sample, reason = await probe.build_sample(str(tmp_path))
    assert sample is None
    assert reason == probe.REASON_BUILD_FAILED


@pytest.mark.asyncio
async def test_build_sample_success_reports_number_word_and_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(probe, "pick_font", lambda: "/usr/share/fonts/x.ttf")

    def _write(argv: list[str]) -> _FakeProc:
        Path(argv[-1]).write_bytes(b"\x00" * 1234)
        return _FakeProc(rc=0)

    argv_seen = _patch_subprocess(monkeypatch, _write)
    sample, reason = await probe.build_sample(str(tmp_path))

    assert reason == probe.REASON_OK
    assert sample is not None
    assert sample.size == 1234
    assert len(sample.number) == 4 and sample.number.isdigit()
    assert sample.word in probe.FIXED_WORDS
    # 样本是 lavfi 合成 + `-an`（只看画面）+ 落在调用方给的 workdir 里
    argv = argv_seen[0]
    assert argv[0] == "ffmpeg"
    assert "-an" in argv
    assert argv[-1] == str(Path(tmp_path) / "probe_l1.mp4")
    assert f"{probe.SAMPLE_WIDTH}x{probe.SAMPLE_HEIGHT}" in " ".join(argv)


@pytest.mark.asyncio
async def test_build_sample_timeout_is_build_failed_and_kills_the_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(probe, "pick_font", lambda: "/usr/share/fonts/x.ttf")
    monkeypatch.setattr(probe, "SAMPLE_TIMEOUT_S", 0.01)
    procs: list[_FakeProc] = []

    def _factory(_argv: list[str]) -> _FakeProc:
        proc = _FakeProc(rc=0, delay=0.2)
        procs.append(proc)
        return proc

    _patch_subprocess(monkeypatch, _factory)
    sample, reason = await probe.build_sample(str(tmp_path))

    assert sample is None
    assert reason == probe.REASON_BUILD_FAILED
    assert procs and procs[0].killed, "超时后必须 kill 子进程（不许留僵尸）"


@pytest.mark.asyncio
async def test_build_sample_with_empty_product_is_build_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(probe, "pick_font", lambda: "/usr/share/fonts/x.ttf")
    _patch_subprocess(monkeypatch, lambda _argv: _FakeProc(rc=0))  # 不写文件
    sample, reason = await probe.build_sample(str(tmp_path))
    assert sample is None
    assert reason == probe.REASON_BUILD_FAILED


# --------------------------------------------------------------------------- #
# 4. run_l1：调用 + 判定（答错/空/超时/抛错都不是"支持"）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_run_l1_passes_only_when_the_model_answers_correctly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_sample(monkeypatch, _sample_file(tmp_path))
    provider = native_video(text="数字=7319，单词=MOVING")

    result = await probe.run_l1(provider, workdir=str(tmp_path), timeout=5)

    assert result.ok is True
    assert result.reason == probe.REASON_OK
    # 唯一一次原生调用：固定提问 + max_tokens 1024（与 §7.2 护栏① 同一条）
    calls = provider.payloads()
    assert len(calls) == 1
    assert calls[0]["max_tokens"] == probe.L1_MAX_TOKENS == 1024
    assert provider.prompt_texts() == [probe.L1_QUESTION]
    assert "数字=?" in probe.L1_QUESTION and "单词=?" in probe.L1_QUESTION


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    ["数字=7319，单词=WRONG", "数字=0000，单词=MOVING", "我看不清这张图", ""],
    ids=["wrong-word", "wrong-number", "no-answer", "empty"],
)
async def test_run_l1_answer_mismatch_is_not_supported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    """答错 / 空 ⇒ `mismatch` / `empty`，**`ok=False`**（探测失败不得当成"支持"）。"""
    _patch_sample(monkeypatch, _sample_file(tmp_path))
    provider = native_video(text=answer)

    result = await probe.run_l1(provider, workdir=str(tmp_path), timeout=5)

    assert result.ok is False
    assert result.reason in {probe.REASON_MISMATCH, probe.REASON_EMPTY}


@pytest.mark.asyncio
async def test_run_l1_200_with_empty_content_is_empty_not_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T2 的"空 content"面：200 但正文为空 ⇒ `empty`（不是"通过"）。"""
    _patch_sample(monkeypatch, _sample_file(tmp_path))
    result = await probe.run_l1(native_video(text="   "), workdir=str(tmp_path), timeout=5)
    assert result.ok is False
    assert result.reason == probe.REASON_EMPTY


@pytest.mark.asyncio
async def test_run_l1_provider_error_is_not_supported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_sample(monkeypatch, _sample_file(tmp_path))
    result = await probe.run_l1(
        native_video(fail_with=RuntimeError("boom")), workdir=str(tmp_path), timeout=5
    )
    assert result.ok is False
    assert result.reason == probe.REASON_ERROR


@pytest.mark.asyncio
async def test_run_l1_timeout_is_reported_as_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_sample(monkeypatch, _sample_file(tmp_path))
    result = await probe.run_l1(
        native_video(delay=0.3), workdir=str(tmp_path), timeout=0.05
    )
    assert result.ok is False
    assert result.reason == probe.REASON_TIMEOUT


@pytest.mark.asyncio
async def test_run_l1_without_a_native_channel_is_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_sample(monkeypatch, _sample_file(tmp_path))
    result = await probe.run_l1(
        native_video(with_client=False), workdir=str(tmp_path), timeout=5
    )
    assert result.ok is False
    assert result.reason == probe.REASON_ERROR


@pytest.mark.asyncio
async def test_run_l1_sample_failure_never_calls_the_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """样本都造不出来 ⇒ 根本不发请求（省一次真金白银）。"""
    _patch_sample_failure(monkeypatch, probe.REASON_NO_FONT)
    provider = native_video()
    result = await probe.run_l1(provider, workdir=str(tmp_path), timeout=5)
    assert result.ok is False
    assert result.reason == probe.REASON_NO_FONT
    assert not provider.creates()


# --------------------------------------------------------------------------- #
# 5. 缓存（§3.3 / §9.2）：命中 / 过期 / 关闭 / 清理
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_second_call_within_ttl_uses_the_cache_and_costs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: probe.ProbeStore
) -> None:
    _patch_sample(monkeypatch, _sample_file(tmp_path))
    provider = native_video(text="数字=7319，单词=MOVING")

    first = await probe.ensure_l1(
        provider,
        provider_id="vid1",
        model="m",
        plugin_version="0.3.0",
        ttl_seconds=604800,
        workdir=str(tmp_path),
        store=store,
    )
    second = await probe.ensure_l1(
        provider,
        provider_id="vid1",
        model="m",
        plugin_version="0.3.0",
        ttl_seconds=604800,
        workdir=str(tmp_path),
        store=store,
    )

    assert (first.ok, first.source) == (True, "live")
    assert (second.ok, second.source) == (True, "cache")
    assert len(provider.creates()) == 1, "缓存命中不许再花一次视频调用"
    # 落盘位置：`…/probe/v1/<64hex>.json`
    key = probe.build_key("vid1", "m", "0.3.0")
    entry_path = Path(store.directory) / f"{key}.json"
    assert entry_path.is_file(), entry_path
    entry = json.loads(entry_path.read_text(encoding="utf-8"))
    assert entry["v"] == probe.PROBE_SCHEMA_VERSION
    assert entry["provider_id"] == "vid1" and entry["model"] == "m"
    assert entry["plugin_version"] == "0.3.0"
    assert entry["l1"]["ok"] is True


@pytest.mark.asyncio
async def test_a_cached_failure_is_still_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: probe.ProbeStore
) -> None:
    """缓存里的 `ok=false` 不许被读成"支持"（§2.2-⑥：缓存未通过仍走回退链）。"""
    _patch_sample(monkeypatch, _sample_file(tmp_path))
    provider = native_video(text="数字=0000，单词=WRONG")

    first = await probe.ensure_l1(
        provider, provider_id="v", model="m", plugin_version="0.3.0",
        ttl_seconds=604800, workdir=str(tmp_path), store=store,
    )
    second = await probe.ensure_l1(
        provider, provider_id="v", model="m", plugin_version="0.3.0",
        ttl_seconds=604800, workdir=str(tmp_path), store=store,
    )

    assert first.ok is False and second.ok is False
    assert second.source == "cache"
    assert second.reason == probe.REASON_MISMATCH
    assert len(provider.creates()) == 1


@pytest.mark.asyncio
async def test_expired_entry_is_a_miss_and_reruns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: probe.ProbeStore
) -> None:
    _patch_sample(monkeypatch, _sample_file(tmp_path))
    provider = native_video(text="数字=7319，单词=MOVING")

    await probe.ensure_l1(
        provider, provider_id="v", model="m", plugin_version="0.3.0",
        ttl_seconds=100, workdir=str(tmp_path), store=store,
    )
    probe.reset_memo()  # 只测磁盘 TTL，不让进程内记忆兜住

    later = __import__("time").time() + 101
    monkeypatch.setattr(probe.time, "time", lambda: later)
    second = await probe.ensure_l1(
        provider, provider_id="v", model="m", plugin_version="0.3.0",
        ttl_seconds=100, workdir=str(tmp_path), store=store,
    )

    assert second.source == "live"
    assert len(provider.creates()) == 2


@pytest.mark.asyncio
async def test_ttl_zero_disables_disk_reads_and_costs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: probe.ProbeStore
) -> None:
    """`ttl_seconds=0` ⇒ **不读盘**；重复调用也不会重复花视频调用。

    生产上的"关闭探测"由 `analyze._native_channel` 的更早一道闸实现：`ttl<=0` 时**根本
    不会调用 `ensure_l1`**（记 `video_probe_disabled`，见 `tests/test_video_chain.py`）。
    所以本用例只钉模块自己的两条可观察面：
    ① `store.read(key, 0)` 恒 `None`（关掉了磁盘读）；② 第二次调用不再产生 provider 调用。

    **已知残留（照实登记，不粉饰）**：SPEC-v0.3 §3.3 的措辞是"既不读也不写"，实现里
    `ProbeStore.write` 不看 TTL、进程内 `_MEMO` 也仍会命中 —— 两者都发生在
    "analyze 已决定不探测"之后，对生产零影响（`tests/MAPPING-v03.md` §3 在册，
    待 code 侧收口时把 `write`/`_MEMO` 也挡在 `ttl<=0` 之外）。
    """
    _patch_sample(monkeypatch, _sample_file(tmp_path))
    provider = native_video(text="数字=7319，单词=MOVING")

    first = await probe.ensure_l1(
        provider, provider_id="v", model="m", plugin_version="0.3.0",
        ttl_seconds=0, workdir=str(tmp_path), store=store,
    )
    second = await probe.ensure_l1(
        provider, provider_id="v", model="m", plugin_version="0.3.0",
        ttl_seconds=0, workdir=str(tmp_path), store=store,
    )

    assert first.source == "live"
    assert second.source in {"live", "cache"}
    assert len(provider.creates()) == 1, "ttl=0 的重复调用不许放大成本"
    assert await store.read(probe.build_key("v", "m", "0.3.0"), 0) is None, "ttl=0 不许读盘"


@pytest.mark.asyncio
async def test_provider_model_and_version_all_invalidate_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: probe.ProbeStore
) -> None:
    _patch_sample(monkeypatch, _sample_file(tmp_path))
    provider = native_video(text="数字=7319，单词=MOVING")

    async def _run(**kwargs):
        return await probe.ensure_l1(
            provider,
            ttl_seconds=604800,
            workdir=str(tmp_path),
            store=store,
            **kwargs,
        )

    await _run(provider_id="v", model="m", plugin_version="0.3.0")
    assert (await _run(provider_id="v2", model="m", plugin_version="0.3.0")).source == "live"
    assert (await _run(provider_id="v", model="m2", plugin_version="0.3.0")).source == "live"
    assert (await _run(provider_id="v", model="m", plugin_version="0.3.1")).source == "live"
    assert (await _run(provider_id="v", model="m", plugin_version="0.3.0")).source == "cache"
    assert len(provider.creates()) == 4


@pytest.mark.asyncio
async def test_clear_removes_the_probe_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: probe.ProbeStore
) -> None:
    _patch_sample(monkeypatch, _sample_file(tmp_path))
    await probe.ensure_l1(
        native_video(text="数字=7319，单词=MOVING"),
        provider_id="v", model="m", plugin_version="0.3.0",
        ttl_seconds=604800, workdir=str(tmp_path), store=store,
    )
    assert Path(store.directory).is_dir()

    await store.clear()

    assert not Path(store.directory).exists()


@pytest.mark.asyncio
async def test_corrupt_entry_is_a_miss_and_reruns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: probe.ProbeStore
) -> None:
    _patch_sample(monkeypatch, _sample_file(tmp_path))
    provider = native_video(text="数字=7319，单词=MOVING")
    key = probe.build_key("v", "m", "0.3.0")
    Path(store.directory).mkdir(parents=True, exist_ok=True)
    (Path(store.directory) / f"{key}.json").write_text("{not json", encoding="utf-8")

    outcome = await probe.ensure_l1(
        provider, provider_id="v", model="m", plugin_version="0.3.0",
        ttl_seconds=604800, workdir=str(tmp_path), store=store,
    )
    assert outcome.source == "live"


@pytest.mark.asyncio
async def test_write_failure_degrades_to_memory_and_still_costs_only_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: probe.ProbeStore
) -> None:
    """**落盘失败也不放大成本**：写盘炸了只告警一次，第二次调用由进程内记忆兜住。"""
    log = RecordingLogger()
    monkeypatch.setattr(probe, "LOG", log)
    _patch_sample(monkeypatch, _sample_file(tmp_path))

    def _boom(_path: str, _data: bytes) -> None:
        raise OSError("ENOSPC")

    monkeypatch.setattr(probe, "_write_atomic", _boom)
    provider = native_video(text="数字=7319，单词=MOVING")

    first = await probe.ensure_l1(
        provider, provider_id="v", model="m", plugin_version="0.3.0",
        ttl_seconds=604800, workdir=str(tmp_path), store=store,
    )
    second = await probe.ensure_l1(
        provider, provider_id="v", model="m", plugin_version="0.3.0",
        ttl_seconds=604800, workdir=str(tmp_path), store=store,
    )

    assert first.ok is True and first.source == "live"
    assert second.source == "cache", "落盘失败后必须退化为进程内记忆"
    assert len(provider.creates()) == 1
    warnings = [message for level, message in log.records if level == "WARNING"]
    assert len(warnings) == 1, warnings
    assert "ENOSPC" not in warnings[0], "告警只打异常类名，不打异常正文"


@pytest.mark.asyncio
async def test_store_read_failure_is_a_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: probe.ProbeStore
) -> None:
    def _boom(_path: str) -> bytes:
        raise OSError("EACCES")

    monkeypatch.setattr(probe, "_read_bytes", _boom)
    assert await store.read("a" * 64, 604800) is None
    assert store._broken is True, "读失败一次就该熔断（不再反复踩同一块坏了的地）"


def test_clamp_ttl_matches_the_config_clamp() -> None:
    assert probe._clamp_ttl("abc") == 0
    assert probe._clamp_ttl(None) == 0
    assert probe._clamp_ttl(-5) == 0
    assert probe._clamp_ttl(604800) == 604800
    assert probe._clamp_ttl(10**9) == 2592000


def test_probe_outcome_reason_is_the_l1_reason() -> None:
    outcome = probe.ProbeOutcome(False, "cache", probe.L1Result(False, "mismatch", 3))
    assert outcome.reason == "mismatch"
    assert outcome.ok is False and outcome.source == "cache"


# --------------------------------------------------------------------------- #
# 6. 通道类失败不是裁决（VERIFY-v0.3 F1：一个离线工具毒化线上 7 天）
#
# 事故：`tools/probe_video_capability.py` 用 urllib 默认 UA ⇒ Cloudflare 回 403
# （`error code: 1010`）⇒ 工具把 403 记成 `l1_ok=false` 并**写进共享缓存** ⇒ 插件此后
# 1 ms、0 网络就记 `video_probe_l1_failed`，**7 天**不走原生视频（`--clear` 后插件自己
# 的 L1 11.6 s 通过）。所以本节的契约是两条：
#
# 1. **读侧**：`ok=false` 且 `reason` 属通道类 ⇒ **视同 miss**，重新实测；
# 2. **写侧**：通道类失败**绝不落盘**（只在本进程内短期冷却，冷却不等于裁决）。
# --------------------------------------------------------------------------- #
def _seed_entry(
    store: probe.ProbeStore,
    *,
    provider_id: str,
    model: str,
    version: str,
    ok: bool,
    reason: str,
    elapsed_ms: int = 1234,
) -> Path:
    """往共享 probe 缓存里种一条条目（模拟"别的工具写坏了"）。"""
    key = probe.build_key(provider_id, model, version)
    path = Path(store.directory) / f"{key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "v": probe.PROBE_SCHEMA_VERSION,
                "provider_id": provider_id,
                "model": model,
                "plugin_version": version,
                "l1": {
                    "ok": ok,
                    "reason": reason,
                    "at": int(probe.time.time()),
                    "elapsed_ms": elapsed_ms,
                },
                "latency": None,
                "updated_at": int(probe.time.time()),
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    "reason",
    [
        "channel_error",
        "http_403",
        "http_401",
        "http_429",
        "http_500",
        "timeout",
        "error",
        "dns_error",
        "network_unreachable",
        "connect_timeout",
        "channel_unavailable",
    ],
)
@pytest.mark.asyncio
async def test_channel_class_cached_failure_is_a_miss_and_reprobes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: probe.ProbeStore, reason: str
) -> None:
    """F1 正面修复：通道类条目**不许**当"这个模型看不见视频"的证据。"""
    _patch_sample(monkeypatch, _sample_file(tmp_path))
    path = _seed_entry(
        store, provider_id="vid1", model="m", version="0.3.0", ok=False, reason=reason
    )
    provider = native_video(text="数字=7319，单词=MOVING")

    outcome = await probe.ensure_l1(
        provider,
        provider_id="vid1",
        model="m",
        plugin_version="0.3.0",
        ttl_seconds=604800,
        workdir=str(tmp_path),
        store=store,
    )

    assert (outcome.ok, outcome.source) == (True, "live"), (
        f"reason={reason!r} 是通道类失败，必须视同 miss 重新实测"
    )
    assert len(provider.creates()) == 1, "必须真的发一次请求（不是 1 ms 命中失败）"
    entry = json.loads(path.read_text(encoding="utf-8"))
    assert entry["l1"]["ok"] is True and entry["l1"]["reason"] == probe.REASON_OK


@pytest.mark.parametrize(
    "reason", [probe.REASON_MISMATCH, probe.REASON_EMPTY, probe.REASON_NO_FONT]
)
@pytest.mark.asyncio
async def test_verdict_cached_failure_is_still_honoured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: probe.ProbeStore, reason: str
) -> None:
    """反例（防止修 F1 时把"真的看不见"也放过）：裁决性失败**照旧命中**、零调用。"""
    _patch_sample(monkeypatch, _sample_file(tmp_path))
    _seed_entry(
        store, provider_id="vid1", model="m", version="0.3.0", ok=False, reason=reason
    )
    provider = native_video(text="数字=7319，单词=MOVING")

    outcome = await probe.ensure_l1(
        provider,
        provider_id="vid1",
        model="m",
        plugin_version="0.3.0",
        ttl_seconds=604800,
        workdir=str(tmp_path),
        store=store,
    )

    assert (outcome.ok, outcome.source) == (False, "cache")
    assert outcome.reason == reason
    assert not provider.creates(), "裁决性失败命中缓存，不许再花一次调用"


@pytest.mark.asyncio
async def test_channel_failure_is_never_written_to_disk_and_cools_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: probe.ProbeStore
) -> None:
    """写侧契约：通道类失败不落盘；同一进程内进冷却、不反复打网。"""
    _patch_sample(monkeypatch, _sample_file(tmp_path))
    provider = native_video(fail_with=ConnectionError("cf 403 error code: 1010"))

    first = await probe.ensure_l1(
        provider,
        provider_id="vid1",
        model="m",
        plugin_version="0.3.0",
        ttl_seconds=604800,
        workdir=str(tmp_path),
        store=store,
    )
    key = probe.build_key("vid1", "m", "0.3.0")
    entry_path = Path(store.directory) / f"{key}.json"

    assert first.ok is False and first.source == "live"
    assert first.reason == probe.REASON_ERROR
    assert not entry_path.exists(), "通道类失败绝不许写成 l1_ok=false（F1 的根因）"

    second = await probe.ensure_l1(
        provider,
        provider_id="vid1",
        model="m",
        plugin_version="0.3.0",
        ttl_seconds=604800,
        workdir=str(tmp_path),
        store=store,
    )
    assert second.source == "cooldown" and second.ok is False
    assert len(provider.creates()) == 1, "冷却窗口内不许反复打网（成本保护）"
    assert not entry_path.exists()


@pytest.mark.asyncio
async def test_channel_cooldown_expires_and_reprobes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: probe.ProbeStore
) -> None:
    """冷却**不是裁决**：窗口一过就重新实测（否则又变成另一种"毒化"）。"""
    _patch_sample(monkeypatch, _sample_file(tmp_path))
    failing = native_video(fail_with=ConnectionError("boom"))

    await probe.ensure_l1(
        failing, provider_id="vid1", model="m", plugin_version="0.3.0",
        ttl_seconds=604800, workdir=str(tmp_path), store=store,
    )
    later = probe.time.time() + probe.CHANNEL_RETRY_SECONDS + 1
    monkeypatch.setattr(probe.time, "time", lambda: later)

    healthy = native_video(text="数字=7319，单词=MOVING")
    outcome = await probe.ensure_l1(
        healthy, provider_id="vid1", model="m", plugin_version="0.3.0",
        ttl_seconds=604800, workdir=str(tmp_path), store=store,
    )

    assert (outcome.ok, outcome.source) == (True, "live")
    assert len(healthy.creates()) == 1


def test_is_channel_failure_reason_classifies_the_two_families() -> None:
    """分类表本身（纯函数）：**裁决** vs **通道**，逐个钉住。"""
    for reason in ("ok", probe.REASON_MISMATCH, probe.REASON_EMPTY):
        assert probe.is_channel_failure_reason(reason) is False, reason
        assert probe.is_verdict_reason(reason) is True, reason
    for reason in ("channel_error", "http_403", "http_503", "timeout", "error",
                   "dns_error", "network_unreachable", "connect_timeout", "channel_x"):
        assert probe.is_channel_failure_reason(reason) is True, reason
        assert probe.is_verdict_reason(reason) is False, reason
    assert probe.is_channel_failure_reason("") is False
    assert probe.is_channel_failure_reason(None) is False
    # 大小写不敏感（外部工具可能写 "HTTP_403"）
    assert probe.is_channel_failure_reason("HTTP_403") is True


# --------------------------------------------------------------------------- #
# 7. CLI 那半边（`tools/probe_video_capability.py`）：UA + 三类响应
#
# 与插件半边的分工：插件保证"不把通道失败当证据"；CLI 保证"不制造这种证据"。
# 两条都做，才不会再有下一次"一个离线工具毒化线上 7 天"。
# --------------------------------------------------------------------------- #
_CLI_SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "probe_video_capability.py"


def _load_cli():
    import importlib.util

    spec = importlib.util.spec_from_file_location("probe_video_capability_under_test", _CLI_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_cli_sends_a_browser_acceptable_user_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """F1 直接原因：urllib 默认 `Python-urllib/3.x` 被 CF 当爬虫 ⇒ 必须带 SDK 形状的 UA。"""
    import urllib.request

    cli = _load_cli()
    agent = cli.user_agent()
    assert "/Python" in agent, agent
    assert "urllib" not in agent.lower(), agent

    captured: dict[str, str] = {}

    class _Resp:
        status = 200

        def read(self) -> bytes:
            return b'{"choices":[{"message":{"content":"\\u6570\\u5b57=1"}}]}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def _fake_urlopen(request, timeout=None):
        captured.update({k.lower(): v for k, v in request.headers.items()})
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    sample = tmp_path / "s.mp4"
    sample.write_bytes(b"\x00" * 2048)
    provider = cli.Provider("p", "m", "http://127.0.0.1:9/v1", "k", {}, "openai_chat_completion")
    result = cli.call_video(provider, str(sample), "q", max_tokens=1024, timeout=5)

    # `Request.headers` 的 `.items()` 会把键名规整成小写（URL 头名本就大小写不敏感）
    assert captured.get("user-agent") == cli.user_agent()
    assert captured.get("accept") == "application/json"
    assert result.kind == cli.CALL_OK and result.is_verdict is True


def test_cli_classifies_channel_errors_apart_from_verdicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """403/网络/超时/坏 JSON ⇒ `channel_error`（不可写缓存）；200 空文本 ⇒ `rejected`（裁决）。"""
    import email.message
    import io
    import urllib.error
    import urllib.request

    cli = _load_cli()
    sample = tmp_path / "s.mp4"
    sample.write_bytes(b"\x00" * 2048)
    provider = cli.Provider("p", "m", "http://127.0.0.1:9/v1", "k", {}, "openai_chat_completion")

    def _http_error(code: int):
        return urllib.error.HTTPError(
            "http://x", code, "err", email.message.Message(), io.BytesIO(b"")
        )

    def _raise(exc):
        def _fake(request, timeout=None):
            raise exc

        return _fake

    def _body(payload: bytes):
        class _Resp:
            status = 200

            def read(self) -> bytes:
                return payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return lambda request, timeout=None: _Resp()

    cases = {
        "403": (403,), "401": (401,), "429": (429,), "500": (500,), "400": (400,),
    }
    for label, (code,) in cases.items():
        monkeypatch.setattr(urllib.request, "urlopen", _raise(_http_error(code)))
        result = cli.call_video(provider, str(sample), "q", max_tokens=1024, timeout=5)
        assert result.kind == cli.CALL_CHANNEL_ERROR, label
        assert result.is_verdict is False, label
        assert f"http {code}" == result.status

    for exc in (ConnectionError("reset"), TimeoutError("slow"), OSError("dns")):
        monkeypatch.setattr(urllib.request, "urlopen", _raise(exc))
        result = cli.call_video(provider, str(sample), "q", max_tokens=1024, timeout=5)
        assert result.kind == cli.CALL_CHANNEL_ERROR, type(exc).__name__

    monkeypatch.setattr(urllib.request, "urlopen", _body(b"not json"))
    assert cli.call_video(provider, str(sample), "q", max_tokens=1024, timeout=5).kind == cli.CALL_CHANNEL_ERROR

    monkeypatch.setattr(urllib.request, "urlopen", _body(b'{"oops":1}'))
    assert cli.call_video(provider, str(sample), "q", max_tokens=1024, timeout=5).kind == cli.CALL_CHANNEL_ERROR

    monkeypatch.setattr(
        urllib.request, "urlopen", _body(b'{"choices":[{"message":{"content":""}}]}')
    )
    empty = cli.call_video(provider, str(sample), "q", max_tokens=1024, timeout=5)
    assert empty.kind == cli.CALL_REJECTED and empty.is_verdict is True


def test_cli_and_plugin_share_the_same_channel_reason_vocabulary() -> None:
    """两处**独立实现**（脚本不 import 插件）必须同口径，否则又会分叉。"""
    cli = _load_cli()
    for reason in (
        "ok", "mismatch", "empty", "no_font", "ffmpeg_missing", "build_failed",
        "channel_error", "http_403", "timeout", "error", "dns_error", "network_x",
    ):
        assert cli.is_channel_failure_reason(reason) == probe.is_channel_failure_reason(reason), reason


# --------------------------------------------------------------------------- #
# 8. 评审 F3 / F2：结论分类 TTL 与"进程内记忆也要有时效"
# --------------------------------------------------------------------------- #
def test_cache_ttl_for_splits_verdict_local_and_channel() -> None:
    """**评审 F3** 的分流表（纯函数）：真裁决 7 天、本地环境 1 小时、通道类不缓存。"""
    week = 604800
    assert probe.cache_ttl_for(True, "ok", week) == week
    assert probe.cache_ttl_for(False, probe.REASON_MISMATCH, week) == week
    assert probe.cache_ttl_for(False, probe.REASON_EMPTY, week) == week
    for reason in ("no_font", "ffmpeg_missing", "build_failed"):
        assert probe.cache_ttl_for(False, reason, week) == probe.LOCAL_FAILURE_TTL_S
        assert probe.cache_ttl_for(False, reason, 60) == 60, "配置更短时取更短"
    for reason in ("channel_error", "http_403", "timeout", "error"):
        assert probe.cache_ttl_for(False, reason, week) == 0, "通道类不该留在缓存里"


@pytest.mark.asyncio
async def test_local_failure_expires_after_an_hour_but_a_verdict_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: probe.ProbeStore
) -> None:
    """**评审 F3**：装个字体是一分钟的事，不该让插件因为一次缺字体而 7 天不用原生视频。"""
    _patch_sample(monkeypatch, _sample_file(tmp_path))
    provider = native_video(text="数字=7319，单词=MOVING")

    # ① 本地环境类：2 小时前写的 `no_font` ⇒ 过期（重新实测）
    _seed_entry(
        store, provider_id="v", model="m", version="0.3.0",
        ok=False, reason=probe.REASON_NO_FONT,
    )
    assert await store.read(probe.build_key("v", "m", "0.3.0"), 604800) is not None
    two_hours = probe.time.time() + 2 * 3600
    monkeypatch.setattr(probe.time, "time", lambda: two_hours)
    probe.reset_memo()
    assert await store.read(probe.build_key("v", "m", "0.3.0"), 604800) is None, (
        "本地环境类失败 1 小时后必须失效（F3）"
    )
    probe.reset_memo()
    outcome = await probe.ensure_l1(
        provider, provider_id="v", model="m", plugin_version="0.3.0",
        ttl_seconds=604800, workdir=str(tmp_path), store=store,
    )
    assert (outcome.ok, outcome.source) == (True, "live")
    assert len(provider.creates()) == 1

    # ② 真裁决：同样 2 小时前写的 `mismatch` ⇒ 仍然有效地命中（7 天）
    _seed_entry(
        store, provider_id="v2", model="m", version="0.3.0",
        ok=False, reason=probe.REASON_MISMATCH,
    )
    probe.reset_memo()
    verdict = await probe.ensure_l1(
        provider, provider_id="v2", model="m", plugin_version="0.3.0",
        ttl_seconds=604800, workdir=str(tmp_path), store=store,
    )
    assert (verdict.ok, verdict.source) == (False, "cache")
    assert len(provider.creates()) == 1, "真裁决不许被重新实测（省一次真金白银）"


@pytest.mark.asyncio
async def test_memo_is_bounded_so_deleting_the_cache_takes_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: probe.ProbeStore
) -> None:
    """**评审 F2**：`_MEMO` 命中也必须看时效，否则 `rm -rf probe/` 对运行中的插件无效。"""
    _patch_sample(monkeypatch, _sample_file(tmp_path))
    provider = native_video(text="数字=7319，单词=MOVING")

    first = await probe.ensure_l1(
        provider, provider_id="v", model="m", plugin_version="0.3.0",
        ttl_seconds=604800, workdir=str(tmp_path), store=store,
    )
    assert (first.ok, first.source) == (True, "live")

    # 磁盘被删（等价于运维 `rm -rf probe/`），但进程内记忆还在
    key = probe.build_key("v", "m", "0.3.0")
    Path(store.directory, f"{key}.json").unlink()
    within = await probe.ensure_l1(
        provider, provider_id="v", model="m", plugin_version="0.3.0",
        ttl_seconds=604800, workdir=str(tmp_path), store=store,
    )
    assert within.source == "cache", "记忆寿命内仍可零 IO 复用"
    assert len(provider.creates()) == 1

    # 记忆超过 MEMO_MAX_AGE_S ⇒ 必须回磁盘确认（磁盘已空 ⇒ 重新实测）
    later = probe.time.time() + probe.MEMO_MAX_AGE_S + 1
    monkeypatch.setattr(probe.time, "time", lambda: later)
    after = await probe.ensure_l1(
        provider, provider_id="v", model="m", plugin_version="0.3.0",
        ttl_seconds=604800, workdir=str(tmp_path), store=store,
    )
    assert (after.ok, after.source) == (True, "live"), "记忆过期后必须重新实测"
    assert len(provider.creates()) == 2


@pytest.mark.asyncio
async def test_clear_also_drops_the_in_process_memo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: probe.ProbeStore
) -> None:
    """**评审 F2**：`clear()` 连记忆一起清 —— 否则"清缓存"对运行中的插件等于没做。"""
    _patch_sample(monkeypatch, _sample_file(tmp_path))
    provider = native_video(text="数字=7319，单词=MOVING")

    await probe.ensure_l1(
        provider, provider_id="v", model="m", plugin_version="0.3.0",
        ttl_seconds=604800, workdir=str(tmp_path), store=store,
    )
    await store.clear()

    assert probe._MEMO == {}, "记忆必须被清空"
    after = await probe.ensure_l1(
        provider, provider_id="v", model="m", plugin_version="0.3.0",
        ttl_seconds=604800, workdir=str(tmp_path), store=store,
    )
    assert after.source == "live"
    assert len(provider.creates()) == 2
