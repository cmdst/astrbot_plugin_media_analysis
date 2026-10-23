# astrbot_plugin_media_analysis

上游仓库：<https://github.com/cmdst/astrbot_plugin_media_analysis>（v0.2.0）

给 LLM 一个工具 `media_analyze`：把**当前消息或引用消息**里的语音/视频（**含以「群文件」
形式投递的 mp4/m4a/silk…**）先做音轨转写，需要时再按需抽帧交视觉模型描述画面，
返回固定形状的 JSON 信封；解析结果**本地缓存带 TTL**，同一媒体二次引用不再花 token。

存在的理由很朴素：AstrBot 里**没有任何 provider 支持 `video` 模态**（`modalities` 含
`video` 的 0 个），而 `Record` 到手已是本地 wav、`Video` 一行 `convert_to_file_path()`
就是本地文件、群文件一行 `file_`/`url` 就能拿到 —— 硬件条件齐了，缺的只是
"把视频降维成模型能吃的东西"这一段编排。

## v0.2 改了什么

| # | 变更 | 一句话 |
|---|---|---|
| ① | **群文件（`File` 段）支持** | 本平台群视频以「群文件」投递，v0.1 只认 `Record`/`Video` ⇒ 上线至今**零成功**；v0.2 认 `File`，扩名白名单 + 内容探测双保险 |
| ② | **音频改走多模态对话模型** | 不再依赖 `STTProvider`：`text_chat(prompt=…, audio_urls=[本地 16 kHz 单声道 wav])` |
| ③ | **语音/视频各自独立提示词** | 4 份可配提示词：`prompts.audio` / `prompts.video` / `prompts.frame` / `prompts.summary` |
| ④ | **结果本地缓存带 TTL** | 落 `data/plugin_data/<插件名>/cache/v1/`，默认 1 天，命中回 `cached: true`（0 token） |
| ⑤ | **配置 4 键 → 11 键 + 迁移脚本** | 旧键 `stt_provider_id` 保留在 schema 里（不删＝不丢设置），`tools/migrate_config_v02.py` 负责搬值 |

## 工具契约

单个工具、三个参数（全部有默认值，schema 由 docstring 生成，**没有 `required`**）：

| 参数 | 类型 | 默认 | 语义 |
|---|---|---|---|
| `target` | string | `"current"` | 只认 `current`（当前消息）/ `quoted`（被引用的那条消息） |
| `focus` | string | `""` | 想了解的重点问题；空＝通篇摘要 |
| `include_frames` | boolean | `false` | 只对视频有意义。`true`＝抽帧并描述画面 |

> **`target` 不接受文件路径**（v0.2 明确裁决）：模型读的是"用户可控文本 + 框架注入的
> 路径文本"，接受路径就等于给模型一个"读任意本地文件并送进远端模型"的原语。
> 群里以**文件**形式发来的视频/语音，同样用 `current` / `quoted` 取 —— docstring 与
> `BAD_TARGET` 的 hint 都写了这一句。

**默认只做音轨转写**，`include_frames=true` 才抽帧（不做"检测到无音轨就自动改道"
这类隐式分支：隐式分支＝不可预测的成本）。

`include_frames` 收的是布尔，但**模型经常发字符串**（`"false"` 在 Python 里是真值）。
门面按真值表归一：真＝`"1"/"true"/"yes"/"on"/非零数`，假＝`""/"0"/"false"/"no"/"off"/0`，
其余非法值**告警一次并按默认 `False`**（`main._as_bool`）—— 不会因为模型手滑就静默多抽
`max_frames` 帧、多花 `max_frames` 次视觉调用。

成功信封（9 键，顺序固定）：

```json
{
  "ok": true,
  "kind": "video",
  "duration_s": 58.32,
  "transcript": [{"t": 0.0, "text": "…"}],
  "frames": [{"at_s": 4.86, "desc": "…"}],
  "summary": "…",
  "notes": ["source:file", "audio_extracted", "audio_normalized",
            "audio_provider:…", "vision_provider:…"],
  "elapsed_ms": 4213,
  "cached": false
}
```

失败信封**恰好 3 键**：`{"ok": false, "error": "MEDIA_TOO_LONG", "hint": "…"}`。
`error` 取值是 **18 条**错误码的封闭集合（见 `envelope.py`），`hint` 是**常量**文案
（不夹带路径/异常文本/时长/provider id）。**任何分支都不会把异常抛给模型**；唯一例外是
`asyncio.CancelledError`（任务被取消时向上传播，否则框架无法取消正在跑的工具）。

三条容易混淆的错误码：

- `MEDIA_NOT_FOUND` ＝ 消息里**真的没有** `Record`/`Video`/`File`（`log_detail` 会带上
  链里实际出现的组件类型名，例如 `chain=[Plain, Reply]`，排障时先看这一条）；
- `UNSUPPORTED_MEDIA_TYPE` ＝ **`File` 段**在，但内容既解不成音频也探不出视频流
  （`.pdf`/`.zip`/改了扩展名的垃圾）—— 与 `PROBE_FAILED`（`Record`/`Video` 的 ffprobe
  失败）分开，因为 hint 该说的是"这不是媒体"，不是"媒体坏了"；
- `MEDIA_UNRESOLVABLE` ＝ 组件**在**但取不到文件（`File` 的 `file_` 与 `url` 都空、
  我们自己下载失败/超时、或读消息链本身就抛异常）。

`transcript` 恒为**单项** `t=0.0`：多模态模型只返回整段文本，没有时间戳，伪造时间戳就是
编数据。`frames[].at_s` 是**真**时间戳（抽帧坐标）。

## 提示词（`prompts.*`，4 份）

| 键 | 用在哪 | 可用占位符 |
|---|---|---|
| `prompts.audio` | 语音消息的音轨转写 | `{focus_clause}` |
| `prompts.video` | **视频**的音轨转写 | `{focus_clause}` |
| `prompts.frame` | 每一帧的画面描述 | `{focus_clause}` |
| `prompts.summary` | 最后一步摘要 | `{kind_cn}` `{task}` `{material}` |

- **留空＝内置默认**（面板上看到的就是默认文本）。
- **写坏不生效**：非法花括号、未知占位符（超出上表）⇒ 回退默认值 + 一次 `WARNING`；
  超 2000 字符 ⇒ 截断并加 note `prompt_trimmed:<key>`。
- **焦点永不被丢**：模板里没有 `{focus_clause}` 而用户给了 `focus` ⇒ 把焦点从句追加到末尾，
  并加 note `focus_appended:<key>`。
- 摘要的 `{task}` 是"通篇概括"或"定向回答"的固定指令，`{material}` 是材料正文；
  材料超 8000 字符从**尾部**截断（先保转写），记 note `material_trimmed`。
- **改提示词会让缓存失效**：缓存键里含 4 份生效模板的指纹（`prompts_fp`）。

`[无语音]` 是**提示词约定的哨兵**：模型认为整段无人声时只输出它。插件侧把"空文本"与
`[无语音]` 都归一成 `transcript: []` + note `audio_empty`（**不是错误**）。

## 配置（4 组 / 11 键）

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `provider_settings.audio_provider_id` | string | `""` | 音频（多模态对话）provider，模型能力必须勾了「音频」；空＝回落核心当前对话 provider（不支持音频 ⇒ `AUDIO_UNAVAILABLE`） |
| `provider_settings.vision_provider_id` | string | `""` | 读图的对话 provider；空＝核心当前对话 provider |
| `provider_settings.stt_provider_id` | string | `""` | **【已弃用】** v0.2 起不生效，只为保留历史设置而留在 schema 里（删了会被核心静默删除并落盘） |
| `prompts.audio` / `prompts.video` / `prompts.frame` / `prompts.summary` | text | 内置模板 | 见上一节 |
| `limits.max_media_seconds` | int | `60` | 超过即 `MEDIA_TOO_LONG`；允许 5–300（越界钳位 + 告警） |
| `limits.max_frames` | int | `6` | `include_frames=true` 时最多抽几帧；允许 1–12 |
| `limits.max_media_bytes` | int | `67108864`（64 MiB） | 群文件取件的硬闸，超限 `MEDIA_TOO_LARGE`；允许 1 MiB–512 MiB |
| `cache.ttl_seconds` | int | `86400`（1 天） | 缓存 TTL；`0`＝关闭缓存；允许 0–2592000（30 天） |

插件内**没有** API key 字段：模型一律走 AstrBot 的 provider 引用。
`modalities` 取**严格口径**（`None`/缺省 ⇒ 不支持）：核心在两处按"未配置＝全支持"兜底，
而音频链路上"未配置"与"真支持"在 provider 层无法区分 —— 取宽松口径会把"音频根本没发出去"
记成 `ok:true`。给 provider 勾上「音频」即可，**不需要改插件代码**。

> **别把上闸 300 s 读成"能转 300 s"**：300 只是防手滑，不是能力承诺。wav × 4/3 就是
> base64 上传体积：**60 s → 2.44 MiB**、**300 s → 12.21 MiB**（多数远端接口 10 MB 上限）。
> 建议留在默认 60 s。

> **兼容性注意**：预处理阶段的全局 STT（`provider_stt_settings.enable`）与本工具**互斥**。
> 一旦打开，核心会把 `Record` 替换成 `Plain`（`preprocess_stage/stage.py:266-282`），
> 本工具在语音上必然 `MEDIA_NOT_FOUND`。

## 缓存（v0.2 新增）

- **位置**：`<AstrBot 根>/data/plugin_data/astrbot_plugin_media_analysis/cache/v1/<前两位>/<64hex>.json`
  —— 每插件一个同名子目录，由核心 `get_astrbot_plugin_data_path()` 给出，**永不**落插件目录、
  库里或 `data/temp`；目录**懒创建**（第一次写入时才有）。
- **只存信封文本**：条目里只有 `{v, key_inputs, created_at, envelope}`，`envelope` 恰好 9 键。
  媒体字节、base64、抽帧 jpg、wav 路径**一律不入缓存**（写盘前有一道硬闸：序列化文本里
  出现 `/tmp/`、`/root/`、`data:` 就拒绝写入并告警）。
- **TTL**：默认 1 天；读时用**当前** TTL 判过期（改小立即生效）。`ttl_seconds=0` ＝关闭。
- **容量**：≤ 256 条、总 ≤ 16 MiB、单条 ≤ 256 KiB（超了跳过写，**绝不截断**）；超限按
  `mtime` 最旧优先淘汰，命中会刷新 `mtime`（＝LRU）。
- **降级**：损坏 / 过期 / 权限 / `ENOSPC` / 写失败 ⇒ 一律"当未命中"或"本进程禁用缓存"，
  **不产生 notes、不产生错误码**（缓存是内部优化，主链路永不受影响）。
- **并发**：同键 `singleflight`（两个并发调用同一媒体只有一个花 token）；跨进程不处理
  —— 本部署单进程，多进程最坏是双方都算一遍，文件不会坏（同分区 `os.replace` 原子）。
- **命中语义**：`cached: true` + `elapsed_ms` 是**本次**真实耗时 + `notes` 追加
  `cache_hit`、`cache_age_s:<n>`（原始 notes 保留在前）。
- **怎么清**：`rm -rf data/plugin_data/astrbot_plugin_media_analysis/cache`（无副作用，之后全 miss）。
- **会进备份**：核心备份清单包含整个 `data/plugin_data` ⇒ 缓存条目会出现在备份包里
  （正文就是日志里已有的那类文本，TTL 默认 1 天）。需要更严的留存策略就调小
  `cache.ttl_seconds` 或直接关掉（`0`）。

## 部署与迁移

1. 目录名必须**逐字**等于 `metadata.yaml` 的 `name`（`astrbot_plugin_media_analysis`）。
2. `astrbot_version` 是 PEP 440 range：`>=4.29,<5`（当前核心 4.29.1 在内）。
3. **人格工具白名单**：`persona["tools"]` 是 JSON 文本 —— `null`＝不限制工具集（本插件
   **自动可见，免改 DB**）、非空数组＝只给列出的名字（**必须把 `media_analyze` 加进去**，
   否则工具对模型不可见）。**先确认 QQ 群体实际绑定的 persona 再决定要不要改**：
   本机实测（`VERIFY.md` §5-F3，只读 `personas.tools` 的长度/布尔）23 个 persona 中
   **7 个是 `null`**、**16 个是固定清单（4–50 项，无一含 `media_analyze`）**。
   改人格＝写生产 DB，归 fix + 人，且**启动时载入内存、改完要重启**。
4. 需要 `ffmpeg` / `ffprobe` 在 `PATH` 上（本机在 `/usr/bin`）；silk 解码依赖核心自带的
   `pysilk`（核心 `tencent_record_helper` 已带）。
5. 覆盖安装前**整目录备份**（插件目录 + `data/config/astrbot_plugin_media_analysis_config.json`）。
6. 上线后按这三处查注册：`star.star_manager`（Loading）→ `provider.func_tool_manager:390`
   （`Added llm tool: media_analyze`）→ 插件注册行；**加载失败只出现在**
   `grep -a 'Failed to import plugin astrbot_plugin_media_analysis'` 里，别去搜工具名。

### v0.1 → v0.2 的顺序（**不可换**）

```
① 覆盖插件目录（新代码 + 新 schema） → ② 重启/重载 → ③ 跑迁移脚本 → ④ 验证
```

```bash
python3 tools/migrate_config_v02.py --root /root/AstrBot              # 干跑：逐键 + summary
python3 tools/migrate_config_v02.py --root /root/AstrBot --apply      # 备份(0600)+原子落盘
python3 tools/migrate_config_v02.py --root /root/AstrBot --report-legacy   # 旧键何时可删
```

- **反序会丢设置**：先迁移、后上代码时，**旧 schema** 不认识新键，下次加载会把
  `provider_settings.audio_provider_id` 当 stale 键**删掉并落盘**。
- 脚本**不造新键**（缺失的新键由核心 `check_config_integrity` 在加载时补默认），它负责
  **备份 + 搬值 + 回滚锚点**。搬值**带能力校验**：旧 `stt_provider_id` 语义上是 STT id，
  只有"该 id 是 chat 类且 `modalities` 明确含 `audio`"才搬到 `audio_provider_id`，否则
  **原样留在旧键**并给原因码（`not_found` / `not_chat` / `no_audio`，退出码 2 等人工）。
- 任何模式都**不打印配置值**（只有键名/动作/类型/长度）。
- **落盘不改权限**：备份固定 0600；**目标配置文件保留原权限位**（生产是 0600 ⇒ 迁移后仍是
  0600，不会被放宽成默认的 644）；目标不存在时才按 0600 新建。
- **回滚成对**：回代码 → `--rollback <备份>` → 重启（`v0.1` 的 schema 不认识新键，
  先回配置会被旧 schema 删；`v0.2` 的 schema 认识旧键，所以两个方向都不丢值）。

完整派单（含备份/回滚/校验命令）在 dev lane 的交接地
`/root/workspace/astrbot/handoff/lanes/dev/`。

## 隐私与日志

**本插件的转写正文与画面描述会作为工具返回值被 AstrBot 框架写入生产日志（INFO），
这是框架行为，不是本插件的额外输出。**（`tool_loop_agent_runner.py:1241` 记参数、
`:1439` 记结果。）插件自身只打错误码、耗时、帧数、字符数、provider id，
**不**打印转写正文或画面描述。

> **兜底脱敏只覆盖"token 形状"（仅降级路径）**：正常路径用核心 `redact_sensitive_text`
> （还认 `sk-…`、`Bearer`、URL 里的 key 等形态，与核心逐字等价）。只有当核心内部面搬家、
> 插件退到本地 `_fallback_redact` 时，规则缩成「**≥24 位连续 ASCII `[A-Za-z0-9_-]` 串 →
> `[REDACTED]`**」⇒ **中文正文、长路径、含 `/ ? :` 的长 URL 都不脱敏**。
> 也就是说降级路径只保证"长 token 不整段落日志"，不保证全面脱敏；导入期若出现
> `核心内部面 … 不可用，已切换到本地兜底脱敏` 这条 `WARNING`，应按"日志脱敏能力下降"对待。

中间产物（转码 wav、抽帧 jpg、**我们为群文件下载的字节**）只落 `/tmp` 下
`tempfile.mkdtemp(prefix="media_analysis_")` 创建的目录，用完即删；不落库、不落工作区、
不进生产树。核心自己下到 `data/temp` 的群文件（`File.get_file()` 的产物）我们**只读不删**
（不是我们的产物，同一次请求里核心可能还要用）。

## 非目标（都是决定，不是待办）

- 不接 URL / 分享链接 / 合并转发（只吃会话内媒体）。群文件是"会话内媒体"，不在此列。
- **不去扫 `data/temp` 猜文件**：只用本次组件的 `file_`，或自己按 `url` 有界下载
  —— 目录里是别的消息留下的残缺品，按名字匹配等于把别人的文件当成本次媒体。
- 图片不进本插件（多数 provider 自带 `image`，主模型自己就能吃）。
- 不做消息监听自动分析（每条群消息都跑＝给每条消息上税），只做 LLM 工具向。
- 不做权限闸门（成本由 `limits.*` 封顶）。
- 一次工具调用只处理**一条**媒体（当前消息或引用消息里的第一条 `Record`/`Video`/`File`）。

## 许可

MIT © 2026 大肥鱼。
