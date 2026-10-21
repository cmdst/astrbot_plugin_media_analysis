# astrbot_plugin_media_analysis

给 LLM 一个工具 `media_analyze`：把**当前消息或引用消息**里的语音/视频先做音轨转写，
需要时再按需抽帧交视觉模型描述画面，返回固定形状的 JSON 信封。

存在的理由很朴素：AstrBot 里 **26 个 provider 中没有任何一个支持 `video` 模态**
（`modalities` 含 `image` 19 个、含 `audio` 6 个、含 `video` 0 个），而 `Record` 到手
已是本地 wav、`Video` 一行 `convert_to_file_path()` 就是本地文件 —— 硬件条件齐了，
缺的只是"把视频降维成模型能吃的东西"这一段编排。

## 工具契约

单个工具、三个参数（全部有默认值，schema 由 docstring 生成，**没有 `required`**）：

| 参数 | 类型 | 默认 | 语义 |
|---|---|---|---|
| `target` | string | `"current"` | 只认 `current`（当前消息）/ `quoted`（被引用的那条消息） |
| `focus` | string | `""` | 想了解的重点问题；空＝通篇摘要 |
| `include_frames` | boolean | `false` | 只对视频有意义。`true`＝抽帧并描述画面 |

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
  "notes": ["stt_provider:mimo_stt", "vision_provider:gpt-4o"],
  "elapsed_ms": 4213,
  "cached": false
}
```

失败信封**恰好 3 键**：`{"ok": false, "error": "MEDIA_TOO_LONG", "hint": "…"}`。
`error` 取值是 16 条错误码的封闭集合（见 `envelope.py`），`hint` 是**常量**文案
（不夹带路径/异常文本/时长/provider id）。**任何分支都不会把异常抛给模型**；唯一例外是
`asyncio.CancelledError`（任务被取消时向上传播，否则框架无法取消正在跑的工具）。

两条容易混淆的错误码：`MEDIA_NOT_FOUND`＝真的没有语音/视频（含 `Reply.chain` 为空、
`event` 上压根没有 `get_messages`）；`MEDIA_UNRESOLVABLE`＝组件**在**但取不到文件，
**或**读消息链本身就抛异常（坏链不伪装成"没有媒体"）。

`transcript` 在 v0.1 恒为**单项** `t=0.0`：`STTProvider.get_text` 只返回整段文本，
没有时间戳，伪造时间戳就是编数据。`frames[].at_s` 是**真**时间戳（抽帧坐标）。

## 配置（2 组 / 4 键）

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `provider_settings.stt_provider_id` | string | `""` | STT provider；空＝按五级阶梯回落（配的 id → 核心当前 STT → 当前 STT 实例 → 任一已加载 STT → `STT_UNAVAILABLE`） |
| `provider_settings.vision_provider_id` | string | `""` | 读图的对话 provider；空＝核心当前对话 provider |
| `limits.max_media_seconds` | int | `60` | 超过即 `MEDIA_TOO_LONG`；允许 5–300（越界钳位 + 告警） |
| `limits.max_frames` | int | `6` | `include_frames=true` 时最多抽几帧；允许 1–12 |

插件内**没有** API key 字段：模型一律走 AstrBot 的 provider 引用。缓存是内部实现
（内容 `sha256[:16]` + 参数指纹 → 内存 LRU(32)，命中回 `cached: true`），**不是旋钮**，
也不会落盘。

> **别把上闸 300 s 读成"能转 300 s"**：`limits.max_media_seconds` 把越界值钳到 5–300，
> 300 只是**防手滑的上闸**，不是能力承诺。本机实测（60 s / 1280×720 合成视频）：
> 视频 → 16 kHz 单声道 wav = **1,920,090 B（1.83 MiB）**，而 STT 链路**不做二次转码**、
> 直接把 wav 塞进 base64 data URL ⇒ 实际 POST body = wav × 4/3：
> **60 s → 2.44 MiB**、**300 s → 12.21 MiB**。300 s 已贴着/超过多数远端 ASR 接口 10 MB 的
> 请求体上限（provider 侧报错 ⇒ `STT_FAILED`，用户看到的是"配置允许、实际必失败"）。
> **建议留在默认 60 s**；要改大先按「4/3 × wav 体积」算一遍上传体，别只看秒数。

> **兼容性注意**：预处理阶段的全局 STT（`provider_stt_settings.enable`）与本工具**互斥**。
> 一旦打开，核心会把 `Record` 替换成 `Plain`（`preprocess_stage/stage.py:266-282`），
> 本工具在语音上必然 `MEDIA_NOT_FOUND`。

## 部署

1. 目录名必须**逐字**等于 `metadata.yaml` 的 `name`（`astrbot_plugin_media_analysis`）。
2. `astrbot_version` 是 PEP 440 range：`>=4.29,<5`（当前核心 4.29.1 在内）。
3. **人格工具白名单**：`persona["tools"]` 是 JSON 文本 —— `null`＝不限制工具集（本插件
   **自动可见，免改 DB**）、非空数组＝只给列出的名字（**必须把 `media_analyze` 加进去**，
   否则工具对模型不可见）。**先确认 QQ 群体实际绑定的 persona 再决定要不要改**：
   本机实测（`VERIFY.md` §5-F3，只读 `personas.tools` 的长度/布尔）23 个 persona 中
   **7 个是 `null`**、**16 个是固定清单（4–50 项，无一含 `media_analyze`）**。
   改人格＝写生产 DB，归 fix + 人，且**启动时载入内存、改完要重启**。
4. 需要 `ffmpeg` / `ffprobe` 在 `PATH` 上（本机在 `/usr/bin`）。
5. 覆盖安装前**整目录备份**。
6. 上线后按这三处查注册：`star.star_manager`（Loading）→ `provider.func_tool_manager:390`
   （`Added llm tool: media_analyze`）→ 插件注册行；**加载失败只出现在**
   `grep -a 'Failed to import plugin astrbot_plugin_media_analysis'` 里，别去搜工具名。

完整派单（含备份/回滚/校验命令）在 dev lane 的交接地：
`/root/workspace/astrbot/handoff/lanes/dev/20261022-order-fix-deploy-media-analysis-v0.1.0.md`。

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

中间产物（转码 wav、抽帧 jpg）只落 `/tmp` 下 `tempfile.mkdtemp(prefix="media_analysis_")`
创建的目录，用完即删；不落库、不落工作区、不进生产树。

## 非目标（都是决定，不是待办）

- 不接 URL / 分享链接 / 合并转发（只吃会话内媒体）。
- 图片不进 v0.1（19 个 provider 自带 `image`，主模型自己就能吃）。
- 不做消息监听自动分析（每条群消息都跑＝给每条消息上税），只做 LLM 工具向。
- 不做权限闸门（成本由 `limits.*` 封顶）。
- 一次工具调用只处理**一条**媒体（当前消息或引用消息里的第一条 `Record`/`Video`）。

## 许可

MIT © 2026 大肥鱼。
