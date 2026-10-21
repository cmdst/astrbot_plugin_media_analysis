"""provider 解析阶梯 + 能力判定（SPEC §2.2）。

两个同名 `get_provider_by_id` 的坑（本模块的注释就是为此写的）：
`Context.get_provider_by_id` 是**同步**（`star/context.py:447-470`，插件只拿得到它），
`ProviderManager.get_provider_by_id` 是**异步**（`provider/manager.py:221-223`）。
规格统一用 `Context` 的那个，**不要 `await`**。
"""

from __future__ import annotations

from astrbot.api import logger as LOG
from astrbot.api.provider import Provider, STTProvider

from . import envelope

# `modalities` 的可选值由核心定义：["text","image","audio","tool_use"]
# （`config/default.py:2246-2255`）。
MODALITY_IMAGE = "image"


def supports_modality(provider: object, modality: str) -> bool:
    """严格口径（SPEC §2.2）：`modalities is None` ⇒ 不支持。

    与核心 `astr_main_agent.py:1412-1417` 的 `_provider_supports_modality` 同口径：
    迁移遗留的**空列表**按"未配置"处理（算支持），`None`/缺省算不支持。
    已知核心内部不一致：`agent/runners/tool_loop_agent_runner.py:1109-1113` 用宽松口径
    （空/None 都算支持）；本插件取严格一侧，理由见 SPEC §2.2（严格口径给出可操作
    的错误码且零 API 花费）。
    """
    config = getattr(provider, "provider_config", None)
    if not isinstance(config, dict):
        return False
    modalities = config.get("modalities", None)
    if isinstance(modalities, list) and not modalities:
        return True
    return isinstance(modalities, list) and modality in modalities


def provider_id_of(provider: object) -> str:
    """取 provider 的配置 id，用于 `notes` 溯源；取不到就回 `"unknown"`。

    provider id 是**配置值不是凭据**（核心自己也这么打日志，`context.py:464-470`）。
    """
    config = getattr(provider, "provider_config", None)
    if isinstance(config, dict):
        value = config.get("id")
        if isinstance(value, str) and value:
            return value
    return "unknown"


async def resolve_stt_provider(context, provider_id: str, umo: str) -> STTProvider:
    """五级阶梯（SPEC §2.2 冻结）：① 配的 id → ② 核心当前 STT → ③ 当前 STT 实例
    （绕过 `enable`）→ ④ 任一已加载 STT → ⑤ `STT_UNAVAILABLE`。

    为什么需要 ③：生产 `provider_stt_settings.enable=False`（SPEC §1 #14），此时
    `get_using_stt_provider_async` **直接返回 None**（`provider/manager.py:260-266`），
    而 `curr_stt_provider_inst` 是 `initialize()` 里**不看 enable** 兜出来的
    （`provider/manager.py:385-392`）。
    """
    if provider_id:
        prov = context.get_provider_by_id(provider_id)  # 同步，别 await
        if prov is None:
            raise envelope.ToolError(
                envelope.ERR_PROVIDER_NOT_FOUND,
                log_detail=f"stt_provider_id={provider_id}",
            )
        if not isinstance(prov, STTProvider):
            raise envelope.ToolError(
                envelope.ERR_PROVIDER_CAPABILITY_MISMATCH,
                log_detail=f"stt_provider_id={provider_id} type={type(prov).__name__}",
            )
        return prov

    try:
        prov = await context.get_using_stt_provider_async(umo)
    except ValueError as exc:  # 核心在"解析出的不是 STT"时抛 ValueError
        LOG.warning(
            "media_analysis: get_using_stt_provider_async 类型不符（%s），继续下落",
            type(exc).__name__,
        )
        prov = None
    if isinstance(prov, STTProvider):
        return prov

    manager = getattr(context, "provider_manager", None)
    prov = getattr(manager, "curr_stt_provider_inst", None)
    if isinstance(prov, STTProvider):
        return prov

    getter = getattr(context, "get_all_stt_providers", None)
    insts: list[STTProvider] = []
    if callable(getter):
        try:
            insts = list(getter() or [])
        except Exception as exc:  # noqa: BLE001 —— 阶梯不许把异常漏出去
            LOG.warning(
                "media_analysis: get_all_stt_providers 失败（%s）", type(exc).__name__
            )
            insts = []
    if insts:
        return insts[0]

    raise envelope.ToolError(envelope.ERR_STT_UNAVAILABLE)


async def resolve_chat_provider(
    context,
    provider_id: str,
    umo: str,
    *,
    require_image: bool,
) -> Provider:
    """对话 provider 解析：配了 id 就用它，空串回落核心当前对话 provider。

    `require_image=True` 时追加严格的能力判定（`modalities` 必须含 `image`），
    否则只要求是 `Provider`（摘要阶段是纯文本调用，不该被图像能力挡住）。
    """
    if provider_id:
        prov = context.get_provider_by_id(provider_id)  # 同步，别 await
        if prov is None:
            raise envelope.ToolError(
                envelope.ERR_PROVIDER_NOT_FOUND,
                log_detail=f"vision_provider_id={provider_id}",
            )
        if not isinstance(prov, Provider):
            raise envelope.ToolError(
                envelope.ERR_PROVIDER_CAPABILITY_MISMATCH,
                log_detail=(
                    f"vision_provider_id={provider_id} type={type(prov).__name__}"
                ),
            )
    else:
        try:
            prov = await context.get_using_provider_async(umo)
        except ValueError as exc:
            LOG.warning(
                "media_analysis: get_using_provider_async 类型不符（%s）",
                type(exc).__name__,
            )
            prov = None
        if not isinstance(prov, Provider):
            raise envelope.ToolError(envelope.ERR_VISION_UNAVAILABLE)

    if require_image and not supports_modality(prov, MODALITY_IMAGE):
        raise envelope.ToolError(
            envelope.ERR_PROVIDER_CAPABILITY_MISMATCH,
            log_detail=f"provider={provider_id_of(prov)} modalities 不含 image",
        )
    return prov


async def resolve_vision_provider(context, provider_id: str, umo: str) -> Provider:
    """读图专用入口 = `resolve_chat_provider(..., require_image=True)`。"""
    return await resolve_chat_provider(
        context, provider_id, umo, require_image=True
    )
