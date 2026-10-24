"""AstrBot 插件包标记：`astrbot_plugin_media_analysis`。

核心把插件目录**当作包**导入 `main.py`（模块名 `data.plugins.<目录名>.main`，
`star_manager.py:461` 的 `__import__("data.plugins.<目录名>.main", fromlist=["main"])`），
插件目录本身**不在** `sys.path` 上 ⇒ 包内模块之间只能用相对导入：

    from . import analyze, cache, envelope, media, providers, settings

扁平 `import media` / `from media import …` 只在手动把插件目录塞进 `sys.path`
时才成立，**生产必然 `ModuleNotFoundError`**，而且加载失败是**静默的**（目录在、
工具不注册，界面上看起来像"没装"）。事故全貌：父手册 §3.4（2026-10-18 拒载）。

本文件刻意**不** import 任何子模块：加载器只需要一个可导入的包，提前 import
`main` 会在 `star_manager` 完成注册前触发工具注册副作用。
"""

__all__ = [
    "main",
    "settings",
    "envelope",
    "providers",
    "media",
    "cache",
    "analyze",
    "probe",
    "video_native",
    "background",
]
