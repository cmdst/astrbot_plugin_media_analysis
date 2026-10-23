# v0.1 → v0.2 测试映射表（113 条失效用例，一条不漏）

**这份文件是什么**：t2 把 v0.2.0 落到库 `main`（`42ea534`）时，v0.1 的测试套件里共有
**113 条用例失效**（`247 - 134 = 113`；基线 247 passed）。这不是回归，是**接口换代**：
`ResultCache` → `DiskCache`、`resolve_stt_provider` → `resolve_audio_provider`、
`Settings(stt_provider_id=)` → 11 键、`resolve_media_path(handle)` → 必填关键字、
16 码 → 18 码。

t3 的纪律：**不许用删除来变绿**。每条旧用例都要有处置结论 —— 保留（同名改写）/ 改写（改名，
契约不变）/ 废弃（能力被谁覆盖）。本表就是那张逐条账。

| 处置 | 条数 |
| --- | --- |
| 保留（同名，内部适配 v0.2 接口） | **89** |
| 改写（改名，守同一份契约） | **20** |
| 废弃（v0.2 主动删掉的能力；写明被谁覆盖） | **4** |
| 合计 | **113** |

> 门禁：`424 passed / 0 failed / 0 skipped / 0 xfail`（基线 247，净增 **+177**）。
> 新增用例清单见文末「新增文件与用例」。

## 1. 逐条映射（113 行）

| # | 旧用例（文件名见下方分组标题） | 处置 | v0.2 落点 | 理由 |
| --- | --- | --- | --- | --- |
| 1 | `test_all_frame_descriptions_failing_is_vision_failed` | 保留 | （同名改写） | 内部改用 audio_chat + 真 describe_frames(settings=)；契约不变 |
| 2 | `test_audio_extract_failure_is_normalized` | 保留 | （同名改写） | 改 patch `ensure_provider_wav(..., extracted=True)`（v0.2 的抽音轨入口） |
| 3 | `test_audio_has_no_video_stream_in_envelope` | 保留 | （同名改写） | 判据从 probe_* 计数改为 `classify==1` + 不抽音轨（v0.2 合并了三次 ffprobe） |
| 4 | `test_audio_with_include_frames_is_ignored_with_note` | 保留 | （同名改写） | 语音忽略 include_frames + frames_skipped_audio_only |
| 5 | `test_blank_focus_is_treated_as_empty` | 保留 | （同名改写） | 断言从聊天 provider 移到音频 provider 的提示词记录 |
| 6 | `test_budget_exhausted_before_first_frame_keeps_transcript` | 保留 | （同名改写） | 脚本时钟从 3 段改 4 段（多了音频阶段一次 monotonic） |
| 7 | `test_budget_exhausted_with_nothing_to_deliver_is_still_a_failure` | 保留 | （同名改写） | DEADLINE_S 0.001→0.5：v0.2 抽帧闸是 `剩余<15s`，0.001 会先在音频阶段判 AUDIO_FAILED |
| 8 | `test_budget_sufficient_path_is_unchanged` | 保留 | （同名改写） | 回归护栏原样保留 |
| 9 | `test_cached_payload_is_a_copy` | 保留 | （同名改写） | 落盘缓存每次重新解码 ⇒ 更强（序列化隔离） |
| 10 | `test_configured_missing_stt_id_is_provider_not_found` | 改写 | test_configured_missing_audio_id_is_provider_not_found | STT 键 → audio_provider_id，语义（配了但不存在）不变 |
| 11 | `test_describe_frames_all_attempts_failed_is_vision_failed` | 保留 | （同名改写） | 补 `settings=` 必填关键字（提示词进 describe_frames） |
| 12 | `test_describe_frames_single_call_timeout_degrades` | 保留 | （同名改写） | 补 `settings=` |
| 13 | `test_describe_frames_truncates_without_raising_when_no_call_started` | 保留 | （同名改写） | 补 `settings=` |
| 14 | `test_different_digest_misses_cache` | 保留 | （同名改写） | 键含内容哈希，口径不变 |
| 15 | `test_different_focus_misses_cache` | 保留 | （同名改写） | 键含 focus，口径不变 |
| 16 | `test_empty_material_skips_summary_call` | 保留 | （同名改写） | 音频模型返回空串 ⇒ 材料为空 ⇒ 不调摘要 |
| 17 | `test_empty_stt_id_falls_back_to_curr_stt_instance` | 改写 | test_empty_audio_id_falls_back_to_current_chat_provider | v0.2 无 STT 阶梯：空串回落到核心当前对话 provider |
| 18 | `test_empty_stt_id_with_nothing_loaded_is_unavailable` | 改写 | test_empty_audio_id_with_nothing_loaded_is_audio_unavailable | 错误码 STT_UNAVAILABLE → AUDIO_UNAVAILABLE |
| 19 | `test_empty_transcript_is_not_an_error` | 保留 | （同名改写） | note `stt_empty` → `audio_empty`；语义不变 |
| 20 | `test_empty_vision_id_uses_current_chat_provider` | 保留 | （同名改写） | core provider 现在要同时带 `audio`+`image`（回落面只有一个 provider） |
| 21 | `test_empty_vision_id_with_text_only_provider_is_mismatch` | 改写 | test_empty_vision_id_with_no_image_modality_is_vision_unavailable | SPEC-v0.2 §2.5 覆盖 v0.1：回落不合格 ⇒ VISION_UNAVAILABLE |
| 22 | `test_exactly_at_limit_is_allowed` | 保留 | （同名改写） | 边界 `>` 判定不变 |
| 23 | `test_extracted_wav_is_tracked_on_event` | 保留 | （同名改写） | 产物登记仍走 `track_temporary_local_file`（音轨名 audio.wav） |
| 24 | `test_frame_extractor_leaking_raw_exception_is_internal_error` | 保留 | （同名改写） | 裸异常 ⇒ INTERNAL_ERROR 信封 |
| 25 | `test_frame_extract_process_timeout_is_still_a_failure` | 保留 | （同名改写） | wait_for 超时 ≠ 预算截断，仍 FRAME_EXTRACT_FAILED |
| 26 | `test_frame_extract_total_failure` | 保留 | （同名改写） | 0 帧 ⇒ FRAME_EXTRACT_FAILED |
| 27 | `test_frames_and_notes_share_at_s_formatting` | 保留 | （同名改写） | `frames[].at_s` 与 notes 同格式化 |
| 28 | `test_frames_truncated_kept_matches_frames_length` | 保留 | （同名改写） | **硬守卫**：kept ≡ len(frames)（描述层 want=已抽帧数） |
| 29 | `test_hint_is_static_across_runs` | 保留 | （同名改写） | hint 是常量，不夹带运行时数据 |
| 30 | `test_include_frames_false_like_values_skip_vision[0_0]` | 保留 | （同名改写） | `_as_bool` 真值表（参数化保留） |
| 31 | `test_include_frames_false_like_values_skip_vision[0_1]` | 保留 | （同名改写） | 同上 |
| 32 | `test_include_frames_false_like_values_skip_vision[false]` | 保留 | （同名改写） | 同上 |
| 33 | `test_include_frames_false_like_values_skip_vision[False]` | 保留 | （同名改写） | 同上 |
| 34 | `test_include_frames_false_like_values_skip_vision[  FALSE  ]` | 保留 | （同名改写） | 同上 |
| 35 | `test_include_frames_false_like_values_skip_vision[no]` | 保留 | （同名改写） | 同上 |
| 36 | `test_include_frames_false_never_calls_vision` | 保留 | （同名改写） | 默认只转写 ⇒ 零视觉调用 |
| 37 | `test_include_frames_flag_is_part_of_cache_key` | 保留 | （同名改写） | 键含 include_frames |
| 38 | `test_include_frames_invalid_value_falls_back_to_false_with_one_warning` | 保留 | （同名改写） | 非法值 ⇒ False + **恰好一条**告警 |
| 39 | `test_include_frames_true_like_values_trigger_vision[1_0]` | 保留 | （同名改写） | `_as_bool` 真值表（参数化保留） |
| 40 | `test_include_frames_true_like_values_trigger_vision[1_1]` | 保留 | （同名改写） | 同上 |
| 41 | `test_include_frames_true_like_values_trigger_vision[true]` | 保留 | （同名改写） | 同上 |
| 42 | `test_include_frames_true_like_values_trigger_vision[  True  ]` | 保留 | （同名改写） | 同上 |
| 43 | `test_include_frames_true_like_values_trigger_vision[True]` | 保留 | （同名改写） | 同上 |
| 44 | `test_include_frames_true_like_values_trigger_vision[yes]` | 保留 | （同名改写） | 同上 |
| 45 | `test_include_frames_true_triggers_vision` | 保留 | （同名改写） | 对称面：true ⇒ 抽帧 + 视觉各一次 |
| 46 | `test_invalid_target_returns_bad_target_envelope[]` | 保留 | （同名改写） | BAD_TARGET 首行判定（参数化 5 条保留） |
| 47 | `test_invalid_target_returns_bad_target_envelope[Current]` | 保留 | （同名改写） | 同上 |
| 48 | `test_invalid_target_returns_bad_target_envelope[quoted ]` | 保留 | （同名改写） | 同上 |
| 49 | `test_invalid_target_returns_bad_target_envelope[\u5f53\u524d]` | 保留 | （同名改写） | 同上 |
| 50 | `test_invalid_target_returns_bad_target_envelope[x]` | 保留 | （同名改写） | 同上 |
| 51 | `test_limit_follows_configured_value` | 保留 | （同名改写） | 上限可配（钳位 5 s 下限） |
| 52 | `test_long_focus_is_trimmed_with_note` | 保留 | （同名改写） | focus 截断 200 字 + note；断言改到音频提示词 |
| 53 | `test_lru_evicts_oldest_when_maxsize_is_one` | 改写 | test_cache_survives_a_new_cache_instance ＋ tests/test_cache.py::test_evicts_by_entry_count / test_evicts_by_total_bytes | 内存 LRU 已删：容量 SQL 口径搬到 test_cache.py，主链路只留"跨实例持久"这一面 |
| 54 | `test_missing_media_is_media_not_found` | 保留 | （同名改写） | MEDIA_NOT_FOUND（log_detail 现带真实组件类型） |
| 55 | `test_no_audio_track_with_frames_is_success_with_note` | 保留 | （同名改写） | 无音轨 + 要画面 ⇒ 成功 + note；断言改为"音频 provider 未被解析" |
| 56 | `test_no_audio_track_without_frames_is_error` | 保留 | （同名改写） | 无音轨 + 只要转写 ⇒ NO_AUDIO_TRACK |
| 57 | `test_notes_always_come_from_the_frozen_vocabulary` | 保留 | （同名改写） | 词表 12 → 20 条（stt_provider→audio_provider/audio_empty/新增 8 条） |
| 58 | `test_no_video_track_is_error` | 保留 | （同名改写） | 要画面但无视频流 ⇒ NO_VIDEO_TRACK |
| 59 | `test_over_limit_is_media_too_long` | 保留 | （同名改写） | >上限 ⇒ MEDIA_TOO_LONG；断言"零 provider 调用"改到音频计数器 |
| 60 | `test_partial_frame_failure_keeps_success` | 保留 | （同名改写） | 单帧失败只降级该帧 |
| 61 | `test_probe_failure_is_probe_failed` | 保留 | （同名改写） | 时长读不出 ⇒ PROBE_FAILED（Record/Video 口径不变） |
| 62 | `test_provider_resolver_leaking_raw_exception_is_internal_error` | 保留 | （同名改写） | 改 patch `resolve_audio_provider` |
| 63 | `test_quoted_target_reads_reply_chain` | 保留 | （同名改写） | quoted 从 Reply.chain 取媒体 |
| 64 | `test_second_identical_call_hits_cache` | 保留 | （同名改写） | 命中回 `cached:true`；notes 追加 cache_hit/cache_age_s:<n>（断言按新口径） |
| 65 | `test_six_failed_descriptions_with_full_budget_still_vision_failed` | 保留 | （同名改写） | 预算充足但全失败 ⇒ VISION_FAILED |
| 66 | `test_slow_stt_eating_budget_keeps_transcript_and_degrades_frames` | 改写 | test_slow_audio_eating_budget_keeps_transcript_and_degrades_frames | STT → 多模态音频调用；预算闸换成 0.5 s/15 s 口径 |
| 67 | `test_stt_capability_mismatch` | 改写 | test_audio_capability_mismatch ＋ test_configured_text_only_audio_provider_is_capability_mismatch（真解析器） | 配了但不符合 ⇒ PROVIDER_CAPABILITY_MISMATCH |
| 68 | `test_stt_exception_is_stt_failed` | 改写 | test_audio_call_exception_is_audio_failed | `get_text` 抛错 → `text_chat` 抛错 ⇒ AUDIO_FAILED |
| 69 | `test_stt_provider_unavailable` | 改写 | test_audio_provider_unavailable | STT_UNAVAILABLE → AUDIO_UNAVAILABLE |
| 70 | `test_stt_timeout_is_stt_failed` | 改写 | test_audio_call_timeout_is_audio_failed ＋ test_audio_budget_exhausted_before_call_is_audio_failed | 超时口径 `min(60s, 剩余预算)`，并补"预算已耗尽不发起调用" |
| 71 | `test_success_envelope_audio_has_exactly_nine_ordered_keys` | 保留 | （同名改写） | 9 键顺序不变；note 前缀 stt_provider→audio_provider |
| 72 | `test_success_envelope_video_with_frames` | 保留 | （同名改写） | 视频：音轨转写 + 逐帧描述 |
| 73 | `test_success_log_contains_counts_not_transcript` | 保留 | （同名改写） | 成功日志只打计数（正文不进日志） |
| 74 | `test_summary_failure_keeps_ok_with_note` | 保留 | （同名改写） | 摘要失败只降级 |
| 75 | `test_summary_provider_unavailable_is_degraded` | 保留 | （同名改写） | 对话 provider 解析不出来 ⇒ summary_failed |
| 76 | `test_summary_skipped_when_budget_is_short` | 保留 | （同名改写） | 剩余 < SUMMARY_TIMEOUT_S ⇒ 不调模型 |
| 77 | `test_summary_uses_transcript_material` | 保留 | （同名改写） | 材料拼法 `转写：<text>` 不变；摘要走对话 provider |
| 78 | `test_tool_controlled_failure_is_logged_and_enveloped` | 保留 | （同名改写） | ToolError ⇒ INFO 日志 + 3 键信封，log_detail 不进信封 |
| 79 | `test_tool_facade_propagates_cancelled_error` | 保留 | （同名改写） | **硬守卫**：CancelledError 必须传播 |
| 80 | `test_tool_facade_turns_unexpected_errors_into_envelope[KeyboardInterrupt]` | 保留 | （同名改写） | 任何 BaseException（除取消）⇒ INTERNAL_ERROR 信封 |
| 81 | `test_tool_facade_turns_unexpected_errors_into_envelope[KeyError]` | 保留 | （同名改写） | 同上 |
| 82 | `test_tool_facade_turns_unexpected_errors_into_envelope[RuntimeError]` | 保留 | （同名改写） | 同上 |
| 83 | `test_tool_facade_turns_unexpected_errors_into_envelope[ValueError]` | 保留 | （同名改写） | 同上 |
| 84 | `test_tool_failure_envelope_always_has_exactly_three_keys` | 保留 | （同名改写） | 失败信封恰好 3 键（四类分支） |
| 85 | `test_unresolvable_media_is_media_unresolvable` | 保留 | （同名改写） | 组件在但取不到文件 ⇒ MEDIA_UNRESOLVABLE |
| 86 | `test_unserializable_envelope_never_reaches_the_model` | 保留 | （同名改写） | 信封不可序列化 ⇒ INTERNAL_ERROR |
| 87 | `test_vision_provider_capability_mismatch_is_error` | 保留 | （同名改写） | 视觉位拿到无 image 能力的 provider ⇒ 能力不匹配 |
| 88 | `test_workdir_is_removed_even_on_failure` | 保留 | （同名改写） | **硬守卫**：/tmp 工作目录成功/失败都要删 |
| 89 | `test_key_map_values_are_exactly_the_settings_fields` | 保留 | （同名改写） | KEY_MAP 值 == Settings 字段（4 → **11**），并补"派生量不许混进字段" |
| 90 | `test_main_read_points_match_key_map_exactly` | 保留 | （同名改写） | main 白名单读取点 4 → **11** |
| 91 | `test_main_settings_assembles_all_four_keys` | 改写 | test_main_settings_assembles_all_eleven_keys | 四键 → 11 键（含 prompts 去空白、cache ttl 钳位） |
| 92 | `test_provider_id_leaves_declare_webui_special` | 保留 | （同名改写） | 三个 provider 叶子都要 `_special`（含弃用键的 select_provider_stt） |
| 93 | `test_schema_groups_are_object_with_description_and_hint` | 保留 | （同名改写） | 分组 2 → 4（provider_settings / prompts / limits / cache），顺序即渲染顺序 |
| 94 | `test_schema_has_exactly_the_frozen_four_leaves` | 改写 | test_schema_has_exactly_the_frozen_eleven_leaves | 契约快照 4 → 11 个点号键 |
| 95 | `test_settings_dataclass_is_frozen_and_defaults_match_schema` | 保留 | （同名改写） | 默认值对齐扩展到 11 键（含 4 份提示词默认逐字） |
| 96 | `test_resolve_media_path_falls_back_to_convert` | 保留 | （同名改写） | 补必填关键字 `workdir` / `max_media_bytes`（v0.2 签名） |
| 97 | `test_resolve_media_path_prefers_existing_path` | 保留 | （同名改写） | 同上（path 优先，convert 零调用） |
| 98 | `test_resolve_media_path_times_out` | 保留 | （同名改写） | 同上（convert 超时 ⇒ UNRESOLVABLE） |
| 99 | `test_resolve_media_path_unresolvable_variants` | 保留 | （同名改写） | 同上（三种取不到文件） |
| 100 | `test_stt_ladder_survives_core_value_error` | 改写 | test_audio_step2_survives_core_value_error | 核心 ValueError ⇒ 吞成 None ⇒ AUDIO_UNAVAILABLE |
| 101 | `test_stt_ladder_survives_get_all_failure` | 改写 | test_audio_step2_survives_generic_exception | 任意异常都不许漏给模型 ⇒ AUDIO_UNAVAILABLE |
| 102 | `test_stt_step1_configured_id_wins_and_is_looked_up_synchronously` | 改写 | test_audio_step1_configured_id_wins_and_is_looked_up_synchronously | 同步查表断言保留（await 会 TypeError） |
| 103 | `test_stt_step1_unknown_id_is_provider_not_found` | 改写 | test_audio_step1_unknown_id_is_provider_not_found | 语义不变 |
| 104 | `test_stt_step1_wrong_provider_type_is_capability_mismatch` | 改写 | test_audio_step1_stt_provider_in_audio_slot_is_capability_mismatch ＋ test_audio_step1_non_provider_object_is_capability_mismatch | 类型面判据 `${isinstance STTProvider}` → `${isinstance Provider}` |
| 105 | `test_stt_step2_uses_current_stt_provider` | 改写 | test_audio_step2_empty_id_falls_back_to_current_chat_provider | 回落目标从"当前 STT"变成"当前对话 provider" |
| 106 | `test_stt_step3_curr_inst_bypasses_disabled_switch` | 废弃 | test_audio_step2_empty_id_falls_back_to_current_chat_provider | v0.2 删掉了 STT 五级阶梯（SPEC-v0.2 §2.5）："绕过 enable 开关读 curr_stt_provider_inst"这条隐式分支**不再存在**，其"空串回落"能力由音频阶梯②覆盖 |
| 107 | `test_stt_step3_ignores_wrong_type_instance` | 废弃 | test_audio_step2_non_provider_return_is_audio_unavailable | 同上：没有"忽略错类型实例再往下找"的步骤；"拿到非 Provider"这一面由音频阶梯②覆盖 |
| 108 | `test_stt_step3_is_skipped_when_provider_manager_missing` | 废弃 | test_audio_step2_no_current_provider_is_audio_unavailable | v0.2 不读 `provider_manager`（无 ③④ 步）；"没有可用 provider"由阶梯②覆盖 |
| 109 | `test_stt_step4_uses_first_loaded_stt` | 废弃 | test_audio_step2_capable_provider_missing_audio_is_audio_unavailable | v0.2 无"任一已加载 provider"兜底（生产上等价于随机选模型，SPEC §2.5 明确不做）；其结果面由阶梯②的 AUDIO_UNAVAILABLE 覆盖 |
| 110 | `test_stt_step5_raises_unavailable` | 改写 | test_audio_step2_no_current_provider_is_audio_unavailable | STT_UNAVAILABLE → AUDIO_UNAVAILABLE |
| 111 | `test_envelope_hints_cover_all_sixteen_codes` | 改写 | test_envelope_hints_cover_all_eighteen_codes ＋ test_removed_stt_codes_are_really_gone | 16 → 18 码（删 STT_*，增 4 条） |
| 112 | `test_envelope_hints_match_the_frozen_snapshot_verbatim` | 保留 | （同名改写） | **硬守卫**：18 条 hint 逐字快照（dict 全等） |
| 113 | `test_plugin_construction_is_side_effect_free_whitebox` | 保留 | （同名改写） | `len(plugin._cache)==0` → `plugin._cache is None`（缓存实例改在 initialize 建） |

（文件名：第 1–88 行属 `tests/test_analyze.py`，89–95 属 `tests/test_config_map.py`，
96–99 属 `tests/test_media.py`，100–110 属 `tests/test_providers.py`，111–113 属
`tests/test_registration.py`。）

## 2. 四条废弃的完整说明（v0.2 主动删掉的 STT 阶梯第 ③④ 步）

v0.1 的 `resolve_stt_provider` 是**五级阶梯**，其中第 ③ 步
（`provider_manager.curr_stt_provider_inst`，**绕过** `provider_stt_settings.enable=False`）
和第 ④ 步（任一已加载 STT）是生产环境唯一能拿到 `mimo_stt` 的路。v0.2 改走多模态对话模型后，
整个阶梯按 SPEC-v0.2 §2.5 删除，只剩两级：

```
① audio_provider_id 非空 → 同步查表 → 要求 isinstance(_, Provider) 且 modalities 含 audio
② 空串 → 核心当前对话 provider → 它不符合 ⇒ AUDIO_UNAVAILABLE
```

所以那 4 条用例守的不是"某个行为"，而是"一条已经不存在的路径"。它们的**能力面**由以下用例覆盖：

| 被删的能力 | v0.2 里由谁覆盖 |
| --- | --- |
| 空串 ⇒ 回落（③ 那一步的产出） | `test_audio_step2_empty_id_falls_back_to_current_chat_provider` / `test_empty_audio_id_falls_back_to_current_chat_provider` |
| 拿到错类型实例 ⇒ 继续往下找 | `test_audio_step2_non_provider_return_is_audio_unavailable` |
| `provider_manager` 缺失 ⇒ 跳过该步 | `test_audio_step2_no_current_provider_is_audio_unavailable` |
| "任一已加载 provider" 兜底 | `test_audio_step2_capable_provider_missing_audio_is_audio_unavailable` |

**为什么不是"漏测"**：v0.2 里没有任何一行代码读 `provider_manager` / `curr_stt_provider_inst`
（`providers.py` 全文可查），复刻这 4 条用例只能靠"再造一个 STT 阶梯"来让它们绿 ——
那正是 v0.2 要删掉的东西。

## 3. 硬守卫在 v0.2 套件里的落点（captain 2026-10-23 的口径，逐条对齐）

| 守卫 | v0.2 落点 |
| --- | --- |
| 18 条 `hint` 的**逐字字面量快照** | `test_registration.py::test_envelope_hints_match_the_frozen_snapshot_verbatim`（`HINTS_SNAPSHOT` 18 条）+ `::test_envelope_hints_cover_all_eighteen_codes` |
| `kept ≡ len(frames)` | `test_analyze.py::test_frames_truncated_kept_matches_frames_length` |
| `CancelledError` 必须传播 | `test_analyze.py::test_tool_facade_propagates_cancelled_error` |
| `_as_bool` 真值表 | `test_analyze.py::test_include_frames_{false,true}_like_values_*`（6+6 参数）+ `::test_include_frames_invalid_value_falls_back_to_false_with_one_warning` |
| `probe_streams` 的 `rc != 0 ⇒ PROBE_FAILED` | `test_media.py::test_probe_streams_nonzero_rc_is_probe_failed`（**未改**） |
| 坏链 ⇒ `MEDIA_UNRESOLVABLE` | `test_media.py::test_broken_message_chain_is_media_unresolvable` ＋ `test_file_segment.py::test_broken_message_chain_is_still_media_unresolvable` |
| 配置三方一致（schema ⇄ settings ⇄ main 白名单），**11 键** | `test_config_map.py`（7 条，含 planted-key 反例）+ `test_migrate_config.py::test_key_order_covers_every_settings_key`（第四处：迁移脚本） |
| 缓存命中 `cached:true` | `test_analyze.py::test_second_identical_call_hits_cache` / `::test_cache_survives_a_new_cache_instance` / `test_prompts.py::test_prompt_change_invalidates_the_cache_for_the_same_media` |
| LRU / 容量淘汰 | `test_cache.py::test_evicts_by_entry_count` / `::test_evicts_by_total_bytes` |
| `/tmp` 工作目录清理 | `test_analyze.py::test_workdir_is_removed_even_on_failure` ＋ `test_file_segment.py::test_analyze_file_download_note`（下载物落 `/tmp` 且已被删） |
| 套件内不许挂 xfail | `test_fixtures.py::test_no_xfail_markers_are_parked_in_the_suite`（**未改**，仍守整包） |

## 4. 新增文件与用例（v0.2 新面）

| 文件 | 用例数 | 覆盖 |
| --- | --- | --- |
| `tests/test_file_segment.py` | 44 | **File 段**：定位/优先级/`log_detail` 真实类型名；`file_`→有界下载→UNRESOLVABLE 决策树；`.file`/`get_file()` 禁用探针；字节上限/状态码/空产物/scheme/后缀穿越；三段判型梯子（白名单内真视频、白名单外非媒体、ffprobe 失败、**silk 反例**）；端到端（真 ffprobe/ffmpeg + 假 provider） |
| `tests/test_cache.py` | 56 | **TTL 缓存**：11 分量键、命中/`cached:true` 的机制面、过期与时钟回拨、损坏/形状不符/拒写超限、`/tmp/`+`/root/`+`data:` 拒写、容量淘汰、跨实例持久、目录归属 `ASTRBOT_ROOT/data/plugin_data/…`、single-flight、ENOSPC 自禁用 |
| `tests/test_migrate_config.py` | 35 | **迁移脚本**：干跑不改文件、`--apply` 搬值 + 0600 备份 + 原子写、不可搬时退出码 2 且旧值原样、幂等（`changed=0` 不再备份）、`--rollback` 逐字节重建、不打印任何配置值、BOM/坏 JSON/缺 root 失败面、`KEY_ORDER` ⇄ `KEY_MAP` 交叉守卫 |
| `tests/test_prompts.py` | 27 | **双提示词**：空串回默认、坏值回默认 + 一次告警、超长截断（含截断破坏占位符 ⇒ 回退）、逐份占位符白名单、`prompts_fp` 覆盖四份、两键互不覆盖、焦点从句注入与补偿、摘要三占位符补偿、主链路注入（audio/video/frame 各就各位） |

`test_analyze.py` 里另新增 6 条"多模态音频调用形状"用例（`audio_urls` 只放 1 个本地 wav、
不带 `image_urls`/`contexts`/`system_prompt`、视频音轨指向抽出的 wav、摘要不带音频、
STT 面已从 `providers` 消失、旧配置形态撞能力不匹配）。
