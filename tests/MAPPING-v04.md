# MAPPING-v04 —— v0.4 测试面台账（SPEC-v0.4 §9 矩阵 → 实际用例 + 偏差登记）

> 对象：库 `main` @ **`6b775ab`**（v0.4.0 实现）+ 本轮 `tests/` 改动
> 基线：`6b775ab` 的门禁 **697 passed**（v0.3 基线 `44aa2c8` 是 695；+2 来自 `background.py`
> 让两条按文件参数化的守卫各多一例）
> 本轮门禁：**798 passed / 0 failed / 0 skipped / 0 xfail**（+101 cell；1 warning 仍是核心
> `tencent_record_helper` 的 `audioop` Deprecation，与本插件无关）
> 命令（固定，与 `AGENTS.md` §3 同一条）：

```bash
cd /root/workspace/projects/astrbot_plugin_media_analysis && \
  PYTHONDONTWRITEBYTECODE=1 /root/AstrBot/.venv/bin/python -m pytest tests/ -q -p no:cacheprovider
```

## 0. 一句话结论

v0.4 的两条主契约（**GIF 以视频解析**、**工具调用后台异步**）在 `6b775ab` 里**没有测试覆盖**
（t2 只做了必要适配）。本轮补 **101 个 cell**：新文件两个（`test_gif.py` 55 / `test_background.py` 34）、
既有文件三处（`test_analyze.py` +8 / `test_media.py` +2 / `test_registration.py` +2）、
新夹具一个（`tests/_gif.py`，零依赖手工 GIF 编码器）。**实现侧只修了一个缺陷**（见 §4），
没有为过测放宽任何断言。

## 1. 新增 / 改动的文件

| 文件 | cell | 守什么 |
| --- | --- | --- |
| `tests/_gif.py`（新，夹具） | — | 零依赖手工 GIF 编码器（GIF87a/89a + GCE + Netscape 循环 + LZW + 子块）+ 真 PNG/APNG、JPEG/WebP 反例字节 |
| `tests/test_gif.py`（新） | **55** | 魔数判据两版；PNG/APNG/JPEG/WebP/文本反例（后缀也写成 `.gif`）；候选优先级（Record→Video→Image(GIF)→File）；远程 `Image` 的后缀门槛；`confirm_gif` 三分支 + 幂等；冻结 argv 逐字符；四道闸各自的码 + "字节闸在 ffmpeg 之前"/"超时/失败 unlink 半成品"；真 ffmpeg 的 241 宽奇数样本（输出 240）；单帧 ⇒ `gif_static`；PNG/APNG 的 `codec_type=video` 事实守卫；真链路端到端（Image 与 File 两来源、送出去的必须是 mp4 装箱） |
| `tests/test_background.py`（新） | **34** | 受理 4 键/形状/延迟上界/受理路径 0 重 IO（spy）；投递恰好一次、`False` 兜底一次、抛错只 WARNING 不重试、取消不投递、完成不二次投递；`ToolError`/异常/取消三条失败面；去重（同消息同参 / 参数不同 / 无 `message_id`）；会话闸 3 / 全局闸 6；`terminate()` 真取消（`task.cancelled()`）且幂等；状态机 `delivered|undelivered`；诊断缓冲有界；`wait()` 超时语义；日志只带 8 位 `task_id`；事件循环不被阻塞；`render()` 三形态 + 截断 + 无运行时数据；受理信封不入缓存；notes 缓存中立 |
| `tests/test_analyze.py` | **+8** | GIF 分支接线：`kind=video` + notes 三条 + **跳过 `classify`**；`[3.5] confirm` 必须早于 `[4-pre] normalize`（spy 顺序）；源时长闸在归一之前；归一失败 ⇒ 后台投递 `GIF_CONVERT_FAILED` 失败消息；缓存 digest 取**源**字节（换产物仍命中 / 换源必 miss，两侧都断言）；Image 与 File 同字节共享缓存；远程 `.gif` 下载后魔数不符 ⇒ `MEDIA_NOT_FOUND`；`gif_static` 跟源帧数走 |
| `tests/test_media.py` | **+2** | 形状回归：`find_media` 是协程（`confirm_gif` 仍同步）；`MediaHandle.gif` 默认 `False`、`gif=True` ⇒ `kind="video"` |
| `tests/test_registration.py` | **+2** | docstring 必须写清"立即返回 + 后台自动发送 + 别再调用 + 受理 4 键"；`ACCEPT_HINT` 常量纪律（无数字/路径/占位符/`task_id`） |
| `CHANGELOG.md` | 1 段 | 登记 §4 的实现缺陷修复；顺手校正"受理 4 键与失败 3 键键名不重叠（除 ok）"这句话（实际还共用 `hint`） |

## 2. SPEC-v0.4 §9 矩阵 → 落点对照

| 矩阵要求 | 落点 |
| --- | --- |
| 魔数双判据（87a/89a/大小写/不足 6 字节） | `test_gif.py::test_has_gif_magic_*`（3 条，含 5 条 parametrize） |
| 后缀只作提示 | `test_looks_like_gif_is_only_a_hint`（7 组 parametrize）+ `test_find_media_remote_image_needs_gif_suffix` |
| Image 本地 / 远程两态 | `test_find_media_accepts_gif_image_with_gif_suffix` / `_without_any_suffix` / `_remote_image_needs_gif_suffix` / `test_remote_gif_image_is_rejected_after_download_when_magic_fails` |
| `File .gif` 转正 | `test_confirm_gif_promotes_file_gif_and_demotes_its_lookalike` + `test_file_segment_gif_is_treated_as_video_not_as_raw_bytes` |
| PNG/APNG/WebP/JPEG 全部拒绝（§5.5 反例逐行） | `test_find_media_rejects_plain_images`（4 条 parametrize）+ `test_png_and_apng_probe_as_video_but_are_still_not_gif` + `test_file_lookalike_gif_falls_back_to_the_old_ladder` |
| 静态 GIF 的 `gif_static` | `test_normalize_gif_single_frame_is_static` / `test_gif_static_note_tracks_the_source_frame_count` / `test_gc_less_static_gif_is_accepted_and_marked_static` |
| 三道闸各自的码 | `test_normalize_gif_source_byte_gate_*`（`MEDIA_TOO_LARGE`）/ `test_gif_source_duration_gate_*`（`MEDIA_TOO_LONG`）/ `test_normalize_gif_timeout_gate_*`、`_nonzero_rc_*`、`_empty_output_*`、`_product_duration_unavailable_*`（`GIF_CONVERT_FAILED`）/ `test_normalize_gif_output_byte_gate_*` |
| argv 逐字符快照 | `test_gif_argv_is_frozen_verbatim` |
| `trunc(.../2)*2` 防回归（M9 的 rc=187） | `test_gif_argv_forces_even_width_and_never_truncates_duration`（字面量）+ `test_normalize_gif_real_conversion_forces_even_width`（真 241 宽 ⇒ 输出 240、产物非空） |
| 受理 4 键与顺序 / `state` 枚举 | `test_background.py::test_accept_envelope_has_exactly_the_four_frozen_keys` + `test_registration.py::test_envelope_exports_the_frozen_key_orders`（t2 落） |
| 去重（同 task_id / 只起 1 个 / 无 `message_id` 不去重） | `test_dedupe_same_message_same_params_returns_same_task_id` / `_dedupe_key_includes_mode_and_focus` / `_dedupe_is_disabled_without_message_id` |
| 两级并发闸 ⇒ `TOO_MANY_TASKS` | `test_session_cap_is_three` / `test_global_cap_is_six` / `test_synchronous_rejections_create_no_task` |
| 取消（`terminate` ⇒ cancelled、不投递；含停止信号/上层取消 ⇒ `CancelledError` 上抛） | `test_terminate_cancels_running_jobs_and_delivers_nothing` / `test_cancelled_job_is_never_delivered` / `test_cancel_during_delivery_sends_no_second_message` / `test_cancel_all_returns_count_and_is_idempotent` / `test_stop_signal_cancelling_the_tool_call_is_not_swallowed` |
| 投递一次（spy 计数 = 1） | `test_completion_is_delivered_exactly_once` / `test_completion_does_not_deliver_again_later` |
| `False` ⇒ 兜底且总数仍 1 | `test_send_message_false_falls_back_once_and_total_stays_one` |
| 投递抛错 ⇒ 只 WARNING 不重试 | `test_send_message_exception_warns_and_never_retries`（含"异常文本不进日志"） |
| `render()` 三形态与截断 | `test_render_*`（5 条，纯函数） |
| notes 缓存中立（同步 vs 后台） | `test_notes_are_cache_neutral_between_sync_and_background` |
| **必测防静默三条** | ① spy `context.send_message`：见 `test_completion_*`/`test_send_message_*`；② `task.cancelled()` 为真：`test_terminate_*`/`test_cancel_during_delivery_*`；③ 受理路径 0 ffmpeg/provider：`test_accept_path_touches_no_heavy_io`（包住**真** analyze 的闸门，放开后计数才变 1 —— 证明探针真的接上了） |

## 3. 测试策略（为什么这些用例不会"看起来绿"）

- **零真实群媒体**：GIF/PNG/APNG 全部由 `tests/_gif.py` 现场编码（字节可精确控制），
  库内不落任何媒体；中间产物只落 `tmp_path`（`/tmp` 下）。
- **需要 ffmpeg 的用例统一 `skip`**（`_require_ffmpeg()`，本机有 `/usr/bin/ffmpeg` 所以真跑）：
  真解码、真归一、真 `probe_streams`。无 ffmpeg 的机器上这些用例**跳过而不是失败**，
  其余 40+ 条 GIF 用例仍全绿（闸/argv/判据都是纯逻辑）。
- **时间纪律**（`test_background.py`）：后台用例用 `asyncio.Event` 当闸门，不 sleep 等结果；
  唯一一次 `asyncio.sleep(0.2)` 是"事件循环没被阻塞"的对照，整文件真实 sleep < 1 s、
  整文件耗时 < 1 s（15 s 全量门禁里占比很小）。
- **不许"测死代码"**（`AGENTS.md` §9-10）：受理路径的 0 计数用"包住真 analyze 的闸门 +
  放开后计数变 1"两侧证明；`confirm_gif`/`normalize_gif` 的顺序用 spy 断言。

## 4. 实现缺陷修复（本轮唯一一处实现改动，同笔提交）

**现象**：没有 GCE 块的单帧 GIF（GIF87a 老图；`/usr/share/doc/**/*.gif` 抽样 8/9 如此）
`ffprobe` 给不出 `format.duration`（实测 `N/A`）⇒ `media.probe_duration_s` 抛 `PROBE_FAILED`
⇒ 整条 GIF 链在 `[4-pre]` 的**时长闸**上就死了。SPEC-v0.4 §5.5 的"单帧 GIF ⇒ 受理 → 视频链 +
`gif_static`"因此对这类输入**落不了地**，§5.9 的失败阶梯里也没有 `PROBE_FAILED` 这一行。

**修复**（`media.probe_gif_duration_s` + `analyze` 一行改调用，2 处）：
只把"时长缺失"（`log_detail == DURATION_UNAVAILABLE`）降级成 `0.0`；ffprobe 真坏
（缺失/超时/异常）仍 `PROBE_FAILED`。`0.0` 安全的理由：闸②是**上界**闸，无时长只可能来自
"单帧/延迟全 0"这类极短源；真实时长随后由产物 mp4 提供（那次探测失败仍
`GIF_CONVERT_FAILED`）。

**覆盖**：`test_probe_gif_duration_treats_gc_less_static_gif_as_zero`（真 ffprobe，含 0.1 s
的对照）、`test_probe_gif_duration_keeps_real_probe_failures`（只降级那一种）、
`test_gc_less_static_gif_is_accepted_and_marked_static`（端到端行为级）。

## 5. 偏差与遗留（登记，不藏）

1. **文件落点**：SPEC §9 把 GIF 的媒体层用例分配给 `tests/test_media.py`（+6）。本轮集中在
   **新文件 `tests/test_gif.py`**（同一批断言 + 更全），`test_media.py` 只补 2 条接口形状回归。
   理由：GIF 的判据/归一/闸是一个**自洽的契约面**，散在 media 的老文件里会与"文件/流探测"的
   用例混读；幂等与优先级用例若两处都写，必然漂（本项目 §9-4 的老教训）。
2. **SPEC §3.1 / CHANGELOG 原话"受理 4 键与失败 3 键键名不重叠（除 `ok`）"不准确**：
   实际还共用 `hint`（`FAIL_KEYS=("ok","error","hint")`）。判别靠 `state` 与键数（4/9/3），
   所以"一次断言判定形状"仍成立。测试按**事实**断言（`{ok, hint}` 交集），CHANGELOG 已校正；
   `SPEC-v0.4.md` 的这句话留给评审裁决是否改文本（本 lane 不动冻结规格）。
3. **`File` 无 `.gif` 后缀但内容确为 GIF** 仍走 v0.3 的三段梯子（候选需要后缀才能"去证实"，
   SPEC §5.2 的候选规则如此）。⇒ 这类文件仍可能被 `_classify_file` 判成 video 后把**原始 GIF
   字节**按 `data:video/mp4` 直送（v0.3 缺陷的残留形态）。本地 `File` 其实已有字节、嗅探零成本，
   建议 v0.4.1 把 `File` 的候选条件放宽成"有字节就嗅 6 字节"；本版**未改**（属规格变更）。
4. **`gif_static` 的语义边界**：源帧数读不出（`nb_frames` 缺失）时按"非静态"处理（不误标）；
   这是 v0.4 代码的既有取舍，测试只用真样本（1 帧/8 帧）两侧钉住。
5. **后台任务无持久化**（SPEC §10.2 已否决）：进程重启丢任务，测试不覆盖（属产品行为）。
