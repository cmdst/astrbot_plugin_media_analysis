# astrbot_plugin_media_analysis

上游仓库：<https://github.com/cmdst/astrbot_plugin_media_analysis>（v0.3.0）

给 LLM 一个工具 `media_analyze`：把**当前消息或引用消息**里的语音/视频（**含以「群文件」
形式投递的 mp4/m4a/silk…**）交给多模态模型。**视频的主路径是「原生视频直送」**——
整段 mp4 以内联 base64 一次调用同时拿画面与声音；只有**视频模型**不可用（没配 / 非
`openai_chat_completion` 适配器 / L1 未通过）、超过直送门槛且压不下去、原生调用失败时，
才**回退**到「抽帧 + 独立的图片模型」。返回固定形状的 JSON 信封；
解析结果**本地缓存带 TTL**，同一媒体二次引用不再花 token。

存在的理由很朴素：AstrBot 里**没有任何 provider 支持 `video` 模态**（`modalities` 含
`video` 的 0 个），而 `Record` 到手已是本地 wav、`Video` 一行 `convert_to_file_path()`
就是本地文件、群文件一行 `file_`/`url` 就能拿到 —— 硬件条件齐了，缺的只是
"把视频降维成模型能吃的东西"这一段编排。

## v0.3 改了什么

| # | 变更 | 一句话 |
|---|---|---|
| ① | **视频主路径改原生直送** | `client.chat.completions.create(messages=[text, video_url(base64 data url)])`；核心 `text_chat` 放不进 video 块（`assemble_context` 硬抛 `ValueError`）⇒ 走 provider 底层 `client`，三条护栏见下 |
| ② | **抽帧降级为回退** | 原生不可用 / 超门槛压不动 / 调用失败时才抽帧；`vision_provider_id` 语义收窄为"仅回退用的图片模型" |
| ③ | **三模型位** | `video_provider_id`（原生视频，**新增**）+ `vision_provider_id`（仅抽帧）+ `audio_provider_id`（音轨） |
| ④ | **超门槛先压缩** | 默认 8 MiB 直送门槛（按**延迟**定，不是 provider 上限）；ffmpeg 480p + ABR + AAC 64k mono，最多 2 趟，产物只落 `/tmp` |
| ⑤ | **L1 可见性探测** | 4 s 合成小样片 + "数字=?,单词=?"（**双 token 都命中**才算过，200 ≠ 看见）；探测结论缓存 7 天，每窗口最多自动 1 次 |
| ⑥ | **`include_frames` 变三态** | `auto`（默认）/`true`/`false` 字符串；`false` + 原生失败 ⇒ **绝不静默抽帧** |
| ⑦ | **配置 11 → 21 键** | 新增 `video` 组 8 键 + `video_provider_id` + `prompts.video_native`；旧 11 键一个不动，`tools/migrate_config_v03.py` 补默认并审计 |

**三条护栏（原生路径碰的是核心内部面 `provider.client`，不是公开 API）**：
1. `max_tokens` 硬下限 **1024**（实测有模型在 64 时返回 200 但正文为空，全被推理吃掉）；
2. **空 `content`/哨兵判失败并回退**（`[无可用内容]` / `[无语音]` 等同空文本）；
3. 自建 `asyncio.wait_for`（`min(native_timeout_seconds, 剩余预算−35 s)`），底层调用**绕过**
   核心的超时/重试。整块包在 `try/except Exception` 里：拿不到 `client`、适配器类型不符、调用
   抛错 ⇒ 只记 `video_native_unavailable`/`video_native_failed` **回退**，绝不让它变成新的
   "静默拒载"点。

**不做**：体积上限阶梯探测（实测两条通道到 47.2 MiB / 36.5 MiB 都 200 ⇒ "体积上限"不是事实，
先撞的是延迟）、视频 URL 引用、非 mp4 容器、用 `modalities` 判视频能力（核心没有 `video` 选项）。

### 三模型位（v0.3）

视频链路上有**三个独立的 provider 位**（都在 `provider_settings` 组，字符串 id，空串＝不指定）：

| 位 | 键 | 用在哪 | 空串语义 |
|---|---|---|---|
| **视频（原生）** | `video_provider_id`（**v0.3 新增**） | 原生视频直送：一次调用同时要"画面 + 声音" | **不回落**核心当前对话 provider —— 视频是"声明式"能力，没声明就走回退（`video_native_unavailable`），不对未知模型盲发 8 MiB |
| **视觉（仅回退）** | `vision_provider_id` | 抽帧回退：每帧一张图；摘要也可复用它 | 回落核心当前对话 provider（v0.2 语义不变） |
| **音频** | `audio_provider_id` | 音轨转写：原生不可用/失败后的保底，也是纯语音消息的主路径 | 回落核心当前对话 provider（不支持音频 ⇒ `AUDIO_UNAVAILABLE`） |

**视频位的解析阶梯**（任一步失败都只"回退"，不判死整次调用）：

1. 键为空 ⇒ 无原生通道（note `video_native_unavailable`）；
2. `get_provider_by_id` 取不到 ⇒ `PROVIDER_NOT_FOUND`；不是 `Provider` 实例 ⇒ `PROVIDER_CAPABILITY_MISMATCH`；
3. 适配器不是 `openai_chat_completion`，或实例拿不到 `client.chat.completions`（**运行期 `getattr` 兜底**，
   属性抛异常也只算"没有原生通道"）⇒ `video_native_unavailable`，**不判错**；
4. 探测开启且 L1 未通过（缓存 `l1.ok=false` 且未过期）⇒ 本次无原生通道（notes 记 `video_probe_l1_failed`；
   **不再另加** `video_native_unavailable`，两者是不同成因）；
5. 其余 ⇒ 可用（note `video_provider:<id>`；显式配置另有 `video_asserted`）。

显式配置**不等于**能力证明：配置只算"声明"，真正能不能看见由 L1 实测（下一节）。
视频位为空时**不做**"回落核心对话 provider 猜视频能力"——那等于对未知模型盲发大文件。

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
| `include_frames` | **string** | **`"auto"`** | 三态：`auto`＝要画面、插件自己选路（**未配视频模型时不抽帧**，只转写）；`true`＝明确要画面（原生失败必须抽帧回退）；`false`＝明确不要画面（原生失败**绝不抽帧**）。旧布尔值仍接受 |

> **`target` 不接受文件路径**（v0.2 明确裁决）：模型读的是"用户可控文本 + 框架注入的
> 路径文本"，接受路径就等于给模型一个"读任意本地文件并送进远端模型"的原语。
> 群里以**文件**形式发来的视频/语音，同样用 `current` / `quoted` 取 —— docstring 与
> `BAD_TARGET` 的 hint 都写了这一句。

**默认走 `auto`**：视频优先**原生直送**（一次调用同时拿画面与声音）；原生不可用、且配了
视觉位时**才回退抽帧**（`true` 点名要画面，回退必须发生；`false` 明确不要画面，绝不抽帧）。
不做"检测到无音轨就自动改道"这类隐式分支：隐式分支＝不可预测的成本。

`include_frames` 在 v0.3 是**字符串三态**（旧布尔值仍接受）。门面按白名单归一
（`main._as_bool` / `_as_mode`；字符串先 `strip`、再按**大小写不敏感**比对）：

| 输入 | 判为 | 说明 |
|---|---|---|
| `"auto"` / `"true"` / `"false"` | 原样 | 大小写不敏感、去空白 |
| `True` / `False`（旧客户端） | `true` / `false` | — |
| `"1" "yes" "on"` | `true` | — |
| `"" "0" "false" "no" "off"` | `false` | — |
| `None` | `auto` | — |
| 其它：`2.5` / `{}` / `"maybe"` / 未知类型 | **`auto`** | 每次调用打一条 `WARNING`（带类型名，不带值）。v0.3 起**不再**默认 `false` |

保守方向是刻意的：**误发值只该少抽帧，不该多花钱** —— 按"非零即真"猜会让一次误发变成
`max_frames` 次视觉调用；落到 `auto` 最坏只是这次没抽帧（原生路径仍会试）。

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
`error` 取值是 **21 条**错误码的封闭集合（v0.3：既有 18 条逐字未动 + `VIDEO_UNAVAILABLE` /
`VIDEO_FAILED` / `VIDEO_TOO_LARGE`；见 `envelope.py`），`hint` 是**常量**文案
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

## 回退七跳（v0.3）

`[0]` 取件判型是 v0.2 的既有链路（`source:file` / `file_downloaded`），**不在七跳里**。
下面每一跳的 `notes` 与终态码都是实现里的常量；照这张表读日志就能定位"卡在哪一跳"。

| # | 跳 | 触发 / 判据 | 动作 | notes（成功 / 失败） |
|---|---|---|---|---|
| ① | **解析视频位** | 见上一节的五步阶梯 | 有原生通道 ⇒ ②；没有 ⇒ ⑤ | `video_provider:<id>` / `video_asserted` / `video_native_unavailable` |
| ② | **L1 可见性探测**（仅首次） | 缓存 miss 或过期；`video.probe_enabled=true` 且 `probe_ttl_seconds>0` | 4 s 合成样片 + "数字=?单词=?"，**两条都命中才算过** | `video_probe_cached` / `video_probe_l1_ok` / `video_probe_l1_failed` / `video_probe_disabled` / `video_skip_budget` |
| ③ | **直送闸 → 压缩** | `size > video.max_direct_bytes` 且 `compress_enabled=true` | ffmpeg 480p + ABR + AAC 64k mono，最多 **2 趟**；预算 = `min(compress_target_bytes, max_direct_bytes)` | `video_compressed:<from>-><to>`（命中）；`video_compress_skipped` / `video_compress_infeasible` / `video_compress_failed`，然后 ⑤ |
| ④ | **原生调用** | 通道可用且（≤门槛 或 压缩命中） | `client.chat.completions.create(messages=[text, video_url(data:video/mp4;base64,…)])`，`max_tokens≥1024` | `video_native`；失败/超时 `video_native_failed`、200 但空文本 `video_native_empty` ⇒ ⑤ |
| ⑤ | **回退（`include_frames` 三态）** | ①无通道 / ④失败 / ③压不动 | `false` ⇒ **绝不抽帧**；`auto` 且无视频模型 ⇒ 只转写；`auto` 且无视觉位 ⇒ 只转写；其余 ⇒ 抽帧 | `video_frames_forbidden` / `video_unavailable_no_provider` / `video_no_vision_provider` / `video_fallback_frames` + `vision_provider:<id>` / `video_skip_budget` |
| ⑥ | **摘要** | 有转写或画面 | 复用视觉位；材料超 8000 字符从尾部截断；摘要失败不判死整次调用 | `material_trimmed` |
| ⑦ | **信封** | — | 成功 9 键 / 失败**恰好 3 键** | — |

- **有音轨时的默认结局永远是 `ok:true` + 转写**：⑤ 的所有"不抽帧"分支都只降级，不把整次调用判死。
  只有"**无音轨**且不得抽帧"才落到 `VIDEO_UNAVAILABLE` / `VIDEO_FAILED` / `VIDEO_TOO_LARGE`。
- **`false` 是最硬的一条**：`auto` 的"允许回退"只对 `auto` 生效，`false` 是用户的显式成本约束
  ⇒ 原生失败也**不抽帧**（note `video_frames_forbidden`）。
- **`auto` 且未配视频模型**（评审 F7）⇒ 不抽帧、退化为"只转写"（note
  `video_unavailable_no_provider`）：视觉位早就配好的部署不会因为升级 v0.3 而把每条视频
  从 0 次视觉调用抬到 6 次。`mode=true` 是用户点名要画面，不受此限。
- **预算饿死**：原生之后必须留出抽帧的最小预算（`NATIVE_RESERVE_S=35` ≥
  `FRAMES_MIN_BUDGET_S=25`）；剩余预算不够 ⇒ `video_skip_budget`，`auto/false` 按设计降级，
  只有 `mode=true` 才算"想要却没拿到"。
- **音轨超时也分流**（评审 R1/R2）：本轮**可能抽帧**（`mode=true` 且有视觉位/视频流，或
  `auto` + 视频模型 + 视觉位 —— 判据是 `frames_possible()`）⇒ 音轨按 `remaining − 25 s` 取超时，超时只记
  note `audio_timeout` 并**继续画面阶段**；**不要画面**（语音消息 / `mode=false` / 无回退画面）⇒
  超时仍 `AUDIO_FAILED`（主诉求是转写，不能假成功）。**provider 抛错**（4xx/5xx/安全过滤）
  一律 `AUDIO_FAILED`，不走降级 —— 那说明模型侧真出问题，不是"预算不够"。

## 提示词（`prompts.*`，5 份）

| 键 | 用在哪 | 可用占位符 |
|---|---|---|
| `prompts.audio` | 语音消息的音轨转写 | `{focus_clause}` |
| `prompts.video` | **视频**的音轨转写 | `{focus_clause}` |
| `prompts.frame` | 每一帧的画面描述 | `{focus_clause}` |
| `prompts.video_native` | **原生视频**（一次调用同时要"画面 + 声音"） | `{focus_clause}` |
| `prompts.summary` | 最后一步摘要 | `{kind_cn}` `{task}` `{material}` |

- **留空＝内置默认**（面板上看到的就是默认文本）。
- **写坏不生效**：非法花括号、未知占位符（超出上表）⇒ 回退默认值 + 一次 `WARNING`；
  超 2000 字符 ⇒ 截断并加 note `prompt_trimmed:<key>`。
- **焦点永不被丢**：模板里没有 `{focus_clause}` 而用户给了 `focus` ⇒ 把焦点从句追加到末尾，
  并加 note `focus_appended:<key>`。
- 摘要的 `{task}` 是"通篇概括"或"定向回答"的固定指令，`{material}` 是材料正文；
  材料超 8000 字符从**尾部**截断（先保转写），记 note `material_trimmed`。
- **改提示词会让缓存失效**：缓存键里含 5 份生效模板的指纹（`prompts_fp`）。
- 原生视频的回答恰为 `[无可用内容]`（或 `[无语音]`）⇒ 等同空文本 ⇒ 记 `video_native_empty` 并回退。

`[无语音]` 是**提示词约定的哨兵**：模型认为整段无人声时只输出它。插件侧把"空文本"与
`[无语音]` 都归一成 `transcript: []` + note `audio_empty`（**不是错误**）。

## 配置（5 组 / 21 键）

v0.3 新增 **`video` 组（8 键）** + `provider_settings.video_provider_id` +
`prompts.video_native`；v0.2 的 11 个键**名字/类型/默认值一个都没动**（新 schema 是超集
⇒ 用户旧配置零迁移可用）。键表（**21 个叶子键**，与 `settings.KEY_MAP`、
`main._settings()`、`_conf_schema.json`、迁移脚本 `KEY_ORDER` **四处互钉**）：

| 组 | 键 | 类型 | 默认 | 说明 |
|---|---|---|---|---|
| `provider_settings` | `audio_provider_id` | string | `""` | **v0.2 新增**。音频（多模态对话）provider，模型能力必须勾了「音频」；空＝回落核心当前对话 provider（不支持音频 ⇒ `AUDIO_UNAVAILABLE`） |
| `provider_settings` | `vision_provider_id` | string | `""` | 读图的对话 provider；空＝核心当前对话 provider |
| `provider_settings` | `video_provider_id` | string | `""` | **v0.3 新增**。原生视频模型（需要 `openai_chat_completion` 适配器且真的接受视频输入）；空＝不走原生，视频改走抽帧。显式配置 ⇒ note `video_asserted`，但仍由 L1 实测验证 |
| `provider_settings` | `stt_provider_id` | string | `""` | **【已弃用】** v0.2 起不生效，只为保留历史设置而留在 schema 里（删了会被核心静默删除并落盘）；值非空时打一条弃用 `WARNING`（不打值） |
| `prompts` | `audio` / `video` / `frame` / `summary` / `video_native` | text | 内置模板 | 4 份 v0.2 新增；`video_native` **v0.3 新增**（原生视频的“画面+声音”提示词），见「提示词」节 |
| `limits` | `max_media_seconds` | int | `60` | 超过即 `MEDIA_TOO_LONG`；允许 5–300（越界钳位 + 告警） |
| `limits` | `max_frames` | int | `6` | `include_frames=true` 时最多抽几帧；允许 1–12 |
| `limits` | `max_media_bytes` | int | `67108864`（64 MiB） | **v0.2 新增**。群文件取件的硬闸，超限 `MEDIA_TOO_LARGE`；允许 1 MiB–512 MiB |
| `video` | `probe_enabled` | bool | `true` | **v0.3 新增**。L1 自动探测开关（关＝信任显式声明，省一次调用） |
| `video` | `probe_ttl_seconds` | int | `604800`（7 天） | **v0.3 新增**。探测结果有效期；`0`＝关闭探测（不读不写）；允许 0–2592000 |
| `video` | `max_direct_bytes` | int | `8388608`（8 MiB） | **v0.3 新增**。**按延迟定的**直送门槛（不是 provider 上限）；超了先压缩。允许 1–32 MiB（32 MiB 同时是内存硬上限） |
| `video` | `native_timeout_seconds` | int | `60` | **v0.3 新增**。单次原生调用上限，且 ≤ 剩余预算−**35 s**（保住抽帧最小预算 `FRAMES_MIN_BUDGET_S=25` + 余量）；允许 20–110 |
| `video` | `native_max_tokens` | int | `2048` | **v0.3 新增**。**只能调大**（硬下限 1024，防止"200 但空正文"）；允许 1024–4096。**mimo 当视频模型时必须设 4096**（实测 8 MiB + 长提示词在 1024/2048 下都会 200 但正文为空） |
| `video` | `compress_enabled` | bool | `true` | **v0.3 新增**。超门槛时是否先压缩（压缩远比抽帧便宜） |
| `video` | `compress_target_bytes` | int | `8388608`（8 MiB） | **v0.3 新增**。压缩预算上界；实际取 `min(它, max_direct_bytes)`；允许 512 KiB–32 MiB |
| `video` | `compress_timeout_seconds` | int | `30` | **v0.3 新增**。两趟转码共享这一个预算；允许 10–60 |
| `cache` | `ttl_seconds` | int | `86400`（1 天） | **v0.2 新增**。缓存 TTL；`0`＝关闭缓存；允许 0–2592000（30 天） |

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

## 缓存（v0.2 新增；v0.3 键版本 → `v3`）

- **位置**：`<AstrBot 根>/data/plugin_data/astrbot_plugin_media_analysis/cache/v1/<前两位>/<64hex>.json`
  —— 每插件一个同名子目录，由核心 `get_astrbot_plugin_data_path()` 给出，**永不**落插件目录、
  库里或 `data/temp`；目录**懒创建**（第一次写入时才有）。
- **键版本 `v3`（12 个分量）**：`v3 | digest | kind | mode | max_frames | prompts_fp | focus |
  audio_provider_id | video_provider_id | vision_provider_id | video_fp | PLUGIN_VERSION`。
  `mode`（`auto/true/false`）与 `video_fp`（直送门槛/压缩开关/压缩预算的指纹）是 v0.3 新增的
  两个分量 —— 同一媒体在三种模式下产物不同、改门槛即失效，**不许互相命中**。
  旧的 `v2` 条目不写迁移代码：键变了自然不命中，按 TTL 老化淘汰。
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

## 压缩（v0.3；超直送门槛时）

- **触发**：`size > video.max_direct_bytes` 且 `video.compress_enabled=true`（默认 true）。
- **字节预算** = `min(video.compress_target_bytes, video.max_direct_bytes)`（默认 `min(8 MiB, 8 MiB)`
  = 8,388,608 B）。源已在预算内 ⇒ **0 次转码**（8 MiB 档对 48.9 s / 5.64 MB 的样本就是 SKIP）。
- **目标码率**：`floor(预算×8 / 时长 / 1000) − 64`（64 = AAC 音频固定开销）。`< 64 kbps` ⇒
  `video_compress_infeasible`，不硬转（48.9 s 源在 512 KiB 预算下就会落这一支）。
- **参数（冻结）**：`scale=-2:480`（720p→480p，竖屏 720x1280 → 270x480）+ libx264 `veryfast`
  + **ABR 双向钳制**（`-b:v` = `-maxrate`，bufsize 2×）+ `-pix_fmt yuv420p` +
  AAC **64 kbps 单声道** + `+faststart`；源无音轨则去掉音频三参（note `video_compressed_noaudio`）。
- **迭代上限 2 趟**：第 1 趟超预算 ⇒ 第 2 趟按 `kbps × 预算/实际 × 0.95` 修正；仍超 ⇒
  `video_compress_failed`（不跑第 3 趟：收益趋零、时间锯齿上涨）。
- **产物只落 `/tmp`**（`tempfile.mkdtemp(prefix="media_analysis_")`，`finally` 整目录删）；
  **源文件只读**，绝不覆盖、绝不写回 `data/`；压缩前先 `stat` 判体积（不把大文件读进内存）。

| 预算档（48.901 s / 5,640,723 B 生产样本，只读） | 转码 | 视频码率 | 产物 | 耗时 |
|---|---|---|---|---|
| 8 MiB（默认） | **0 趟**（源已达标） | — | 5,640,723（原样直送） | 0 ms |
| **4 MiB** | **1 趟** | **622k**（实现公式按 48.901 s；t8 用 48 s 整数算得 635k） | **3,873,915 B**（t4 复测；t8 首测 3,939,056 B） | **4,207 ms**（t4；t8 4,810 ms） |
| 2 MiB | 2 趟 | 285k → 269k | 2,098,819 → **2,008,617 B** | 4,455 + 4,870 ms |
| 1 MiB（t4 三跳信封用的预算） | 2 趟 | — | **1,031,801 B** | 含在 24.4 s 端到端内 |

- 4 MiB 档的实测：**720x1280 → 270x480、时长不变、音轨保留（AAC 64k 单声道）、压缩比 0.687**；
  产物在 `/tmp` 工作目录内，脚本退出后目录已删，源文件 sha/mtime 前后一致。
- **为什么压而不直接抽帧**：压缩 **~4.2 s / 0 token**；抽帧是 6 帧 ffmpeg + **6 次视觉调用**。
  原生一次调用同时拿画面 + 声音，信息不丢 ⇒ 对"就想知道视频里发生了啥"的主诉求，压缩比抽帧便宜。
- **失败都只进下一跳**：ffmpeg rc≠0 / 超时 / 产物空 / 产物 ≥ 源 ⇒ `video_compress_failed` ⇒ ⑤ 回退。

## 视频能力探测缓存（v0.3 新增）

**只做 L1（"这个模型真的能看见视频吗"），不做体积阶梯**：200 ≠ 看见，体积上限也不存在
（见「实测风险」）。判据是**双 token**——4 s / 1280x720 / ~150 KB 合成样片，画面正中一个
4 位白色数字、右下角一个黄色英文单词（8 词固定表，逐字母无歧义）：

- 提问固定：`视频画面正中有一个很大的白色数字，右下角有一个黄色英文单词。只回答这两项：数字=?,单词=?。不要解释。`
- **数字与单词两条都命中才算过**（数字按字符串匹配、全角先归一；单词大小写不敏感）；
  答错、答"看不到视频"、空文本、超时、异常一律 `l1.ok=false`。
- 单次超时 `L1_TIMEOUT_S=30`（t8 实测 Gemini 3.2 s / mimo 18.4 s）；`max_tokens=1024`（防"200 但空正文"）。

- **位置**：`data/plugin_data/astrbot_plugin_media_analysis/probe/v1/<64hex>.json`，键含
  `provider_id + model + PLUGIN_VERSION`（升版本天然失效）；**首次视频调用时才创建目录**。
  条目字段：`v` / `provider_id` / `model` / `plugin_version` / `l1.{ok,reason,at,elapsed_ms}` /
  `latency.{p50_ms,size_bytes,at}|null` / `updated_at`。
- **只存能力事实**：**不存媒体内容、不存 base64**（与结果缓存同一条禁令）。
- **TTL 默认 7 天**（`video.probe_ttl_seconds`，`0`＝关闭探测：既不读也不写；与结果缓存的
  1 天**互不影响**）。TTL 按结论类型分流（评审 F3）：真裁决（`mismatch`/`empty`）用配置值；
  **本地环境类**（`no_font`/`ffmpeg_missing`/`build_failed`）最多 **1 小时**；**通道类**
  （4xx/5xx/timeout/dns）**0＝不缓存**。
- **成本保护**：每个 TTL 窗口**最多自动跑 1 次**（失败也写 `l1.ok=false` 占位，防打爆）；
  落盘失败静默降级为进程内记忆（仍然只跑 1 次）。
- **通道失败 ≠ 裁决**（`VERIFY-v0.3.md` F1 的教训）：请求带与核心 HTTP 客户端同形状的
  `User-Agent`（缺 UA 会被 Cloudflare 回 403 `error code: 1010`，那不是"模型看不见"）。
  插件读缓存时遇到"通道类 `reason`"**视同 miss 重新实测**；写入侧通道失败**绝不落盘**，
  只做进程内 60 s 冷却（冷却过期即重测）。
- **进程内记忆最长 5 分钟**：`_MEMO` 命中要过两道时效（条目 TTL + `MEMO_MAX_AGE_S`）⇒
  `--clear` / `rm -rf probe/` 对**运行中**的插件最多 5 分钟生效。
- **怎么清**：`rm -rf data/plugin_data/astrbot_plugin_media_analysis/probe`，或
  `python3 tools/probe_video_capability.py --provider <id> --clear`（磁盘 + 进程内记忆一起清）。

### 延迟校准工具（`tools/probe_video_capability.py`）

```bash
python3 tools/probe_video_capability.py --provider <id>                 # L1 + 1 MiB×3
python3 tools/probe_video_capability.py --provider <id> --sizes 1,4,8 --repeat 3
python3 tools/probe_video_capability.py --provider <id> --show           # 只看缓存（不联网）
python3 tools/probe_video_capability.py --provider <id> --clear          # 清该 provider 的缓存
```

- 输出 `suggested_max_direct_bytes` = **p50 ≤ 45 s 的最大档**（100 s 全局预算，留 ≥55 s 给后续步骤）。
- **一次调用可能数分钟**（1 MiB≈10–32 s、8 MiB≈43–75 s）⇒ 别在生产高峰跑。
- 只读 `data/cmd_config.json`；**不打印 key、不打印 base64**（只打字节数与耗时）；
  只写 `probe/v1/` 缓存，不碰插件配置、不 import 插件模块。
- **档位标称尺寸有 −7%~+16% 偏差**（工具用码率逼近体积）⇒ 报告/引用尺寸**一律用实测字节**。
- **通道类失败不写缓存**：响应分三类 —— `ok` / `rejected`（200 但正文空，是裁决）/
  `channel_error`（4xx/5xx、DNS、连接、超时）；最后一类**不落盘**、退出码 **`2`**
  （`0`＝有裁决、`1`＝用法/配置错误）。`--show` 会标 `channel_class=yes|no`；
  插件读到通道类条目会**视同 miss 重新实测**。

## 实测风险（v0.3，必读）

下面三条是**真机实测**的口径（`VERIFY-v0.3.md`，2026-10-24），它们不是"待办"，是运维与排障时
不要踩的坑：

1. **代理存在瞬时 `http 400` 窗口（与尺寸无关）**：同一批探测档第一次跑（02:06–02:13）
   **五档全 400，连 1 MiB 都 400**；4 分钟后同批重测 **全 200**（13.6 / 36.6 / 49.0 / 93.2 / 208.4 s）。
   ⇒ **不得把单次 400 当 SLA 或能力结论**；链路遇到 400 会记 `video_native_failed` 并按规定回退，
   行为正确，重试可能就好了。也**别**把"8 MiB 直送"写成 SLA。
2. **mimo 建议 `native_max_tokens=4096`**：8 MiB + 长提示词在 **1024 / 2048** 下会 200 但
   正文为空（`video_native_empty`，推理把预算吃光）。护栏会判失败并回退（**不会假成功**），
   但等于白花一次调用钱。默认 2048 只够 Gemini 类通道；**mimo 请设 4096**，
   或把 `video.max_direct_bytes` 留在 **4 MiB** 档。
3. **原生视频没有体积上限，但有延迟上限**：Gemini 到 47.2 MiB、mimo 到 36.5 MiB 都 200
   （0 次拒绝）；但 8 MiB ≈ **45.6 / 49.0 / 50.4 s**（±10% 抖动）、16 MiB 93.2 s、32 MiB 208.4 s，
   而全局预算只有 **100 s** ⇒ **>8 MiB 直送不安全**（"能过"不等于"来得及"）。默认门槛
   `video.max_direct_bytes = 8 MiB`（钳 **1–32 MiB**，32 MiB 同时是内存硬上限）是**按延迟定的**，
   不是 provider 上限。

> 另有两条已登记的行为（不是缺陷）：① CLI 的标称档位与实际字节差 −7%~+16%（引用用实测字节）；
> ② 空文本哨兵是**精确串**匹配（`[无可用内容]` / `[无语音]`）—— 模型回个近似说法（如
> `[无用内容]`）会被当成"原生成功"，见 `CHANGELOG.md` 遗留 F6。

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

### v0.2 → v0.3 的顺序（**不可换**）

```
① 备份（插件目录 + 插件配置） → ② 覆盖插件目录（新代码 + 新 schema 超集） → ③ 重载
   → ④ （可选）python3 tools/migrate_config_v03.py --apply → ⑤ 再重载一次 → ⑥ 验证
```

```bash
python3 tools/migrate_config_v03.py --root /root/AstrBot            # 干跑（默认；21 键逐条）
python3 tools/migrate_config_v03.py --root /root/AstrBot --report   # 只读逐键报告
python3 tools/migrate_config_v03.py --root /root/AstrBot --apply    # 备份(0600) + 原子落盘
```

- **必须"代码先行"**：v0.2 的 schema 不认识 `video.*`，先跑新迁移器的值会被旧 schema 当
  stale **删掉并落盘**（`astrbot_config.py:252-266`）。v0.3 的新 schema 是**超集**（只加键）
  ⇒ 顺序是"覆盖代码 → 重载 → 迁移"（与 v0.2 同一条规则）。
- **迁移脚本只补 10 个新键的默认值 + 审计**：本机生产是 v0.2 的 11 键配置 ⇒ 干跑应看到
  **11×`keep` + 10×`add`**、`summary: changed=1`；`--apply` 先写 **0600 备份**、保留原权限位、
  原子落盘（注意：与 v0.2 迁移器一样**丢 BOM**，核心读 `utf-8-sig`，功能无影响）。
- **回滚必须成对且反向**：回滚 v0.3 → v0.2 时新 schema 的 10 个新键会被 v0.2 当 stale 删掉，
  所以**同时**恢复①插件目录②配置备份，并再重载一次。只回滚代码 = 静默丢设置。
- **验证（四件必验 + 三处位置）**：
  ① ready 行含 `video_provider_id=` / `video.max_direct_bytes=… native_timeout_s=… native_max_tokens=…` /
     `compress=on target=… probe=on ttl=…`；
  ② **原生被看见**：真群视频 ⇒ `ok:true` / `kind:"video"` / notes 含 `video_native`（首次另有
     `video_probe_l1_ok`，或 `video_probe_disabled`）/ `summary` 非空；`frames=0`、`transcript=[]` 是
     **设计如此**（原生一次调用同时拿画面与声音）；
  ③ **压缩生效**：超门槛视频 ⇒ notes `video_compressed:<from>-><to>` + `video_native`；
  ④ **`mode=false` 不偷抽帧**：同一视频 `include_frames="false"` ⇒ `frames=0`，notes **没有**
     `video_fallback_frames` / `vision_provider:<id>`（原生失败时则应是 `video_frames_forbidden`）。
- **三处首次生成位置**（部署前都不存在，属正常）：
  `data/plugin_data/astrbot_plugin_media_analysis/probe/v1/<64hex>.json`（首次原生视频调用）、
  `…/cache/v1/<2hex>/<64hex>.json`（首次成功信封落盘）、
  `…/cache/v1/` 目录本身（懒创建）。
- **从 v0.1 直跳 v0.3 不在本版支持范围**：`stt_provider_id` 的搬值仍归
  `tools/migrate_config_v02.py`（带能力校验），先 v0.1→v0.2 再 v0.2→v0.3。本机生产已是 v0.2.0。
- 本版完整派单（备份 / 覆盖 / 重载 / 迁移 / 再重载 / 复验 / 回滚）在 dev lane：
  `/root/workspace/astrbot/handoff/lanes/dev/20261024-order-fix-deploy-media-analysis-v0.3.0.md`。

### v0.1 → v0.2 的顺序（**不可换**）

```
① 备份（插件目录 + 插件配置） → ② 覆盖插件目录（新代码 + 新 schema） → ③ 重载（让新 schema 生效）
   → ④ 迁移脚本（干跑 → --apply） → ⑤ 再重载一次（运行中的插件持有的是内存配置） → ⑥ 验证
```

```bash
python3 tools/migrate_config_v02.py --root /root/AstrBot                    # 干跑（默认；逐键 + summary）
python3 tools/migrate_config_v02.py --root /root/AstrBot --apply            # 备份(0600)+原子落盘
python3 tools/migrate_config_v02.py --root /root/AstrBot --report-legacy    # 旧键何时可删（只读）
```

> 脚本有两个名字：**实现是 `tools/migrate_config_v02.py`**（`v02` 带前导零，SPEC 与测试都指它），
> `tools/migrate_config_v2.py` 是等价的转发 shim（派单文字里出现过这个名字）。两者输出与退出码一致。
> 没有 `--dry-run` 开关：**不带 `--apply` 就是干跑**。

- **反序会丢设置**：先迁移、后上代码时，**旧 schema** 不认识新键，下次加载会把
  `provider_settings.audio_provider_id` 当 stale 键**删掉并落盘**（核心
  `check_config_integrity()` + `save_config()`，已用真核心复现，不是推测）。
- **第 ⑤ 步不能省**：插件把配置快照存在内存里（`main._settings_cache`，`initialize()` 时装配），
  迁移改了磁盘文件但**运行中的实例看不到**。同理，**迁移后到重载前不要在面板上保存本插件配置**
  —— 核心会把内存里的旧 dict 写回，覆盖迁移结果。
- 脚本**不造新键**（缺失的新键由核心 `check_config_integrity` 在加载时补默认并落盘），它负责
  **备份 + 搬值 + 回滚锚点**。所以正常现场（旧值为空）**预判 `changed=0`：不写盘、不产生备份**
  ⇒ 回滚锚点必须来自第 ① 步的手工 `cp -a`，不能指望脚本的 `.bak`。
- 搬值**带能力校验**：旧 `stt_provider_id` 语义上是 STT id，只有"该 id 是 chat 类且
  `modalities` 明确含 `audio`"才搬到 `audio_provider_id`，否则**原样留在旧键**并给原因码
  （`not_found` / `not_chat` / `no_audio`，退出码 2 等人工）。
- 任何模式都**不打印配置值**（只有键名/动作/类型/长度）。
- **落盘不改权限**：备份固定 0600；**目标配置文件保留原权限位**（生产是 0600 ⇒ 迁移后仍是
  0600，不会被放宽成默认的 644）；目标不存在时才按 0600 新建。
- **落盘丢 BOM**（`--apply` 用 `json.dumps(...)` + `utf-8` 重写；核心自己读 `utf-8-sig`
  ⇒ 功能无影响）。做**字节级**审计时注意：`--apply` 之后文件不再以 `EF BB BF` 开头。
- **回滚成对且反向**：先回代码 → 再回配置（`--rollback <备份>` 或直接覆盖） → 重启
  （`v0.1` 的 schema 不认识新键，先回配置会被旧 schema 删；`v0.2` 的 schema 认识旧键，
  所以"先进可退、后缀可回"，两个方向都不丢值）。缓存可顺手清掉
  （`rm -rf data/plugin_data/astrbot_plugin_media_analysis/cache`，无副作用）。

完整派单（含备份/回滚/校验命令）在 dev lane 的交接地
`/root/workspace/astrbot/handoff/lanes/dev/20261024-order-fix-deploy-media-analysis-v0.3.0.md`
（v0.2 的历史派单：`20261023-order-fix-deploy-media-analysis-v0.2.0.md`，已于 2026-10-23 23:10:20 执行）。

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
