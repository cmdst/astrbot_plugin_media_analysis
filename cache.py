"""进程内 LRU 缓存（SPEC §8）：`sha256[:16]` + 参数指纹 → 成功信封。

§5-10 的契约：缓存是**内部实现，不是旋钮** —— 不加配置键、不落盘、不进 `data/`。
缓存里**有转写正文**，所以只能活在进程内存里；`terminate()` 会 `clear()`。
"""

from __future__ import annotations

import json
from collections import OrderedDict

DEFAULT_MAXSIZE = 32

# 命中/未命中都返回**新 dict**：存的是序列化快照，防调用方改写污染缓存（SPEC §8）。
_DEFAULT_KEY_SEP = ":"


class ResultCache:
    """定长 LRU：`OrderedDict` + `move_to_end`，`put` 超限弹最旧。"""

    def __init__(self, maxsize: int = DEFAULT_MAXSIZE) -> None:
        self.maxsize = max(1, int(maxsize))
        self._store: OrderedDict[str, str] = OrderedDict()

    def get(self, key: str) -> dict[str, object] | None:
        """命中返回信封副本（并把它挪到队尾），未命中返回 `None`。"""
        payload = self._store.get(key)
        if payload is None:
            return None
        self._store.move_to_end(key)
        try:
            loaded = json.loads(payload)
        except (TypeError, ValueError):
            self._store.pop(key, None)
            return None
        return loaded if isinstance(loaded, dict) else None

    def put(self, key: str, value: dict[str, object]) -> None:
        """写入快照；不可序列化的值被静默忽略（缓存不该影响主链路成败）。"""
        try:
            payload = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return
        self._store[key] = payload
        self._store.move_to_end(key)
        while len(self._store) > self.maxsize:
            self._store.popitem(last=False)

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)

    @staticmethod
    def make_key(
        digest: str,
        *,
        kind: str,
        focus: str,
        include_frames: bool,
        max_frames: int,
    ) -> str:
        """参数指纹：`<digest>:<kind>:<0|1>:<max_frames>:<focus>`（focus 用截断后的值）。"""
        parts = (
            str(digest),
            str(kind),
            "1" if include_frames else "0",
            str(int(max_frames)),
            str(focus),
        )
        return _DEFAULT_KEY_SEP.join(parts)
