# CHANGELOG

## v0.1.0 — 2026-10-22

首个版本。单工具 `media_analyze`，音轨优先 + 抽帧按需。

- **工具**：`media_analyze(event, target="current", focus="", include_frames=False) -> str`，
  只挂 `@filter.llm_tool`（不叠加 `permission_type`，否则会被当消息处理器）；
  参数全部有默认值，schema 由 docstring 生成（无 `required`）；返回恒为 `str`。
- **媒体取用**：`Record` 到手即本地 wav（核心预处理已写回 `component.path`）、
  `Video` 用 `convert_to_file_path()` 兜底；两处遍历＝`event.get_messages()` 与
  `Reply.chain`（后者只能取一层）。取不到 ⇒ `MEDIA_NOT_FOUND` / `MEDIA_UNRESOLVABLE`。
- **音轨阶段**：STT provider 五级回落阶梯；`get_text(audio_url=本地 wav)`；
  `transcript` 单项 `t=0.0`（`get_text` 无时间戳，不伪造）。
- **抽帧阶段**：`include_frames=true` 才跑；坐标取每等分段**中点**
  （`round((i+0.5)*duration/count, 2)`），缩放 768 px；单帧失败降级为 note，
  全失败才 `FRAME_EXTRACT_FAILED` / `VISION_FAILED`。
- **错误面**：16 条错误码 + 12 条 `notes` 词表 + 成功 9 键 / 失败 3 键信封，
  全部是常量与封闭集合；任何分支都不向模型抛异常（`main` 是唯一异常收口）。
- **预算**：全局 100 s（框架 `tool_call_timeout` 120 s 留 20 s 余量），单步各 10–60 s；
  预算耗尽走 note 降级（`frames_truncated` / `summary_skipped_budget`）而不是框架超时。
- **缓存**：内容 `sha256[:16]` + 参数指纹 → 进程内 LRU(32)，命中回 `cached: true`；
  不加配置键、不落盘（缓存里有转写正文）。
- **配置**：2 组 4 键（`provider_settings.{stt,vision}_provider_id`、
  `limits.{max_media_seconds,max_frames}`），三方一致（schema ⇄ KEY_MAP ⇄ `main._settings()`）。
- **隐私**：代码里不打印转写正文/画面描述；中间产物只落 `/tmp` 且用完即删。

已知限制见 `README.md`「非目标」与 SPEC §13（含"全局 STT 开关与本工具互斥"）。
