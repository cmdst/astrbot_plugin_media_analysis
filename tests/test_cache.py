"""落盘缓存 + TTL（SPEC-v0.2 §6）：键公式、命中/过期、容错、容量淘汰、跨实例持久、目录归属。

为什么单开一个文件（v0.1 的 `ResultCache` 是内存 LRU，只有 3 条用例挂在 `test_analyze.py` 里）：
v0.2 把它换成了**磁盘缓存**，于是多出一整层"文件系统会怎么坏"的契约 —— 损坏的 JSON、
时钟回拨、权限/ENOSPC、单条超限、总量淘汰、`.tmp` 残留、跨进程复用。这些都是**缓存自己的**
行为，不该混进主链路用例里；主链路只关心"命中回 `cached:true` + 0 token"（那三条仍在
`tests/test_analyze.py`）。

三条硬约束（§6.6）在这里逐条被钉：
1. **只存成功信封**：条目键集合恰好 `{v, key_inputs, created_at, envelope}`，`envelope` 必须是
   9 键 `OK_KEYS` 且 `ok is True`；
2. **不存本地路径**：序列化文本里出现 `/tmp/`、`/root/`、`data:` 一律拒写；
3. **故障永不影响主链路**：损坏/过期/权限/ENOSPC ⇒ 静默 miss 或"本进程禁用"，**不抛**。

目录归属也在这一层钉死：`<ASTRBOT_ROOT>/data/plugin_data/<插件名>/cache/v1/` ——
绝不落库目录、绝不落插件目录、绝不落生产树（测试用 `ASTRBOT_ROOT=/tmp`）。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from astrbot_plugin_media_analysis import cache as cache_mod
from astrbot_plugin_media_analysis import envelope
from _fakes import RecordingLogger

_REPO_DIR = Path(__file__).resolve().parents[1]
_PLUGIN = "astrbot_plugin_media_analysis"


def _envelope(text: str = "转写正文", *, summary: str = "摘要") -> dict[str, object]:
    return envelope.ok_envelope(
        kind="audio",
        duration_s=30.0,
        transcript=[{"t": 0.0, "text": text}],
        frames=[],
        summary=summary,
        notes=["audio_extracted"],
        elapsed_ms=12,
        cached=False,
    )


def _key(**overrides) -> str:
    base = dict(
        digest="0123456789abcdef",
        kind="audio",
        include_frames=False,
        max_frames=6,
        focus="",
        prompts_fp="ffffffffffffffff",
        audio_provider_id="a1",
        vision_provider_id="v1",
        plugin_version="0.2.0",
    )
    base.update(overrides)
    return cache_mod.build_key(**base)


@pytest.fixture()
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> cache_mod.DiskCache:
    """每个用例一个独立插件数据目录（**绝不**指向库目录或真实 ASTRBOT_ROOT）。"""
    monkeypatch.setattr(
        cache_mod, "_core_plugin_data_path", lambda: str(tmp_path / "plugin_data")
    )
    return cache_mod.DiskCache(plugin_name=_PLUGIN, ttl_seconds=86400)


# --------------------------------------------------------------------------- #
# 1. 键公式（§6.1）：10 个分量、64 hex、逐分量敏感
# --------------------------------------------------------------------------- #
def test_build_key_is_64_hex_and_deterministic() -> None:
    key = _key()
    assert len(key) == 64
    assert set(key) <= set("0123456789abcdef")
    assert key == _key(), "同输入必须同键（否则缓存永远命不中）"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("digest", "ffff0000ffff0000"),
        ("kind", "video"),
        ("include_frames", True),
        ("max_frames", 12),
        ("focus", "他在说什么"),
        ("prompts_fp", "0000000000000000"),
        ("audio_provider_id", "a2"),
        ("vision_provider_id", "v2"),
        ("plugin_version", "0.2.1"),
    ],
)
def test_build_key_changes_with_every_component(field: str, value: object) -> None:
    """每个分量都必须改变键 —— 少一个都会让"配置变了还命中旧结果"成为可能。"""
    assert _key(**{field: value}) != _key(), f"{field} 没有进缓存键"


def test_build_key_is_not_ambiguous_about_separator_collisions() -> None:
    """`|` 连接的下一个坑：分量里本身带 `|` 时不许与另一组输入撞键。

    实现用的是固定顺序拼接，所以 `focus="a|1"` 与 `focus="a"`+`include_frames=True`
    在**字符串**上可能相同；这条用例把"哪些输入算同键"钉在行为面上（撞了就红）。
    """
    assert _key(focus="a|1") != _key(focus="a", include_frames=True)


# --------------------------------------------------------------------------- #
# 2. 读写往返 / 跨实例持久（等价于跨进程）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_put_then_get_roundtrip(cache: cache_mod.DiskCache) -> None:
    key = _key()
    payload = _envelope()
    await cache.put(key, payload)

    entry = await cache.get(key)
    assert entry is not None
    assert entry["envelope"] == payload
    assert entry["age_s"] == 0
    assert set(entry) == set(cache_mod.ENTRY_KEYS) | {"age_s"}


@pytest.mark.asyncio
async def test_entry_layout_is_exactly_the_four_frozen_keys(cache: cache_mod.DiskCache) -> None:
    """§6.6-1：条目**只**有 4 个顶层键，`envelope` 恰好 9 键（多一键就是多一个契约面）。"""
    key = _key()
    await cache.put(key, _envelope())
    raw = json.loads(Path(cache._entry_path(key)).read_text(encoding="utf-8"))
    assert tuple(raw) == cache_mod.ENTRY_KEYS
    assert tuple(raw["envelope"]) == envelope.OK_KEYS
    assert raw["key_inputs"] == {"key": key}


@pytest.mark.asyncio
async def test_cache_persists_across_instances(
    cache: cache_mod.DiskCache, tmp_path: Path
) -> None:
    """跨实例持久（＝跨进程复用的等价物）：写一份文件，**新建实例**能读回。"""
    key = _key()
    await cache.put(key, _envelope("跨实例"))
    await cache.close()

    reopened = cache_mod.DiskCache(plugin_name=_PLUGIN, ttl_seconds=86400)
    entry = await reopened.get(key)
    assert entry is not None
    assert entry["envelope"]["transcript"] == [{"t": 0.0, "text": "跨实例"}]


@pytest.mark.asyncio
async def test_returned_envelope_is_a_copy(cache: cache_mod.DiskCache) -> None:
    key = _key()
    await cache.put(key, _envelope())
    first = await cache.get(key)
    first["envelope"]["notes"].append("被调用方改了")
    second = await cache.get(key)
    assert "被调用方改了" not in second["envelope"]["notes"], "缓存必须每次重新解码，不能被就地改写"


# --------------------------------------------------------------------------- #
# 3. TTL：过期 / 时钟回拨 / ttl=0 关闭
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_expired_entry_is_a_miss_and_gets_deleted(cache: cache_mod.DiskCache) -> None:
    key = _key()
    await cache.put(key, _envelope())
    path = Path(cache._entry_path(key))

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["created_at"] = int(time.time()) - 86401  # 刚过期 1 秒
    path.write_text(json.dumps(raw), encoding="utf-8")

    assert await cache.get(key) is None
    assert not path.exists(), "过期条目应顺手删掉（否则目录只会越长越大）"


@pytest.mark.asyncio
async def test_fresh_entry_within_ttl_is_a_hit(cache: cache_mod.DiskCache) -> None:
    key = _key()
    await cache.put(key, _envelope())
    path = Path(cache._entry_path(key))
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["created_at"] = int(time.time()) - 86399
    path.write_text(json.dumps(raw), encoding="utf-8")

    entry = await cache.get(key)
    assert entry is not None
    assert entry["age_s"] == 86399


@pytest.mark.asyncio
async def test_future_created_at_is_treated_as_expired(cache: cache_mod.DiskCache) -> None:
    """时钟回拨：`created_at` 在**未来** ⇒ 当过期（否则会出现"永生条目"）。"""
    key = _key()
    await cache.put(key, _envelope())
    path = Path(cache._entry_path(key))
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["created_at"] = int(time.time()) + 3600
    path.write_text(json.dumps(raw), encoding="utf-8")

    assert await cache.get(key) is None
    assert not path.exists()


def test_ttl_zero_disables_the_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`cache.ttl_seconds=0` ⇒ 关掉缓存（读写都不该碰磁盘、也不该建目录）。"""
    monkeypatch.setattr(
        cache_mod, "_core_plugin_data_path", lambda: str(tmp_path / "plugin_data")
    )
    disabled = cache_mod.DiskCache(plugin_name=_PLUGIN, ttl_seconds=0)
    assert disabled.disabled is True
    assert not (tmp_path / "plugin_data").exists(), "关缓存时连目录都不该建"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(86400, 86400), (0, 0), (-5, 0), (10**9, 2592000), ("60", 60), ("abc", 0), (None, 0), (3.9, 3)],
)
def test_ttl_is_clamped(raw: object, expected: int) -> None:
    """TTL 钳位到 `[0, 30 天]`；非法值当 0（＝关闭）—— 宁可关缓存也不许"永不过期"。"""
    assert cache_mod._clamp_ttl(raw) == expected


@pytest.mark.asyncio
async def test_ttl_zero_noops_even_when_directory_exists(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        cache_mod, "_core_plugin_data_path", lambda: str(tmp_path / "plugin_data")
    )
    store = cache_mod.DiskCache(plugin_name=_PLUGIN, ttl_seconds=0)
    key = _key()
    await store.put(key, _envelope())
    assert await store.get(key) is None
    assert not Path(store._entry_path(key)).exists()


# --------------------------------------------------------------------------- #
# 4. 容错：损坏 / 形状不符 / 权限 / 写失败（永不抛、永不进 notes）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_corrupt_bytes_are_a_miss_and_get_deleted(cache: cache_mod.DiskCache) -> None:
    key = _key()
    await cache.put(key, _envelope())
    path = Path(cache._entry_path(key))
    path.write_bytes(b"\x00\x01 not json \xff")

    assert await cache.get(key) is None
    assert not path.exists()


@pytest.mark.asyncio
async def test_entry_with_wrong_shape_is_a_miss(cache: cache_mod.DiskCache) -> None:
    key = _key()
    await cache.put(key, _envelope())
    path = Path(cache._entry_path(key))

    # ① 缺键 ② envelope 不是 dict ③ envelope 键顺序/集合不是 9 键 ④ created_at 是 bool
    for broken in (
        {"v": 1, "created_at": int(time.time()), "envelope": _envelope()},
        {"v": 1, "key_inputs": {}, "created_at": int(time.time()), "envelope": "nope"},
        {
            "v": 1,
            "key_inputs": {},
            "created_at": int(time.time()),
            "envelope": {"ok": True},
        },
        {"v": 1, "key_inputs": {}, "created_at": True, "envelope": _envelope()},
    ):
        path.write_text(json.dumps(broken), encoding="utf-8")
        assert await cache.get(key) is None, broken
        assert not path.exists()


@pytest.mark.asyncio
async def test_only_ok_envelopes_are_cached(cache: cache_mod.DiskCache) -> None:
    """失败信封不进缓存（否则一次 provider 故障会被缓存 TTL 那么久）。"""
    key = _key()
    await cache.put(key, envelope.fail_envelope(envelope.ERR_AUDIO_FAILED))
    assert await cache.get(key) is None
    assert not Path(cache._entry_path(key)).exists()


@pytest.mark.asyncio
async def test_envelope_with_extra_key_is_not_cached(cache: cache_mod.DiskCache) -> None:
    key = _key()
    payload = _envelope()
    payload["extra"] = "x"
    await cache.put(key, payload)
    assert await cache.get(key) is None


@pytest.mark.parametrize("banned", list(cache_mod.BANNED_SUBSTRINGS))
@pytest.mark.asyncio
async def test_entries_with_local_paths_are_refused(
    cache: cache_mod.DiskCache, monkeypatch: pytest.MonkeyPatch, banned: str
) -> None:
    """§6.6-2：序列化文本里出现 `/tmp/`、`/root/`、`data:` ⇒ 拒写 + 一次 WARNING。

    信封本来就不该带路径；这条是"别把会话/平台内部结构写进会进备份的文件里"的兜底。
    """
    log = RecordingLogger()
    monkeypatch.setattr(cache_mod, "LOG", log)
    key = _key()
    await cache.put(key, _envelope(f"正文里带了 {banned}foo"))
    assert await cache.get(key) is None
    assert "拒绝写入" in log.text()


@pytest.mark.asyncio
async def test_oversize_entry_is_skipped_not_truncated(
    cache: cache_mod.DiskCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单条超上限 ⇒ **不缓存**（截断会破坏 9 键契约，比不缓存糟得多）。"""
    monkeypatch.setattr(cache_mod, "MAX_ENTRY_BYTES", 256)
    key = _key()
    await cache.put(key, _envelope("长" * 500))
    assert await cache.get(key) is None


@pytest.mark.parametrize("key", ["", "short", "Z" * 64, "a" * 63, "a" * 65, "../" + "a" * 61])
@pytest.mark.asyncio
async def test_invalid_keys_are_rejected(cache: cache_mod.DiskCache, key: str) -> None:
    """键必须是 64 位小写 hex：挡住路径穿越与意外文件名。"""
    assert await cache.get(key) is None
    await cache.put(key, _envelope())  # 不抛即可
    assert cache.directory and not list(Path(cache.directory).glob("**/*.json"))


@pytest.mark.asyncio
async def test_write_failure_disables_the_cache_without_raising(
    cache: cache_mod.DiskCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENOSPC / 权限：写失败一次就"本进程禁用"，**绝不**影响主链路。"""
    log = RecordingLogger()
    monkeypatch.setattr(cache_mod, "LOG", log)

    def _boom(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cache_mod.DiskCache, "_write_atomic", _boom)
    await cache.put(_key(), _envelope())  # 不抛
    assert cache.disabled is True
    assert "已禁用缓存" in log.text()
    assert await cache.get(_key()) is None


@pytest.mark.asyncio
async def test_read_failure_is_a_miss_with_one_warning(
    cache: cache_mod.DiskCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = RecordingLogger()
    monkeypatch.setattr(cache_mod, "LOG", log)

    def _boom(_path: str) -> bytes:
        raise PermissionError(13, "denied")

    monkeypatch.setattr(cache_mod, "_read_bytes", _boom)
    assert await cache.get(_key()) is None
    assert await cache.get(_key()) is None
    warnings = [m for level, m in log.records if level == "WARNING"]
    assert len(warnings) == 1, "同一类读失败只告警一次（否则每条消息都刷日志）"


def test_missing_core_path_api_disables_the_cache_without_failing_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """核心内部面搬家 ⇒ 缓存整体禁用 + 一次 WARNING，**插件照常加载**。"""
    monkeypatch.setattr(cache_mod, "_core_plugin_data_path", None)
    store = cache_mod.DiskCache(plugin_name=_PLUGIN, ttl_seconds=86400)
    assert store.directory == ""
    assert store.disabled is True


def test_path_api_raising_disables_the_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom():
        raise RuntimeError("root unresolved")

    monkeypatch.setattr(cache_mod, "_core_plugin_data_path", _boom)
    store = cache_mod.DiskCache(plugin_name=_PLUGIN, ttl_seconds=86400)
    assert store.directory == ""
    assert store.disabled is True


# --------------------------------------------------------------------------- #
# 5. 容量淘汰（§6.2）：条目数上限 / 总量上限，按 mtime 最旧优先
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_evicts_by_entry_count(
    cache: cache_mod.DiskCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超过 `MAX_ENTRIES` ⇒ 删最旧的（mtime 最小），条目数回到上限内。"""
    monkeypatch.setattr(cache_mod, "MAX_ENTRIES", 3)
    keys = []
    for index in range(5):
        key = _key(focus=f"问题{index}")
        keys.append(key)
        await cache.put(key, _envelope(f"第 {index} 条"))
        await asyncio.sleep(0.01)  # 让 mtime 有区分度（同秒内写入会并列）

    files = list(Path(cache.directory).glob("**/*.json"))
    assert len(files) == 3, f"条目数没被压到上限：{len(files)}"
    alive = [key for key in keys if (Path(cache._entry_path(key))).exists()]
    assert alive == keys[-3:], "淘汰必须删最旧的（不是最旧的留着、新的被删）"


@pytest.mark.asyncio
async def test_evicts_by_total_bytes(
    cache: cache_mod.DiskCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cache_mod, "MAX_TOTAL_BYTES", 1)
    await cache.put(_key(focus="a"), _envelope())
    await cache.put(_key(focus="b"), _envelope())
    files = list(Path(cache.directory).glob("**/*.json"))
    assert len(files) <= 1, f"总量闸没生效：{len(files)} 个文件"


@pytest.mark.asyncio
async def test_eviction_never_raises_on_missing_directory(cache: cache_mod.DiskCache) -> None:
    """目录不存在时淘汰扫描必须安静返回（首次写入前就可能被调用）。"""
    cache._evict()  # 不抛即通过


# --------------------------------------------------------------------------- #
# 6. 并发：single-flight 同键串行化（§6.5）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_singleflight_serializes_the_same_key(cache: cache_mod.DiskCache) -> None:
    """同键并发 ⇒ 临界区最多次只有一个协程（"同一媒体只花一次 token"的机制面）。"""
    key = _key()
    inside = 0
    peak = 0

    async def _worker() -> None:
        nonlocal inside, peak
        async with cache.singleflight(key):
            inside += 1
            peak = max(peak, inside)
            await asyncio.sleep(0.02)
            inside -= 1

    await asyncio.gather(*(_worker() for _ in range(4)))
    assert peak == 1, f"同键并发没有串行化（峰值 {peak}）"


@pytest.mark.asyncio
async def test_singleflight_allows_different_keys_concurrently(cache: cache_mod.DiskCache) -> None:
    """不同键不互相阻塞（否则一个慢媒体会把别人的请求全堵住）。"""
    order: list[str] = []

    async def _worker(key: str) -> None:
        async with cache.singleflight(key):
            order.append(f"in:{key[:4]}")
            await asyncio.sleep(0.01)
            order.append(f"out:{key[:4]}")

    await asyncio.gather(_worker(_key(focus="a")), _worker(_key(focus="b")))
    # 两个协程都先进入临界区（不同键不互斥），然后才退出
    assert [item.split(":")[0] for item in order[:2]] == ["in", "in"], order


@pytest.mark.asyncio
async def test_singleflight_is_a_noop_when_disabled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        cache_mod, "_core_plugin_data_path", lambda: str(tmp_path / "plugin_data")
    )
    store = cache_mod.DiskCache(plugin_name=_PLUGIN, ttl_seconds=0)
    async with store.singleflight(_key()):
        pass  # 不抛即通过


# --------------------------------------------------------------------------- #
# 7. 目录归属（§4.2）：`ASTRBOT_ROOT/data/plugin_data/<插件名>/cache/v1/`
# --------------------------------------------------------------------------- #
def test_directory_is_rooted_at_astrbot_plugin_data_not_the_repo() -> None:
    """缓存目录必须由**核心路径 API** 决定（`<ASTRBOT_ROOT>/data/plugin_data/…`）。

    这条同时守住两件事：① 测试里 `ASTRBOT_ROOT=/tmp/…` ⇒ 缓存不落生产树；
    ② 缓存**绝不**落在插件目录/库目录里（那会让 `git status` 变脏、也会被当成源码发布）。
    """
    from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

    store = cache_mod.DiskCache(plugin_name=_PLUGIN, ttl_seconds=86400)
    expected_root = Path(get_astrbot_plugin_data_path()) / _PLUGIN / "cache" / "v1"
    assert Path(store.directory) == expected_root

    root_env = os.environ.get("ASTRBOT_ROOT", "")
    assert root_env, "conftest 必须把 ASTRBOT_ROOT 钉到 /tmp（本 lane 不写生产树）"
    assert Path(store.directory).is_relative_to(Path(root_env).resolve()), store.directory
    assert not Path(store.directory).is_relative_to(_REPO_DIR), (
        f"缓存目录落进了库目录：{store.directory}"
    )


@pytest.mark.asyncio
async def test_tmp_directory_is_inside_the_cache_and_left_clean(
    cache: cache_mod.DiskCache,
) -> None:
    """原子写用 `.tmp/`（与 `v1/` 同分区）⇒ `os.replace` 不会 EXDEV，且不留残骸。"""
    assert Path(cache.tmp_directory) == Path(cache.directory) / ".tmp"
    await cache.put(_key(), _envelope())
    leftovers = list(Path(cache.tmp_directory).iterdir())
    assert leftovers == [], f"`.tmp/` 里有残骸：{leftovers}"
    stray = [
        name
        for name in os.listdir(cache.directory)
        if name.startswith(".tmp-") or name.startswith("w-")
    ]
    assert stray == [], f"缓存根目录里有未清理的临时文件：{stray}"


@pytest.mark.asyncio
async def test_clear_removes_entries_but_keeps_the_directory(cache: cache_mod.DiskCache) -> None:
    await cache.put(_key(), _envelope())
    assert list(Path(cache.directory).glob("**/*.json"))

    await cache.clear()
    assert Path(cache.directory).is_dir(), "`data/plugin_data` 的约定是保留目录本身"
    assert not list(Path(cache.directory).glob("**/*.json"))


@pytest.mark.asyncio
async def test_clear_is_safe_when_directory_is_missing(cache: cache_mod.DiskCache) -> None:
    await cache.clear()  # 不抛即通过
