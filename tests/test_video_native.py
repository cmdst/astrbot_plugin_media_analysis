"""原生视频通道 `video_native.py` 的单元守卫（SPEC-v0.3 §7.2 / §10.2 / §16.1-R1）。

这一层守的是**最容易悄悄坏掉**的部分：形状与三条护栏。它们都发生在 `provider.client`
这条**核心内部面**上（`astrbot.api.*` 里没有 `video_urls`，核心 `text_chat` 的形参没有
video、`assemble_context` 对未知块直接 `ValueError`）：

1. **形状**：`{"type":"video_url","video_url":{"url":"data:video/mp4;base64,…"}}` + 一个 text 块；
   `model` / `stream=False` / `max_tokens`；
2. **`max_tokens` 硬下限 1024**：mimo 在 64 时 **200 但 content 空**（全进 reasoning）；
3. **空文本判失败**：200 ≠ 看见，`video_native_empty` 必须走回退，不能记 `ok:true`；
4. **自建超时**：底层调用绕过核心的 `retry_provider_request`/超时 ⇒ 插件自己 `wait_for`；
5. **缺失即回退**：没有 `client` / 适配器不对 ⇒ `unavailable`，**绝不外抛**；
6. **内存护栏**：超 `HARD_MAX_DIRECT_BYTES` ⇒ **一次都不读文件**（先 `stat` 再读）。

失败形态一律是 `NativeOutcome(ok=False)`：这条链路坏了只能是"少一条路"，不能是"插件坏了"。
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from astrbot_plugin_media_analysis import media, video_native
from _fakes import FakeChatProvider, native_video


def _write_video(tmp_path: Path, payload: bytes = b"\x00\x01abc" * 100) -> Path:
    path = tmp_path / "clip.mp4"
    path.write_bytes(payload)
    return path


# --------------------------------------------------------------------------- #
# 1. channel_reason：没有原生通道 vs 配置写错
# --------------------------------------------------------------------------- #
def test_openai_adapter_with_a_client_has_a_native_channel() -> None:
    assert video_native.channel_reason(native_video()) == ""


def test_other_adapters_have_no_native_channel() -> None:
    """不是 `openai_chat_completion` ⇒ `adapter_not_openai`（回退，**不**判配置错误）。

    用户在面板上换一个适配器（gemini/anthopic/本地 vllm…）是**正常操作**：形状没验过 ≠ 配置错。
    """
    provider = native_video(adapter_type="gemini_chat_completion")
    assert video_native.channel_reason(provider) == "adapter_not_openai"


def test_missing_client_is_unavailable_not_an_exception() -> None:
    """核心内部面搬家 / 换 SDK 形状 ⇒ `no_client`（回退），绝不 import 期或调用期炸。"""
    provider = native_video(with_client=False)
    assert video_native.channel_reason(provider) == "no_client"


def test_plain_objects_without_provider_config_are_rejected() -> None:
    class _Bare:
        pass

    assert video_native.channel_reason(_Bare()) == "adapter_not_openai"


def test_channel_reason_deliberately_ignores_modalities() -> None:
    """**刻意不读 `modalities`**（SPEC-v0.3 §16.2-2）：核心没有 `video` 选项，读了恒 False。

    读取它会让"能直送"的模型被判成不可用（因为没有任何 provider 会勾上一个不存在的选项）。
    """
    provider = native_video()
    provider.provider_config["modalities"] = ["text"]  # 连 image 都没有
    assert video_native.channel_reason(provider) == ""


# --------------------------------------------------------------------------- #
# 2. 形状（§7.2 冻结）
# --------------------------------------------------------------------------- #
def test_build_messages_has_exactly_one_text_block_and_one_video_block() -> None:
    messages = video_native.build_messages("看这段视频", "data:video/mp4;base64,AAA")
    assert len(messages) == 1
    message = messages[0]
    assert message["role"] == "user"
    assert [block["type"] for block in message["content"]] == ["text", "video_url"]
    assert message["content"][0]["text"] == "看这段视频"
    assert message["content"][1]["video_url"] == {"url": "data:video/mp4;base64,AAA"}


def test_video_block_type_and_data_prefix_are_frozen_literals() -> None:
    """t8 唯一验过的形状；改一个字符就是换协议（`image_url` 的键名不能混用）。"""
    assert video_native.VIDEO_BLOCK_TYPE == "video_url"
    assert video_native.DATA_URL_PREFIX == "data:video/mp4;base64,"
    assert video_native.ADAPTER_TYPE == "openai_chat_completion"
    assert video_native.MAX_TOKENS_FLOOR == 1024
    assert video_native.HARD_MAX_DIRECT_BYTES == 32 * 1024 * 1024


def test_model_of_prefers_get_model_then_config_then_unknown() -> None:
    provider = native_video(model_name="mimo-v2.6")
    assert video_native.model_of(provider) == "mimo-v2.6"

    class _ConfigOnly:
        provider_config = {"model": "gemini-3-flash"}

    assert video_native.model_of(_ConfigOnly()) == "gemini-3-flash"

    class _Nothing:
        provider_config = {}

    assert video_native.model_of(_Nothing()) == "unknown"


def test_completion_text_tolerates_the_shapes_we_have_seen() -> None:
    """`content` 可能是 str、也可能是 part 列表；形状不符 ⇒ `""`（由调用方判空）。"""

    def _completion(message):
        return type("C", (), {"choices": [type("X", (), {"message": message})()]})()

    assert video_native.completion_text(_completion(type("M", (), {"content": "  答  "})())) == "答"
    parts = [{"text": "第一"}, {"text": "第二"}, {"type": "image"}]
    assert video_native.completion_text(_completion(type("M", (), {"content": parts})())) == "第一第二"
    assert video_native.completion_text(_completion(type("M", (), {"content": None})())) == ""
    assert video_native.completion_text(object()) == ""
    assert video_native.completion_text(type("C", (), {"choices": []})()) == ""


# --------------------------------------------------------------------------- #
# 3. 成功路径的形状与护栏
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_success_records_one_call_with_the_frozen_shape(tmp_path: Path) -> None:
    payload = b"\x00\x01abc" * 100
    path = _write_video(tmp_path, payload)
    provider = native_video(text="画面里有一个数字 7319")

    outcome = await video_native.call_native(
        provider, prompt="看这段视频", video_path=str(path), max_tokens=1024, timeout=5
    )

    assert outcome.ok is True
    assert outcome.note == video_native.NOTE_OK
    assert outcome.text == "画面里有一个数字 7319"
    assert isinstance(outcome.elapsed_ms, int) and outcome.elapsed_ms >= 0

    calls = provider.payloads()
    assert len(calls) == 1, calls
    call = calls[0]
    assert call["model"] == "fake-native"
    assert call["stream"] is False
    assert call["max_tokens"] == 1024
    # 内联 base64 的 data URL，且**就是文件字节**（没有截断、没有二次编码）
    url = provider.video_urls()[0]
    assert url.startswith(video_native.DATA_URL_PREFIX)
    assert base64.b64decode(url[len(video_native.DATA_URL_PREFIX) :]) == payload
    assert provider.prompt_texts() == ["看这段视频"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("configured", "expected"), [(64, 1024), (1, 1024), (1024, 1024), (2048, 2048)])
async def test_max_tokens_is_clamped_up_to_the_floor(
    tmp_path: Path, configured: int, expected: int
) -> None:
    """护栏①：`max_tokens < 1024` ⇒ **拉回 1024**（"传了什么就是什么"会踩 mimo 的空文本）。

    64 是 t8 实测值：那一次 **200 但 `content` 为空**（全被 reasoning 吃掉）。所以这里
    断言的不是"参数透传"，而是"低于下限被抬上去"。
    """
    path = _write_video(tmp_path)
    provider = native_video()
    outcome = await video_native.call_native(
        provider, prompt="p", video_path=str(path), max_tokens=configured, timeout=5
    )
    assert outcome.ok is True
    assert provider.payloads()[0]["max_tokens"] == expected


# --------------------------------------------------------------------------- #
# 4. 护栏②：空文本 ≠ 看见
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["", "   ", "\n\t "], ids=["empty", "spaces", "newlines"])
async def test_empty_content_is_a_failure_not_a_success(tmp_path: Path, content: str) -> None:
    """200 + 空文本 ⇒ `video_native_empty`（**不是** `ok:true`）。

    这是 v0.3 最贵的一条"假成功"：mimo 在 `max_tokens=64` 时就是 200 但正文为空，
    只看状态码会把"没看见"记成"看见了"，还把结果写进缓存。
    """
    path = _write_video(tmp_path)
    provider = native_video(text=content)
    outcome = await video_native.call_native(
        provider, prompt="p", video_path=str(path), max_tokens=1024, timeout=5
    )

    assert outcome.ok is False
    assert outcome.note == video_native.NOTE_EMPTY
    assert outcome.text == ""


@pytest.mark.asyncio
async def test_empty_content_reports_reasoning_length_only_in_detail(tmp_path: Path) -> None:
    """空文本时读一眼 `reasoning_content` 长度（只进 `detail`/日志，**不进信封正文**）。"""
    path = _write_video(tmp_path)
    provider = native_video(text="", reasoning=1234)
    outcome = await video_native.call_native(
        provider, prompt="p", video_path=str(path), max_tokens=1024, timeout=5
    )
    assert outcome.note == video_native.NOTE_EMPTY
    assert "reasoning_chars=1234" in outcome.detail


# --------------------------------------------------------------------------- #
# 5. 护栏③：异常/超时一律收成 ok=False（依赖这个函数绝不外抛）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_sdk_exception_becomes_a_fallback_signal(tmp_path: Path) -> None:
    path = _write_video(tmp_path)
    provider = native_video(fail_with=RuntimeError("upstream 400"))
    outcome = await video_native.call_native(
        provider, prompt="p", video_path=str(path), max_tokens=1024, timeout=5
    )
    assert outcome.ok is False
    assert outcome.note == video_native.NOTE_FAILED
    assert outcome.detail == "RuntimeError", "detail 只记异常类名（不提正文/路径）"


@pytest.mark.asyncio
async def test_self_built_timeout_is_reported(tmp_path: Path) -> None:
    """底层调用不走核心的超时/重试 ⇒ 插件自己 `wait_for`；超时 ⇒ `detail=timeout <s>`。"""
    path = _write_video(tmp_path)
    provider = native_video(delay=0.3)
    outcome = await video_native.call_native(
        provider, prompt="p", video_path=str(path), max_tokens=1024, timeout=0.05
    )
    assert outcome.ok is False
    assert outcome.note == video_native.NOTE_FAILED
    assert outcome.detail.startswith("timeout ")


@pytest.mark.asyncio
async def test_missing_client_returns_unavailable(tmp_path: Path) -> None:
    path = _write_video(tmp_path)
    provider = native_video(with_client=False)
    outcome = await video_native.call_native(
        provider, prompt="p", video_path=str(path), max_tokens=1024, timeout=5
    )
    assert outcome.ok is False
    assert outcome.note == video_native.NOTE_UNAVAILABLE
    assert outcome.text == ""


@pytest.mark.asyncio
async def test_client_that_is_not_callable_is_unavailable(tmp_path: Path) -> None:
    """`client.chat.completions.create` 存在但不可调 ⇒ 也按"没有通道"处理。"""
    path = _write_video(tmp_path)
    provider = native_video()
    provider.client.chat.completions.create = "not-a-callable"  # type: ignore[assignment]
    outcome = await video_native.call_native(
        provider, prompt="p", video_path=str(path), max_tokens=1024, timeout=5
    )
    assert outcome.note == video_native.NOTE_UNAVAILABLE


# --------------------------------------------------------------------------- #
# 6. 内存护栏（§10.2）：先 stat，再读；超硬上限一次都不读
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_oversize_video_is_rejected_without_reading_a_single_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T9：超过 `HARD_MAX_DIRECT_BYTES` ⇒ **`media.read_bytes` 一次都不许被调用**。

    用 monkeypatch 让 `read_bytes` 直接炸：只要实现里"先读后判"，这条就红。
    32 MiB 的硬上限是内存护栏的最后一道（Python 侧 base64 峰值 ≈42.6 MiB）。
    """
    path = _write_video(tmp_path)

    def _must_not_read(_path: str) -> bytes:
        raise AssertionError("超限文件被读进来了（内存护栏失效）")

    monkeypatch.setattr(media, "read_bytes", _must_not_read)
    monkeypatch.setattr(
        media, "_size_of", lambda _path: video_native.HARD_MAX_DIRECT_BYTES + 1
    )

    outcome = await video_native.call_native(
        native_video(), prompt="p", video_path=str(path), max_tokens=1024, timeout=5
    )

    assert outcome.ok is False
    assert outcome.note == video_native.NOTE_FAILED
    assert outcome.detail.startswith("too_large ")


@pytest.mark.asyncio
async def test_stat_failure_is_a_fallback_signal(tmp_path: Path) -> None:
    """文件不存在/stat 失败 ⇒ `detail="stat"`（不是裸 `OSError`）。"""
    outcome = await video_native.call_native(
        native_video(),
        prompt="p",
        video_path=str(tmp_path / "nope.mp4"),
        max_tokens=1024,
        timeout=5,
    )
    assert outcome.ok is False
    assert outcome.note == video_native.NOTE_FAILED
    assert outcome.detail == "stat"


@pytest.mark.asyncio
async def test_read_failure_is_a_fallback_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stat 过了但读失败（权限/竞态删除）⇒ 只记异常类名。"""
    path = _write_video(tmp_path)

    def _boom(_path: str) -> bytes:
        raise PermissionError("denied")

    monkeypatch.setattr(media, "read_bytes", _boom)
    outcome = await video_native.call_native(
        native_video(), prompt="p", video_path=str(path), max_tokens=1024, timeout=5
    )
    assert outcome.ok is False
    assert outcome.detail == "read PermissionError"


# --------------------------------------------------------------------------- #
# 7. 与"核心 text_chat"的分界（形状不可退回 v0.2）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_native_call_never_uses_text_chat(tmp_path: Path) -> None:
    """视频位只走 `provider.client`；`text_chat` 一次都不该被调用。

    `FakeNativeProvider.text_chat` 直接抛 `AssertionError` —— 谁把原生链路退回 v0.2 的
    `text_chat(image_urls=…)`，这条立刻红（核心对 video 块会 `ValueError`，见 §1.2）。
    """
    path = _write_video(tmp_path)
    provider = native_video()
    outcome = await video_native.call_native(
        provider, prompt="p", video_path=str(path), max_tokens=1024, timeout=5
    )
    assert outcome.ok is True
    assert not provider.calls.named("text_chat")


def test_a_plain_chat_provider_has_no_native_channel() -> None:
    """普通 `FakeChatProvider`（适配器对、但实例上没有 `client`）⇒ `no_client`。

    这正是"v0.2 的 provider 形状不够用"的最小证据：能 `text_chat` 不等于能直送视频。
    """
    provider = FakeChatProvider()
    assert provider.provider_config.get("type") == "openai_chat_completion"
    assert not hasattr(provider, "client")
    assert video_native.channel_reason(provider) == "no_client"


# --------------------------------------------------------------------------- #
# 评审 F10：核心内部面的属性也可能**抛异常**，不许落成 INTERNAL_ERROR
# --------------------------------------------------------------------------- #
class _ExplodingClient:
    """`client` 是个会抛异常的 property（核心换实现 / 懒加载失败都会长这样）。"""

    provider_config = {"type": "openai_chat_completion", "id": "boom", "model": "m"}
    model_name = "m"

    @property
    def client(self):
        raise RuntimeError("内部面搬家了")

    def get_model(self) -> str:
        return self.model_name


@pytest.mark.asyncio
async def test_client_property_that_raises_is_a_missing_channel(tmp_path: Path) -> None:
    """**评审 F10**：属性访问抛错 ⇒ `no_client` / `video_native_unavailable`（不是 INTERNAL_ERROR）。"""
    provider = _ExplodingClient()
    sample = tmp_path / "s.mp4"
    sample.write_bytes(b"\x00" * 2048)

    assert video_native.channel_reason(provider) == "no_client"

    outcome = await video_native.call_native(
        provider, prompt="q", video_path=str(sample), max_tokens=1024, timeout=5
    )
    assert outcome.ok is False
    assert outcome.note == video_native.NOTE_UNAVAILABLE
    assert "INTERNAL" not in outcome.detail


@pytest.mark.asyncio
async def test_provider_without_provider_config_is_adapter_mismatch(tmp_path: Path) -> None:
    """`provider_config` 取不到 ⇒ `adapter_not_openai`（保守：不猜它能不能发视频）。"""

    class _Bare:
        @property
        def provider_config(self):
            raise RuntimeError("boom")

    assert video_native.channel_reason(_Bare()) == "adapter_not_openai"
