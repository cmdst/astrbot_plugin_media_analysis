# MAPPING-v03 —— v0.2 → v0.3 测试面变更台账（77 条失败逐条交账 + 新增覆盖 + 偏差登记）

> 对象：库 `main` @ **`44f9afa`**（v0.3.0 实现；测试面本轮只改 `tests/`）
> 基线：`d9628a4`（v0.2.0，库内门禁 **442 passed**）
> 本轮门禁：**658 passed / 0 failed / 0 skipped / 0 xfail**（1 warning 是核心 `tencent_record_helper` 的 `audioop` Deprecation，与本插件无关）
> 命令（固定，与 AGENTS.md §3 同一条）：

```bash
cd /root/workspace/projects/astrbot_plugin_media_analysis && \
  ASTRBOT_ROOT=/tmp PYTHONDONTWRITEBYTECODE=1 \
  /root/AstrBot/.venv/bin/python -m pytest tests/ -q -p no:cacheprovider
```

## 0. 一句话结论

v0.3 把视频主路径换成"原生直送 + 抽帧回退"，因此**旧套件有 77 条按 v0.2 语义写死的用例必然红**
（它们守的是"视频只转写 / 布尔 `include_frames` / 4 份提示词 / 11 键 / 18 码 / v2 缓存键"）。
本轮把这 77 条**逐条归类**（保留 51 / 改写 26 / 废弃 0），并把套件从 **442 → 658** 个 cell
（净增 **216**：新文件 193 + 既有文件 23），覆盖 v0.3 的新面（探测、原生通道、压缩、
回退矩阵、内存护栏、21 键四处一致）。

**旧套件 0 条被删除**：442 条 v0.2 用例的**意图**全部仍在（422 条连 id 都逐字未变，
20 个 collected id 因参数集/名字随规格变更而换名，见 §1 末尾）。

72 条"失败"之外还有 4 条是**自动扩参**产生的（v0.3 新增两个源码模块 ⇒ 两条按文件参数化的
守卫各 +2）：它们不是 v0.2 的用例，属于"新模块自动纳入既有守卫"。

---

## 1. 77 条失败逐条交账

分类口径（与 v0.2 的 `MAPPING-v02.md` 同构）：

- **保留**：用例名与断言**一字未改**；红的原因是**共用夹具**升级（`build_rig` 的 video 默认
  配 `vision_provider_id`、`FakeNativeProvider`、`_key` 助手补 v0.3 分量）。改夹具 = 一处改动，
  覆盖一片用例，且新夹具本身被新用例直接测（见 §2）。
- **改写**：断言/参数/时钟脚本随 v0.3 规格变更；每条都写明"为什么必须变"。
- **废弃**：能力被删除。**本轮 0 条**（唯一的"能力删除"是任务单里的 L2 阶梯，见 §4）。

### 1.1 `tests/test_analyze.py`（27 条）

| 旧用例 | 归类 | 理由 |
| --- | --- | --- |
| `test_success_envelope_video_with_frames` | 保留 | 断言不变；`build_rig` 给 video 默认配 vision 位（v0.3：回退抽帧以"显式 vision 位"为前提） |
| `test_include_frames_true_triggers_vision` | 保留 | 同上 |
| `test_no_audio_track_with_frames_is_success_with_note` | 保留 | 同上（多一条 `video_fallback_frames`，断言是子串匹配） |
| `test_partial_frame_failure_keeps_success` | 保留 | 同上 |
| `test_all_frame_descriptions_failing_is_vision_failed` | 保留 | 同上 |
| `test_six_failed_descriptions_with_full_budget_still_vision_failed` | 保留 | 同上 |
| `test_vision_provider_capability_mismatch_is_error` | 保留 | 同上（v0.3 仍把 `resolve_vision_provider` 的 `ToolError` 原样上抛） |
| `test_frame_extract_total_failure` | 保留 | 同上 |
| `test_frame_extract_process_timeout_is_still_a_failure` | 保留 | 同上 |
| `test_frame_extractor_leaking_raw_exception_is_internal_error` | 保留 | 同上 |
| `test_frames_and_notes_share_at_s_formatting` | 保留 | 同上（另测 `describe_frames` 的 2 位小数口径） |
| `test_budget_sufficient_path_is_unchanged` | 改写 | 显式 `cfg=Settings(max_frames=6)` 会**覆盖**夹具默认值 ⇒ 补 `vision_provider_id`（v0.3 语义，不是放宽断言） |
| `test_include_frames_flag_is_part_of_cache_key` | 保留 | `false → true` 两次都不命中仍然成立；v0.3 的"`mode` 进键"由 `test_cache.py::test_mode_is_a_component_*` 与 `test_video_chain.py::test_mode_auto_and_false_do_not_share_a_cache_entry` 另钉 |
| `test_include_frames_true_like_values_trigger_vision[true/yes/TRUE/  True  /True]` | 保留 | 白名单不变（`"1"/"yes"/"on"/True ⇒ true`） |
| `test_include_frames_true_like_values_trigger_vision[1_0]`（裸 `int 1`） | 改写 | v0.3 参数类型是 **string 三态**，裸整数不再是布尔（`bool` 是 `int` 子类，但"没按 schema 发值"≠"true"）⇒ 移入 `…invalid_value_falls_back_to_auto…`（回 `auto`，仍抽帧，并告警一次） |
| `test_include_frames_false_like_values_skip_vision["false"/"no"/"0"/"  FALSE  "/"False"/False]` | 保留 | 白名单不变 |
| `test_include_frames_false_like_values_skip_vision[0_0]`（裸 `int 0`） | 改写 | 同上：`int 0` 归 `auto`（旧行为是 `false`，v0.3 **反向**：非法值按 `auto`，避免把"没看画面"伪装成成功） |
| `test_include_frames_invalid_value_falls_back_to_false_with_one_warning[maybe/2.5/1.0/2/-1]` | 改写 | 断言**反转**：非法值 ⇒ `auto` + 抽帧（若配了 vision）+ 恰一条告警；名字改为 `…falls_back_to_auto_with_one_warning`。理由：v0.3 主路径就是"看视频"，默认 `false` 会静默降级成"只转写" |
| `test_include_frames_invalid_value_falls_back_to_false_with_one_warning[None]` | 改写 | `None` 是**合法**的"未传值"⇒ `auto` 且**不告警**（新用例 `test_include_frames_none_is_auto_without_warning`）；旧用例把 None 当坏值，是因为 v0.2 的 `_as_bool` 会对它静默返回 False |
| `test_no_audio_track_without_frames_is_error` | 改写 | 终态码 `NO_AUDIO_TRACK` → **`VIDEO_UNAVAILABLE`**（SPEC-v0.3 §7.4 第一行；"按表不按括注"，captain 2026-10-24 指令）。更名 `…is_video_unavailable` 并在 docstring 写明裁决 |
| `test_empty_vision_id_uses_current_chat_provider` | 改写 | v0.3 **覆盖** v0.2：抽帧回退**不回落**核心当前对话 provider（回退要花 6 次视觉调用，必须是显式配置）。更名 `test_empty_vision_id_does_not_fall_back_for_frames`，并故意让核心 provider **能读图**，断言仍不抽帧 |
| `test_empty_vision_id_with_no_image_modality_is_vision_unavailable` | 改写 | 同上反向面：`mode=true` + 空 vision ⇒ `VISION_UNAVAILABLE`（不论核心 provider 能不能读图）。更名 `test_empty_vision_id_with_mode_true_is_vision_unavailable` |
| `test_notes_always_come_from_the_frozen_vocabulary` | 改写 | 词表 20 → **39** 条（v0.2 的 20 条逐字不变 + v0.3 的 19 条）；词表副本抽到 `tests/_vocab.py`，两个主链路文件共用 |
| `test_slow_audio_eating_budget_keeps_transcript_and_degrades_frames` | 改写 | v0.3 在抽帧回退前多一道 25 s 闸：`frames_truncated:0/6` → **`video_skip_budget`**（预算优先于功能，SPEC-v0.3 §5.2 推论 3） |
| `test_budget_exhausted_with_nothing_to_deliver_is_still_a_failure` | 改写 | 情形①（无音轨 + `mode=true` + 预算耗尽）终态码 → **`VIDEO_UNAVAILABLE`**；情形②（有音轨但转写空 + 帧空）仍是 `FRAME_EXTRACT_FAILED`（诚实兜底不变） |
| `test_budget_exhausted_before_first_frame_keeps_transcript` | 改写 | 假时钟序列多一格：v0.3 多了"抽帧回退 25 s 闸"这一次 `monotonic()`（`_ScriptedClock(0,0,0,0,100)`） |

### 1.2 `tests/test_cache.py`（36 条）

| 旧用例 | 归类 | 理由 |
| --- | --- | --- |
| `test_build_key_*` 中 7 个分量参数（`digest` / `kind` / `max_frames` / `focus` / `prompts_fp` / `audio_provider_id` / `vision_provider_id`） | 保留 | 分量语义未变；红的原因是共用 `_key()` 助手还在用 v2 签名 |
| `test_build_key_changes_with_every_component[plugin_version-0.2.1]` | 改写 | 同一用例，参数值随版本走（`0.3.1`） |
| `test_build_key_changes_with_every_component[include_frames-True]` | 改写 | 布尔分量 → **`mode`**（`auto/true/false`）；另加 `video_provider_id` / `video_fp` 两个 v0.3 新分量参数，总数 9 → 11 |
| `test_build_key_is_64_hex_and_deterministic` | 保留 | 断言不变（`_key()` 升级即可） |
| `test_build_key_is_not_ambiguous_about_separator_collisions` | 改写 | 歧义示例换成 `mode`（`focus="a|1"` vs `focus="a",mode="1"`） |
| 其余 28 条（读写往返 / 4 键布局 / TTL 命中与过期 / 时钟回拨 / 损坏 / 拒写本地路径 ×3 / 单条超限 / 写失败自禁用 / 读失败一次告警 / 容量淘汰 ×2 / singleflight ×3 / `.tmp` 清洁 / clear / 跨实例） | 保留 | 断言一字未改 —— 缓存的行为契约没变，变的是键公式（v2 → v3） |
| 新增补强 | 新增 | `test_key_schema_version_is_v3_*` / `test_mode_is_a_component_so_auto_and_false_never_collide` / `test_video_fp_is_a_component_*` / `test_a_v2_style_key_material_never_matches_the_v3_key` |

### 1.3 `tests/test_config_map.py`（4 条）

| 旧用例 | 归类 | 理由 |
| --- | --- | --- |
| `test_schema_has_exactly_the_frozen_eleven_leaves` | 改写 | 11 → **21** 叶子（新增 `video` 组 8 键 + `video_provider_id` + `prompts.video_native`）。更名 `…twenty_one_leaves` |
| `test_schema_groups_are_object_with_description_and_hint` | 改写 | 分组顺序 = `provider_settings → video → prompts → limits → cache`（v0.3 插入 `video` 组） |
| `test_key_map_values_are_exactly_the_settings_fields` | 改写 | 21 字段；派生量清单补 `video_fp` / `compress_budget_bytes` |
| `test_main_read_points_match_key_map_exactly` | 改写 | 读取点 11 → 21 |
| 同一文件改名/合并的另三条 | 改写 | `test_main_settings_assembles_all_eleven_keys → …twenty_one_keys`；`test_three_way_diff_is_empty_for_the_real_tree → test_four_way_diff_…`（第四处 = `migrate_config_v03.KEY_ORDER`） |

### 1.4 `tests/test_core_imports.py`（1 条）

| 旧用例 | 归类 | 理由 |
| --- | --- | --- |
| `test_scan_scope_is_the_source_modules_only` | 改写 | 源码模块清单补 `probe.py` / `video_native.py`：漏扫 = 没扫（两个新模块同样不许裸 import 核心内部面） |

### 1.5 `tests/test_migrate_config.py`（1 条）

| 旧用例 | 归类 | 理由 |
| --- | --- | --- |
| `test_key_order_covers_every_settings_key` | 改写 | v0.2 迁移器**冻结在 11 键**（它承诺的是 v0.1→v0.2）；21 键的四处一致改由 `migrate_config_v03.KEY_ORDER` 承担。更名 `test_v02_key_order_is_the_frozen_eleven_keys`，并断言"11 键 ⊆ 21 键"（v0.3 没删没改名） |

### 1.6 `tests/test_prompts.py`（1 条）

| 旧用例 | 归类 | 理由 |
| --- | --- | --- |
| `test_prompts_tuple_order_is_frozen` | 改写 | 4 → **5** 元组 `(audio, video, frame, summary, video_native)`；新键**追加在末位**（v0.2 的四个老下标不动，少一次全表错位的机会）。另新增原生提示词的焦点补偿用例 |

### 1.7 `tests/test_registration.py`（4 条）

| 旧用例 | 归类 | 理由 |
| --- | --- | --- |
| `test_tool_params_use_only_docstring_renderable_types` | 改写 | `include_frames` 注解 `bool → str`（三态，SPEC-v0.3 §13.1） |
| `test_tool_is_registered_with_three_params_and_no_required` | 改写 | 注册面 `include_frames: boolean → string`；三参数 / 无 `required` / 只有 `filter.llm_tool` 一条装饰器均不变 |
| `test_envelope_hints_cover_all_eighteen_codes` | 改写 | 18 → **21** 码（+`VIDEO_UNAVAILABLE` / `VIDEO_FAILED` / `VIDEO_TOO_LARGE`）；同文件加"18 条老码一个都没消失"与"新 hint 不含数字/路径"两条 |
| `test_envelope_hints_match_the_frozen_snapshot_verbatim` | 改写 | 逐字快照 18 → 21 条（新增 3 条来自 `envelope.HINTS`，逐字） |

### 1.8 `tests/test_version.py`（3 条）

| 旧用例 | 归类 | 理由 |
| --- | --- | --- |
| `test_plugin_version_equals_metadata_version` | 改写 | `EXPECTED_VERSION = 0.3.0`（`PLUGIN_VERSION` 同时进结果缓存键与 probe 键） |
| `test_metadata_version_is_a_plain_semver` | 改写 | 同上 |
| `test_changelog_top_entry_mentions_the_version` | 改写 | 同上（CHANGELOG 顶部条目 = v0.3.0） |

### 1.9 归类合计

| 文件 | 失败数 | 保留 | 改写 |
| --- | --- | --- | --- |
| `test_analyze.py` | 27 | 18 | 9 |
| `test_cache.py` | 36 | 33 | 3 |
| `test_config_map.py` | 4 | 0 | 4 |
| `test_core_imports.py` | 1 | 0 | 1 |
| `test_migrate_config.py` | 1 | 0 | 1 |
| `test_prompts.py` | 1 | 0 | 1 |
| `test_registration.py` | 4 | 0 | 4 |
| `test_version.py` | 3 | 0 | 3 |
| **合计** | **77** | **51** | **26** |

> 计数口径：按**收集到的 cell** 计（参数化用例一个参数算一条），与失败列表逐行对齐。
> `test_analyze.py` 的 27 = 21 个独立用例 + `true_like_values_trigger_vision` 的 6 个参数 cell
> （原 7 个里 `int 1` 那格被改写）+ `invalid_value…` 的 1 格（`None`）。

"保留"用例红在夹具上，本轮对夹具的改动逐条如下（都在 `tests/_rig.py` / `tests/_fakes.py`）：

1. `build_rig(kind="video")` 默认 `Settings(vision_provider_id="fake-chat")`（v0.3：回退抽帧
   以显式 vision 位为前提；想测"没配 vision"就显式传 `cfg`）；
2. `build_rig` 新增 `native=` / `probe_ok=` / `probe_source=` 三个旋钮，并默认把
   `probe.ensure_l1` 桩成"实跑通过"（真 L1 行为在 `test_probe.py`，这里只测"探测结果如何
   影响链路"）；
3. `_fakes.FakeNativeProvider`：有 `client.chat.completions.create`（记录 kwargs、可注入
   异常/延迟/空文本），`text_chat` 刻意抛 `AssertionError`（形状退回 v0.2 立刻红）；
4. `_key()` 助手补 `mode` / `video_provider_id` / `video_fp`（v3 的 12 分量）。

---

## 2. 本轮新增用例（净增 216 个 cell）

| 文件 | 条数 | 守什么 |
| --- | --- | --- |
| `tests/test_probe.py` | 38 | **L1 可见性探测**：双 token 判据（答错/空/超时/抛错 ⇒ 不可用）、样本合成五种失败面、`probe/v1` 缓存（命中/过期/清理/键含 provider+model+版本/落盘失败降级为进程内记忆/损坏条目 miss） |
| `tests/test_video_native.py` | 27 | **原生通道**：`video_url` 块形状与 data URL（解码后逐字节等于文件）、`max_tokens` 硬下限 1024（64 ⇒ 1024）、空文本/纯空白 ⇒ `video_native_empty`、异常/超时收成 `ok=False`、缺 `client`/适配器不符 ⇒ `unavailable`、**超 32 MiB 时 `read_bytes` 一次都不被调用** |
| `tests/test_compress.py` | 19 | **压缩**：码率公式与 64 kbps 下限、冻结 argv（480p/veryfast/ABR 双向钳制/AAC 64k mono/无音轨去三参）、源在预算内 0 趟、第二趟 0.95 修正、两趟仍超 ⇒ `budget_exceeded`、rc≠0/空产物/超时都只是"原因"、两趟共享超时预算、**真 ffmpeg 转码产物 ≤ 预算且只落 workdir、源字节/mtime/sha 不变** |
| `tests/test_video_chain.py` | 79 | **回退链**：原生成功零多余开销 / 空文本与哨兵 / 压缩被禁-失败-不可行 / 探测四态 / 预算闸与 `timeout=min(上限, remaining-15)` / **`mode=false` 任何组合都不抽帧且不解析视觉位** / §7.4 四行终态码 / **36 组合回退矩阵** / 缓存键含 `mode` 与 `video_fp` / 压缩产物随 workdir 一起删 |
| `tests/test_memory_guards.py` | 5 | **内存护栏**：32 MiB 硬上限与配置钳位同值、超上限不读、上限内恰好读一次、`del raw` 静态守卫、内容哈希 1 MiB 分块流式读 |
| `tests/test_migrate_config_v03.py` | 25 | **v0.3 迁移器**：21 键/顺序=schema 顺序、10 个新键默认值与 `settings.py`+schema 逐字相同、干跑默认、`--apply` 保留权限位（600/640/444）、备份 0600 逐字节、原子写、幂等、`--report` 21 行且不打印值、回滚、BOM |
| `tests/test_analyze.py` | 104 → 108（+4） | 三态归一（`None` 合法 / 非法 ⇒ auto）、词表条数 39 与共用副本 |
| `tests/test_cache.py` | 56 → 62（+6） | v3 键版本、`mode` 三分量互斥、`video_fp` 分量、v2 键材料不可能等于 v3 |
| `tests/test_config_map.py` | 18 → 25（+7） | 21 键四处一致（含 `KEY_ORDER` 顺序）、默认值逐字、`flag()` 只认真布尔、`compress_budget_bytes`、`video_fp` 公式、建议表冻结 |
| `tests/test_registration.py` | 30 → 33（+3） | 21 码快照全覆盖、新 hint 不含运行时数据、老码一个不删 |
| `tests/test_prompts.py` | 27 → 28（+1） | 第 5 份模板的焦点补偿 |
| `tests/test_core_imports.py` | 14 → 16（+2） | 新模块被参数化守卫自动纳入 |
| **净增合计** | **442 → 658（+216）** | 193 在新文件 + 23 在既有文件 |

支撑文件（非用例）：

- `tests/_rig.py`：`Rig` / `build_rig` / `_stub_media` / `_stub_providers` —— 两个主链路文件
  共用同一套台（否则会出现"一边修好、另一边还在用旧台"的半红半绿）；
- `tests/_vocab.py`：39 条 notes 词表 + 匹配器 + `bad_notes()`；
- `tests/_fakes.py`：+`FakeNativeProvider` / `native_video()`。

---

## 3. 任务单（t3 派单文本）与冻结规格的冲突：按规格执行

派单文本写于 t9 冻结规格**之前**，有四处与 `SPEC-v0.3.md` 冲突。按"规格优先 + 能力被谁覆盖"
逐条登记（与 §7.4 的"按表不按括注"同一条纪律）：

| # | 派单要求 | 冻结规格 | 本轮做法 | 能力被谁覆盖 |
| --- | --- | --- | --- | --- |
| ① | "L2 阶梯（遇拒即停 + 安全系数）" | §16.2-1 **删除 L2**（t8 实测 0 次拒绝，47.2 MiB / 36.5 MiB 仍 200 ⇒ "体积上限"不是事实） | 不测 L2（没有实现） | L1 双 token 判据（`test_probe.py`，13 条）+ 延迟建议表（`test_config_map.py::test_suggest_direct_bytes_table_is_frozen`）+ 直送门槛/压缩（`test_video_chain.py`、`test_compress.py`） |
| ② | "内置上限表：mimo=50 MB、运行期以缓存上限覆盖" | §16.3-4：口述的 "<50 MB" **未证伪也未坐实** ⇒ 不进表；§16.2-6："按 provider 自动调门槛"被否决（第二事实源） | 表按实际冻结值测：`gemini-3*flash*`⇒8 MiB、`mimo*`⇒4 MiB、未知⇒4 MiB；**运行期只认 `video.max_direct_bytes`** | `settings.suggest_direct_bytes()` 仅用于 `ready` 提示与校准工具；运行期判定由 `test_video_chain.py` 的压缩/直送闸用例覆盖 |
| ③ | "探测失败时不得当成'支持'" | 一致（§2.2-⑥ / §3.2） | 测：缓存 `ok=false` 仍是失败；L1 未过 ⇒ 不直送、走回退 | — |
| ④ | ① 里的"L2 安全系数" | 无此概念 | 不实现、不测 | 保守默认值（4 MiB）+ 钳位（1–32 MiB）+ 建议表 |

另有两处**规格文字**与实际算术/实现不一致，照实登记（都不改规格冻结文本）：

1. **§15-T5 写"24 组合"，实为 3×3×2×2 = 36**。本轮按 36 覆盖（超集，无遗漏）；
2. **§7.3 括注**说 `NO_AUDIO_TRACK` 语义"沿用"，而 **§7.4 表**把该情形判成 `VIDEO_*`。
   按 captain 2026-10-24 指令**以表为准**，并写了一条专门用例
   （`test_analyze.py::test_no_audio_track_without_frames_is_video_unavailable`）留证据。
   `NO_AUDIO_TRACK` 仍在 21 码表里（v0.2 的 18 条一个不删），只是本版**不再由该分支产出**。

---

## 4. 废弃项与"能力被谁覆盖"

**用例层：0 条废弃。**（442 条 v0.2 用例的意图全部保留；20 个 id 因参数/改名而变化，
逐条见 §1）

**能力层（只登记规格已删除的东西，避免以后有人按旧派单找测试）：**

| 被删能力 | 出处 | 被谁覆盖 |
| --- | --- | --- |
| L2 体积阶梯（探测/配置/自动跑） | SPEC-v0.3 §16.2-1 | L1 双 token 判据 + `video.max_direct_bytes` 门槛 + 压缩 |
| "自动按 provider 调直送门槛" | §16.2-6 | 用户显式配置 `video.max_direct_bytes`；建议表只打一行 INFO |
| "原生 + 音轨双跑" | §16.2-7 | 原生成功即 `transcript=[]`（`test_native_success_fills_summary_and_skips_every_fallback`） |
| `include_frames` 布尔语义（默认 False） | §7.1 | 三态 `auto`（默认）/`true`/`false` + 旧布尔兼容 |
| v2 缓存键（10 分量） | §9.1 | v3（12 分量，+`mode` +`video_fp`）；v2 条目自然不命中、按 TTL 老化 |

---

## 5. 硬守卫清单（"不得减少"，逐条核对）

| 守卫 | 位置（v0.3） | 状态 |
| --- | --- | --- |
| 21 条错误码**逐字字面量**快照 | `test_registration.py::test_envelope_hints_match_the_frozen_snapshot_verbatim` + `…cover_all_twenty_one_codes` | 保留并扩到 21 |
| `kept ≡ len(frames)` | `test_analyze.py::test_frames_truncated_kept_matches_frames_length` | 保留（未改） |
| `CancelledError` 传播 | `test_analyze.py::test_tool_facade_propagates_cancelled_error` | 保留（未改） |
| `_as_bool`（v0.2）→ `_as_mode` 三态 | `test_analyze.py` 的三态归一 4 条 + `test_registration.py` 的参数类型断言 | **等价替换**：白名单归一 + 恰一条 WARNING + 非法值不抛异常；语义按 §7.1 从 `false` 改为 `auto`（有专门用例） |
| `probe_streams` rc≠0 ⇒ `PROBE_FAILED` | `test_media.py::test_probe_streams_nonzero_rc_is_probe_failed` + `…zero_rc_with_empty_stdout…` | 保留（未改） |
| 坏链 ⇒ `MEDIA_UNRESOLVABLE` | `test_analyze.py::test_unresolvable_media_is_media_unresolvable` | 保留（未改） |
| 配置**四处**一致 | `test_config_map.py`（schema ⇄ `KEY_MAP` ⇄ `main._settings()` ⇄ `migrate_config_v03.KEY_ORDER`，含顺序） | 升级为四处 × 21 键 |
| 缓存 `cached:true` / 容量淘汰 | `test_analyze.py::test_second_identical_call_hits_cache` + `test_cache.py::test_evicts_by_entry_count/total_bytes` | 保留（未改） |
| `/tmp` 清理 | `test_analyze.py::test_workdir_is_removed_even_on_failure` + `test_video_chain.py::test_workdir_and_compressed_product_are_removed_after_the_run` | 保留并扩到压缩产物 |
| 套件内不许挂 xfail | `test_fixtures.py::test_no_xfail_markers_are_parked_in_the_suite` | 保留（未改；门禁 0 xfail） |

---

## 6. 已知残留与偏差（照实登记，不进"已通过"）

| # | 现象 | 影响面 | 处置 |
| --- | --- | --- | --- |
| R1 | **`probe` 的 `ttl_seconds=0` 并非"既不读也不写"**：`ProbeStore.write` 不看 TTL、进程内 `_MEMO` 仍会命中（SPEC-v0.3 §3.3 的措辞是"0 = 关闭 = 既不读也不写"） | **生产零影响**：`analyze._native_channel` 在 `ttl<=0` 时**根本不调用 `ensure_l1`**（记 `video_probe_disabled`），有专门用例断言 `ensure_l1 == 0` | 测试锁定"不读盘 + 不放大成本"两件可观察面；建议 code 侧下版把 `write`/`_MEMO` 也挡在 `ttl<=0` 之外（本轮不改实现） |
| R2 | `probe` 条目里的 `latency` 字段当前恒 `None`（预留给延迟校准） | 零影响（不进任何判定） | 登记；校准工具 `tools/probe_video_capability.py` 的输出不写回缓存 |
| R3 | `NO_AUDIO_TRACK` 在 21 码表里但 v0.3 不再产出 | 模型侧可能看到"码存在但永不出现" | 规格 §11.1 要求"18 条一个不删"，故保留；§7.4 表为准（见 §3 ④） |
| R4 | `CompressResult.reason="not_smaller"` 分支实际不可达（预算 < 源体积时产物 ≤ 预算必然 < 源） | 防御性代码 | 不写"永远绿"的假用例；`test_compress.py` 只测可达分支 |

---

## 7. 判别力自证（"改坏就会红"）

在 `/tmp` 的三份**副本**上做变异（不动库、不动生产），每份只改一行实现，跑同一门禁命令：

| 变异 | 改动 | 结果 |
| --- | --- | --- |
| M1 "退回 v0.2 的布尔语义" | `analyze._frames_fallback` 的 `if mode == MODE_FALSE:` → `if False and …` | **26 failed**（含 `test_fallback_matrix_*` 6 格、`test_mode_false_never_extracts_frames_or_resolves_vision` 2 条、`test_analyze` 的 false-like 8 条） |
| M2 "缓存键不含 mode" | `cache.build_key` 的 material 删掉 `str(mode)` | **4 failed**（`test_mode_is_a_component_*`、`test_mode_auto_and_false_do_not_share_a_cache_entry`、`test_include_frames_flag_is_part_of_cache_key`、`test_build_key_changes_with_every_component`） |
| M3 "max_tokens 不抬到 1024" | `video_native.call_native` 的 `max(MAX_TOKENS_FLOOR, …)` → `int(max_tokens)` | **3 failed**（`test_max_tokens_is_clamped_up_to_the_floor` ×2、`test_native_max_tokens_below_the_floor_is_raised_at_call_time`） |

另一条结构性证据：把**旧套件**（`44f9afa` 归档树）跑在 v0.3 实现上 = **77 failed / 369 passed**；
换成新套件 = **658 passed / 0 failed**（§0 的命令）。⇒ 绿色是新套件挣来的，不是选择器凑的。

---

## 8. 本轮没做（明确边界）

- 未改任何实现（`analyze.py` / `probe.py` / `video_native.py` / `media.py` / `cache.py` /
  `settings.py` / `main.py` / `tools/*` 一字未动）；
- 未 `git commit` / 未 `git push`（收尾单统一提交）；
- 未碰生产树（`/root/AstrBot` 只读；测试全程 `ASTRBOT_ROOT=/tmp`）；
- 未用真实群媒体做夹具（全部 lavfi 合成 / 全零字节桩）；
- 未跑真 provider（网络一律 mock；t8 的实测数据只作为**阈值依据**引用）。
