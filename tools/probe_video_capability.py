#!/usr/bin/env python3
"""视频能力 / 延迟校准工具（SPEC-v0.3 §3.5）。**运维侧独立脚本**，不 import 插件模块。

它回答两个问题，且**只**回答这两个：

1. **能看见吗**（L1）：用一段 4 s / 1280x720 / ~150 KB 的**合成**样片（画面正中一个 4 位白色
   数字、右下角一个黄色英文单词），问"数字=?单词=?"；**两条都命中才算过**。
   —— 200 不等于看见：实测有模型在 `max_tokens=64` 时返回 200 但正文为空（全被推理吃掉）。
2. **多大的延迟能接受**（延迟校准）：按给定的体积档各跑 N 次，输出 p50/p90，并给出
   `suggested_max_direct_bytes` = **p50 ≤ 45 s 的最大档**（100 s 全局预算，留 ≥55 s 给后续步骤）。

它**不**测"体积上限"：t8 实测两条通道到 47.2 MiB / 36.5 MiB 都 200（0 次拒绝），
所谓"体积上限"不是事实；真正先撞的是延迟（`MEASURE-v0.3.md` §3/§4/§6，SPEC §16.2-1）。

用法：

    # L1 + 单档延迟（默认 1 MiB × 3 次取 p50）
    python3 tools/probe_video_capability.py --provider <provider_id>
    # 多档校准（每档 3 次）→ 输出"建议 max_direct_bytes"
    python3 tools/probe_video_capability.py --provider <id> --sizes 1,4,8 --repeat 3
    # 不造样本，直接量一个现成文件
    python3 tools/probe_video_capability.py --provider <id> --file /tmp/clip.mp4
    # 只看/清探测缓存（**不发网络请求**）
    python3 tools/probe_video_capability.py --provider <id> --show
    python3 tools/probe_video_capability.py --provider <id> --clear

纪律（照 t8）：
- 只**读** `data/cmd_config.json`（默认 `<root>/data/cmd_config.json`，root 取 `--root` 或
  `$ASTRBOT_ROOT`），**不写**它；不打印 key、不打印 base64（只打字节数与耗时）；
- 只写 `data/plugin_data/<插件>/probe/v1/` 里的 `l1` / `latency` 字段（插件会复用它，
  所以别在高峰乱跑）；
- 合成样本只落 `/tmp`（`--workdir` 可指定），退出时删。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

PLUGIN_NAME = "astrbot_plugin_media_analysis"
PROBE_SCHEMA_VERSION = "probe-v1"
PROBE_DIR_VERSION = "v1"
PLUGIN_VERSION_FALLBACK = "0.3.0"

ADAPTER_TYPE = "openai_chat_completion"
L1_QUESTION = (
    "视频画面正中有一个很大的白色数字，右下角有一个黄色英文单词。"
    "只回答这两项：数字=?,单词=?。不要解释。"
)
FIXED_WORDS = ("MOVING", "GREEN", "CAMERA", "WINDOW", "PAPER", "ORANGE", "SILVER", "GUITAR")
FONT_CANDIDATES = ("/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",)
L1_SAMPLE_SECONDS = 4
L1_SAMPLE_WIDTH = 1280
L1_SAMPLE_HEIGHT = 720
P50_BUDGET_MS = 45000  # §3.5：p50 ≤ 45 s 的最大档
MIB = 1024 * 1024

WARNING = (
    "一次调用可能数分钟（1 MiB≈10–32 s、8 MiB≈43–75 s），不要在生产高峰运行；"
    "本工具只校准延迟，不测体积上限（已实测不存在）。"
)


# --------------------------------------------------------------------------- #
# 托管方配置（只读，不打印 key）
# --------------------------------------------------------------------------- #
class Provider:
    __slots__ = ("provider_id", "model", "api_base", "api_key", "headers", "adapter")

    def __init__(self, provider_id, model, api_base, api_key, headers, adapter):
        self.provider_id = provider_id
        self.model = model
        self.api_base = api_base
        self.api_key = api_key
        self.headers = headers
        self.adapter = adapter


def load_provider(root: str, provider_id: str) -> Provider:
    path = os.path.join(root, "data", "cmd_config.json")
    with open(path, "rb") as handle:
        config = json.loads(handle.read().decode("utf-8-sig"))
    entry = None
    for item in config.get("provider") or []:
        if isinstance(item, dict) and item.get("id") == provider_id:
            entry = item
            break
    if entry is None:
        raise SystemExit(f"error: provider 不存在：{provider_id}")

    source = entry
    source_id = entry.get("provider_source_id")
    if source_id:
        for item in config.get("provider_sources") or []:
            if isinstance(item, dict) and item.get("id") == source_id:
                source = {**item, **entry}
                source["id"] = entry.get("id")
                break

    keys = source.get("key") or []
    if isinstance(keys, str):
        keys = [keys]
    key = ""
    for candidate in keys:
        if isinstance(candidate, str) and candidate:
            key = os.environ.get(candidate[1:], "") if candidate.startswith("$") else candidate
            if key:
                break

    api_base = str(source.get("api_base") or "").rstrip("/")
    model = str(entry.get("model") or source.get("model") or "unknown")
    headers = dict(source.get("custom_headers") or {})
    adapter = str(entry.get("type") or source.get("type") or "")
    return Provider(provider_id, model, api_base, key, headers, adapter)


# --------------------------------------------------------------------------- #
# 样本
# --------------------------------------------------------------------------- #
def _pick_font() -> str:
    for candidate in FONT_CANDIDATES:
        if os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
            return candidate
    return ""


def _run_ffmpeg(argv: list[str], timeout: float = 120.0) -> bool:
    try:
        proc = subprocess.run(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=timeout
        )
    except (FileNotFoundError, OSError):
        print("error: ffmpeg 不可用（L1 样本无法合成）", file=sys.stderr)
        return False
    except subprocess.TimeoutExpired:
        print("error: ffmpeg 超时", file=sys.stderr)
        return False
    if proc.returncode != 0:
        print(
            f"error: ffmpeg rc={proc.returncode} "
            f"{proc.stderr.decode('utf-8', 'replace')[:160]}",
            file=sys.stderr,
        )
        return False
    return True


def build_l1_sample(workdir: str) -> tuple[str, str, str]:
    """合成 L1 样片，返回 `(path, number, word)`；失败抛 `SystemExit`。"""
    font = _pick_font()
    if not font:
        raise SystemExit("error: 找不到可用字体，L1 无法合成（记 no_font，不可测）")
    rng = random.SystemRandom()
    number = f"{rng.randrange(0, 10000):04d}"
    word = FIXED_WORDS[rng.randrange(0, len(FIXED_WORDS))]
    out = os.path.join(workdir, "l1.mp4")
    vf = (
        "noise=alls=12:allf=t,"
        f"drawtext=fontfile={font}:text='{number}':fontsize=200:fontcolor=white:"
        "x=(w-tw)/2:y=(h-th)/2,"
        f"drawtext=fontfile={font}:text='{word}':fontsize=50:fontcolor=yellow:"
        "x='mod(t*200\\,w-tw)':y=h*0.86"
    )
    argv = [
        "ffmpeg", "-nostdin", "-y", "-v", "error",
        "-f", "lavfi",
        "-i", f"color=c=0x0a1a2a:s={L1_SAMPLE_WIDTH}x{L1_SAMPLE_HEIGHT}:r=12:d={L1_SAMPLE_SECONDS}",
        "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", out,
    ]
    if not _run_ffmpeg(argv):
        raise SystemExit("error: L1 样本合成失败")
    return out, number, word


def build_size_sample(workdir: str, target_bytes: int, *, seconds: int = 6) -> str:
    """按目标字节数合成一段 1080p 噪点样片（只用码率逼近目标，实际体积会略有偏差）。"""
    kbps = max(64, int(target_bytes * 8 / seconds / 1000) - 64)
    out = os.path.join(workdir, f"size_{target_bytes}.mp4")
    argv = [
        "ffmpeg", "-nostdin", "-y", "-v", "error",
        "-f", "lavfi",
        "-i", f"color=c=0x102030:s=1920x1080:r=12:d={seconds}",
        "-vf", "noise=alls=40:allf=t",
        "-c:v", "libx264", "-preset", "veryfast",
        "-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", out,
    ]
    if not _run_ffmpeg(argv, timeout=300.0):
        raise SystemExit("error: 体积档样本合成失败")
    return out


# --------------------------------------------------------------------------- #
# 一次调用（形状与插件一致：`video_url` + 内联 base64）
# --------------------------------------------------------------------------- #
def call_video(provider: Provider, path: str, prompt: str, *, max_tokens: int, timeout: float):
    """发一次原生视频请求，返回 `(ok, text, elapsed_ms, status_or_err)`；绝不打印密钥/base64。"""
    with open(path, "rb") as handle:
        raw = handle.read()
    data_url = "data:video/mp4;base64," + base64.b64encode(raw).decode("ascii")
    del raw
    body = {
        "model": provider.model,
        "max_tokens": max(1024, int(max_tokens)),
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "video_url", "video_url": {"url": data_url}},
                ],
            }
        ],
    }
    payload = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    headers.update({str(k): str(v) for k, v in provider.headers.items()})
    if provider.api_key:
        headers.setdefault("Authorization", f"Bearer {provider.api_key}")
    url = provider.api_base + "/chat/completions"
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw_body = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        return False, "", int((time.monotonic() - started) * 1000), f"http {exc.code}"
    except Exception as exc:  # noqa: BLE001 —— 网络侧什么都可能抛，工具只报类型
        return False, "", int((time.monotonic() - started) * 1000), type(exc).__name__
    elapsed_ms = int((time.monotonic() - started) * 1000)
    try:
        decoded = json.loads(raw_body.decode("utf-8", "replace"))
    except ValueError:
        return False, "", elapsed_ms, f"http {status} bad json"
    text = ""
    try:
        message = decoded["choices"][0]["message"]
        content = message.get("content")
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            text = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            ).strip()
    except (KeyError, IndexError, TypeError):
        text = ""
    return (status < 400 and bool(text)), text, elapsed_ms, f"http {status}"


# --------------------------------------------------------------------------- #
# probe 缓存（与插件同公式）
# --------------------------------------------------------------------------- #
def probe_dir(root: str) -> str:
    return os.path.join(root, "data", "plugin_data", PLUGIN_NAME, "probe", PROBE_DIR_VERSION)


def probe_path(root: str, provider_id: str, model: str, plugin_version: str) -> str:
    material = "|".join((PROBE_SCHEMA_VERSION, provider_id, model, plugin_version))
    key = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return os.path.join(probe_dir(root), f"{key}.json")


def read_probe(root: str, path: str) -> dict:
    try:
        with open(path, "rb") as handle:
            entry = json.loads(handle.read().decode("utf-8"))
    except (OSError, ValueError):
        return {}
    return entry if isinstance(entry, dict) else {}


def write_probe(root: str, path: str, entry: dict) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False).encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _plugin_version(root: str) -> str:
    """从 `<root>/data/plugins/<插件>/metadata.yaml` 读版本；读不到用兜底常量。"""
    path = os.path.join(root, "data", "plugins", PLUGIN_NAME, "metadata.yaml")
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("version:"):
                    value = line.split(":", 1)[1].strip().strip("\"'")
                    return value[1:] if value[:1] in {"v", "V"} else value
    except OSError:
        pass
    return PLUGIN_VERSION_FALLBACK


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def judge(text: str, number: str, word: str) -> bool:
    table = str.maketrans("０１２３４５６７８９", "0123456789")
    normalized = (text or "").translate(table)
    return (str(number) in normalized) and (str(word).lower() in normalized.lower())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        add_help=True,
        description="视频能力/延迟校准（只校准延迟，不测体积上限；不打印密钥/base64）",
    )
    parser.add_argument("--provider", required=True, help="provider id（见后台模型列表）")
    parser.add_argument("--root", default="", help="AstrBot 根目录（未给则取 $ASTRBOT_ROOT）")
    parser.add_argument("--sizes", default="1", help="体积档（MiB，逗号分隔），默认 1")
    parser.add_argument("--repeat", type=int, default=3, help="每档次数（默认 3，取 p50/p90）")
    parser.add_argument("--file", default="", help="直接量一个现成 mp4（不合成体积档）")
    parser.add_argument("--show", action="store_true", help="只打印 probe 缓存（不发请求）")
    parser.add_argument("--clear", action="store_true", help="删掉该 provider 的 probe 缓存")
    parser.add_argument("--max-tokens", type=int, default=1024, help="默认 1024（硬下限 1024）")
    parser.add_argument("--timeout", type=float, default=300.0, help="单次请求超时（秒）")
    parser.add_argument("--workdir", default="", help="合成样本目录（默认 /tmp 下随机目录）")
    parser.add_argument("--skip-l1", action="store_true", help="跳过 L1（只校准延迟）")
    args = parser.parse_args(argv)

    print(WARNING)

    root = args.root or os.environ.get("ASTRBOT_ROOT") or ""
    if not root:
        print("error: 缺少 --root 且未设置 ASTRBOT_ROOT（不猜生产路径）", file=sys.stderr)
        return 1
    root = os.path.abspath(os.path.expanduser(root))

    try:
        provider = load_provider(root, args.provider)
    except (OSError, ValueError) as exc:
        print(f"error: 读配置失败（{type(exc).__name__}）", file=sys.stderr)
        return 1

    version = _plugin_version(root)
    cache_path = probe_path(root, provider.provider_id, provider.model, version)
    entry = read_probe(root, cache_path)
    entry.setdefault("v", PROBE_SCHEMA_VERSION)
    entry.setdefault("provider_id", provider.provider_id)
    entry.setdefault("model", provider.model)
    entry.setdefault("plugin_version", version)
    entry.setdefault("l1", None)
    entry.setdefault("latency", None)

    print(f"provider={provider.provider_id} model={provider.model} adapter={provider.adapter or '-'}")

    if args.show:
        l1 = entry.get("l1") or {}
        latency = entry.get("latency") or {}
        print(
            f"probe_cache={cache_path}\n"
            f"l1={'ok' if l1.get('ok') else 'failed'} reason={l1.get('reason', '-')} "
            f"at={l1.get('at', '-')}\n"
            f"latency_p50_ms={latency.get('p50_ms', '-')} size_bytes={latency.get('size_bytes', '-')}"
        )
        return 0

    if args.clear:
        removed = 0
        for path in (cache_path,):
            try:
                os.unlink(path)
                removed += 1
            except OSError:
                pass
        print(f"probe_cache_cleared={removed} path={cache_path}")
        return 0

    if provider.adapter and provider.adapter != ADAPTER_TYPE:
        print(
            f"error: 适配器类型是 {provider.adapter}，不是 {ADAPTER_TYPE}；"
            "插件的原生视频通道只支持后者",
            file=sys.stderr,
        )
        return 1
    if not provider.api_base:
        print("error: 该 provider 没有 api_base（provider_source 里未配置）", file=sys.stderr)
        return 1

    workdir = args.workdir or tempfile.mkdtemp(prefix="probe_video_")
    created = not args.workdir
    try:
        # ---- L1 ----
        if not args.skip_l1:
            l1_ok = None
            if entry.get("l1") and entry["l1"].get("ok"):
                l1_ok = True
                print("l1=cached_ok")
            if l1_ok is None:
                sample, number, word = build_l1_sample(workdir)
                ok, text, elapsed_ms, status = call_video(
                    provider,
                    sample,
                    L1_QUESTION,
                    max_tokens=args.max_tokens,
                    timeout=args.timeout,
                )
                hit = ok and judge(text, number, word)
                entry["l1"] = {
                    "ok": bool(hit),
                    "reason": "ok" if hit else ("empty" if ok else "error"),
                    "at": int(time.time()),
                    "elapsed_ms": elapsed_ms,
                }
                print(
                    f"l1={'ok' if hit else 'failed'} sample_bytes={os.path.getsize(sample)} "
                    f"elapsed_ms={elapsed_ms} status={status} answer_chars={len(text)}"
                )

        # ---- 延迟阶梯 ----
        if args.file:
            sizes = [("file", os.path.abspath(os.path.expanduser(args.file)))]
        else:
            sizes = []
            for token in str(args.sizes).split(","):
                token = token.strip()
                if not token:
                    continue
                mib = int(float(token) * MIB)
                sizes.append((f"{token}MiB", build_size_sample(workdir, mib)))

        best = 0
        measured = []
        for label, path in sizes:
            size_bytes = os.path.getsize(path)
            samples: list[int] = []
            accepted = False
            for index in range(max(1, args.repeat)):
                ok, text, elapsed_ms, status = call_video(
                    provider,
                    path,
                    L1_QUESTION if not args.skip_l1 else "用一句话描述这段视频。",
                    max_tokens=args.max_tokens,
                    timeout=args.timeout,
                )
                samples.append(elapsed_ms)
                accepted = accepted or ok
                print(
                    f"size={label} run={index + 1}/{max(1, args.repeat)} bytes={size_bytes} "
                    f"elapsed_ms={elapsed_ms} accepted={'yes' if ok else 'no'} "
                    f"chars={len(text)} status={status}"
                )
            p50 = int(statistics.median(samples))
            p90 = int(sorted(samples)[max(0, int(len(samples) * 0.9) - 1)])
            measured.append((label, size_bytes, p50, p90, accepted))
            if accepted and p50 <= P50_BUDGET_MS:
                best = max(best, size_bytes)
            print(f"size={label} p50={p50}ms p90={p90}ms accepted={'yes' if accepted else 'no'}")

        if measured:
            accepted_rows = [row for row in measured if row[4] and row[2] <= P50_BUDGET_MS]
            if accepted_rows:
                last = max(accepted_rows, key=lambda row: row[1])
                entry["latency"] = {
                    "p50_ms": last[2],
                    "size_bytes": last[1],
                    "at": int(time.time()),
                }
        entry["updated_at"] = int(time.time())
        write_probe(root, cache_path, entry)
        print(
            f"suggested_max_direct_bytes={best} "
            f"rule=p50<={P50_BUDGET_MS}ms probe_cache={cache_path}"
        )
        return 0
    finally:
        if created:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
