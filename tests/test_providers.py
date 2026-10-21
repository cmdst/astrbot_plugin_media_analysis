"""provider 解析阶梯与能力判定（SPEC §2.2 / §5 的 provider 类错误码）。

两组契约：

1. **五级阶梯**（`providers.resolve_stt_provider`）：① 配置的 id → ② 核心当前 STT →
   ③ `curr_stt_provider_inst`（**绕过 `enable`** 的那一步，生产 `enable=False` 时唯一能兜到
   `mimo_stt` 的路）→ ④ 任一已加载 STT → ⑤ `STT_UNAVAILABLE`。
2. **严格能力口径**（`supports_modality`）：`modalities is None`/缺省 ⇒ 不支持；
   `[]`（迁移遗留空列表）⇒ 支持；含 `image` ⇒ 支持。与核心 `astr_main_agent.py:1412-1417`
   同口径，和 `tool_loop_agent_runner.py:1109-1113` 的宽松口径**刻意不同**。

本文件还有一个隐形守护：`Context.get_provider_by_id` 是**同步**的
（`star/context.py:447-470`），`FakeContext` 返回普通对象 ⇒ 代码若写成 `await` 会立刻
`TypeError`，测试自然红。
"""

from __future__ import annotations

import pytest

from astrbot_plugin_media_analysis import envelope, providers
from _fakes import FakeChatProvider, FakeContext, FakeSTTProvider, NotAProvider

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
    # 严格口径：缺 modalities 键 ⇒ 不支持（宽松口径的另一侧是 VISION_FAILED，不可操作）
    assert providers.supports_modality(FakeChatProvider(modalities=None), "image") is False


def test_supports_modality_rejects_non_dict_provider_config() -> None:
    assert providers.supports_modality(object(), "image") is False
    assert providers.supports_modality(NotAProvider(modalities=["image"]), "image") is True
    assert providers.supports_modality(NotAProvider(), "image") is False


def test_provider_id_of_reads_config_id_only() -> None:
    assert providers.provider_id_of(FakeSTTProvider(provider_id="mimo_stt")) == "mimo_stt"
    assert providers.provider_id_of(FakeChatProvider()) == "fake-chat"
    assert providers.provider_id_of(object()) == "unknown"
    assert providers.provider_id_of(NotAProvider()) == "unknown"


# --------------------------------------------------------------------------- #
# 2. STT 阶梯 ①–⑤
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_stt_step1_configured_id_wins_and_is_looked_up_synchronously() -> None:
    stt = FakeSTTProvider(provider_id="mimo_stt")
    ctx = FakeContext(by_id={"mimo_stt": stt}, using_stt=FakeSTTProvider(provider_id="other"))
    resolved = await providers.resolve_stt_provider(ctx, "mimo_stt", UMO)
    assert resolved is stt
    assert ctx.provider_ids_asked() == ["mimo_stt"]  # 同步查表，且只查一次
    assert not ctx.calls.named("get_using_stt_provider_async"), "配了 id 就不该再走回落"


@pytest.mark.asyncio
async def test_stt_step1_unknown_id_is_provider_not_found() -> None:
    ctx = FakeContext(by_id={})
    with pytest.raises(envelope.ToolError) as exc_info:
        await providers.resolve_stt_provider(ctx, "ghost", UMO)
    assert _code(exc_info) == envelope.ERR_PROVIDER_NOT_FOUND


@pytest.mark.asyncio
async def test_stt_step1_wrong_provider_type_is_capability_mismatch() -> None:
    """STT 位置拿到对话 provider：`isinstance(prov, STTProvider)` 判据（不看 modalities）。"""
    ctx = FakeContext(by_id={"chat": FakeChatProvider()})
    with pytest.raises(envelope.ToolError) as exc_info:
        await providers.resolve_stt_provider(ctx, "chat", UMO)
    assert _code(exc_info) == envelope.ERR_PROVIDER_CAPABILITY_MISMATCH


@pytest.mark.asyncio
async def test_stt_step2_uses_current_stt_provider() -> None:
    stt = FakeSTTProvider(provider_id="core_stt")
    ctx = FakeContext(using_stt=stt)
    assert await providers.resolve_stt_provider(ctx, "", UMO) is stt
    assert not ctx.calls.named("get_provider_by_id"), "空串不该去查 id"


@pytest.mark.asyncio
async def test_stt_step3_curr_inst_bypasses_disabled_switch() -> None:
    """生产 `provider_stt_settings.enable=False` ⇒ ② 返回 None，③ 是唯一能兜到 mimo_stt 的路。"""
    stt = FakeSTTProvider(provider_id="mimo_stt")
    ctx = FakeContext(using_stt=None, curr_stt=stt)
    assert await providers.resolve_stt_provider(ctx, "", UMO) is stt


@pytest.mark.asyncio
async def test_stt_step3_is_skipped_when_provider_manager_missing() -> None:
    stt = FakeSTTProvider(provider_id="fallback")
    ctx = FakeContext(using_stt=None, has_provider_manager=False, all_stt=[stt])
    assert await providers.resolve_stt_provider(ctx, "", UMO) is stt


@pytest.mark.asyncio
async def test_stt_step3_ignores_wrong_type_instance() -> None:
    stt = FakeSTTProvider(provider_id="real")
    ctx = FakeContext(using_stt=None, curr_stt=FakeChatProvider(), all_stt=[stt])
    assert await providers.resolve_stt_provider(ctx, "", UMO) is stt


@pytest.mark.asyncio
async def test_stt_step4_uses_first_loaded_stt() -> None:
    first, second = FakeSTTProvider(provider_id="a"), FakeSTTProvider(provider_id="b")
    ctx = FakeContext(using_stt=None, all_stt=[first, second])
    assert await providers.resolve_stt_provider(ctx, "", UMO) is first


@pytest.mark.asyncio
async def test_stt_step5_raises_unavailable() -> None:
    ctx = FakeContext(using_stt=None, all_stt=[])
    with pytest.raises(envelope.ToolError) as exc_info:
        await providers.resolve_stt_provider(ctx, "", UMO)
    assert _code(exc_info) == envelope.ERR_STT_UNAVAILABLE


@pytest.mark.asyncio
async def test_stt_ladder_survives_core_value_error() -> None:
    """核心的 `get_using_stt_provider_async` 拿到非 STT 时会抛 `ValueError`（context.py:628-630）。

    阶梯不许把这个异常漏出去：降级下落，最终拿到 ④ 的实例。
    """

    class _RaisingCtx(FakeContext):
        async def get_using_stt_provider_async(self, umo=None):  # type: ignore[override]
            self.calls.record("get_using_stt_provider_async", umo)
            raise ValueError("resolved provider is not an STT provider")

    stt = FakeSTTProvider(provider_id="via_all")
    ctx = _RaisingCtx(using_stt=None, all_stt=[stt])
    assert await providers.resolve_stt_provider(ctx, "", UMO) is stt


@pytest.mark.asyncio
async def test_stt_ladder_survives_get_all_failure() -> None:
    class _BrokenCtx(FakeContext):
        def get_all_stt_providers(self):  # type: ignore[override]
            raise RuntimeError("boom")

    ctx = _BrokenCtx(using_stt=None)
    with pytest.raises(envelope.ToolError) as exc_info:
        await providers.resolve_stt_provider(ctx, "", UMO)
    assert _code(exc_info) == envelope.ERR_STT_UNAVAILABLE


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
    """摘要是纯文本调用：`require_image=False` 时 text-only provider 也允许（SPEC §6.1）。"""
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
