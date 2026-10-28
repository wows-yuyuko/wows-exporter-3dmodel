"""idx 视角的模型清单：把一个 build 的全部 `.geometry` 聚合成「模型目录 → 签名」。

只读 `bin/<build>/idx/*.idx`（约 5 s），**不加载 assets.bin、不解包几何**。
签名来自 idx 里本来就存的 `crc32`（解压后内容的 CRC32，实测与 `zlib.crc32(内容)` 一致）
与 `unpacked_size`，所以「内容没变」必然「签名没变」。

口径（"宽松"，2026-10-28 与用户确认）：crc32 覆盖整个 `.geometry`（含顶点/索引），
所以只改模型细节也算变动 —— 宁可多导一次，不要漏导。

实测（WG 13187581 → 13357625）：31,843 个同名 geometry 里 99.9% 的 crc32 不变。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ModelFile:
    """模型目录下的一个 `.geometry`。"""

    path: str
    role: str          # main / seg
    crc32: int
    size: int


@dataclass
class ModelEntry:
    """一个模型目录（如 BSD100_Waterloo_1956）在某个 build 下的全部几何文件。"""

    model_id: str
    files: list[ModelFile] = field(default_factory=list)
    sig: str = ""
    main_crc: int = 0
    main_size: int = 0

    @property
    def file_count(self) -> int:
        return len(self.files)


def _sig_of(model_id: str, files: list[ModelFile]) -> str:
    """签名 = sha1(目录名 + 每个文件的 (相对名, crc, size))。

    用**相对名**而不是全路径：模型目录被移动到别的分类目录（ship/gun 等）时
    内容没变就不该判为变动。
    """
    h = hashlib.sha1()
    h.update(model_id.encode("utf-8"))
    for f in sorted(files, key=lambda x: x.path):
        name = f.path.rsplit("/", 1)[-1]
        h.update(("\x00%s|%d|%d" % (name, f.crc32, f.size)).encode("utf-8"))
    return h.hexdigest()


def scan(game: str | Path, build: str) -> dict[str, ModelEntry]:
    """扫一个 build 的 idx，返回 {模型目录: ModelEntry}。"""
    from data_extractor import GameExtractor

    out: dict[str, ModelEntry] = {}
    ex = GameExtractor(str(game), bin_folder=str(build))
    try:
        for e in ex.list_files(["content/**/*.geometry"]):
            if getattr(e, "is_directory", False):
                continue
            parts = e.path.rsplit("/", 2)
            if len(parts) < 3:
                continue
            model_id = parts[-2]
            fname = parts[-1]
            is_main = fname[: -len(".geometry")] == model_id
            fi = e.file_info
            entry = out.get(model_id)
            if entry is None:
                entry = out[model_id] = ModelEntry(model_id=model_id)
            entry.files.append(ModelFile(
                path=e.path,
                role="main" if is_main else "seg",
                crc32=int(fi.crc32),
                size=int(fi.unpacked_size),
            ))
            if is_main:
                entry.main_crc = int(fi.crc32)
                entry.main_size = int(fi.unpacked_size)
    finally:
        ex.close()

    for entry in out.values():
        entry.sig = _sig_of(entry.model_id, entry.files)
    return out
