# CHANGELOG

## v0.2.0 — 2026-10-23（**待评审 / 待部署**）

修掉 ops 查出的**上线后零成功**缺陷，并落四项产品优化。规格：`SPEC-v0.2.md`（dev lane 工作区，
1220 行，冻结增量）。

### ① 群文件（`File` 段）—— 缺陷根因与修法

本平台**群视频以「群文件」投递**，而 v0.1 的媒体候选集只有 `Record`/`Video`
（`media.py` `_AUDIO_KINDS`）⇒ 生产日志里一直是
`MEDIA_NOT_FOUND detail=quoted: no Record/Video in chain`。

- 候选集 `Record → Video → File`（**全链优先级**：同一条消息里 `Record` 优先于 `File`）。
- `File` 走**独立取件分支**：`file_` 命中就零 IO 复用（核心多半已 `get_file()` 下好并回写）；
  `file_` 空但 `url` 有 ⇒ **我们自己有界下载**（`aiohttp` 流式 + 边读边数字节 +
  `asyncio` 总时长 20 s）；两者都空 ⇒ `MEDIA_UNRESOLVABLE`。
- **禁用** `File.file` 属性（异步上下文里恒返回 `""` 并打 WARN）与 `File.get_file()`
  （`io.py:306`：超时 1800 s、无大小上限、落生产 `data/temp`）。
- 判型三段梯子（扩展名白名单 → `ensure_wav`（`pysilk`/ffmpeg）→ `ffprobe`）：
  白名单 12 个视频扩展名 / 13 个音频扩展名；**`ffprobe` 单独做不了双保险** —— `silk` 是
  合法音频但 `ffprobe rc=1`，靠 `ensure_wav` 救。三段都不成立 ⇒ **`UNSUPPORTED_MEDIA_TYPE`**。
- 字节闸：`limits.max_media_bytes`（默认 64 MiB）在下载与判型两处；超限 `MEDIA_TOO_LARGE`。
- `log_detail` 现在带上**链里实际出现的组件类型名**（如 `chain=[Plain, Reply]`），
  v0.1 只写 `no Record/Video` —— ops 当时就是靠这条才没误判。

### ② 音频改走多模态对话模型（不再依赖 `STTProvider`）

- `resolve_stt_provider` 五级阶梯整段删除；新增 `resolve_audio_provider`（§2.5 阶梯）。
- 调用形状冻结为 `text_chat(prompt=<提示词>, audio_urls=[16 kHz 单声道 s16 wav])`：
  不传 `image_urls` / `contexts` / `system_prompt` / `func_tool`，`audio_urls` 只放 1 个。
- 交给 provider 的一律是本插件产出的 wav（`ensure_provider_wav`：已是 16k/mono/s16 就
  **零重编码**返回原路径；silk/m4a/mp3/amr 走 `ensure_wav` + ffmpeg）。体积口径：
  2 s 音频我们自己转 ⇒ base64 85,440；让核心转（保留 44.1k 立体声）⇒ 470,504（**5.5×**）。
- 异常映射：超时 / provider 抛错 / `EmptyModelOutputError` / 内容安全过滤 ⇒ `AUDIO_FAILED`；
  正文为空或 `[无语音]` ⇒ `transcript: []` + note `audio_empty`（**不是错误**）；
  `CancelledError` **必须传播**。
- 错误码分工修正（对 v0.1 的一处覆盖）：**显式配了但不符合** ⇒ `PROVIDER_CAPABILITY_MISMATCH`；
  **没配且回落到的核心当前对话 provider 也不符合** ⇒ `AUDIO_UNAVAILABLE` / `VISION_UNAVAILABLE`。

### ③ 语音/视频各自独立提示词

- `prompts.audio` / `prompts.video` / `prompts.frame` / `prompts.summary`（空串回退内置默认）。
- 校验在装配期：非法花括号 / 未知占位符 ⇒ 回退默认 + 一次 `WARNING`；超 2000 字符 ⇒ 截断 +
  note `prompt_trimmed:<key>`；模板缺焦点占位符而用户给了 focus ⇒ 追加从句 +
  note `focus_appended:<key>`（**焦点永不被静默丢弃**）。
- **提示词指纹进缓存键** ⇒ 改提示词即让旧结果失效。

### ④ 解析结果本地缓存（带 TTL）

- `cache.ResultCache`（内存 LRU）→ `cache.DiskCache`：落
  `data/plugin_data/astrbot_plugin_media_analysis/cache/v1/<前两位>/<64hex>.json`，
  懒创建、`.tmp/` 同分区 `os.replace` 原子、**绝无默认路径指向 cwd**。
- 键 = `sha256("v2|digest|kind|include_frames|max_frames|prompts_fp|focus|audio_provider_id|vision_provider_id|PLUGIN_VERSION")`。
- TTL 默认 86400（0＝关闭，钳 0–2592000）；容量 ≤256 条 / ≤16 MiB / 单条 ≤256 KiB；
  超限按 `mtime` 最旧淘汰，命中刷新 `mtime`。
- 命中：`cached: true` + `elapsed_ms` 是**本次**真实耗时 + notes 追加 `cache_hit` /
  `cache_age_s:<n>`（原始 notes 保留在前）。
- **只存信封文本**：条目键集合恰好 `{v, key_inputs, created_at, envelope}`；写盘前硬闸：
  序列化文本含 `/tmp/`、`/root/`、`data:` 就拒绝写入。
- 降级：损坏/过期/权限/`ENOSPC` ⇒ 静默 miss 或本进程禁用，**不产生 notes、不产生错误码**。
- 同键 `singleflight`（并发同媒体只花一次 token）；跨进程不处理（单进程部署）。
- **缓存会进核心备份**（`data/plugin_data` 在备份清单里）；可整目录删，无副作用。

### ⑤ 配置 4 键 → 11 键 + 迁移脚本

- 新键：`provider_settings.audio_provider_id`、`prompts.{audio,video,frame,summary}`、
  `limits.max_media_bytes`、`cache.ttl_seconds`。
- **旧键 `provider_settings.stt_provider_id` 原路径、原类型、原 `_special` 保留在 schema 里**：
  从 schema 删掉的那一刻，核心 `check_config_integrity()` 会把用户旧值当 stale 键
  **静默删除并 `save_config()`**。代码只读不生效（值非空时打一次弃用 WARNING）。
- `tools/migrate_config_v02.py`（任务单里也写作 `migrate_config_v2.py`，后者是转发 shim）：
  默认干跑、`--apply` 先写 **0600** 逐字节备份再原子替换、幂等（第二遍 `changed=0` 且不产生
  新备份）、`--clear-legacy`（值已搬走才允许置空，**不删键**）、`--rollback`、
  `--report-legacy`；**任何模式都不打印配置值**（只有键名/动作/类型/长度）。
  搬值**带能力校验**：只有"该 id 是 chat 类且 `modalities` 明确含 `audio`"才搬，否则原样
  留在旧键并给原因码（`not_found` / `not_chat` / `no_audio`，退出码 2）。
- **顺序不可换**：先上代码+schema → 重启 → 再跑迁移；反序时旧 schema 会把新键当 stale 删掉。
  回滚成对（先回代码、再回配置、最后重启）。

### 版本面

- `metadata.yaml`：`version: v0.2.0`、`desc`/`short_desc` 补"群文件投递 + 结果本地缓存"；
  `name` 与 `astrbot_version`（`>=4.29,<5`）**不动**；`repo` 保持
  `https://github.com/cmdst/astrbot_plugin_media_analysis`；`README.md` 顶部补上游仓库一行。
- 信封不变：成功 **9 键、顺序不变**；失败**恰好 3 键**。
- 错误码 **16 → 18**（删 `STT_UNAVAILABLE`/`STT_FAILED`；增 `MEDIA_TOO_LARGE` /
  `UNSUPPORTED_MEDIA_TYPE` / `AUDIO_UNAVAILABLE` / `AUDIO_FAILED`）。
- `notes` 词表 **12 → 20**（增 `audio_normalized` / `focus_appended:<key>` /
  `prompt_trimmed:<key>` / `material_trimmed` / `source:file` / `file_downloaded` /
  `cache_hit` / `cache_age_s:<n>`；`stt_provider:` → `audio_provider:`、`stt_empty` → `audio_empty`）。

### 评审 t5 修复（同版本内，v0.2.1 之前的必要项）

- **迁移落盘保持原配置权限**：`--apply` 原先用默认 0644 写目标文件（`os.replace` 换 inode），
  把生产 `cmd_config.json` 同款的 **0600 放宽成 644**（配置里有 provider id 与提示词）。
  现在先取原文件 `st_mode` 再原样复制，目标不存在才按 0600 新建；备份仍是 0600。
  回归守卫：`tests/test_migrate_config.py` 的 600/640/444 三档 + "目标不存在"一条。
- **版本守卫落地**（SPEC-v0.2 §11 C9 原先没有实现）：新增 `tests/test_version.py` ——
  `settings.PLUGIN_VERSION` == `metadata.yaml:version`（去 `v` 前缀）== `0.2.0`；
  `name` 一致且是合法标识符；`repo` 逐字 `cmdst/astrbot_plugin_media_analysis`；
  `astrbot_version` 用真 `SpecifierSet` 判含 `4.29.1`（不含即拒载）；CHANGELOG 顶部条目提到本版本。
  自证：只改 version / repo / CHANGELOG 任一，该文件即红。
- **`include_frames` 布尔收口**（v0.1 O2 未关的口径矛盾）：`2.5` / `1.0` 这类数值原先按
  "非零即真"且**不告警**。现在白名单只有 `bool`、整数 `0/1`、大小写不敏感的
  `"true/yes/on/1"` 与 `"false/no/off/0/\"\""`；其余（含 float、越界 int、未知类型）
  一律告警一次并按默认 `False` —— 误发值只会少抽帧，不会多花钱。
- **文案**：缓存键的"11 分量"更正为 **10 个分量**（公式没动，数错的是数词），涉及
  `cache.py` 模块/函数 docstring、`analyze.py` 模块 docstring、`SPEC-v0.2.md` §6.1 标题、两处测试注释。

### 已知状态（下一单处理）

- **库内 `tests/` 仍是 v0.1 契约**（`resolve_stt_provider` / 4 键 / 16 码 / `ResultCache` /
  `resolve_media_path(handle)` 旧签名）⇒ 现在跑门禁会红，属预期；`SPEC-v0.2.md` §12 的测试
  计划由下一单（tester）落地。**本版未改任何测试文件**。
- 未在真实 provider 上跑过（不花运营方额度）；真机验收按 `SPEC-v0.2.md` §14 的 6/7 两步补量。

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
