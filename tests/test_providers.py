"""provider 解析阶梯与能力判定（SPEC-v0.2 §2.4 / §2.5 的 provider 类错误码）。

v0.2 把 v0.1 的**五级 STT 阶梯**换成了**两条对话 provider 阶梯**：

1. `resolve_audio_provider`：① 配了 `audio_provider_id` → 同步查表，要求 `isinstance(_, Provider)`
   且 `modalities` 严格含 `audio`（错配置 ⇒ `PROVIDER_NOT_FOUND` / `PROVIDER_CAPABILITY_MISMATCH`）；
   ② 空串 ⇒ 回落核心当前对话 provider，**它也不符合** ⇒ `AUDIO_UNAVAILABLE`（缺配置，不是配错）。
2. `resolve_chat_provider` / `resolve_vision_provider`：同形状，`require_image` 决定是否加图像判定。

严格能力口径不变（`supports_modality`）：`modalities is None`/缺省 ⇒ 不支持；`[]`（迁移遗留
空列表）⇒ 支持（与核心 `astr_main_agent.py:1412-1417` 同口径，和 `tool_loop_agent_runner.py:1109-1113`
的宽松口径**刻意不同**）。v0.2 新增的两条"反例"用例把它钉在**行为面**上：

- 把 STT provider 填进音频位 ⇒ `PROVIDER_CAPABILITY_MISMATCH`（类型面，`STTProvider` 不是 `Provider`）；
- 配了但 `modalities` 不含 `audio` ⇒ `PROVIDER_CAPABILITY_MISMATCH`（能力面）。

本文件还有一个隐形守护：`Context.get_provider_by_id` 是**同步**的
（`star/context.py:447-470`），`FakeContext` 返回普通对象 ⇒ 代码若写成 `await` 会立刻
`TypeError`，测试自然红。
"""

from __future__ import annotations

import pytest

from astrbot_plugin_media_analysis import envelope, providers
from _fakes import (
    FakeChatProvider,
    FakeContext,
    FakeSTTProvider,
    NotAProvider,
    audio_chat,
)

UMO = "aiocqhttp:GroupMessage:1000"


def _code(exc_info: pytest.ExceptionInfo) -> str:
    assert isinstance(exc_info.value, envelope.ToolError), type(exc_info.value)
    return exc_info.value.code


# --------------------------------------------------------------------------- #
# 1. supports_modality：三态 + 非 dict 的 provider_config
# --------------------------------------------------------------------------- #
def test_supports_modality_strict_three_states() -> None:
    assert providers.supports_modality(FakeChatProvider(modalities=["text", "image"]), "image") is True
    assert providers.supports_modality(FakeChatProvider(modalities=["text"]), "image") is False
    # 迁移遗留：空列表按"未配置"处理，核心同口径 ⇒ 算支持
    assert providers.supports_modality(FakeChatProvider(modalities=[]), "image") is True
    # 严格口径：缺 modalities 键 ⇒ 不支持（宽松口径的另一侧是静默丢音频，不可操作）
    assert providers.supports_modality(FakeChatProvider(modalities=None), "image") is False


def test_supports_modality_rejects_non_dict_provider_config() -> None:
    assert providers.supports_modality(object(), "image") is False
    assert providers.supports_modality(NotAProvider(modalities=["image"]), "image") is True
    assert providers.supports_modality(NotAProvider(), "image") is False


def test_supports_modality_audio_is_a_separate_axis() -> None:
    """音频与图像是两条独立的轴：含 `image` 不等于含 `audio`（v0.2 的音频位就靠这条区分）。"""
    assert providers.supports_modality(FakeChatProvider(modalities=["text", "image"]), "audio") is False
    assert providers.supports_modality(audio_chat(), "audio") is True
    assert providers.supports_modality(audio_chat(), "image") is False


def test_modality_constants_match_core_enum() -> None:
    """核心的 `modalities` 枚举是 `["text","image","audio","tool_use"]`（default.py:2246-2255）。"""
    assert providers.MODALITY_IMAGE == "image"
    assert providers.MODALITY_AUDIO == "audio"


def test_provider_id_of_reads_config_id_only() -> None:
    assert providers.provider_id_of(FakeSTTProvider(provider_id="mimo_stt")) == "mimo_stt"
    assert providers.provider_id_of(FakeChatProvider()) == "fake-chat"
    assert providers.provider_id_of(audio_chat()) == "fake-audio"
    assert providers.provider_id_of(object()) == "unknown"
    assert providers.provider_id_of(NotAProvider()) == "unknown"


def test_stt_resolver_is_gone_in_v02() -> None:
    """v0.2 的硬事实：`providers` 里**不再有** STT 链路（`resolve_stt_provider` / `STTProvider`）。

    这条不是"断言实现细节"，而是防"把 STT 阶梯又加回来"：音频阶段的唯一形状是
    `text_chat(prompt=…, audio_urls=[…])`，多一条 STT 分支＝多一条会静默丢音频的路。
    """
    assert not hasattr(providers, "resolve_stt_provider")
    assert not hasattr(providers, "STTProvider")


# --------------------------------------------------------------------------- #
# 2. 音频阶梯（v0.2 新增；替代 v0.1 的 STT 五级阶梯）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_audio_step1_configured_id_wins_and_is_looked_up_synchronously() -> None:
    prov = audio_chat(model_name="mimo-audio")
    ctx = FakeContext(by_id={"mimo-audio": prov}, using_provider=audio_chat(model_name="other"))
    resolved = await providers.resolve_audio_provider(ctx, "mimo-audio", UMO)
    assert resolved is prov
    assert ctx.provider_ids_asked() == ["mimo-audio"]  # 同步查表，且只查一次
    assert not ctx.calls.named("get_using_provider_async"), "配了 id 就不该再走回落"


@pytest.mark.asyncio
async def test_audio_step1_unknown_id_is_provider_not_found() -> None:
    ctx = FakeContext(by_id={})
    with pytest.raises(envelope.ToolError) as exc_info:
        await providers.resolve_audio_provider(ctx, "ghost", UMO)
    assert _code(exc_info) == envelope.ERR_PROVIDER_NOT_FOUND


@pytest.mark.asyncio
async def test_audio_step1_stt_provider_in_audio_slot_is_capability_mismatch() -> None:
    """反例①：把 STT provider 填进音频位 ⇒ 类型面不匹配。

    v0.1 的 `stt_provider_id` 存的正是这种 id；迁移脚本因此要按模态校验，"看着像"不能搬。
    """
    ctx = FakeContext(by_id={"mimo_stt": FakeSTTProvider(provider_id="mimo_stt")})
    with pytest.raises(envelope.ToolError) as exc_info:
        await providers.resolve_audio_provider(ctx, "mimo_stt", UMO)
    assert _code(exc_info) == envelope.ERR_PROVIDER_CAPABILITY_MISMATCH


@pytest.mark.asyncio
async def test_audio_step1_non_provider_object_is_capability_mismatch() -> None:
    ctx = FakeContext(by_id={"x": object()})
    with pytest.raises(envelope.ToolError) as exc_info:
        await providers.resolve_audio_provider(ctx, "x", UMO)
    assert _code(exc_info) == envelope.ERR_PROVIDER_CAPABILITY_MISMATCH


@pytest.mark.asyncio
async def test_audio_step1_text_only_chat_is_capability_mismatch() -> None:
    """反例②：是 chat provider 但 `modalities` 不含 `audio` ⇒ 能力面不匹配（零 API 花费）。"""
    ctx = FakeContext(by_id={"chat": FakeChatProvider(modalities=["text", "image"])})
    with pytest.raises(envelope.ToolError) as exc_info:
        await providers.resolve_audio_provider(ctx, "chat", UMO)
    assert _code(exc_info) == envelope.ERR_PROVIDER_CAPABILITY_MISMATCH


@pytest.mark.asyncio
async def test_audio_step1_absent_modalities_are_rejected_but_empty_list_accepted() -> None:
    """严格口径的音频侧：`modalities` 缺键 ⇒ 不支持；`[]`（迁移遗留）⇒ 支持。"""
    with pytest.raises(envelope.ToolError) as exc_info:
        await providers.resolve_audio_provider(
            FakeContext(by_id={"none": FakeChatProvider(modalities=None)}), "none", UMO
        )
    assert _code(exc_info) == envelope.ERR_PROVIDER_CAPABILITY_MISMATCH

    legacy = FakeChatProvider(modalities=[], model_name="legacy-audio")
    assert await providers.resolve_audio_provider(
        FakeContext(by_id={"legacy": legacy}), "legacy", UMO
    ) is legacy


@pytest.mark.asyncio
async def test_audio_step2_empty_id_falls_back_to_current_chat_provider() -> None:
    prov = audio_chat(model_name="core-chat")
    ctx = FakeContext(using_provider=prov)
    assert await providers.resolve_audio_provider(ctx, "", UMO) is prov
    assert ctx.calls.named("get_using_provider_async")[0].args == (UMO,)
    assert not ctx.calls.named("get_provider_by_id"), "空串不该去查 id"


@pytest.mark.asyncio
async def test_audio_step2_capable_provider_missing_audio_is_audio_unavailable() -> None:
    """v0.2 的判定分裂：**没配**且回落也不符合 ⇒ `AUDIO_UNAVAILABLE`（hint 指向"去指定模型"）。"""
    ctx = FakeContext(using_provider=FakeChatProvider(modalities=["text"]))
    with pytest.raises(envelope.ToolError) as exc_info:
        await providers.resolve_audio_provider(ctx, "", UMO)
    assert _code(exc_info) == envelope.ERR_AUDIO_UNAVAILABLE


@pytest.mark.asyncio
async def test_audio_step2_no_current_provider_is_audio_unavailable() -> None:
    with pytest.raises(envelope.ToolError) as exc_info:
        await providers.resolve_audio_provider(FakeContext(using_provider=None), "", UMO)
    assert _code(exc_info) == envelope.ERR_AUDIO_UNAVAILABLE


@pytest.mark.asyncio
async def test_audio_step2_non_provider_return_is_audio_unavailable() -> None:
    """回落拿到一个"长得像 provider"的东西（不是 `Provider` 实例）⇒ 仍判缺配置。"""
    ctx = FakeContext(using_provider=NotAProvider(modalities=["audio"]))
    with pytest.raises(envelope.ToolError) as exc_info:
        await providers.resolve_audio_provider(ctx, "", UMO)
    assert _code(exc_info) == envelope.ERR_AUDIO_UNAVAILABLE


@pytest.mark.asyncio
async def test_audio_step2_survives_core_value_error() -> None:
    """核心在"解析出的 provider 不是文本生成类型"时抛 `ValueError`（manager.py:245-258）。

    那是"没有可用对话模型"，不是内部错误 ⇒ 吞成 `None`，最终 `AUDIO_UNAVAILABLE`。
    """

    class _RaisingCtx(FakeContext):
        async def get_using_provider_async(self, umo=None, *, locale="zh-CN"):  # type: ignore[override]
            self.calls.record("get_using_provider_async", umo, locale=locale)
            raise ValueError("resolved provider is not a chat provider")

    with pytest.raises(envelope.ToolError) as exc_info:
        await providers.resolve_audio_provider(_RaisingCtx(), "", UMO)
    assert _code(exc_info) == envelope.ERR_AUDIO_UNAVAILABLE


@pytest.mark.asyncio
async def test_audio_step2_survives_generic_exception() -> None:
    class _BrokenCtx(FakeContext):
        async def get_using_provider_async(self, umo=None, *, locale="zh-CN"):  # type: ignore[override]
            raise RuntimeError("boom")

    with pytest.raises(envelope.ToolError) as exc_info:
        await providers.resolve_audio_provider(_BrokenCtx(), "", UMO)
    assert _code(exc_info) == envelope.ERR_AUDIO_UNAVAILABLE


# --------------------------------------------------------------------------- #
# 3. 对话 / 视觉 provider 解析
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_chat_with_id_and_image_capability() -> None:
    chat = FakeChatProvider(modalities=["image"], model_name="vision-1")
    ctx = FakeContext(by_id={"vision-1": chat})
    assert await providers.resolve_vision_provider(ctx, "vision-1", UMO) is chat


@pytest.mark.asyncio
async def test_chat_with_id_missing_is_not_found() -> None:
    with pytest.raises(envelope.ToolError) as exc_info:
        await providers.resolve_vision_provider(FakeContext(by_id={}), "ghost", UMO)
    assert _code(exc_info) == envelope.ERR_PROVIDER_NOT_FOUND


@pytest.mark.asyncio
async def test_chat_with_non_provider_is_capability_mismatch() -> None:
    ctx = FakeContext(by_id={"x": object()})
    with pytest.raises(envelope.ToolError) as exc_info:
        await providers.resolve_vision_provider(ctx, "x", UMO)
    assert _code(exc_info) == envelope.ERR_PROVIDER_CAPABILITY_MISMATCH


@pytest.mark.asyncio
async def test_vision_rejects_text_only_provider() -> None:
    """严格口径的核心用例：有 Provider 但 `modalities` 不含 image ⇒ 明确错误码（零 API 花费）。"""
    chat = FakeChatProvider(modalities=["text"])
    ctx = FakeContext(by_id={"chat": chat})
    with pytest.raises(envelope.ToolError) as exc_info:
        await providers.resolve_vision_provider(ctx, "chat", UMO)
    assert _code(exc_info) == envelope.ERR_PROVIDER_CAPABILITY_MISMATCH


@pytest.mark.asyncio
async def test_vision_rejects_absent_modalities_but_accepts_empty_list() -> None:
    ctx_chat = FakeContext(by_id={"none": FakeChatProvider(modalities=None)})
    with pytest.raises(envelope.ToolError) as exc_info:
        await providers.resolve_vision_provider(ctx_chat, "none", UMO)
    assert _code(exc_info) == envelope.ERR_PROVIDER_CAPABILITY_MISMATCH

    legacy = FakeChatProvider(modalities=[])
    assert await providers.resolve_vision_provider(FakeContext(by_id={"legacy": legacy}), "legacy", UMO) is legacy


@pytest.mark.asyncio
async def test_summary_path_does_not_require_image_capability() -> None:
    """摘要是纯文本调用：`require_image=False` 时 text-only provider 也允许（SPEC §9 第 11 步）。"""
    chat = FakeChatProvider(modalities=["text"])
    ctx = FakeContext(by_id={"chat": chat})
    resolved = await providers.resolve_chat_provider(ctx, "chat", UMO, require_image=False)
    assert resolved is chat


@pytest.mark.asyncio
async def test_empty_vision_id_falls_back_to_core_current_provider() -> None:
    chat = FakeChatProvider(modalities=["image"])
    ctx = FakeContext(using_provider=chat)
    assert await providers.resolve_vision_provider(ctx, "", UMO) is chat
    assert ctx.calls.named("get_using_provider_async")[0].args == (UMO,)


@pytest.mark.asyncio
async def test_empty_vision_id_without_current_provider_is_vision_unavailable() -> None:
    with pytest.raises(envelope.ToolError) as exc_info:
        await providers.resolve_vision_provider(FakeContext(using_provider=None), "", UMO)
    assert _code(exc_info) == envelope.ERR_VISION_UNAVAILABLE


@pytest.mark.asyncio
async def test_empty_vision_id_with_text_only_fallback_is_vision_unavailable() -> None:
    """v0.2 覆盖 v0.1 的一处判据：**回落**到的 provider 没有 image ⇒ `VISION_UNAVAILABLE`。

    v0.1 这里判 `PROVIDER_CAPABILITY_MISMATCH`，而那条 hint 让人"改选或去勾复选框"——
    真实病因是"你根本没指定视觉模型"。判定分裂后 hint 才对得上操作。
    """
    ctx = FakeContext(using_provider=FakeChatProvider(modalities=["text"]))
    with pytest.raises(envelope.ToolError) as exc_info:
        await providers.resolve_vision_provider(ctx, "", UMO)
    assert _code(exc_info) == envelope.ERR_VISION_UNAVAILABLE


@pytest.mark.asyncio
async def test_empty_vision_id_survives_core_value_error() -> None:
    class _RaisingCtx(FakeContext):
        async def get_using_provider_async(self, umo=None, *, locale="zh-CN"):  # type: ignore[override]
            self.calls.record("get_using_provider_async", umo, locale=locale)
            raise ValueError("not a Provider")

    with pytest.raises(envelope.ToolError) as exc_info:
        await providers.resolve_vision_provider(_RaisingCtx(), "", UMO)
    assert _code(exc_info) == envelope.ERR_VISION_UNAVAILABLE


@pytest.mark.asyncio
async def test_resolve_vision_provider_is_require_image_true_alias() -> None:
    """`resolve_vision_provider` 必须等价于 `resolve_chat_provider(require_image=True)`。"""
    text_only = FakeChatProvider(modalities=["text"])
    ctx = FakeContext(by_id={"c": text_only})
    with pytest.raises(envelope.ToolError) as exc_info:
        await providers.resolve_vision_provider(ctx, "c", UMO)
    assert _code(exc_info) == envelope.ERR_PROVIDER_CAPABILITY_MISMATCH


@pytest.mark.asyncio
async def test_configured_values_and_error_codes_never_leak_into_hints() -> None:
    """`log_detail` 可以带 provider id，`hint` 不许（hint 是常量，会全量进生产日志）。"""
    ctx = FakeContext(by_id={"mimo_stt_9f3a": FakeSTTProvider(provider_id="mimo_stt_9f3a")})
    with pytest.raises(envelope.ToolError) as exc_info:
        await providers.resolve_audio_provider(ctx, "mimo_stt_9f3a", UMO)
    exc = exc_info.value
    assert "mimo_stt_9f3a" in exc.log_detail
    assert "mimo_stt_9f3a" not in envelope.HINTS[exc.code]
