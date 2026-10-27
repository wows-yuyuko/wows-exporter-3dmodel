"""批量导出的增量指纹：记录「这条船用这组参数导出过」。

batch 原本只按 `geometry/<key>.glb` 是否存在跳过 —— 改了贴图分辨率、LOD、烘焙开关
之后仍然跳过，只能 `--force` 全量重导。这里给每组导出参数算一个签名，存在输出目录
的 `.export_index.json` 里：

    sig = sha1(server | build | ship.key | model | lod | texture_size | 开关集合)

签名一致且产物还在 → 跳过；任一变化（换 build、改参数、换模型）→ 重导。
只依赖输入（build + 参数），不猜内容，所以不会漏导；代价是同一组参数下即使游戏
资产变了也需要换 build 才会重导（build 就是官方版本号，够用）。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

INDEX_NAME = ".export_index.json"


def export_signature(server: str, build: str, ship_key: str, model: str,
                     lod: int, texture_size: int, flags: dict) -> str:
    """一组导出参数的指纹（只含输入，不含结果）。"""
    payload = {
        "server": server,
        "build": build,
        "ship": ship_key,
        "model": model,
        "lod": lod,
        "texture_size": texture_size,
        "flags": {k: bool(v) for k, v in sorted(flags.items())},
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class ExportIndex:
    """`<out>/.export_index.json` 的读写（原子替换，进程内加锁）。"""

    def __init__(self, out_dir: Path):
        self.path = Path(out_dir) / INDEX_NAME
        self._lock = threading.Lock()
        self._data: dict[str, str] = {}
        try:
            if self.path.is_file():
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._data = {str(k): str(v) for k, v in loaded.items()}
        except Exception:  # noqa: BLE001
            self._data = {}

    def matches(self, ship_key: str, signature: str) -> bool:
        with self._lock:
            return self._data.get(ship_key) == signature

    def record(self, ship_key: str, signature: str) -> None:
        with self._lock:
            self._data[ship_key] = signature
            self._flush()

    def forget(self, ship_key: str) -> None:
        with self._lock:
            if self._data.pop(ship_key, None) is not None:
                self._flush()

    def _flush(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=0,
                                  sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    def __len__(self) -> int:
        return len(self._data)
