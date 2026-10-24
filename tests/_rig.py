"""测试台：真组件 + 假媒体层 + 假 provider 层（`test_analyze.py` / `test_video_chain.py` 共用）。

为什么单开一个模块：v0.3 的主链路测试分成两个文件（v0.2 语义骨架 / v0.3 原生-压缩-回退链），
两边的"搭台"必须**是同一套** —— 否则一个文件修好了台、另一个文件还在用旧台，
就会出现"同一份实现一半绿一半红"的假象。

口径（每条都对应一处冻结契约）：
- 假 provider 继承**真 ABC**（`FakeChatProvider(Provider)` / `FakeNativeProvider(Provider)`）；
- `provider.provider_config` 是能力判定的唯一读点（`providers.supports_modality`）；
- 视频位走 provider 底层 `client`（`FakeNativeProvider.text_chat` 刻意抛错）；
- 媒体层只桩**重 IO**（`classify` / `ensure_provider_wav` / `extract_frames` / `content_digest`），
  取件面（`find_media` / `resolve_media_path`）保持真实现；
- 探测（`probe.ensure_l1`）默认桩成"通过"：真 L1 要跑 ffmpeg + 一次模型调用，行为在
  `tests/test_probe.py`，这里只测"探测结果如何影响链路"（`probe_ok` / `probe_source`）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from astrbot_plugin_media_analysis import analyze as analyze_mod
from astrbot_plugin_media_analysis import background, envelope
from astrbot_plugin_media_analysis import cache as cache_mod
from astrbot_plugin_media_analysis import main, media, probe, providers, settings
from _fakes import (
    FakeChatProvider,
    FakeContext,
    FakeNativeProvider,
    audio_chat,
    make_event,
    make_record,
    make_video,
)

# --------------------------------------------------------------------------- #
# 测试台：真组件 + 假媒体层 + 假 provider 层
# --------------------------------------------------------------------------- #
@dataclass
class Rig:
    media_path: Path
    event: object
    context: FakeContext
    settings: settings.Settings
    cache: cache_mod.DiskCache
    plugin: main.MediaAnalysisPlugin
    media_calls: dict[str, int]
    prov_calls: dict[str, int]
    audio: FakeChatProvider
    chat: FakeChatProvider
    native: FakeNativeProvider | None = None
    probe_calls: dict[str, int] = field(default_factory=dict)

    async def run(
        self,
        *,
        target: str = "current",
        focus: str = "",
        include_frames: bool = False,
        settings_obj: settings.Settings | None = None,
        cache_obj: cache_mod.DiskCache | None = None,
    ) -> dict[str, object]:
        """直接跑 `analyze.analyze`（受控失败会抛 `ToolError`）。"""
        return await analyze_mod.analyze(
            context=self.context,
            event=self.event,
            target=target,
            focus=focus,
            include_frames=include_frames,
            settings=settings_obj or self.settings,
            cache=cache_obj or self.cache,
        )

    async def tool(
        self,
        *,
        target: str = "current",
        focus: str = "",
        include_frames: bool = False,
    ) -> dict[str, object]:
        """走工具门面（模型看到的路径）：返回值必须是 JSON 字符串。

        v0.4 的门面**受理即返回**（SPEC-v0.4 §3），所以这里分两步：

        1. 调门面 → 必须是 `str`，解析后**要么**是受理信封（4 键）**要么**是失败信封（3 键）；
        2. 受理信封 ⇒ `await background.wait(task_id)` 拿后台任务的信封（成功 9 键 /
           任务内失败 3 键）。

        语义与 v0.3 的"一次调用拿到分析结果"**对齐**：断言面（9 键/3 键、错误码、notes）
        一个字都没改；变的只是"结果从哪来"（后台任务 vs 同步返回）。同步失败（`BAD_TARGET` /
        `MEDIA_NOT_FOUND` / `TOO_MANY_TASKS`）照样直接返回，不会被这一步吞掉。
        """
        raw = await self.plugin.media_analyze(
            self.event, target=target, focus=focus, include_frames=include_frames
        )
        assert isinstance(raw, str), f"工具返回值必须是 str，实际 {type(raw)}"
        payload = json.loads(raw)
        assert isinstance(payload, dict)
        if not payload.get("state"):
            return payload  # 同步失败信封（受理前就拒了）
        assert set(payload) == set(envelope.ACCEPT_KEYS), payload
        finished = await background.wait(str(payload["task_id"]), timeout=60.0)
        assert isinstance(finished, dict), "后台任务没有产出信封（被取消？）"
        return finished

    async def tool_expect_accept(
        self,
        *,
        target: str = "current",
        focus: str = "",
        include_frames: object = False,
    ) -> dict[str, object]:
        """只调门面并断言返回**受理信封**（不等待后台任务，用于取消/并发类用例）。"""
        raw = await self.plugin.media_analyze(
            self.event, target=target, focus=focus, include_frames=include_frames
        )
        assert isinstance(raw, str), f"工具返回值必须是 str，实际 {type(raw)}"
        payload = json.loads(raw)
        assert set(payload) == set(envelope.ACCEPT_KEYS), payload
        return payload


def _stub_media(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    kind: str,
    duration: float | BaseException = 30.0,
    streams: tuple[bool, bool] = (True, True),
    frames: list[tuple[float, str]] | None = None,
    digest: str = "0123456789abcdef",
) -> tuple[Path, dict[str, int]]:
    """把 `media.*` 的**重 IO 面**换成内存实现；返回 (组件用的本地路径, 调用计数)。

    桩的是 v0.2 的两个合并点：
    - `classify`（一次拿 `kind/duration/has_audio/has_video`，`File` 的三段梯子在它内部）；
    - `ensure_provider_wav`（`extracted=True` 时等于视频抽音轨）。

    取件面（`find_media` / `resolve_media_path` / `file_download_needed`）**保持真实现**：
    它们只做 `os.path.isfile` 级别的判断，没有 IO 成本，桩掉反而会让"File 段取件"
    这条 v0.2 的主路径在主链路测试里失去覆盖。
    """
    media_path = tmp_path / ("clip.wav" if kind == "audio" else "clip.mp4")
    media_path.write_bytes(b"\x00" * 2048)
    wav_path = tmp_path / "audio.wav"
    wav_path.write_bytes(b"RIFF" + b"\x00" * 64)

    calls = {
        "classify": 0,
        "ensure_provider_wav": 0,
        "extract_wav": 0,
        "extract_frames": 0,
        "content_digest": 0,
    }

    async def _classify(_handle, _path, *, max_media_bytes: int, workdir: str):
        calls["classify"] += 1
        if isinstance(duration, BaseException):
            raise duration
        return kind, float(duration), streams[0], streams[1]

    async def _digest(_path: str) -> str:
        calls["content_digest"] += 1
        return digest

    async def _provider_wav(_path: str, *, workdir: str, extracted: bool = False) -> str:
        calls["ensure_provider_wav"] += 1
        if extracted:
            calls["extract_wav"] += 1
            return str(wav_path)
        return str(_path)  # 已经是规范 wav ⇒ 零重编码（v0.2 的 ensure_provider_wav 口径）

    async def _frames(
        _path: str, *, count: int, duration_s: float, workdir: str, notes: list[str] | None = None
    ) -> list[tuple[float, str]]:
        calls["extract_frames"] += 1
        produced: list[tuple[float, str]] = []
        for index, (at_s, _unused) in enumerate(frames or []):
            jpg = tmp_path / f"frame_{index}.jpg"
            jpg.write_bytes(b"\xff\xd8" + b"\x00" * 32)
            produced.append((at_s, str(jpg)))
        return produced

    monkeypatch.setattr(media, "classify", _classify)
    monkeypatch.setattr(media, "content_digest", _digest)
    monkeypatch.setattr(media, "ensure_provider_wav", _provider_wav)
    monkeypatch.setattr(media, "extract_frames", _frames)
    return media_path, calls


def _stub_providers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    audio: object,
    chat: object,
    vision: object | None = None,
) -> dict[str, int]:
    """把三条 provider 解析入口换成"记录 + 可注入失败"的假身。

    v0.2 的音频入口是 `resolve_audio_provider`（多模态 chat），**不是** v0.1 的
    `resolve_stt_provider`；后者已从 `providers` 删除 ⇒ 这里 patch 不到就会红，
    等价于一条"STT 链路不许回来"的静态守卫。
    """
    calls = {"audio": 0, "vision": 0, "chat": 0}

    async def _audio(_context, _provider_id, _umo):
        calls["audio"] += 1
        if isinstance(audio, BaseException):
            raise audio
        return audio

    async def _vision(_context, _provider_id, _umo):
        calls["vision"] += 1
        resolved = vision if vision is not None else chat
        if isinstance(resolved, BaseException):
            raise resolved
        return resolved

    async def _chat(_context, _provider_id, _umo, *, require_image):
        calls["chat"] += 1
        if isinstance(chat, BaseException):
            raise chat
        return chat

    monkeypatch.setattr(providers, "resolve_audio_provider", _audio)
    monkeypatch.setattr(providers, "resolve_vision_provider", _vision)
    monkeypatch.setattr(providers, "resolve_chat_provider", _chat)
    return calls


def build_rig(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    kind: str = "audio",
    duration: float | BaseException = 30.0,
    streams: tuple[bool, bool] = (True, True),
    frames: list[tuple[float, str]] | None = None,
    digest: str = "0123456789abcdef",
    audio: object | None = None,
    chat: object | None = None,
    vision: object | None = None,
    cfg: settings.Settings | None = None,
    cache: cache_mod.DiskCache | None = None,
    event=None,
    context: FakeContext | None = None,
    providers_real: bool = False,
    native: FakeNativeProvider | None = None,
    probe_ok: bool = True,
    probe_source: str = "live",
) -> Rig:
    """搭一套"真组件 + 假媒体层 + 假 provider 层"的测试台。

    v0.3 的两个默认值（每一条都对着一个 v0.3 语义变更）：

    - **video 默认配 `vision_provider_id`**：v0.3 的抽帧是**回退**，且 `auto/true` 下的
      回退以"vision 位非空"为前提（SPEC-v0.3 §7.1）。不配 vision 就不会抽帧 ——
      想测"没配 vision"的用例显式传 `cfg=settings.Settings(...)`。
    - **`probe.ensure_l1` 默认被桩成"通过"**：真 L1 要跑 ffmpeg 合成样片 + 一次模型调用
      （秒级、依赖字体）。探测本身的行为在 `tests/test_probe.py`，这里只测"探测结果如何
      影响链路"（`probe_ok` / `probe_source` 两个旋钮）。
    """

    media_path, media_calls = _stub_media(
        monkeypatch,
        tmp_path,
        kind=kind,
        duration=duration,
        streams=streams,
        frames=frames,
        digest=digest,
    )
    audio_obj = audio if audio is not None else audio_chat()
    chat_obj = chat if chat is not None else FakeChatProvider(model_name="fake-chat")
    if providers_real:
        # 走真 `providers.resolve_*` + FakeContext：专测"空串回落阶梯"在 analyze 里的落点
        prov_calls = {"audio": 0, "vision": 0, "chat": 0}
    else:
        prov_calls = _stub_providers(monkeypatch, audio=audio_obj, chat=chat_obj, vision=vision)

    if event is None:
        component = (
            make_record(media_path) if kind == "audio" else make_video(media_path)
        )
        event = make_event(current=[component])

    if cfg is None:
        # v0.3：视频的抽帧是回退，且以"显式配了视觉位"为前提 ⇒ video 用例默认配一个
        cfg = (
            settings.Settings(vision_provider_id="fake-chat")
            if kind == "video"
            else settings.Settings()
        )
    if native is not None:
        cfg = replace(cfg, video_provider_id=str(native.provider_config.get("id") or ""))
    if cache is None:
        # 每个用例一个**独立**的插件数据目录：缓存绝不能落到库目录或生产树
        plugin_data = tmp_path / "plugin_data"
        monkeypatch.setattr(
            cache_mod, "_core_plugin_data_path", lambda: str(plugin_data), raising=False
        )
        cache_obj = cache_mod.DiskCache(
            plugin_name=settings.PLUGIN_NAME, ttl_seconds=cfg.cache_ttl_seconds
        )
    else:
        cache_obj = cache
    by_id = {str(native.provider_config.get("id")): native} if native is not None else {}
    ctx = context or FakeContext(by_id=by_id)
    plugin = main.MediaAnalysisPlugin(context=ctx, config={})
    plugin._settings_cache = cfg

    # L1 探测：默认"实跑通过"（真探测在 tests/test_probe.py）
    probe_calls = {"ensure_l1": 0}

    async def _ensure_l1(_provider, *, provider_id, model, plugin_version, ttl_seconds, workdir, store=None, timeout=None):
        probe_calls["ensure_l1"] += 1
        return probe.ProbeOutcome(
            probe_ok,
            probe_source,
            probe.L1Result(probe_ok, "ok" if probe_ok else "mismatch", 7),
        )

    monkeypatch.setattr(probe, "ensure_l1", _ensure_l1)

    return Rig(
        media_path=media_path,
        event=event,
        context=ctx,
        settings=cfg,
        cache=cache_obj,
        plugin=plugin,
        media_calls=media_calls,
        prov_calls=prov_calls,
        audio=audio_obj if isinstance(audio_obj, FakeChatProvider) else audio_chat(),
        chat=chat_obj if isinstance(chat_obj, FakeChatProvider) else FakeChatProvider(),
        native=native,
        probe_calls=probe_calls,
    )
