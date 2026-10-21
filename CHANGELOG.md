# CHANGELOG

## v0.1.0 — 2026-10-22（**未发布 / 未安装**）

首个版本。单工具 `media_analyze`，音轨优先 + 抽帧按需。

> 状态：库 `main` 的 **v0.1 代码/测试基线** = 提交 `16e0eea`（9 笔提交）；本文件与 `README.md` 的
> 修订作为其后的**文档提交**落库（只动这两份文档，代码面逐字节不变）。**无远端、未 push**；生产
> `/root/AstrBot/data/plugins/astrbot_plugin_media_analysis` **不存在**，`pin-check.sh` 无本插件条目
> ⇒ 「未安装」是实测事实，不是"大概没装"。安装归 fix + 人，派单见
> `/root/workspace/astrbot/handoff/lanes/dev/20261022-order-fix-deploy-media-analysis-v0.1.0.md`。

### 能力

- **工具**：`media_analyze(target="current", focus="", include_frames=False) -> str`，
  只挂 `@filter.llm_tool`（不叠加 `permission_type`，否则会被当消息处理器）；
  参数全部有默认值，schema 由 docstring 生成（无 `required`）；返回恒为 `str`。
- **媒体取用**：只吃会话内媒体。`Record` 到手即本地 wav（核心预处理已写回 `component.path`）、
  `Video` 用 `convert_to_file_path()` 兜底；两处遍历＝`event.get_messages()` 与
  `Reply.chain`（后者只能取一层）。
- **音轨阶段**：STT provider 五级回落阶梯；`get_text(audio_url=本地 wav)`；
  `transcript` 单项 `t=0.0`（`get_text` 无时间戳，不伪造）。
- **抽帧阶段**：`include_frames=true` 才跑；坐标取每等分段**中点**
  （`round((i+0.5)*duration/count, 2)`），缩放 768 px；单帧失败降级为 note。
- **错误面**：16 条错误码 + 12 条 `notes` 词表 + 成功 9 键 / 失败恰好 3 键信封，
  全部是常量与封闭集合；任何分支都不向模型抛异常（`main` 是唯一异常收口，
  `asyncio.CancelledError` 例外，见下）。
- **预算**：全局 100 s（框架 `tool_call_timeout` 120 s 留 20 s 余量），单步各 10–60 s；
  预算耗尽走 note 降级（`frames_truncated` / `summary_skipped_budget`）而不是框架超时。
- **缓存**：内容 `sha256[:16]` + 参数指纹 → 进程内 LRU(32)，命中回 `cached: true`；
  不加配置键、不落盘（缓存里有转写正文）。
- **配置**：2 组 4 键（`provider_settings.{stt,vision}_provider_id`、
  `limits.{max_media_seconds,max_frames}`），三方一致（schema ⇄ KEY_MAP ⇄ `main._settings()`）。
- **隐私**：代码里不打印转写正文/画面描述；中间产物只落 `/tmp` 且用完即删。

### 评审与修复（同日，仍属 v0.1.0；按提交顺序）

- `450e206` 从零建库 + 首版实现（`t2`）。
- `0bbeac9` 核心内部面 `error_redaction` 的 import 加固：内部面搬家时退化为「本地兜底脱敏
  + 导入期告警」，不再整插件静默拒载（`t8`）。兜底规则＝**≥24 位连续 ASCII `[A-Za-z0-9_-]`
  串 → `[REDACTED]`** —— 中文正文、长路径、含 `/ ? :` 的长 URL **不脱敏**，属降级路径的已知边界。
- `0c6dd16` `tests/` 入库（信封契约 / 键序 / 配置三方一致 / 缓存淘汰 / 降级分支）（`t9`）。
- `bc2635b` ERROR 日志的 `traceback` 段也过脱敏（日志是凭据外流的第二现场）。
- `2447f64` 预算截断**不是失败**：抽帧/描述按预算降级，已拿到的转写不丢。
- `9798a0c` 预算降级断言落地 + 两处追加参数的盖章测试（撤掉两处 `xfail`）。
- `b32f4ed` 第二处核心内部面 `media_utils.get_media_duration` 也加固（本地 `_fallback_duration_ms`
  ＝自己起 `ffprobe -show_entries format=duration -of json`，毫秒 int，失败 `None`），
  并新增 `tests/test_core_imports.py` 的 AST 守卫：8 个源码模块里任何 `astrbot.core.*` 的
  import 必须落在**带 except 的** `try` 体内（`t16`）。
- `d854af7` 评审六条（`t18`）：
  - `frames_truncated` 的 `kept` 改用 `len(described)`（实际留在 `frames` 的条数），
    不再用循环下标 —— 中间某帧失败时旧口径会多报；
  - 预算中途截断过 ⇒ 即便描述全失败也**不**判 `VISION_FAILED`（转写保住，按降级出信封）；
  - 门面把 `asyncio.CancelledError` **向上抛**（唯一不套信封的分支）；
  - `include_frames` 按真值表归一（模型发字符串 `"false"` 时不再被当成真）；
  - `probe_streams` 判据补 `rc != 0 ⇒ PROBE_FAILED`（探测坏了 ≠ 没有流；`rc == 0 + 空 stdout`
    才是"该流不存在"）；
  - 读消息链（`event.get_messages()`）抛异常 ⇒ `MEDIA_UNRESOLVABLE`（不再伪装成
    "消息里没有语音/视频"）；`getter` 不存在仍按空链 ⇒ `MEDIA_NOT_FOUND`。
- `16e0eea` 测试守卫补齐（`t21` 落库 `t19`）：`HINTS` 16 条**逐字快照** + 未知码归一
  `INTERNAL_ERROR` + 上述六条行为变更的双向断言 + ffmpeg 缺失 `skip` + 白盒守卫标注（`t19`）。

### 实测（`VERIFY.md`，dev lane 工作区、**未入库**；被测 HEAD `9798a0c`）

> 其后的 `b32f4ed` 只动 import 块与兜底函数、`d854af7` 只动计数/取消/参数归一/判流/坏链归因，
> `extract_wav` / `extract_frames` / `frame_grid` / `probe_duration_s` 的**函数体与冻结 argv 未动**
> （`git show d854af7 -- analyze.py media.py` 里没有这些符号的改动行）⇒ 下面的耗时与体积数字仍成立。

- 60 s / 1280×720 / 30 fps 合成视频 → 16 kHz 单声道 wav **71.2 ms**（1,920,090 B = 1.83 MiB）；
  抽 4 帧 **339.2 ms**、6 帧 445.0 ms、12 帧 901.7 ms（≈75 ms/帧，严格线性）；
  时长探测 22.2 ms、判流 43.7 ms、内容哈希 5.5 ms。
- 端到端**本地**开销（假 provider 零延迟）：`include_frames=false` **150 ms** / `=true`（6 帧）
  **589 ms**。真 provider 延迟**未测**（本机无离线 STT 条件）。
- **STT 上传体没有二次转码**：body = 16k wav × 4/3 ⇒ **2.44 MiB（60 s）/ 12.21 MiB（300 s）**
  ⇒ `limits.max_media_seconds` 的 **300 s 上闸只是防手滑**，300 s 已贴/超多数远端 ASR 的
  10 MB 请求体上限；**建议留在默认 60 s**（详见 `README.md` 配置段的体积口径）。
- `limits.*` 结论：**维持 `max_media_seconds=60` / `max_frames=6`**；超限行为实测
  （70 s ⇒ `MEDIA_TOO_LONG`、恰好 60.000000 s 通过＝严格 `>`、钳位 5–300 / 1–12 越界只告警）。
- 库内门禁：**247 passed / 0 failed / 0 xfailed**（`t21` 提交前后各一次、`t17` 独立复跑一次）。

### 已知限制（v0.1 内不做，见 `README.md`「非目标」与 SPEC §13）

- 没跑过真 STT / 真视觉 provider（本机无离线条件）⇒ 60 s 真实语音的 STT 墙钟**未测**，
  上线后按 SPEC §11.4-1 补量；判真四条里 ①③ 必须上线才能验。
- 全局 STT（`provider_stt_settings.enable`）与本工具**互斥**，二者只能选一个。
- 人格 `tools` 为固定清单时必须人工加 `media_analyze`（改生产 DB，归 fix + 人）。
