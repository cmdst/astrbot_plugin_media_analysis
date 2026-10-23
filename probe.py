"""L1 可见性探测：小样本 + 可区分事实 + `probe/v1` 缓存（SPEC-v0.3 §3）。

**只做 L1**。t8 证明"体积上限"不是事实（Gemini 47.2 MiB / mimo 36.5 MiB 仍 200，
0 次拒绝），而单次大体积探测要 4–6 分钟 ⇒ **L2 体积阶梯整条删除**（§16.2-1）：
不作为功能、不作为配置键、不自动跑。本模块只回答一个问题：

> **这个 provider 真的能"看见"视频吗？**（200 ≠ 看见，§1.1-5）

判据是"答对本次合成的**可区分事实**"（数字 + 单词），不是状态码：

- 4 s / 1280x720 / CRF 30 / ~150 KB 合成样片（`ffmpeg -f lavfi`，**不用真实群媒体**）；
- 画面正中一个 4 位白色数字、右下角一个黄色英文单词（从固定 8 词表随机）；
- 两条**都**命中才算过（数字按字符串匹配、全角先归一；单词大小写不敏感）。

缓存（§3.3）：`data/plugin_data/<插件名>/probe/v1/<64hex>.json`，键含
`provider_id + model + PLUGIN_VERSION`（升版本天然失效），TTL `video.probe_ttl_seconds`
（默认 7 天，`0` = 关闭 = 既不读也不写）。落盘失败**静默降级**并退化为进程内记忆
—— 那一条同时保证"每 TTL 窗口最多自动跑 1 次"（失败也写占位，防打爆成本）。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import random
import shutil
import tempfile
import time

from astrbot.api import logger as LOG

from . import cache as cache_mod
from . import video_native

# --------------------------------------------------------------------------- #
# 冻结常量（SPEC-v0.3 §3.1–§3.3）
# --------------------------------------------------------------------------- #
PROBE_SCHEMA_VERSION = "probe-v1"  # 条目里的 `v` 与键前缀（SPEC §3.3）
PROBE_DIR_VERSION = "v1"  # **目录名**（`probe/v1/`；与条目里的 `v` 是两个概念）
#: 调用方没传 store 时的默认插件名（生产只有一个插件实例，名字 == 目录名）
PROBE_STORE_PLUGIN_NAME = "astrbot_plugin_media_analysis"
PROBE_DIR_NAME = "probe"
KEY_HEX_LEN = 64

L1_TIMEOUT_S = 30.0  # t8 实测 Gemini 3.2 s / mimo 18.4 s，留 1.6× 余量
L1_MAX_TOKENS = 1024  # 与 §7.2 护栏① 同一条（mimo 在 64 时 200 但空文本）
SAMPLE_TIMEOUT_S = 20.0  # 一次 ffmpeg 合成（实测 < 1 s，只防病态环境）
SAMPLE_SECONDS = 4
SAMPLE_WIDTH = 1280
SAMPLE_HEIGHT = 720
SAMPLE_RATE = 12
SAMPLE_CRF = 30

FONT_CANDIDATES: tuple[str, ...] = (
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
)

#: 8 个逐字母无歧义的英文单词（§3.1）
FIXED_WORDS: tuple[str, ...] = (
    "MOVING",
    "GREEN",
    "CAMERA",
    "WINDOW",
    "PAPER",
    "ORANGE",
    "SILVER",
    "GUITAR",
)

#: 固定提问（**不掺 focus**：L1 判的是"能不能看"，不是"答得好不好"，§12.2）
L1_QUESTION = (
    "视频画面正中有一个很大的白色数字，右下角有一个黄色英文单词。"
    "只回答这两项：数字=?,单词=?。不要解释。"
)

# 失败原因码（进缓存 `l1.reason`，**不是**错误码，也不进 notes 正文）
REASON_OK = "ok"
REASON_NO_FONT = "no_font"
REASON_FFMPEG_MISSING = "ffmpeg_missing"
REASON_BUILD_FAILED = "build_failed"
REASON_EMPTY = "empty"
REASON_MISMATCH = "mismatch"
REASON_TIMEOUT = "timeout"
REASON_ERROR = "error"

# --------------------------------------------------------------------------- #
# 通道类失败 vs 裁决（VERIFY-v0.3 F1 的教训，**这是本模块最贵的一条分界**）
#
# `l1.reason` 里有两类东西，语义完全不同：
#
# | 类 | 例子 | 能说明什么 |
# | --- | --- | --- |
# | **裁决**（verdict） | `ok` / `mismatch` / `empty` / `no_font` / `ffmpeg_missing` / `build_failed` | "这次**确实**试过、且结果是它不行" |
# | **通道失败**（channel） | `channel_error` / `http_403` / `timeout` / `error` / `dns_*` / `network_*` | 只说明**这次没打成**，什么都没证明 |
#
# 为什么必须分开：`probe/v1/` 是**共享缓存** —— 任何写它的东西（本地 CLI 工具、旧版本、
# 一次网络抖动）写进去的 `l1_ok=false` 都会让插件在 7 天里**不再尝试原生视频**。
# 真实事故（`VERIFY-v0.3.md` F1）：校准工具用 urllib 默认 UA ⇒ Cloudflare 回 403
# `error code: 1010` ⇒ 工具把 403 记成"看不见"并落盘 ⇒ 插件 1 ms、0 网络就记
# `video_probe_l1_failed`，`--clear` 后插件自己的 L1（真 SDK client）11.6 s **通过**。
#
# 于是三条硬规则（读取侧 + 写入侧）：
# 1. 读缓存：`ok=false` 且 `reason` 属通道类 ⇒ **视同 miss**（重新实测），不当"不支持"证据；
# 2. 写缓存：通道类失败 **不落盘**（只留在进程内做短期冷却），绝不写 `l1_ok=false`；
# 3. 进程内冷却：通道类失败后 `CHANNEL_RETRY_SECONDS` 内不再打网（否则每次调用都白等一次
#    超时，把 100 s 预算吃光）—— 冷却不是裁决，冷却窗口过期即重新实测。
# --------------------------------------------------------------------------- #
REASON_CHANNEL_ERROR = "channel_error"

CHANNEL_REASONS: frozenset[str] = frozenset(
    {REASON_CHANNEL_ERROR, "http_error", "timeout", "error", "dns", "network", "connect"}
)
CHANNEL_REASON_PREFIXES: tuple[str, ...] = (
    "http_",  # http_403 / http_429 / http_5xx（CLI 会把状态码拼进来）
    "dns_",
    "network_",
    "connect_",
    "channel_",
)

#: 通道类失败在同一进程内的冷却窗口（秒）。只影响"要不要再打一次网"，不影响裁决。
CHANNEL_RETRY_SECONDS = 60.0

#: **本地环境类**失败（没有字体 / 没有 ffmpeg / 编码失败）的短 TTL：评审 F3 的裁决 ——
#: "瞬时失败不该按 7 天缓存"。装个字体、补个 ffmpeg 都是分钟级就能修好的事，让插件因为
#: 一次缺字体而 7 天不用原生视频，和 F1 的"毒化"是同一类伤害（只是更轻）。
LOCAL_FAILURE_TTL_S = 3600

#: 进程内记忆的**最长寿命**（秒）：评审 F2 —— `--clear` 或 `rm -rf probe/` 之后，运行中的
#: 插件最多再"记得"这么久就必须回磁盘确认一次。否则删了缓存也白删（内存里还留着结论）。
MEMO_MAX_AGE_S = 300

#: 判据失败（模型答错/空文本）＝**真裁决**，按配置 TTL（默认 7 天）缓存。
VERDICT_FAILURE_REASONS: frozenset[str] = frozenset({REASON_MISMATCH, REASON_EMPTY})


def is_channel_failure_reason(reason: object) -> bool:
    """这个 `reason` 是否属**通道类**（＝不能当作"模型看不见"的证据）。纯函数。"""
    text = str(reason or "").strip().lower()
    if not text:
        return False
    if text in CHANNEL_REASONS:
        return True
    return any(text.startswith(prefix) for prefix in CHANNEL_REASON_PREFIXES)


def is_verdict_reason(reason: object) -> bool:
    """反过来：这个 `reason` 能不能当"试过且不行"的裁决（`ok=true` 时恒为 True）。"""
    return not is_channel_failure_reason(reason)

_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")

#: 进程内记忆（键 → 条目）。存在的理由不只是"省一次读盘"：**落盘失败时**也靠它
#: 保证"每 TTL 窗口最多自动跑 1 次"（§3.3 的降级要求）。
_MEMO: dict[str, dict[str, object]] = {}

#: 通道类失败的**冷却窗口**（键 → `time.time()`）。与 `_MEMO` 分开：记忆是"结论"，
#: 冷却只是"刚打过一次网、别马上再打"（见文件头"通道类失败 vs 裁决"）。
_CHANNEL_LAST_FAIL: dict[str, float] = {}


def reset_memo() -> None:
    """清空进程内记忆与冷却表（测试与 CLI 用；生产没有调用者）。"""
    _MEMO.clear()
    _CHANNEL_LAST_FAIL.clear()


class Sample:
    """一次 L1 合成样本：文件路径 + 两个**本次随机**的可区分事实。"""

    __slots__ = ("path", "number", "word", "size")

    def __init__(self, path: str, number: str, word: str, size: int) -> None:
        self.path = path
        self.number = number
        self.word = word
        self.size = size


class L1Result:
    """L1 判定结果：`ok` + 原因码 + 耗时（`elapsed_ms` 只进缓存与日志）。"""

    __slots__ = ("ok", "reason", "elapsed_ms")

    def __init__(self, ok: bool, reason: str, elapsed_ms: int = 0) -> None:
        self.ok = bool(ok)
        self.reason = reason
        self.elapsed_ms = int(elapsed_ms)

    def __repr__(self) -> str:  # pragma: no cover
        return f"L1Result(ok={self.ok}, reason={self.reason!r}, elapsed_ms={self.elapsed_ms})"


class ProbeOutcome:
    """`ensure_l1` 的返回：结果 + 来源（`cache` / `live`）+ 原因码。"""

    __slots__ = ("ok", "source", "result")

    def __init__(self, ok: bool, source: str, result: L1Result) -> None:
        self.ok = bool(ok)
        self.source = source  # "cache" | "live"
        self.result = result

    @property
    def reason(self) -> str:
        return self.result.reason


# --------------------------------------------------------------------------- #
# 键与判据（纯函数，好测）
# --------------------------------------------------------------------------- #
def build_key(provider_id: str, model: str, plugin_version: str) -> str:
    """`sha256("probe-v1|<provider_id>|<model>|<PLUGIN_VERSION>")` 的 64 hex（§3.3）。"""
    material = "|".join(
        (PROBE_SCHEMA_VERSION, str(provider_id), str(model), str(plugin_version))
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def judge(text: str, number: str, word: str) -> bool:
    """**两条都要**：回答里出现本次的数字与本次的单词（§3.2）。

    - 数字按字符串匹配（全角数字先归一）；样本是 4 位零填充 ⇒ 偶然命中的概率极低；
    - 单词大小写不敏感。
    """
    if not isinstance(text, str) or not text:
        return False
    normalized = text.translate(_FULLWIDTH_DIGITS)
    return (str(number) in normalized) and (str(word).lower() in normalized.lower())


# --------------------------------------------------------------------------- #
# 样本合成
# --------------------------------------------------------------------------- #
def pick_font() -> str:
    """取一个可用的等宽无衬线字体路径；都没有 ⇒ `""`（L1 记 `no_font`，不重试）。"""
    for candidate in FONT_CANDIDATES:
        try:
            if os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
                return candidate
        except OSError:
            continue
    return ""


def _sample_filter(font: str, number: str, word: str) -> str:
    """§3.1 的滤镜链（数字居中、单词右下角横向滚动）。"""
    return (
        "noise=alls=12:allf=t,"
        f"drawtext=fontfile={font}:text='{number}':fontsize=200:fontcolor=white:"
        "x=(w-tw)/2:y=(h-th)/2,"
        f"drawtext=fontfile={font}:text='{word}':fontsize=50:fontcolor=yellow:"
        "x='mod(t*200\\,w-tw)':y=h*0.86"
    )


def sample_argv(font: str, number: str, word: str, out_path: str) -> list[str]:
    """合成样本的 ffmpeg argv（`-an`：L1 只看画面，不带音轨）。"""
    return [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=c=0x0a1a2a:s={SAMPLE_WIDTH}x{SAMPLE_HEIGHT}:r={SAMPLE_RATE}:d={SAMPLE_SECONDS}",
        "-vf",
        _sample_filter(font, number, word),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        str(SAMPLE_CRF),
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-an",
        out_path,
    ]


async def build_sample(workdir: str) -> tuple[Sample | None, str]:
    """合成一份 L1 样本；返回 `(sample, reason)`，失败时 `sample is None`。

    失败**不阻塞主链路**（§3.1）：没有字体 / 没有 ffmpeg / 编码失败 ⇒ 调用方记
    `l1_ok=false, reason=…`（并缓存，防每轮重试），视频链路回退抽帧。
    """
    font = pick_font()
    if not font:
        return None, REASON_NO_FONT
    rng = random.SystemRandom()
    number = f"{rng.randrange(0, 10000):04d}"
    word = FIXED_WORDS[rng.randrange(0, len(FIXED_WORDS))]
    out_path = os.path.join(workdir, "probe_l1.mp4")
    argv = sample_argv(font, number, word, out_path)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError):
        return None, REASON_FFMPEG_MISSING
    try:
        _out, err = await asyncio.wait_for(
            proc.communicate(), timeout=SAMPLE_TIMEOUT_S
        )
    except (asyncio.TimeoutError, TimeoutError):
        with contextlib.suppress(Exception):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return None, REASON_BUILD_FAILED
    if proc.returncode != 0:
        LOG.warning(
            "media_analysis: L1 样本合成失败（rc=%s, %s）",
            proc.returncode,
            err.decode("utf-8", "replace")[:120],
        )
        return None, REASON_BUILD_FAILED
    size = os.path.getsize(out_path) if os.path.isfile(out_path) else 0
    if size <= 0:
        return None, REASON_BUILD_FAILED
    return Sample(out_path, number, word, size), REASON_OK


# --------------------------------------------------------------------------- #
# L1 调用
# --------------------------------------------------------------------------- #
async def run_l1(
    provider: object,
    *,
    workdir: str,
    timeout: float = L1_TIMEOUT_S,
) -> L1Result:
    """合成样本 → 原生形状调用 → **答对才算过**（§3.2）。"""
    sample, reason = await build_sample(workdir)
    if sample is None:
        return L1Result(False, reason)
    outcome = await video_native.call_native(
        provider,
        prompt=L1_QUESTION,
        video_path=sample.path,
        max_tokens=L1_MAX_TOKENS,
        timeout=timeout,
    )
    if outcome.note == video_native.NOTE_UNAVAILABLE:
        # 没有 client / client 形状变了 ⇒ 这次**根本没发出去**，对"能不能看见"零信息
        # ⇒ `REASON_ERROR`（通道类，见上方分类表；不落盘、冷却后重试）
        return L1Result(False, REASON_ERROR, outcome.elapsed_ms)
    if outcome.note == video_native.NOTE_FAILED:
        reason = REASON_TIMEOUT if outcome.detail.startswith("timeout") else REASON_ERROR
        return L1Result(False, reason, outcome.elapsed_ms)
    if not outcome.ok or not outcome.text:
        return L1Result(False, REASON_EMPTY, outcome.elapsed_ms)
    ok = judge(outcome.text, sample.number, sample.word)
    LOG.info(
        "media_analysis: L1 探测 %s（样本 %d B，耗时 %d ms）",
        "通过" if ok else "未通过",
        sample.size,
        outcome.elapsed_ms,
    )
    return L1Result(ok, REASON_OK if ok else REASON_MISMATCH, outcome.elapsed_ms)


# --------------------------------------------------------------------------- #
# probe/v1 缓存
# --------------------------------------------------------------------------- #
def _clamp_ttl(ttl_seconds: object) -> int:
    try:
        value = int(ttl_seconds)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(0, min(2592000, value))


class ProbeStore:
    """`data/plugin_data/<插件名>/probe/v1/` 的读写（**静默降级**：坏了就只记内存）。

    与结果缓存**互不影响**（§9.2）：探测 7 天、结果 1 天，各自可独立清空。
    """

    def __init__(self, *, plugin_name: str) -> None:
        self.plugin_name = str(plugin_name)
        self._broken = False
        self._warned = False

    @property
    def directory(self) -> str:
        root = cache_mod.plugin_data_dir(self.plugin_name)
        if not root:
            return ""
        return os.path.join(root, PROBE_DIR_NAME, PROBE_DIR_VERSION)

    def _path(self, key: str) -> str:
        return os.path.join(self.directory, f"{key}.json")

    async def read(self, key: str, ttl_seconds: int) -> dict[str, object] | None:
        """读一条**未过期**记录；过期/损坏/不存在/禁用 ⇒ `None`（懒删，不后台清理）。"""
        ttl = _clamp_ttl(ttl_seconds)
        if ttl <= 0 or self._broken:
            return None
        path = self._path(key)
        try:
            raw = await asyncio.to_thread(_read_bytes, path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            self._degrade(exc)
            return None
        entry = _decode_entry(raw)
        if entry is None:
            return None
        updated_at = entry.get("updated_at")
        if not isinstance(updated_at, (int, float)):
            return None
        age = time.time() - float(updated_at)
        if age < 0 or age > ttl:
            return None
        # 评审 F3：按结论类型再收一次（本地环境类 1 小时、通道类 0）
        if age > _entry_ttl(entry, ttl):
            return None
        return entry

    async def write(self, key: str, entry: dict[str, object]) -> None:
        """原子落盘一条记录；任何失败 ⇒ 本进程内不再尝试写（熔断 + 一条 WARNING）。"""
        if self._broken:
            return
        path = self._path(key)
        try:
            data = json.dumps(entry, ensure_ascii=False).encode("utf-8")
            await asyncio.to_thread(_write_atomic, path, data)
        except (OSError, TypeError, ValueError) as exc:
            self._degrade(exc)

    async def clear(self) -> None:
        """整目录清空 + **连进程内记忆一起清**（评审 F2）。

        不清记忆的话，「清缓存」对运行中的插件等于没做：结论还在 `_MEMO` 里，
        下一次调用仍会 1 ms 命中一条已经不存在的条目。
        """
        reset_memo()
        directory = self.directory
        if not directory:
            return
        try:
            await asyncio.to_thread(_remove_tree, directory)
        except OSError as exc:
            self._degrade(exc)

    def _degrade(self, exc: BaseException) -> None:
        self._broken = True
        if self._warned:
            return
        self._warned = True
        LOG.warning(
            "media_analysis: probe 缓存不可用（%s），本进程内改为只记内存（主链路照常）",
            type(exc).__name__,
        )


# --------------------------------------------------------------------------- #
# 编排
# --------------------------------------------------------------------------- #
async def ensure_l1(
    provider: object,
    *,
    provider_id: str,
    model: str,
    plugin_version: str,
    ttl_seconds: int,
    workdir: str,
    store: ProbeStore | None = None,
    timeout: float = L1_TIMEOUT_S,
) -> ProbeOutcome:
    """取或跑一次 L1：**每 `provider_id + model + PLUGIN_VERSION` 每 TTL 窗口最多 1 次**。

    调用方（`analyze`）负责其余成本保护：`probe_enabled=false` / `ttl=0` /
    `video_provider_id` 为空 / 剩余预算不足 —— 那四条**不该进到这里**（§3.4）。
    """
    key = build_key(provider_id, model, plugin_version)
    store = store or ProbeStore(plugin_name=PROBE_STORE_PLUGIN_NAME)

    # 1) 进程内记忆：只有**裁决**能直接复用（通道类失败不是结论，见文件头）；
    #    且必须过两道时效（评审 F2）：条目自身的 TTL（F3 分流）与 `MEMO_MAX_AGE_S`
    memo_entry = _MEMO.get(key)
    if _memo_entry_is_fresh(memo_entry, ttl_seconds):
        memo = _outcome_from_entry(memo_entry)
        if memo is not None:
            return memo
    elif memo_entry is not None:
        _MEMO.pop(key, None)  # 过期/失效：丢掉，回磁盘或重测

    # 2) 磁盘缓存：**通道类条目视同 miss**并重新实测
    #    这一条是 F1 的正面修复：任何外部写入（CLI / 旧版本 / 网络抖动）留下的
    #    `l1_ok=false + reason=http_403` 都不许变成"这个模型看不见视频"的 7 天判决。
    cached = await store.read(key, ttl_seconds)
    cached_outcome = _outcome_from_entry(cached)
    if cached_outcome is not None:
        _MEMO[key] = dict(cached)  # 记进内存（带 `updated_at`，寿命受 MEMO_MAX_AGE_S 约束）
        return cached_outcome
    if cached is not None:
        # 刻意**不删**这条条目：这次实测若成功会直接覆盖它（`store.write`）；若又失败，
        # 说明通道还没好，留着也只是"再被忽略一次"，比反复删写更简单。
        LOG.info(
            "media_analysis: probe 缓存里是通道类失败（reason=%s），不是裁决，"
            "视同 miss 重新实测",
            _reason_of(cached),
        )

    # 3) 冷却窗口：刚打过一次网且只得到通道类失败 ⇒ 别马上再打（否则每次调用都白等一次超时）
    last_fail = _CHANNEL_LAST_FAIL.get(key)
    if last_fail is not None and time.time() - last_fail < CHANNEL_RETRY_SECONDS:
        entry = _MEMO.get(key) or {}
        reason = _reason_of(entry) or REASON_CHANNEL_ERROR
        return ProbeOutcome(
            False,
            "cooldown",
            L1Result(False, reason, _elapsed_of(entry)),
        )

    result = await run_l1(provider, workdir=workdir, timeout=timeout)
    now = int(time.time())
    entry: dict[str, object] = {
        "v": PROBE_SCHEMA_VERSION,
        "provider_id": str(provider_id),
        "model": str(model),
        "plugin_version": str(plugin_version),
        "l1": {
            "ok": result.ok,
            "reason": result.reason,
            "at": now,
            "elapsed_ms": result.elapsed_ms,
        },
        "latency": None,
        "updated_at": now,
    }
    _MEMO[key] = entry  # 先记内存：落盘失败也不会把成本放大（§3.3）
    if result.ok or is_verdict_reason(result.reason):
        # 4) 写入侧：**只有裁决才落盘**。通道类失败绝不写 `l1_ok=false`（F1 的根因）
        await store.write(key, entry)
    else:
        _CHANNEL_LAST_FAIL[key] = time.time()
        LOG.info(
            "media_analysis: L1 因通道类失败未完成（reason=%s），本次不写入 probe 缓存",
            result.reason,
        )
    return ProbeOutcome(result.ok, "live", result)


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #
def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def _write_atomic(path: str, data: bytes) -> None:
    """同目录临时文件 + `os.replace`（跨设备 EXDEV 就失败，绝不先写 /tmp）。"""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".w-", suffix=".json", dir=directory)
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


def _remove_tree(directory: str) -> None:
    if os.path.isdir(directory):
        shutil.rmtree(directory, ignore_errors=True)


def cache_ttl_for(ok: bool, reason: object, configured_ttl: int) -> int:
    """这条结论在磁盘/内存里**最多**能活多久（秒）。评审 F3 的冻结分流表：

    | 结论 | TTL |
    | --- | --- |
    | `ok=true`（看见过） | 配置值（默认 7 天） |
    | 判据不过（`mismatch` / `empty`） | 配置值（真裁决，值得记住） |
    | 本地环境类（`no_font` / `ffmpeg_missing` / `build_failed`） | **min(配置值, 1 小时)** |
    | 通道类（4xx/5xx/timeout/dns/error） | **0**（不该在缓存里；写入侧已经拦了） |

    `0` 的含义是"当作过期"（读侧视同 miss），不是"永久"。
    """
    ttl = _clamp_ttl(configured_ttl)
    if ok or str(reason or "") in VERDICT_FAILURE_REASONS:
        return ttl
    if is_channel_failure_reason(reason):
        return 0
    return min(ttl, LOCAL_FAILURE_TTL_S)


def _memo_entry_is_fresh(entry: object, configured_ttl: int) -> bool:
    """进程内记忆里的这条还算不算数（评审 F2）。

    两道闸：① 条目自身 TTL（F3 分流：裁决 7 天 / 本地环境 1 小时 / 通道类 0）；
    ② `MEMO_MAX_AGE_S` —— 再长也不超过 5 分钟，保证 `--clear` / `rm -rf probe/`
    对运行中的进程**最多 5 分钟后生效**（否则删了缓存也白删）。

    `ttl_seconds=0`（探测关闭）时批 `MEMO_MAX_AGE_S`：此时 `analyze` 根本不会调到这里，
    留这条只是让"直接调用方"的重复调用不至于反复打网（成本保护优先）。
    """
    if not isinstance(entry, dict):
        return False
    updated_at = entry.get("updated_at")
    if not isinstance(updated_at, (int, float)):
        return False
    age = time.time() - float(updated_at)
    if age < 0:
        return False
    if _clamp_ttl(configured_ttl) <= 0:
        return age <= MEMO_MAX_AGE_S
    return age <= min(MEMO_MAX_AGE_S, _entry_ttl(entry, configured_ttl))


def _entry_ttl(entry: object, configured_ttl: int) -> int:
    """条目 → 有效 TTL（`ok` 与 `reason` 都从 `l1` 里取）。"""
    l1 = entry.get("l1") if isinstance(entry, dict) else None
    if not isinstance(l1, dict):
        return 0
    return cache_ttl_for(bool(l1.get("ok")), l1.get("reason"), configured_ttl)


def _reason_of(entry: object) -> str:
    """条目里的 `l1.reason`（取不到回 `""`）。"""
    l1 = entry.get("l1") if isinstance(entry, dict) else None
    if isinstance(l1, dict):
        return str(l1.get("reason") or "")
    return ""


def _elapsed_of(entry: object) -> int:
    l1 = entry.get("l1") if isinstance(entry, dict) else None
    if isinstance(l1, dict):
        try:
            return int(l1.get("elapsed_ms") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _outcome_from_entry(entry: object) -> ProbeOutcome | None:
    """把一条缓存条目变成结局；**通道类失败返回 `None`（＝视同 miss）**。

    `ok=true` 的条目永远可用（"能看见"是正面证据，通道抖动不会伪造它）。
    """
    if not isinstance(entry, dict):
        return None
    l1 = entry.get("l1")
    if not isinstance(l1, dict):
        return None
    ok = bool(l1.get("ok"))
    reason = str(l1.get("reason") or REASON_ERROR)
    if not ok and not is_verdict_reason(reason):
        return None
    return ProbeOutcome(ok, "cache", L1Result(ok, reason, _elapsed_of(entry)))


def _decode_entry(raw: bytes) -> dict[str, object] | None:
    """解析条目；形状不符算损坏（`None`）。"""
    try:
        entry = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(entry, dict):
        return None
    if entry.get("v") != PROBE_SCHEMA_VERSION:
        return None
    if not isinstance(entry.get("l1"), dict):
        return None
    return entry
