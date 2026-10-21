# SEAMS — 让测试不碰网络、不碰真实 provider 的注入点清单

面向 `builder`。每一行都能指到 `SPEC.md` 的**冻结签名**：只要那一条按 SPEC 写，测试就能
零网络、零真实 provider、零真实群媒体地覆盖它；反过来，写成"import 期绑死 / 全局单例 /
默认参数捕获常量"的形态，测试就只能靠真机跑，回归价值为零。

配套件（都在 `tests/`，**不含插件源码**）：

| 文件 | 作用 |
|---|---|
| `conftest.py` | 真核心优先的后端判定、`ASTRBOT_ROOT` 隔离、`sys.path` 注入、合成夹具 fixture |
| `_fakes.py` | `FakeEvent` / `TrackingFakeEvent` / `FakeChatProvider` / `FakeSTTProvider` / `FakeContext` / `RecordingLogger` |
| `_astrbot_stub.py` | 只在**真核心不可用**时兜底的最小 astrbot 桩 |
| `make_fixtures.py` | 合成夹具生成器（lavfi `sine` + `testsrc`，幂等、逐字节可复现） |
| `test_fixtures.py` | 地基自检（28 条；**不 import 插件模块**） |
| `SEAMS.md` | 本文件 |

跑法（与 `AGENTS.md` §3 一致）：

```bash
cd /root/workspace/projects/astrbot_plugin_media_analysis && \
  PYTHONDONTWRITEBYTECODE=1 /root/AstrBot/.venv/bin/python -m pytest tests/ -q -p no:cacheprovider
```

---

## 0. 一句话口径

**测试要的是"能被替身顶掉的名字"，不是"能被 monkeypatch 的私有变量"。**
SPEC 已经把 `analyze.analyze` 的 4 个协作者（`context` / `event` / `settings` / `cache`）设计成
**构造注入**，把全部 IO 收在 `media.*` 与 `providers.*` 的**模块级 async 函数**里
（§3.1 的 import DAG 就是为这个服务的）。只要不把它们在 import 期绑死，下面 17 条接缝全部可用。

---

## 1. 接缝清单

图例：**✅ 已由 SPEC 冻结** ＝ 照抄签名即可；**⚠️ 形态要求** ＝ 签名之外还必须怎么写才可测。

### S1 `analyze.analyze` 的四个协作者（构造注入）
- 签名：`async def analyze(*, context, event, target: str, focus: str, include_frames: bool, settings: Settings, cache: ResultCache) -> dict[str, object]`（`SPEC.md:483-487`）
- 用法：`await analyze.analyze(context=FakeContext(...), event=make_event(...), target="current", focus="", include_frames=False, settings=Settings(max_media_seconds=60, max_frames=6), cache=ResultCache(maxsize=2))`
- ⚠️ 形态要求：`settings` / `cache` **只能**从形参来；不得在函数里读 `os.environ`、读文件、或引用模块级单例。
- 覆盖的用例：信封 9 键、`MEDIA_TOO_LONG`、两处分界、缓存命中、预算降级（`test_analyze.py`）。

### S2 `providers.resolve_*` 必须是**模块属性**调用
- 签名：`async def resolve_stt_provider(context, provider_id: str, umo: str)` / `resolve_chat_provider(context, provider_id: str, umo: str, *, require_image) -> Provider` / `resolve_vision_provider(...)`（`SPEC.md:438-443`）
- 用法：`monkeypatch.setattr(providers, "resolve_stt_provider", fake_resolver)` —— 因为 `analyze.py` 写的是 `from . import providers` 后 `providers.resolve_stt_provider(...)`
- ⚠️ 形态要求：**禁止**在 `analyze.py` 顶部写 `from .providers import resolve_stt_provider`。那样名字在 import 期绑进 `analyze` 的名字空间，`monkeypatch.setattr(providers, …)` 不再生效，测试只能连真 provider 跑（=不可测）。
- 覆盖的用例：阶梯 ①–⑤、三种能力不匹配（`test_providers.py`）。

### S3 `media.*` 同一模式（全部 IO 的唯一出口）
- 签名：`find_media` / `resolve_media_path` / `probe_duration_s` / `probe_streams` / `extract_wav` / `extract_frames` / `content_digest`（`SPEC.md:453-460`）
- 用法：`monkeypatch.setattr(media, "probe_duration_s", lambda _p: 70.0)` —— 用来**不造 70 s 文件**也能驱动 `MEDIA_TOO_LONG`；`monkeypatch.setattr(media, "extract_wav", raiser)` 用来驱动 `AUDIO_EXTRACT_FAILED`。
- ⚠️ 形态要求：同 S2（`from . import media` + 属性调用，`SPEC.md:332` 的 DAG 已经这么规定）。
- 覆盖的用例：`test_media.py` 的三种 `find_media` 结果 + `resolve_media_path` 两条路。

### S4 核心 `get_media_duration` 的**模块级导入点**
- 事实：它是 `async def get_media_duration(file_path: str) -> int | None`，**返回毫秒**、失败 `None`；**不在** `astrbot.api.*` 再导出面里 ⇒ 只能 `from astrbot.core.utils.media_utils import get_media_duration`（`SPEC.md:245-247`，全插件唯一的 core 内部导入）
- 用法：`monkeypatch.setattr(media, "get_media_duration", fake)`，fake 返回 `5000` / `None` 两种
- ⚠️ 形态要求：这个 import 写在 `media.py` **模块级**（SPEC 已如此），测试 patch 的是 `media.get_media_duration`。若为了"少耦合"改成函数内 import，也必须保留同名模块级符号，否则这条接缝没了。
- 覆盖的用例：`probe_duration_s` 的 2 位小数取整、`PROBE_FAILED`（返回 `None`）。

### S5 `ffmpeg` / `ffprobe` 的 argv 形态（用 PATH 注入，不改代码）
- 冻结命令行：抽音轨 / 抽帧的 argv 列表（`SPEC.md:875-884`），**不经 shell**
- 用法：测试造一个假二进制目录并 `monkeypatch.setenv("PATH", fake_dir + os.pathsep + old)`；假 `ffmpeg` 用脚本按 rc 返回不同码即可驱动 `AUDIO_EXTRACT_FAILED` / 单帧 `frame_extract_failed:<at_s>`
- ⚠️ 形态要求：`argv[0]` 必须是**裸** `"ffmpeg"` / `"ffprobe"`（SPEC 如此冻结），这样 PATH 注入才生效。**不要**写成绝对路径 `/usr/bin/ffmpeg`，也**不要**走 `shell=True`。
- 可选增强（不加也行）：加 `FFMPEG_BIN = "ffmpeg"` / `FFPROBE_BIN = "ffprobe"` 两个模块常量并把 `argv[0]` 取自它们，默认值必须与 SPEC 逐字相同 —— 这样测试既能 patch 常量、也能走 PATH。
- 覆盖的用例：抽帧网格、`frame_extract_failed` 单帧降级、`AUDIO_EXTRACT_FAILED`。

### S6 能力判定的唯一读点：`provider.provider_config["modalities"]`
- 签名：`def supports_modality(provider: object, modality: str) -> bool`（`SPEC.md:438`，实现口径 `SPEC.md:178-186`）
- 用法：`FakeChatProvider(modalities=None)`（无键 ⇒ 严格口径判**不支持**）、`modalities=[]`（迁移遗留空列表 ⇒ **支持**）、`modalities=["text"]`（不支持图像）
- ⚠️ 形态要求：判定只读 `provider.provider_config`（**不要**读 `provider.modalities` 之类的便捷属性，假 provider 不会有）。
- 覆盖的用例：`PROVIDER_CAPABILITY_MISMATCH` 的三条触发面（`test_providers.py`）。

### S7 `settings.Settings` 是 frozen dataclass，`ConfigView` 可独立构造
- 签名：`class Settings`（4 字段 + 默认值，`SPEC.md:371-376`）、`class ConfigView(raw)`（`SPEC.md:378-384`）、`KEY_MAP`（`SPEC.md:359-364`）
- 用法：测试直接 `Settings(max_media_seconds=60, max_frames=6)`，**不需要**任何配置文件；`ConfigView` 的钳位用 `{"limits": {...}}` 普通 dict 驱动。
- ⚠️ 形态要求：不提供第二个 `load_*` 入口（SPEC 已如此要求，`SPEC.md:384`）。

### S8 `cache.ResultCache` 实例注入 + 静态键函数
- 签名：`ResultCache(maxsize: int = 32)` / `get` / `put` / `clear` / `__len__` / `@staticmethod make_key(digest, *, kind, focus, include_frames, max_frames)`（`SPEC.md:462-471`）
- 用法：`ResultCache(maxsize=2)` 直接测淘汰；`make_key(...)` 纯函数直接测键稳定性（同 digest 不同 `focus` 不同键）。
- ⚠️ 形态要求：`analyze` 只能用传进来的那个实例（`SPEC.md:807-808` 的 hit / `:840` 的 put），不得新建。

### S9 预算常量必须**调用期**读取
- 常量：`DEADLINE_S` / `PROBE_TIMEOUT_S` / `STT_TIMEOUT_S` / `FRAME_EXTRACT_TIMEOUT_S` / `FRAME_CALL_TIMEOUT_S` / `SUMMARY_TIMEOUT_S`（`SPEC.md:474-479`）
- 用法：`monkeypatch.setattr(analyze, "SUMMARY_TIMEOUT_S", 999.0)` ⇒ 强制 `summary_skipped_budget`；`monkeypatch.setattr(analyze, "DEADLINE_S", -1.0)` ⇒ 强制"预算耗尽"的所有降级分支；`STT_TIMEOUT_S = 0.01` + `FakeSTTProvider(delay=0.05)` ⇒ 真 `STT_FAILED`（不用真网络）。
- ⚠️ 形态要求：常量在**函数体内**被读（`asyncio.wait_for(..., timeout=STT_TIMEOUT_S)` 即可）。**不要**写成 `def transcribe(stt, path, *, timeout=STT_TIMEOUT_S)` —— 默认参数在 def 期就求值，monkeypatch 之后不生效。

### S10 时钟：模块级 `import time`，调用期取
- 事实：`elapsed_ms` 是"处理器入口到信封构造的墙钟毫秒（`time.monotonic()`）"（`SPEC.md:759`），全局预算也从它起算（`SPEC.md:800`）
- 用法：把一个假 `time` 模块（只有 `monotonic()`，按调用序号递增）patch 到 `analyze.time` ⇒ `elapsed_ms` 变成确定性数值，判"预算不足"的边界不用 `sleep`。
- ⚠️ 形态要求：`analyze.py` 里写 `import time` + `time.monotonic()`；**不要** `from time import monotonic`（名字绑死后 patch 不到）。

### S11 工作目录：`tempfile.mkdtemp` 经模块属性调用
- 签名：`analyze` **自己**建/删 workdir（`tempfile.mkdtemp(prefix="media_analysis_")`），调用方不关心（`SPEC.md:486-487`、§9-4 在 `SPEC.md:945`）
- 用法：`monkeypatch.setattr(analyze.tempfile, "mkdtemp", lambda **kw: str(tmp_path / "run1"))` ⇒ 断言"跑完产物没了"；或直接扫 `tempfile.gettempdir()` 里有没有残留 `media_analysis_*`。
- ⚠️ 形态要求：`import tempfile` + `tempfile.mkdtemp(...)`（**不要** `from tempfile import mkdtemp`），且删除放在 `finally`（SPEC §7 第 11 步）。

### S12 产物登记要 `getattr` 兜底
- 事实：`getattr(event, "track_temporary_local_file", None)` 可用时把每个产物登记给事件（`SPEC.md:945`），因为**测试用 fake event 没有这个方法**
- 用法：`make_event(...)`（**无**该方法，走 `getattr` 的 None 分支）/ `make_event(..., tracking=True)`（有该方法，断言 `event.tracked_files`）
- ⚠️ 形态要求：直接 `event.track_temporary_local_file(...)` 会在假 event 上 `AttributeError` ⇒ 测试只能真机跑；必须 `getattr` 兜底（SPEC 已写）。

### S13 工具门面可直接构造，`__init__` 形参名逐字
- 签名：`class MediaAnalysisPlugin(Star)`，`__init__(self, context: Context, config: AstrBotConfig | None = None)`；`async def media_analyze(self, event, target="current", focus="", include_frames=False) -> str`（`SPEC.md:496-503`、骨架 `SPEC.md:566-606`）
- 用法：`plugin = MediaAnalysisPlugin(context=FakeContext(), config={"limits": {...}})`；`got = await plugin.media_analyze(event)` ⇒ `json.loads(got)` 断言信封；`plugin._settings()` 直接断言 4 键白名单。
- ⚠️ 形态要求：
  1. 第二个形参**必须**叫 `config`（加载器按 `context=…, config=…` 关键字调用，`SPEC.md:610`）；
  2. `__init__` 里不做 IO / 不解析 provider / 不建 workdir（否则构造即联网）；
  3. 工具方法只挂 `@filter.llm_tool`（`SPEC.md:565`、`:612`）。
- 覆盖的用例：`test_registration.py`（schema/docstring）、`test_analyze.py` 的异常收口（`analyze.analyze` 抛裸异常 ⇒ `INTERNAL_ERROR` 信封）。

### S14 `envelope` 的常量与构造函数是纯函数
- 签名：16 个 `ERR_*`、`HINTS`（逐字文案）、`ToolError(code, *, log_detail="")`、`fail_envelope(code)`、`ok_envelope(...)`、`dumps(...)`（`SPEC.md:386-435`）
- 用法：`set(HINTS) == {…16…}`、`list(fail_envelope("X")) == ["ok","error","hint"]`、逐字快照 16 条 hint（C5/C6/C9）。
- ⚠️ 形态要求：`HINTS` 是**模块级常量 dict**，不是函数调用结果；`ok_envelope` 的 9 个关键字参数名与顺序即信封键序（`SPEC.md:734-760`）。

### S15 `transcribe` / `describe_frames` / `build_summary` 是模块级函数
- 签名：`async def transcribe(stt, wav_path: str, *, notes: list[str]) -> str` / `async def describe_frames(provider, frames, *, focus, deadline, notes)` / `async def build_summary(provider, *, material, focus, notes)`（`SPEC.md:488-493`）
- 用法：`FakeSTTProvider(text="…")` 直喂；`FakeChatProvider(fail_with=…)` 直喂；断言 `notes` 里的 `stt_empty` / `frame_desc_failed:<at_s>` / `summary_failed`。
- ⚠️ 形态要求：`notes` 由调用方传入并原地追加（不要返回新列表，否则断言要对返回值）。

### S16 `extract_frames` 的 `duration_s` 是**可注入的网格参数**
- 签名：`async def extract_frames(video_path: str, *, count: int, duration_s: float, workdir: str) -> list[tuple[float, str]]`（`SPEC.md:458-459`，网格公式 `SPEC.md:865-871`）
- 用法：给 5 s 夹具传 `duration_s=30.0, count=6` ⇒ 网格点 2.5/7.5/12.5/17.5/22.5/27.5，后 5 个越过 EOF（`-ss` 越界时 ffmpeg **rc=234 且不产出文件**）⇒ 断言"前 1 帧成功、其余走 `frame_extract_failed:<at_s>`、函数**不抛**"。
- ⚠️ 形态要求：网格**只**由 `(count, duration_s)` 算，不要自己再 probe 一遍时长；单帧失败必须单条降级（`SPEC.md:787`、`:871`），只有**全失败**才抛 `FRAME_EXTRACT_FAILED`。

### S17 STT 调用面：关键字 `audio_url=`、传本地路径
- 签名：`async def get_text(self, audio_url: str) -> str`（`SPEC.md:190-197` 引用 `provider/provider.py:218-227`）
- 用法：`FakeSTTProvider(require_existing_file=True)` 一开，就等于自动断言"插件给的是**存在的本地文件路径**，不是 URL、不是 base64"。
- ⚠️ 形态要求：`transcribe()` 内调用写成 `stt.get_text(audio_url=wav_path)`（与核心 `stage.py:246` 同形），别传位置参数混着用 —— 假 provider 记录的是 kwargs，两种都能断言，但统一形态省事。

---

## 2. 夹具清单（`tests/make_fixtures.py` 生成到 `/tmp/media_analysis_fixtures`）

| 键（`media_fixtures[...]`） | 文件 | 时长 | 音轨 | 视频流 | 体积（实测） | 用途 |
|---|---|---|---|---|---|---|
| `audio_30s` | `audio_30s.wav` | 30 s | ✔ | ✘ | 960,078 B | `Record` 那条路（16 kHz 单声道，与抽音轨目标同形） |
| `video_30s` | `video_30s.mp4` | 30 s | ✔ | ✔ | 106,073 B | `include_frames` 两种路径的主样本 |
| `video_70s` | `video_70s.mp4` | 70 s | ✔ | ✔ | 240,569 B | `duration_s > max_media_seconds(60)` ⇒ `MEDIA_TOO_LONG` |
| `video_5s` | `video_5s.mp4` | 5 s | ✔ | ✔ | ~20.7 KB | SPEC §11.2：6 帧中点网格全部落在片内 |
| `video_5s_noaudio` | `video_5s_noaudio.mp4` | 5 s | ✘ | ✔ | ~8.9 KB | `NO_AUDIO_TRACK` vs note `no_audio_track` 的分界 |
| `video_audio_only` | `video_audio_only.mp4` | 5 s | ✔ | ✘ | ~11.4 KB | 伪 `Video` 组件 ⇒ `NO_VIDEO_TRACK` |

约束与实现细节：

- 全部 **≤ 1 MB**（最大的 30 s wav 960 KB）；`MAX_BYTES = 1_000_000` 由 `test_fixtures.py` 兜住。
- 纯合成：`lavfi sine`（440 Hz）+ `lavfi testsrc`（彩条 + 计数器），**无真实群媒体、无真人画面、无 PII**（`SPEC.md:948`）。
- 幂等：已存在且形状对 ⇒ 跳过；`--force` 才重造。同参数重造**逐字节一致**（`-fflags +bitexact` + `-flags:v/-flags:a +bitexact` + `-map_metadata -1`，实测两次 sha256 相同），因此 `MANIFEST.sha256` 可用于对账。
- 落 `/tmp` 是房屋约定（`AGENTS.md` §8）；`tests/` 里只留生成脚本，二进制不进库。`$MA_FIXTURES_DIR` 可换目录，`conftest` 认它。
- 缺 ffmpeg 且夹具不齐 ⇒ `pytest.skip`（不是 fail）：本机 `/usr/bin/ffmpeg` → `/opt/ffmpeg/bin/ffmpeg` 实测在（`SPEC.md:23`）。
- 30 s wav 用 16 kHz 是因为插件抽音轨的目标就是 16 kHz 单声道（`SPEC.md:877-878`）；视频里的 aac 用 8 kHz 只是为了压体积，不影响任何判定。

手动：

```bash
python3 tests/make_fixtures.py --verify     # 只复核（0＝全好，2＝形状不符，3＝缺 ffmpeg）
python3 tests/make_fixtures.py --force      # 全量重造
```

---

## 3. 安全与"不写生产树"的两条实测

1. **只 import 核心不写生产**（2026-10-22 实测）：把 `/root/AstrBot/data/cmd_config.json`
   复制到 `ASTRBOT_ROOT=/tmp/x2` 后 import `astrbot.api.star` / `astrbot.core.provider.*`，
   前后 `sha256` **完全一致**（`24cd843e…`）⇒ 纯 import 无副作用。
2. **但构造 `AstrBotConfig` 会回写**：核心 `AstrBotConfig.__init__` 会跑
   `migrate_config_on_load()`（`astrbot/core/config/astrbot_config.py:18` →
   `astrbot/core/utils/migra_helper.py:317`），迁移完**保存** `<root>/data/cmd_config.json`。
   所以 `conftest.py` 在任何 `import astrbot` 之前把 `ASTRBOT_ROOT` 钉到
   `/tmp/media_analysis_testroot`（除非外部已显式设置）。**测试里任何"加载插件配置"的
   用例都必须走这条路**，否则等于改生产配置（本 lane 红线）。

---

## 4. 桩（`_astrbot_stub.py`）的边界

- **正常门禁走真核心**：`/root/AstrBot/.venv/bin/python` 里 `astrbot` 就是部署树，
  `Record.fromFileSystem` / `Video.fromFileSystem` / `isinstance(x, STTProvider)` 都是真类型面
  （`SPEC.md:977` 的实测）。`conftest.py` 的 `pytest_report_header` 首行会打印
  `backend=real-core`，跑门禁时一眼可查。
- 桩只保证"核心不可用（例如拿系统 `python3`）时 `tests/` 仍能 collect"：
  实测 `/usr/bin/python3`（无 pytest、无 astrbot）下 `_astrbot_stub.install()` 后 `import _fakes` 通过、
  `isinstance(FakeSTTProvider(), STTProvider)` 成立。
- 桩**不**提供 `astrbot.core.utils.media_utils.get_media_duration` 的真实实现（恒 `None`）⇒
  插件源码在桩模式下跑不通，**这是刻意的**：本插件与核心的耦合点只有一个，没有真核心就不该
  假装能测。

---

## 5. 对 builder 的硬要求（少一条，测试就只能上真机）

- [ ] `analyze.py` / `main.py` 用 `from . import media, providers, envelope, settings, cache`（`SPEC.md:327-337` 的 DAG），**不** `from .x import y` 取函数。
- [ ] 四个 `analyze.analyze` 协作者全部由形参进来（`SPEC.md:483-487`），函数内不碰全局/环境/配置。
- [ ] `media.get_media_duration` 是模块级符号（`SPEC.md:245-247`）。
- [ ] 预算常量在函数体内读（`SPEC.md:474-479`），不写进 def 默认参数。
- [ ] `import time` / `import tempfile` 用模块属性调用（`SPEC.md:800`、`:486-487`）。
- [ ] ffmpeg/ffprobe 的 `argv[0]` 是裸名、`argv` 列表直传、不走 shell（`SPEC.md:875-884`）。
- [ ] 产物登记 `getattr(event, "track_temporary_local_file", None)`（`SPEC.md:945`）。
- [ ] `MediaAnalysisPlugin.__init__(context, config)` 形参名逐字、构造零 IO（`SPEC.md:610-611`）。
- [ ] `notes` 原地追加（`SPEC.md:488-493` 的签名就是 `notes: list[str]`）。
- [ ] 单测文件里异步用例打 `@pytest.mark.asyncio`（或整文件 `pytestmark = pytest.mark.asyncio`）；
      本库**不建** `pytest.ini`/`pyproject.toml`（`SPEC.md:325`），pytest-asyncio 1.4.0 走 strict 模式，别指望自动转协程。

---

## 6. 地基自检配方（可粘贴；都是 2026-10-22 实测过的）

**① 空测试也能跑**（t3 的完成条件之一：conftest 不依赖任何插件源码）

```bash
R=/root/workspace/projects/astrbot_plugin_media_analysis/tests
rm -rf /tmp/ground-empty && mkdir -p /tmp/ground-empty/tests
cp "$R"/{conftest.py,_fakes.py,_astrbot_stub.py,make_fixtures.py} /tmp/ground-empty/tests/
printf 'def test_noop():\n    assert True\n' > /tmp/ground-empty/tests/test_empty.py
cd /tmp/ground-empty && PYTHONDONTWRITEBYTECODE=1 \
  /root/AstrBot/.venv/bin/python -m pytest tests/ -q -p no:cacheprovider
# → 1 passed；报告头第一行是 backend=real-core
```

**② 地基自检 + 夹具形状**（本任务交付的那套 28 条，**不 import 插件模块**）

```bash
cd /root/workspace/projects/astrbot_plugin_media_analysis && \
  PYTHONDONTWRITEBYTECODE=1 /root/AstrBot/.venv/bin/python -m pytest tests/test_fixtures.py -q -p no:cacheprovider
# → 28 passed in 1.41s（实测；从 /tmp 用绝对路径跑同样 28 passed ⇒ 不依赖 cwd）
```

**③ 夹具重跑 / 复核**

```bash
python3 tests/make_fixtures.py --verify   # 0=全好，2=形状不符，3=缺 ffmpeg
python3 tests/make_fixtures.py --force    # 全量重造（实测 1.45 s）
```

**④ 这套 `sys.path` 形态确实能按包导入插件**（builder 的测试会依赖它）

```bash
cd /tmp && ASTRBOT_ROOT=/tmp/media_analysis_testroot PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH=/root/workspace/projects:/root/workspace/projects/astrbot_plugin_media_analysis/tests \
  /root/AstrBot/.venv/bin/python -c "
import astrbot_plugin_media_analysis.main as m
from astrbot.core.provider.register import llm_tools
t = llm_tools.get_func('media_analyze')
print(m.__package__, bool(t), sorted(t.parameters['properties']), sorted(t.parameters))"
# 实测输出：astrbot_plugin_media_analysis True ['focus','include_frames','target'] ['properties','type']
```

**⑤ 一条给全队的提醒（不是本任务的产物，但踩的是同一条红线）**

实测：`2026-10-22 04:55:24` 与 `04:55:42`，生产 `/root/AstrBot/data/cmd_config.json`
被**回写**过两次（文件与目录 mtime 同时变）；结果是核心的 v5 迁移落地
（`config_version = 5`、`provider_settings.computer_use_local_permissions.*` 被补进来、
`image_compress_*` 两个叶子被核心迁移摘掉，26 个 provider 未变）。

- 触发条件：**在 `cwd = /root/AstrBot` 或未设 `ASTRBOT_ROOT` 的情形下构造 `AstrBotConfig`**
  （`get_astrbot_root()` 默认取 `os.getcwd()`，`astrbot/core/utils/astrbot_path.py:30-36`）。
- 已排除"纯 import"：把同一份配置复制到 `ASTRBOT_ROOT=/tmp/x2` 后 import
  `astrbot.api.star` / `astrbot.core.provider.*`，前后 `sha256` 一致（`24cd843e…`）。
- 服务没有重启（`ActiveEnterTimestamp=2026-10-22 02:34:10`，SubState=running）⇒ 不是服务自己写的。
- 本任务全程 `ASTRBOT_ROOT=/tmp/media_analysis_testroot`（`conftest.py` 强制），未碰生产。
- 结论：**做任何探测都先钉 `ASTRBOT_ROOT`**；`conftest.py` 已经这么做了，临时脚本请照抄。
