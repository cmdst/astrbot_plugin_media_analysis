"""本地磁盘缓存（SPEC-v0.2 §6 / SPEC-v0.3 §9.1）：键 = 哈希(12 个分量)，值 = **成功信封文本**，带 TTL。

为什么从内存 LRU 换成落盘（v0.1 的 `ResultCache` 已删）：团队裁决 ③ 的诉求是
"**同一媒体二次引用不再花 token**" —— 进程内 LRU 重启即失效，做不到。落盘换来这条能力，
代价（正文会进核心备份、留 TTL）已在 SPEC-v0.2 §16-11 逐条登记。

三条硬约束（§6.6，实现层硬闸，不是口头约定）：
1. **只存信封文本**：条目键集合恰好 `{v, key_inputs, created_at, envelope}`，
   `envelope` 键集合与顺序**恰好**等于 `envelope.OK_KEYS`（9 键）；媒体字节/base64/
   抽帧 jpg/wav 路径一律不入缓存。
2. **不存本地路径**：写盘前扫一遍序列化文本，出现 `/tmp/`、`/root/`、`data:` 就拒绝写入
   并告警（路径会泄露会话/平台内部结构，而信封本来就不该带路径）。
3. **故障永不影响主链路**：损坏/过期/权限/ENOSPC 一律"当 miss"或"本进程禁用"，
   **不产生 notes、不产生错误码**（`notes` 是给模型看的降级通道，缓存是内部优化）。

目录（§4.2）：`<root>/data/plugin_data/<插件名>/cache/v1/<前两位>/<64hex>.json`，
`.tmp/` 与 `v1/` 同分区 ⇒ `os.replace` 原子；**懒创建**（`import` 期不建目录）。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import tempfile
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

from astrbot.api import logger as LOG

from . import envelope

# --------------------------------------------------------------------------- #
# 核心内部面加固：`astrbot_path` 不在 `astrbot.api.*` 的公开导出面里
# （SPEC-v0.2 §4.1）。搬家时退化为「缓存整体禁用 + 一次 WARNING」，**绝不因此拒载**。
# --------------------------------------------------------------------------- #
try:
    from astrbot.core.utils.astrbot_path import (
        get_astrbot_plugin_data_path as _core_plugin_data_path,
    )
except Exception:  # noqa: BLE001 —— 拿不到路径就禁用缓存，主链路不受影响
    LOG.warning(
        "media_analysis: 核心内部面 astrbot_path 不可用，结果缓存已整体禁用（插件仍加载）"
    )
    _core_plugin_data_path = None  # type: ignore[assignment]

CACHE_SCHEMA_VERSION = "v1"  # 目录名；换布局就换它（老目录自然失效，无需迁移代码）
KEY_SCHEMA_VERSION = "v3"  # 键公式版本（SPEC-v0.3 §9.1）；改公式就 +1
# v2 → v3（v0.3）：`include_frames` 布尔变三态字符串 `mode`，并加 `video_provider_id`
# 与 `video_fp` 两个分量 ⇒ 12 个分量。旧 v2 条目**不写迁移代码**：键变了自然不命中，
# 按 TTL 老化淘汰（SPEC-v0.3 §9.1 冻结）。

MAX_ENTRIES = 256  # 条目数上限（不做配置键）
MAX_TOTAL_BYTES = 16 * 1024 * 1024  # 总字节上限
MAX_ENTRY_BYTES = 256 * 1024  # 单条目上限：超了**不缓存**，绝不截断（截断破坏 9 键契约）

ENTRY_KEYS: tuple[str, ...] = ("v", "key_inputs", "created_at", "envelope")
TMP_DIR_NAME = ".tmp"
KEY_HEX_LEN = 64
BUCKET_LEN = 2

# 序列化文本里**禁止**出现的子串（§6.6-2）。`data:` 覆盖 base64 data URL。
BANNED_SUBSTRINGS: tuple[str, ...] = ("/tmp/", "/root/", "data:")

_WARNED_PATH = False  # 目录取不到只告警一次（probe 与 cache 共用这一条）


def build_key(
    *,
    digest: str,
    kind: str,
    mode: str,
    max_frames: int,
    focus: str,
    prompts_fp: str,
    audio_provider_id: str,
    video_provider_id: str,
    vision_provider_id: str,
    video_fp: str,
    plugin_version: str,
) -> str:
    """缓存键（SPEC-v0.3 §9.1 冻结公式）：**12 个分量**按固定顺序 `|` 连接后 sha256。

    分量为 `v3 | digest | kind | mode | max_frames | prompts_fp | focus |
    audio_provider_id | video_provider_id | vision_provider_id | video_fp |
    PLUGIN_VERSION`。两个 v0.3 的新分量各有明确理由：

    - `mode`（`auto|true|false`）：**同一媒体**在三种模式下产物不同（原生/压缩/抽帧回退）
      ⇒ 互相命中就是把"没看画面"的结果当"看过"返回。v0.2 的布尔值不够表达"auto"。
    - `video_fp`：影响产物的**配置**（直送门槛 / 压缩开关 / 压缩预算）进键
      ⇒ 改门槛或关压缩即让旧结果失效，不会拿旧压缩产物冒充新门槛下的结果。

    用**配置值**（可能为 `""`）而不是"解析出的 provider id"：解析发生在缓存查询之后，
    提前解析会把"provider 配置坏了"变成"连缓存命中都拿不到"（§6.1 已知残留已披露：
    同一 provider id 下换底层模型时，旧条目最多存活一个 TTL）。
    """
    material = "|".join(
        (
            KEY_SCHEMA_VERSION,
            str(digest),
            str(kind),
            str(mode),
            str(int(max_frames)),
            str(prompts_fp),
            str(focus),
            str(audio_provider_id),
            str(video_provider_id),
            str(vision_provider_id),
            str(video_fp),
            str(plugin_version),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def plugin_data_dir(plugin_name: str) -> str:
    """`<root>/data/plugin_data/<插件名>` —— 结果缓存与 probe 缓存的共同根。

    路径 API 不可用（核心内部面搬家）时回 `""`：调用方据此"本进程禁用落盘"，
    **绝不**在 cwd 或库目录里造目录（v0.1 的教训）。
    """
    global _WARNED_PATH
    if _core_plugin_data_path is None:
        return ""
    try:
        base = _core_plugin_data_path()
    except Exception as exc:  # noqa: BLE001 —— 拿不到就当禁用，不抛给主链路
        if not _WARNED_PATH:
            _WARNED_PATH = True
            LOG.warning(
                "media_analysis: 取插件数据目录失败（%s），本进程内缓存与探测记录均不落盘",
                type(exc).__name__,
            )
        return ""
    if not isinstance(base, str) or not base:
        return ""
    return os.path.join(base, str(plugin_name))


def _clamp_ttl(ttl_seconds: object) -> int:
    try:
        value = int(ttl_seconds)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(0, min(2592000, value))


class DiskCache:
    """一个插件一个实例；`ttl_seconds == 0` 或路径不可用 ⇒ 全部读写 no-op。"""

    def __init__(self, *, plugin_name: str, ttl_seconds: int = 86400) -> None:
        self.plugin_name = str(plugin_name)
        self.ttl_seconds = _clamp_ttl(ttl_seconds)
        self._lock = asyncio.Lock()  # 目录级写锁：put（含淘汰扫描）与 clear 共用
        self._flights: dict[str, tuple[asyncio.Lock, int]] = {}
        self._broken = False  # 写失败一次 ⇒ 本进程内彻底禁用
        self._warned_read = False
        self._warned_write = False

    # ------------------------------------------------------------------ #
    # 路径
    # ------------------------------------------------------------------ #
    @property
    def directory(self) -> str:
        """缓存根目录（`…/cache/v1`）；路径 API 不可用时回 `""`（= 禁用）。"""
        root = plugin_data_dir(self.plugin_name)
        if not root:
            return ""
        return os.path.join(root, "cache", CACHE_SCHEMA_VERSION)

    @property
    def tmp_directory(self) -> str:
        return os.path.join(self.directory, TMP_DIR_NAME)

    @property
    def disabled(self) -> bool:
        """`True` ⇒ 读写都是 no-op（TTL=0 / 路径不可用 / 写失败后自禁用）。"""
        if self._broken or self.ttl_seconds <= 0:
            return True
        return not self.directory

    def _entry_path(self, key: str) -> str:
        return os.path.join(self.directory, key[:BUCKET_LEN], f"{key}.json")

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #
    async def get(self, key: str) -> dict[str, object] | None:
        """命中返回条目副本（`envelope` + 计算出的 `age_s`），未命中/损坏/过期回 `None`。"""
        if self.disabled or not self._valid_key(key):
            return None
        path = self._entry_path(key)
        try:
            raw = await asyncio.to_thread(_read_bytes, path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            self._warn_read(exc)
            return None

        entry = _decode_entry(raw)
        if entry is None:
            LOG.debug("media_analysis: 缓存条目损坏，已丢弃（%s）", path[-KEY_HEX_LEN:])
            self._discard(path)
            return None

        created_at = float(entry["created_at"])  # type: ignore[arg-type]
        age_s = int(time.time() - created_at)
        if age_s < 0 or age_s > self.ttl_seconds:
            # 过期（含时钟回拨：`created_at` 在未来一律当过期，防"永生条目"）
            self._discard(path)
            return None

        with contextlib.suppress(OSError):  # 命中刷新 LRU（用 mtime 当时间戳，不另建索引）
            await asyncio.to_thread(os.utime, path, None)

        payload = dict(entry)
        payload["age_s"] = age_s
        return payload

    # ------------------------------------------------------------------ #
    # 写
    # ------------------------------------------------------------------ #
    async def put(self, key: str, payload: Mapping[str, object]) -> None:
        """写入一份**成功信封**；任何异常一律吞掉（缓存不许影响主链路成败）。"""
        if self.disabled or not self._valid_key(key):
            return
        if not _is_ok_envelope(payload):
            return
        entry = {
            "v": 1,
            "key_inputs": {"key": key},  # 键是哈希，不含原文；留给人工排查
            "created_at": int(time.time()),
            "envelope": dict(payload),
        }
        try:
            text = json.dumps(entry, ensure_ascii=False)
            data = text.encode("utf-8")
        except (TypeError, ValueError):
            return
        if len(data) > MAX_ENTRY_BYTES:
            LOG.debug(
                "media_analysis: 缓存条目 %d B 超过单条上限 %d B，跳过写入",
                len(data),
                MAX_ENTRY_BYTES,
            )
            return
        if any(token in text for token in BANNED_SUBSTRINGS):
            LOG.warning(
                "media_analysis: 缓存条目疑似含本地路径或内联数据，已拒绝写入（键 %s…）",
                key[:8],
            )
            return

        async with self._lock:
            try:
                await asyncio.to_thread(self._write_atomic, key, data)
                await asyncio.to_thread(self._evict)
            except OSError as exc:
                self._break_cache(exc)

    async def clear(self) -> None:
        """清空缓存目录内容（best-effort；`terminate()` 用，可重复调用）。"""
        directory = self.directory
        if not directory:
            return
        async with self._lock:
            with contextlib.suppress(OSError):
                await asyncio.to_thread(_remove_tree, directory)

    async def close(self) -> None:
        """关掉 single-flight 锁表（没有后台任务/连接需要收尾）。"""
        self._flights.clear()

    # ------------------------------------------------------------------ #
    # 并发
    # ------------------------------------------------------------------ #
    @asynccontextmanager
    async def singleflight(self, key: str) -> AsyncIterator[None]:
        """同键串行化（§6.5）：两个并发调用同一媒体时只有一个花 token。

        跨进程**不处理**：本部署单进程；多进程最坏结果是双方都算一遍（文件不会坏，
        同分区 `os.replace` 原子）。不做 flock —— 锁文件残留会让缓存**永久**失效。
        """
        if self.disabled:
            yield
            return
        lock, refs = self._flights.get(key) or (asyncio.Lock(), 0)
        self._flights[key] = (lock, refs + 1)
        try:
            async with lock:
                yield
        finally:
            current_lock, current_refs = self._flights.get(key) or (lock, 1)
            if current_refs <= 1:
                self._flights.pop(key, None)
            else:
                self._flights[key] = (current_lock, current_refs - 1)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    @staticmethod
    def _valid_key(key: str) -> bool:
        """键必须是 64 位小写 hex（挡住路径穿越与意外文件名）。"""
        if not isinstance(key, str) or len(key) != KEY_HEX_LEN:
            return False
        return all(char in "0123456789abcdef" for char in key)

    def _write_atomic(self, key: str, data: bytes) -> None:
        """同分区 `.tmp/` + `os.replace` 原子落盘（跨设备会 EXDEV ⇒ 禁止先写 /tmp）。"""
        path = self._entry_path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_dir = self.tmp_directory
        os.makedirs(tmp_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix="w-", suffix=".json", dir=tmp_dir)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise

    def _evict(self) -> None:
        """按 `mtime` 最旧优先删，直到条目数与总字节都不超上限（§6.2）。"""
        directory = self.directory
        if not directory:
            return
        entries: list[tuple[float, int, str]] = []
        total_bytes = 0
        try:
            with os.scandir(directory) as iterator:
                buckets = [
                    item.path
                    for item in iterator
                    if item.is_dir() and not item.name.startswith(".")
                ]
        except FileNotFoundError:
            return
        for bucket in buckets:
            with contextlib.suppress(OSError):
                with os.scandir(bucket) as iterator:
                    for item in iterator:
                        if not item.name.endswith(".json"):
                            continue
                        try:
                            stat = item.stat()
                        except OSError:
                            continue
                        entries.append((stat.st_mtime, stat.st_size, item.path))
                        total_bytes += stat.st_size
        if len(entries) <= MAX_ENTRIES and total_bytes <= MAX_TOTAL_BYTES:
            return
        entries.sort(key=lambda item: item[0])  # 最旧（mtime 最小）先删
        remaining = len(entries)
        for _mtime, size, path in entries:
            if remaining <= MAX_ENTRIES and total_bytes <= MAX_TOTAL_BYTES:
                break
            with contextlib.suppress(OSError):
                os.unlink(path)
                remaining -= 1
                total_bytes -= size

    def _discard(self, path: str) -> None:
        """删掉一个坏/过期条目（best-effort，失败忽略）。"""
        with contextlib.suppress(OSError):
            os.unlink(path)

    def _warn_read(self, exc: BaseException) -> None:
        if self._warned_read:
            return
        self._warned_read = True
        LOG.warning(
            "media_analysis: 结果缓存读失败（%s），本次按未命中处理",
            type(exc).__name__,
        )

    def _break_cache(self, exc: BaseException) -> None:
        if self._broken:
            return
        self._broken = True
        if not self._warned_write:
            self._warned_write = True
            LOG.warning(
                "media_analysis: 结果缓存写失败（%s），本进程内已禁用缓存（主链路照常）",
                type(exc).__name__,
            )


# --------------------------------------------------------------------------- #
# 纯函数（好测：不碰事件循环、不碰实例状态）
# --------------------------------------------------------------------------- #
def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def _remove_tree(directory: str) -> None:
    """清空目录**内容**但保留目录本身（`data/plugin_data` 的约定）。"""
    if not os.path.isdir(directory):
        return
    for name in os.listdir(directory):
        target = os.path.join(directory, name)
        if os.path.isdir(target) and not os.path.islink(target):
            shutil.rmtree(target, ignore_errors=True)
        else:
            with contextlib.suppress(OSError):
                os.unlink(target)


def _is_ok_envelope(payload: Mapping[str, object]) -> bool:
    """只缓存成功信封，且键集合与顺序必须**恰好**是 9 键（§6.6-1）。"""
    if not isinstance(payload, Mapping):
        return False
    if tuple(payload.keys()) != envelope.OK_KEYS:
        return False
    return payload.get("ok") is True


def _decode_entry(raw: bytes) -> dict[str, object] | None:
    """解析并校验条目形状；任何不符都算损坏（返回 `None`，调用方删文件当 miss）。"""
    try:
        entry = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(entry, dict):
        return None
    if set(entry.keys()) != set(ENTRY_KEYS):
        return None
    created_at = entry.get("created_at")
    if isinstance(created_at, bool) or not isinstance(created_at, (int, float)):
        return None
    payload = entry.get("envelope")
    if not isinstance(payload, dict):
        return None
    if tuple(payload.keys()) != envelope.OK_KEYS:
        return None
    if payload.get("ok") is not True:
        return None
    return entry
