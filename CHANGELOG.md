# CHANGELOG

## v0.4.0 — 2026-10-25（**实现完成，待评审 / 待推送 `main` / 待部署**）

两个变更：**① GIF 以视频解析**（`Image` 与群文件里的 GIF 一律先用 ffmpeg 归一成 mp4，
再走**完全不变**的原生视频链）；**② 工具调用改为后台异步**（`media_analyze` **受理即返回**，
分析在后台跑完并**唯一投递**一条结果消息；视频 30 s–数分钟不再阻塞会话）。
规格：`SPEC-v0.4.md`（dev lane 工作区，926 行，冻结增量）。

**配置 0 新键、工具参数 0 改动**（21 键 / 3 参数）；错误码 21 → **23**、notes 41 → **44**（只加不改）。

### ① GIF 以视频解析（`Image` + 群文件）

- **判据＝魔数**：文件前 6 字节 ∈ `{GIF87a, GIF89a}` 是**唯一**判定依据；`.gif` 后缀只用来
  决定"要不要去证实"（远程 `Image` 无本地文件时，没后缀就**不下载、不接受**）。
- **为什么不能靠"能解出视频流"**：`ffprobe` 对 **PNG / APNG / WebP** 也报 `codec_type=video`，
  只有 GIF 带 `format.duration`。今天 PNG 被拒靠的是"时长缺失"这个**副作用**，不是判据
  ⇒ 一旦 ffmpeg 给 APNG 补上时长，图片就会**静默**流进视频链。
- **候选优先级**：`Record → Video → Image(GIF) → File`（只在 `File` 前插一档，既有顺序不变）。
  普通图片（JPEG/PNG/WebP/APNG）在**受理路径**就被拒（`MEDIA_NOT_FOUND`，同步返回）——
  契约「图片不进本插件」原样成立。
- **归一参数冻结**：`-an` + `fps=12` + `scale='trunc(min(640,iw)/2)*2':-2` + `yuv420p` +
  libx264 `veryfast`/`crf 26` + `-threads 1` + `+faststart` + `-f mp4`。
  其中 **`trunc(.../2)*2` 是硬要求**：奇数宽 GIF + `yuv420p` + libx264 ⇒ **`rc=187`、0 字节产物**
  （实测 241x162 的样本）；朴素 `scale=min(640,iw)` 会安静地吐一个空文件。
- **四道闸**（顺序固定）：源字节 ≤ `limits.max_media_bytes` ⇒ 否则 `MEDIA_TOO_LARGE`；
  源时长 ≤ `limits.max_media_seconds` ⇒ 否则 `MEDIA_TOO_LONG`；整次 ffmpeg ≤ **10 s**（实测
  7.8 MB / 20 s 的 GIF 只要 **0.62 s**）⇒ 否则 `GIF_CONVERT_FAILED`；产物非空且不超上限。
- **`kind` 仍是 `"video"`**（不新增 kind）：原生/压缩/抽帧/摘要/缓存键/`_KIND_CN` 全部复用；
  GIF 走这条分支时**跳过 `classify`**（那一步会把 `Image` 判成 `kind=audio`）。
- **notes 三条**：`source:gif`、`gif_converted:<in>-><out>`、`gif_static`（源只有 1 帧）。
- **缓存键不加 gif 标记**：`digest` 取**源 GIF 字节**（不是产物 mp4）⇒ 同一张 GIF 无论以
  `Image` 还是群文件形态到达都是同一个键（共享缓存）；参数是代码常量，随 `PLUGIN_VERSION` 失效。
- **`-t` 刻意不加**：时长闸在转换**之前**；加了 `-t` 只会在 ffprobe 低报时长时**静默截断**
  用户内容。

### ② 修复（v0.3 潜在缺陷）：群文件 `.gif` 曾被贴 `data:video/mp4` 直送

v0.3 的 `File` + `.gif` 已经"进链"了，但是错的那种：`_classify_file` 第 3 段用
"有视频流"判型，而 ffprobe 对 GIF 就是 `codec_type=video` ⇒ **原始 GIF 字节**被
`video_native` 的 `data:video/mp4` 前缀贴出去送给模型（MIME 错、容器错，且没有任何测试覆盖）。
`Image` + `.gif` 更糟：被判成 `kind=audio`（`has_audio=True`）。
本版两者一并修掉：**所有 GIF 一律先归一成 mp4**，从不把原始 GIF 字节送给模型。

### ③ 工具调用改为后台异步（受理即返回 + 唯一投递）

- **受理信封（第三种形状，4 键、顺序固定）**：
  `{"ok":true,"state":"accepted|running","task_id":"<32hex>","hint":"<常量>"}`。
  与成功 9 键 / 失败 3 键**键名不重叠**（除 `ok`）。模型**只会**看到"受理"或"失败"；
  成功信封变成一条**发给用户**的中文消息（不再回到模型）。
- **机制**：插件自管任务表（`_TASKS` / `_BY_KEY` / `_BY_UMO`）+ `asyncio.create_task`；
  去重键 `umo|target|message_id|mode|focus`（同一条消息同一组参数重复调用 ⇒ 返回**同一个
  `task_id`**，`state="running"`，不新建任务；拿不到 `message_id` 时**关掉**去重）。
- **并发闸**：单会话 3 / 单实例 6 ⇒ 超限返回 `TOO_MANY_TASKS`（同步、失败 3 键）。
- **投递 at-most-once**：一次 `context.send_message`；**只有**它返回 `False`（＝没找到平台、
  什么都没发）时才 `event.send` 兜底一次；任何异常只记 WARNING、**不重试**。
  **重复消息比缺一条消息更伤** —— 所以宁可少一条，绝不重复。
- **取消**：`terminate()` 对全部任务 `cancel()` 并等它们收尾（`star_manager.py:1965`），
  **不留持有 event 的孤儿 task**；取消**不投递**任何消息。
- **为什么不用核心的 `is_background_task`**：核心 4.29.1 的派发层读的是
  `_PermissionGuardedTool` **包装器**（只镜像 name/description/parameters/active/
  handler_module_path，**没有** `__getattr__`），而 `get_full_tool_set()` 每次重建包装器
  ⇒ 在 `llm_tools.get_func(...)` 上置 flag **恒为 False**（`/tmp` 探针实测：仍同步执行）；
  即便改置到包装器实例上，回执文案被核心写死、多一次整段历史的 agent 回合、而且**投递
  由模型决定**（核心只记一句 WARNING）—— 拿不到"恰好一次"。故采用插件自管的 A2。
- **不做任务持久化**：进程重启/插件重载会丢掉进行中的任务（用户侧表现为"没有结果消息"）。
  重发媒体即可，这是本版明确接受的产品行为。

### ④ 版本与连带的两个冷启动

- `metadata.yaml:version` / `settings.PLUGIN_VERSION` / 本文件顶部条目三处互钉到 **`0.4.0`**。
- **结果缓存与 L1 探测缓存都会"冷启动"一次**：`PLUGIN_VERSION` 是两者的键分量 ⇒ v0.3 写的
  条目**永不命中**（无需动作，按 TTL/容量自然淘汰；急可 `rm -rf data/plugin_data/<插件>/cache/`）；
  每个视频模型首用会**多做一次 L1 探测**（约 3–20 s，可先跑
  `tools/probe_video_capability.py` 预热）。
- **部署比 v0.3 简单一档**：覆盖代码 + **一次重载**即可（schema 未变、21 键一个不动 ⇒ 核心不会
  补键也不会删键、**不需要**迁移器、不需要第二次重载）。回滚也简单：回代码 + 重载，
  **配置无需回滚**（0 新键、0 改名、0 默认值变化）。

### ⑤ 已知限制（都是决定，不是待办）

- 后台任务**不持久化**：重启丢任务，结果不会补发。
- 投递是 **at-most-once**：平台掉线/找不到平台时用户可能**收不到**结果，且插件不会重试。
- 会话 `/reset` 或用户打断**不取消**后台任务（工作已经付费，静默丢弃更糟）——结果照常投递。
- GIF 归一参数是**代码常量**（640 宽 / 12 fps / crf 26）：小字可能糊；要调就随版本调。
- **不支持 APNG / WebP 动图**：它们没有 `format.duration`，"以视频解析"的前提不成立
  （将来要做，判据必须是各自的魔数，而不是"能解出视频流"）。
- 静态 GIF 会被当 1 s 视频处理（记 `gif_static`，语义噪声可接受）。

## v0.3.0 — 2026-10-24（**已评审通过（t14 PASS；t17 复核 R1/R2 PASS）；待推送 `main` / 待部署**）

把**视频主路径**改成「原生视频直送多模态模型」，抽帧降级为**回退**；v0.2 的音频多模态、
5 份提示词与结果缓存全部保留。规格：`SPEC-v0.3.md`（dev lane 工作区，706 行，冻结增量）；
前置实测：`MEASURE-v0.3.md`（t8）；真机验证：`VERIFY-v0.3.md`（t4，427 行，六件必验全部实跑）。

> 评审链：`t5` 全量双轴 **NEEDS-WORK**（0 blocker / 1 high / 3 medium / 6 low）⇒ 同版本内
> 两批修复（F1–F7 / F2–F10）⇒ **`t14` 合并复核 PASS**（七条判据全过，每条都做"改错会红"的
> 变异检验）⇒ `t16` 修 t14 的残留 **R1/R2（音轨预算接线 + 超时分流）**，落成 **`44aa2c8`**
> ⇒ **`t17` 复核 PASS**（spy 证明接线：`reserve=25` 时音轨 timeout 39.998 → 14.997 s；两向分流；
> 冻结面回归）。**代码/测试基线 = `44aa2c8`**，门禁 **695 passed / 0 failed /
> 0 skipped / 0 xfail**（工作树、已提交树、`git archive` 归档树三处一致；`tests/` 27 个文件 /
> 47 个跟踪文件）。**文档提交在其后**（只动 `README.md` + `CHANGELOG.md`，运行面与基线
> 逐字节相同）；要引用文档 sha 用 `git log --oneline --grep '^docs:'`，不写"当前 HEAD"。

### ① 原生视频直送（核心内部面 + 三条护栏）

- 形状冻结：`{"type":"video_url","video_url":{"url":"data:video/mp4;base64,…"}}`，发往
  `{api_base}/chat/completions`（实测 Gemini 3.2 s / mimo 18.4 s 均 200 **且答对画面事实**）。
- **核心 4.29.1 自己的 `text_chat` 放不进 video 块**：`ContentPart` 无 video 变体，
  `assemble_context` 对未知块 `raise ValueError`（`openai_source.py:1424`，实跑复现）⇒
  只能走 provider 底层 `client.chat.completions.create`（`openai_source.py:382`）。
- 三条护栏：① `max_tokens` 硬下限 **1024**（有模型在 64 时 200 但正文为空）；② 空 `content` /
  哨兵 `[无可用内容]`/`[无语音]` 判失败并回退；③ 自建 `asyncio.wait_for`
  （`min(native_timeout_seconds, 剩余预算−35 s)`，底层调用绕过核心超时/重试）。
- 整块 `try/except Exception`：缺失 `client`/适配器类型不符/调用抛错 ⇒
  `video_native_unavailable`/`video_native_failed` ⇒ **回退**，绝不成为新的"静默拒载"点。

### ② 抽帧降级为回退 + `include_frames` 三态

- 链路固定为：原生 → （超门槛）压缩 → 抽帧（`vision_provider_id`）→ 错误信封。
- `include_frames` 类型 `bool` → **`str`**：`auto`（默认）/`true`/`false`；旧布尔值仍接受。
- **`false` + 原生失败 ⇒ 绝不静默抽帧**（用户的显式成本约束）；有音轨则只交转写。
- 新增 3 个终态码：`VIDEO_UNAVAILABLE` / `VIDEO_FAILED` / `VIDEO_TOO_LARGE`（共 21 条，既有
  18 条逐字未动）；新增 19 条 `video_*` note（词表 39 条；评审 F7 再加 1 条、R1 再加 1 条
  ⇒ **41**，见下）。

### ③ 压缩（超直送门槛时）

- `scale=-2:480` + libx264 `veryfast` + ABR 双向钳制 + `aac 64k -ac 1`，最多 **2 趟**
  （第二趟 ×0.95 修正），两趟共享 `compress_timeout_seconds`（默认 30）。
- 预算 = `min(compress_target_bytes, max_direct_bytes)`；源已在预算内 ⇒ **0 次转码**。
- 码率 = `floor(预算×8 / 时长 / 1000) − 64`；`< 64 kbps` ⇒ `video_compress_infeasible`（不硬转）。
- 产物只落 `/tmp`（`media_analysis_*` 工作目录，`finally` 整目录删）；源文件**只读**。
- **实测基线**（生产样本只读：5,640,723 B / 48.901 s / 720x1280）：
  **4 MiB 档 3,873,915 B / 4,207 ms / 1 趟 / 270x480 / 音轨保留**（t4 复测；t8 首测
  3,939,056 B / 4,810 ms，码率按 48 s 整数算得 635k，实现按 48.901 s 算得 622k）；
  8 MiB 档源已在预算内 ⇒ **0 趟**；1 MiB 档 **2 趟 → 1,031,801 B**。

### ④ L1 可见性探测（只做这一层）

- 4 s / 1280x720 / ~150 KB 合成样片（画面正中 4 位白色数字 + 右下角黄色单词），固定提问，
  **两条都命中才算过**（数字按字符串、单词大小写不敏感）。
- 缓存 `data/plugin_data/<插件>/probe/v1/`，键含 `provider_id + model + PLUGIN_VERSION`，
  TTL 默认 **7 天**、`0` = 关闭；每个 TTL 窗口**最多自动跑 1 次**（失败也写占位，防打爆）；
  落盘失败静默降级为进程内记忆。
- **通道类失败不缓存**（F1）：4xx/5xx/DNS/超时只做 60 s 进程内冷却，读到通道类条目视同 miss；
  **`--clear` / `rm -rf probe/` 对运行中插件最多 5 分钟生效**（内存 memo 上限
  `MEMO_MAX_AGE_S=300`）。
- **不做体积阶梯探测**：实测两条通道到 47.2 MiB / 36.5 MiB 都 200（0 次拒绝）⇒ "体积上限"
  不是事实，真正先撞的是延迟（9.4 MB≈43 s、18.6 MB≈91 s、38.3 MB≈193 s）。
  `tools/probe_video_capability.py` 因此是**延迟校准工具**（输出 p50≤45 s 的最大档）。
- **直送门槛建议表**（`settings.VIDEO_DIRECT_BYTES_SUGGESTIONS`：gemini-3*flash* ⇒ 8 MiB、
  mimo ⇒ 4 MiB、其它 ⇒ 4 MiB）**只用于** ready 日志提示与校准工具的建议值，
  **不参与运行期判定**（运行期只认 `video.max_direct_bytes`，避免"表与配置打架"）。

### ⑤ 配置 11 → 21 键（5 组，四处互钉）

- 新增 `provider_settings.video_provider_id` + `video` 组 8 键 + `prompts.video_native`；
  v0.2 的 11 键**名字/类型/默认值一个都没动**（新 schema 是超集 ⇒ 代码先行）。
- 新 `tools/migrate_config_v03.py`（干跑默认 / `--apply` 保留权限位 + 0600 备份 / `--report` /
  `--rollback` / **不打印值**）；v0.2 的迁移器保留不动。
- 布尔读取统一走 `ConfigView.flag()`（非 `bool` ⇒ 告警回默认）。

### ⑥ 缓存键 `v2` → `v3`（10 → 12 分量）

`v3 | digest | kind | mode | max_frames | prompts_fp | focus | audio_provider_id |
video_provider_id | vision_provider_id | video_fp | PLUGIN_VERSION`；`mode` 与 `video_fp`
是新增分量（三种模式不许互相命中；改门槛/开关即失效）。目录仍是 `cache/v1`，旧条目自然不命中。

### 评审修复批次二（`t5` 的 F2/F3/F4/F5/F6/F7/F10）

- **F7（最重要，成本回归）**：`auto` 且**未配视频模型** ⇒ **不抽帧**，退化为"只转写"，
  note 记 `video_unavailable_no_provider`（词表 39 → **40**）。此前"没配视频模型 + 早就配了
  视觉模型"的部署会把每条视频从 v0.2 的 **0 次**视觉调用抬到 **6 次**；只有 `mode=true`
  才强制抽帧。
- **F4（静音视频）**：有音轨但转写为空 + 没抽帧（`mode=false` / `auto` 无视频模型 / 无视觉位）
  ⇒ **`ok:true` + `audio_empty`**，不再抛 `FRAME_EXTRACT_FAILED`（那个码的 hint 会误导用户
  去换媒体，而用户本来就没要画面）。
- **F6（预算饿死）**：`NATIVE_RESERVE_S` 15 → **35**（= `FRAMES_MIN_BUDGET_S`(25) + 10）；
  音轨阶段也按 `remaining − 预留` 取超时（`budgeted_timeout()`，纯函数）。此前原生超时后
  只剩 15 s < 25 s ⇒ 抽帧几乎必然 `video_skip_budget`，**主打回退在最该生效时失效**。
- **F5（max_tokens 默认）**：`native_max_tokens` 默认 **1024 → 2048**（下限仍 1024）；
  ready 行同时打直送门槛与该值；README 写明 **mimo 场景建议 4096**。
- **F3（瞬时失败 TTL）**：probe 结论按 reason 分流 —— 判据不过（`mismatch`/`empty`）按配置
  TTL（7 天）；**本地环境类**（`no_font`/`ffmpeg_missing`/`build_failed`）**1 小时**；
  **通道类**（4xx/5xx/timeout/dns）**不缓存**（与 F1 同一套口径，`cache_ttl_for()`）。
- **F2（记忆穿透）**：`probe._MEMO` 命中改为**过两道时效**（条目 TTL + `MEMO_MAX_AGE_S`=5 min），
  `ProbeStore.clear()` **连记忆一起清** —— 此前 `--clear` / `rm -rf probe/` 对运行中的插件无效。
- **F10（不许抛给模型）**：`video_native` 的每一层属性访问改走兜底 `getattr`（`provider.client`
  是会抛异常的 property 也只算"没有原生通道"）⇒ `video_native_unavailable`，不再落 `INTERNAL_ERROR`。
- 用例：`tests/` +17（F7 成本不变式、F4 两种静音形态、F6 注入时钟 + `budgeted_timeout` 参数化（**该纯函数表已被批次三换成 spy 接线用例**）、
  F5 默认值/ready 行、F3 分流表与 1 小时过期、F2 记忆过期与 clear、F10 两形态）。

### 修复（推送前，`VERIFY-v0.3.md` F1）

- **`tools/probe_video_capability.py` 不带 UA** ⇒ Cloudflare 前置回 403（`error code: 1010`）⇒
  工具把 403 记成"模型看不见"并**写进插件共享的 probe 缓存** ⇒ 插件此后 1 ms、0 网络就记
  `video_probe_l1_failed`，**7 天**不走原生视频（`--clear` 后插件自己的 L1 11.6 s 通过）。
- 工具侧：请求带**与核心 HTTP 客户端同形状**的 `User-Agent`（`AsyncOpenAI/Python <SDK 版本>`，
  版本取自 `importlib.metadata`，不硬编码）；响应分三类 —— `ok` / `rejected`（200 但正文空，
  是裁决）/ `channel_error`（**任何 4xx/5xx、DNS、连接、超时、协议异常**）；
  `channel_error` **不写缓存**且退出码 `2`，`--show` 会标出 `channel_class=yes`。
- 插件侧（更重要）：**通道类 `reason` 的缓存条目不作为"不支持"证据** —— 读到即**视同 miss**
  重新实测；写入侧通道类失败**绝不**写 `l1_ok=false`（只在进程内做 60 s 冷却，冷却过期即重测）。
  只有"200 且内容被判为不可见/答错"（`mismatch`/`empty`）与本地环境类失败
  （`no_font`/`ffmpeg_missing`/`build_failed`，§3.1 冻结口径）才是真正的裁决。
- 用例：`tests/test_probe.py` +20（11 种通道 `reason` 的读侧视同 miss、3 种裁决仍命中、
  写侧不落盘 + 冷却 + 冷却过期重测、分类纯函数、CLI 的 UA/三类响应/与插件同口径）。

### 评审修复批次三（`t14` 的 R1/R2，`44aa2c8`）

- **R1（预算接线，t14 残留 medium）**：`transcribe()` 现在真的走
  `budgeted_timeout(remaining, AUDIO_TIMEOUT_S, reserve_s)`。**修复前**（`97d0104`）
  `budgeted_timeout()` 是**生产零调用**的死函数（spy 实测 `calls=0`，只有纯函数单测），
  慢音轨仍能把抽帧预算吃光；`44aa2c8` 起已接线（`t17` 复核 PASS）。
  **音轨超时按是否要画面分流：要画面 ⇒ `audio_timeout` 继续；不要画面 ⇒ `AUDIO_FAILED`。**
- **新增唯一判据 `frames_possible(mode, settings, has_video)`**：与 `_frames_fallback` 的三道门
  （`has_video` → `mode=false` → 视觉位 → `auto` 且无视频模型）**逐条对齐**，只回答"要不要画面"，
  不产生 note、不判终态码。要画面 ⇒ 音轨给抽帧留 **25 s** 预留；不要画面 ⇒ 不扣。
- **R2（超时分流）**：音轨**超时**时，若本轮可能抽帧 ⇒ note `audio_timeout`、返回空串、
  **继续走画面阶段**（不再 `AUDIO_FAILED` 连抽帧一起丢）；若不要画面（语音消息 / `mode=false` /
  无回退画面）⇒ 仍 `AUDIO_FAILED`（主诉求是转写，不能假成功）。**provider 抛错（4xx/5xx/安全过滤）
  仍一律 `AUDIO_FAILED`** —— 那说明模型侧真出问题，不是"预算不够"。
- 用例：把"纯函数参数表"换成 **spy 接线用例**（断言 `calls ≥ 1` 与预留方向；`t17` 复核实测：
  `reserve=25` 时音轨 `timeout` 从 **39.998 s 降到 14.997 s**）+ 音轨超时两向分流
  + `frames_possible` 矩阵；`notes` 词表 **40 → 41**（新增 `audio_timeout`）。门禁仍
  **695 passed**（替换了原来的纯函数参数化 cell）。

### 真机验证（`VERIFY-v0.3.md`，t4；dev lane 工作区、未入库）

对象 `main` @ `44f9afa` 的 `git archive` 副本（38 文件 / v0.3.0），全程 `ASTRBOT_ROOT=/tmp`。
六件必验事项**全部实跑**（不是复述 t8）：

| # | 事项 | 判定 | 关键数字 |
|---|---|---|---|
| ① | Gemini 体积阈值 | **无阈值，但有瞬时 400 窗口** | 1/4/8/16/32 MiB（raw 0.98–38.7 MB、请求体到 51.5 MB）**全 200**（13.6/36.6/49.0/93.2/208.4 s）；同批首次跑 **五档全 400（含 1 MiB）**，4 分钟后复现不了 |
| ② | 原生**被看见** | **PASS** | Gemini 一次调用读出`数字=8093`+`单词=WINDOW`+声音`"eight zero nine three"`（151 KB / 24.5 s，frames=0/transcript=0）；抽帧 6 帧各自读出`1495`+`CAMERA`（43.1 s / 7 次调用） |
| ③ | 压缩 + 三跳信封 | **PASS** | 生产样本 5,640,723 B → **3,873,915 B / 1 趟 / 4,207 ms / 270x480 / 音轨保留**；信封：原生直送 42.6 s、压缩后原生 24.4 s（压到 1,031,801 B / 2 趟）、抽帧回退 67.7 s（6 帧 + 转写 260 字） |
| ④ | 内存护栏 | **PASS** | 38.9 MiB + 压缩关 ⇒ `VIDEO_TOO_LARGE`、`call_native=0`/`compress=0`、**RSS 增量 2.2 MiB**（整段 base64 需 ≈53.3 MiB）；`max_media_bytes=1 MiB` ⇒ `MEDIA_TOO_LARGE`、增量 0.0 MiB、0 调用；20.3 MiB → 压到 744,367 B → 原生，增量 7.6 MiB |
| ⑤ | 线上现状 | **零流量** | 23:10:20 加载后 `ok kind=` **0**、`rejected` **0**、本插件 `[ERRO]` **0**；`Added llm tool` 2（两次重载）；全日志 2 次 rejected 在 v0.1 时代（02:56） |
| ⑥ | 真机形态复跑 | **PASS** | `Added llm tool: media_analyze` **恰好 1 次**、`include_frames` 已是 **string**、无 `required`、`PLUGIN_VERSION=0.3.0`、`>=4.29,<5` 含 4.29.1 |

**三条必读的实测风险**（运维口径，不是待办）：

1. **代理存在瞬时 400 窗口（与尺寸无关）** ⇒ 不得把单次 400 当 SLA / 能力结论；链路已按规定
   记 `video_native_failed` 并回退。
2. **mimo 建议 `native_max_tokens=4096`**：8 MiB + 长提示词在 **1024 / 2048** 下会 200 但
   正文为空（`video_native_empty`；护栏会回退，但等于白花一次调用钱）。默认 2048 只够
   Gemini 类通道；**mimo 请设 4096**，或把直送门槛留在 **4 MiB**。
3. **原生没有体积上限，但有延迟上限**：47.2 MiB 也能过，但 8 MiB ≈ 45.6–50.4 s、16 MiB 93.2 s、
   32 MiB 208.4 s，而全局预算 100 s ⇒ **>8 MiB 直送不安全**（能过 ≠ 来得及）。默认
   `video.max_direct_bytes = 8 MiB`（钳 **1–32 MiB**）是按延迟定的，不是 provider 上限。

### 遗留（v0.3.1 候选；**只登记，本版未改**）

- **F2（medium）mimo 空文本阈值**：8 MiB + 长提示词下 `max_tokens=1024`（实测 **2048 也会**）
  返回 200 但正文为空。现有护栏判失败并回退（**没有假成功**）；**mimo 建议 4096**，
  或把直送门槛留在 4 MiB。
- **F3（medium，v0.2 既有，不是 v0.3 回归）**：48.9 s 音轨在 mimo 上 `AUDIO_FAILED`
  （`text_chat timeout 60.0s`，同音轨换 Gemini 成功）；**旧行为（v0.2 / `t16` 前）音轨失败会
  中止整条链、连本来能做的抽帧一起丢**。**`t16` 已修掉"连抽帧一起丢"那一半**：本轮可能抽帧时
  音轨超时只记 `audio_timeout` 并继续画面阶段（R2 分流）。**仍保留的是"转写为主诉求"的失败**：
  语音消息 / `mode=false` / 无画面回退时，60 s 超时仍 `AUDIO_FAILED` —— 那是正确判据（不能假成功）。
  ⇒ 要彻底解（调大 `AUDIO_TIMEOUT_S`、音轨独立重试或换 provider）属**规格变更**，留 v0.3.1；
  **改它要同时动 §2.3 音频链路、§7.3 回退链与 `tests/test_video_chain.py` / `test_analyze.py` 的链路用例**。
- **F4（low，外部）**：代理瞬时 `http 400`（无需改代码，已回退）。
- **F5（low）**：CLI 标称档位与实际字节差 **−7%~+16%**（工具用码率逼近体积）⇒ 引用一律用实测字节。
- **F6 / F11（low）哨兵精确匹配**：空文本哨兵是**精确串**（`[无可用内容]`/`[无语音]`）；实测 mimo
  回 `[无用内容]` ⇒ 被记成 `video_native` 成功。先裁决"是否加带方括号短句判据"（行为变更），本版不动。
- **F8（low）非 mp4 容器一律按 `data:video/mp4` 直送**：原生路径的 MIME 是**硬编码** `video/mp4`；
  `.mov` / `.webm` 等容器目前没有单独分支（压缩产物统一 mp4，回退路径不受影响）。要么在 v0.3.1
  按容器探型映射 MIME，要么在文档/配置层面明确"只保证 mp4"（本版取后者）。
- **F9（low）门槛建议表缺元数据**：`settings.VIDEO_DIRECT_BYTES_SUGGESTIONS` 是
  `(pattern, bytes)` 二元组，**没有日期/置信字段**，且最后一行 `*` 会兜底所有未知模型（4 MiB）。
  它**只用于 ready 提示与 CLI 建议**、不参与运行期判定，但表本身无法表达"这条建议有多新/多可信"
  ⇒ v0.3.1 可加日期/置信（或把 `*` 拆成"未知模型"的显式行）。
- **`tests/test_memory_guards.py` 的 `inspect.getsource` 静态断言**在**并发编辑窗口**会间歇红
  （源码正在被改时读到半成品）；不是功能缺陷 ⇒ 文档冻结/提交后按最终 sha 复跑一次即可，
  不要按"内存护栏回归"处理。
- **R1（low，t3）**：`probe_ttl_seconds=0` 时 `ensure_l1` 若被**直接调用**仍会把条目写盘
  （SPEC §3.3 要求"不读不写"）；生产路径由 `analyze` 的 `ttl<=0` 闸挡住 ⇒ 当前不可达。
- **已关闭（原 t14 R1，medium；修复前状态＝`budgeted_timeout()` 生产零调用）**：`44aa2c8` 已接线，
  `transcribe()` 真的按"要不要画面"扣预留；spy 用例断言 `calls ≥ 1`（见批次三）。
- **SPEC 与实现的已知漂移（冻结文本未改，以实现为准）**：`SPEC-v0.3 §8.1` 仍写
  `native_max_tokens` 默认 1024（实现/README 是 **2048**）；`§5.2` 的 `reserve=15` 与 `§7.4`
  的 `include_frames 设为 auto/true` 文案分别落在 **35** 与 **`true`**（评审 F6/F7 的修复）。

### 已知限制 / 未定（照 `MEASURE-v0.3.md` §7 原样登记）

- 形状 B（`input_video`）未测；>47.2 MiB 未测；视频 URL 引用与非 mp4 容器不支持。
- 代理（容器内 CLIProxyAPI）的 body 上限不可读 ⇒ 不区分"代理拒"与"上游拒"，任何失败都当
  "原生不可用"回退。
- 真实群媒体 / 真实群延迟未测 ⇒ 上线后用 `tools/probe_video_capability.py` 校准。
- `NO_AUDIO_TRACK` / `NO_VIDEO_TRACK` 仍在码表里：前者在视频路径已被 `VIDEO_UNAVAILABLE`
  取代（§7.4 表），后者仍是 `mode=true` 且无视频流时的判据。

## v0.2.0 — 2026-10-23（**已评审通过；已推送 `main`（`d9628a4`，2026-10-23 09:39）/ 已部署（v0.2.0，23:10:20）**）

修掉 ops 查出的**上线后零成功**缺陷，并落四项产品优化。规格：`SPEC-v0.2.md`（dev lane 工作区，
1220 行，冻结增量）。

> 评审链：`t5` 全量双轴 → **NEEDS-WORK**（0 blocker / 1 high / 3 medium / 6 low）⇒ `e9ad82b`
> 逐条修复 ⇒ `t10` 增量复核 **PASS**（F1 blocker / F2 权限 / F3 版本守卫全关，含变异检验）。
> 门禁 **442 passed / 0 failed / 0 skipped / 0 xfail**，工作树与 `git archive e9ad82b` 的干净树
> 两处一致。

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
- **顺序不可换**：先上代码+schema → 重载 → 再跑迁移 → **再重载一次**（运行中的插件持有的是
  内存里的配置快照）；反序时旧 schema 会把新键当 stale 删掉并 `save_config()`。
  回滚成对且反向（先回代码、再回配置、最后重启）。细节见 `README.md`「v0.1 → v0.2 的顺序」。

### 迁移要求（部署前读完这五条）

1. **先备份，再动手**：插件目录 `cp -a` + 插件配置 `cp -a`（生产配置是 0600，备份也保持 0600）。
   迁移脚本在本机（旧 `stt_provider_id` 为空）**预判 `changed=0`：不写盘、不产生 `.bak`**
   ⇒ 回滚锚点只能来自这次手工备份。
2. **先上代码，后迁移**：v0.2 的 schema 是**超集**（保留旧键 `stt_provider_id`）⇒ 代码先行安全；
   反序会让 v0.1 的 schema 把新键当 stale 删掉并落盘。
3. **迁移前后各重载一次**：第 ① 次重载让新 schema 补齐 7 个缺省键并落盘；迁移 `--apply` 之后
   必须再重载，运行中的实例才会重新装配设置（`main._settings_cache`）。迁移完成到重载之间
   **不要在面板上保存本插件配置**。
4. **必配音频位**：`provider_settings.audio_provider_id` 需要选一个**模型能力勾了「音频」**的
   对话 provider；留空会回落到核心当前对话 provider，它不支持音频时工具返回
   `AUDIO_UNAVAILABLE`（明确错误码，不花钱）。
5. **部署后必须真机复验 File 段视频**：群里发一条视频（本平台是以**群文件**投递的），
   工具返回 `ok:true` + `kind:"video"`；同一视频第二次引用 ⇒ `cached:true` + `cache_age_s:<n>`。
   这正是 v0.1 零成功的那个形态，不复验等于没修。

### 状态与已知限制

- **库内门禁（v0.2.0 交付面，历史）**：`e9ad82b` 起 **442 passed / 0 failed / 0 skipped / 0 xfail**；
  `tests/` 18 个文件已入库（含 `test_version.py` 的版本/仓库守卫、`MAPPING-v02.md` 的 113 条旧用例
  逐条映射）。**当前最新**是 v0.3.0 的 **695 passed @ `44aa2c8`**（代码/测试基线，见上文 v0.3.0 条目）。
- **上游仓库**：`https://github.com/cmdst/astrbot_plugin_media_analysis` 公开、默认 `main`；
  **`main` 已于 2026-10-23 09:39 推送到 `d9628a4`**（远端 sha == 本地，`git ls-remote` + 匿名
  `clone` 双向复核）。v0.3.0 的推送**不在本版**，归下一单（推送目标见 v0.3.0 派单 §10）。
- **生产已部署 v0.2.0**（2026-10-23 23:10:20 加载，23:59:02 再次重载）：42 个文件（含
  `__pycache__`）、插件配置 **1,594 B / BOM / 11 键**，`max_media_seconds=300`、
  `audio_provider_id` 与 `vision_provider_id` 均非空；`data/plugin_data/<插件>` **不存在**
  ⇒ 上线至今**零成功、零缓存、零流量**（`ok kind=` 0 次；全日志 2 次 `rejected` 都在 v0.1 时代）。
- **真 provider 调用**：v0.2 本身未跑；**v0.3 的 t4 验证实跑过**（Gemini / mimo）——
  原生视频读出画面事实、48.9 s 音轨用 Gemini 转写 260 字、**同一音轨在 mimo 上 60 s 超时
  `AUDIO_FAILED`**（`VERIFY-v0.3.md` §2 / §3.4）。mimo 当音频位时，48 s 级视频会稳定失败。
- **已知遗留（v0.2.1，不是缺陷）**：迁移 `--apply` 丢 BOM（功能无影响，字节审计要留神）；
  视频的 `audio_extracted` 与 `audio_normalized` 两条 note 语义重复（噪声）；`.file` / `get_file()`
  的禁令只有运行时探针、无静态 AST 守卫；迁移器双入口名（实现 + shim）会让文档慢慢漂；
  `file_download_needed` 与 `_resolve_file_path` 双份分支判定（只影响 note）；
  `DiskCache.clear()` 目前没有生产调用者。

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

### 已被 v0.2 覆盖的 v0.1 事实（留档，别照抄）

- v0.1 的"STT provider 五级回落阶梯"与 `STT_UNAVAILABLE` / `STT_FAILED` 两个码**整段删除**；
  音频改由多模态 chat provider 的 `text_chat(audio_urls=…)` 承担（错误码换成
  `AUDIO_UNAVAILABLE` / `AUDIO_FAILED` / `PROVIDER_CAPABILITY_MISMATCH`）。
- v0.1 的进程内 `ResultCache`（LRU 32、不落盘）**已删**，换成带 TTL 的 `DiskCache`
  （`data/plugin_data/<插件名>/cache/v1`，只存信封文本）。
- v0.1 的"2 组 4 键"**已扩到 4 组 11 键**（`stt_provider_id` 保留但不生效）。
- v0.1 记录的 `247 passed` / `26 个跟踪文件` / "生产未安装"**全部作废**：
  现在是 **442 passed** / **34 个跟踪文件** / **生产已装 v0.1.0**（详见上一节）。

## v0.1.0 — 2026-10-22（**未发布 / 未安装**）

首个版本。单工具 `media_analyze`，音轨优先 + 抽帧按需。

> 状态：库 `main` 的 **v0.1 代码/测试基线** = 提交 `16e0eea`（9 笔提交）；本文件与 `README.md` 的
> 修订作为其后的**文档提交**落库（只动这两份文档，代码面逐字节不变）。**无远端、未 push**；生产
> `/root/AstrBot/data/plugins/astrbot_plugin_media_analysis` **不存在**，`pin-check.sh` 无本插件条目
> ⇒ 「未安装」是实测事实，不是"大概没装"。安装归 fix + 人，派单见
> `/root/workspace/astrbot/handoff/lanes/dev/20261022-order-fix-deploy-media-analysis-v0.1.0.md`。
> （**后话**：2026-10-23 00:58 该派单被执行，生产装上了 v0.1.0；2026-10-23 23:10:20 换到 v0.2.0。
> 上面这句只对 2026-10-22 成立；当前生产状态见本文件顶部 v0.3.0 条目与 `AGENTS.md` §4。）

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
