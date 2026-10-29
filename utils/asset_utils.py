"""共享工具函数：材质分类、字符串表、矩阵/哈希换算。

build_strings_dict  — 字符串表哈希 map 构建
material_family     — shader_id 高 16 位 → 技术族
tech_family         — fx 路径 → 渲染技术族（缺失时退回 material_family）
mat_col_to_row_np   — 列主序 16 float → 行主序 4×4
murmur3_32          — MurmurHash3_x86_32
"""

from __future__ import annotations

import struct
from typing import Dict


def build_strings_dict(db) -> Dict[int, str]:
    """遍历 assets.bin 的 StringsSection 哈希表，构建 {hash(u32): name} Python dict。

    assets_cache_service._strings_dict 与 geometry_service._strings_dict 的逐字相同核心，
    各自在外层管理缓存策略。
    """
    out: dict = {}
    try:
        from uncode_assets import binary as B
        hmap = db.strings.offsets_map
        cap = hmap.capacity
        stride = hmap.bucket_stride
        vstride = hmap.value_stride
        buckets = hmap.buckets
        values = hmap.values
        sdata = db.strings.string_data
        read64 = B.read_u64
        read32 = B.read_u32
        read_str = B.read_null_terminated_string
        for idx in range(cap):
            off = idx * stride
            key = read64(buckets, off)
            if stride >= 16:
                if read64(buckets, off + 8) == 0:
                    continue
            else:
                if key == 0:
                    continue
            str_off = read32(values, idx * vstride)
            if str_off < len(sdata):
                s = read_str(sdata, str_off)
                if s:
                    out[key & 0xFFFFFFFF] = s
    except Exception:  # noqa: BLE001
        pass
    return out


def material_family(shader_id: str) -> str:
    """shader_id（0xHHHHLLLL）高 16 位 → 技术族（INDEXED/PBS/其他）。"""
    try:
        family = (int(shader_id, 16) >> 16) & 0xFFFF
    except Exception:  # noqa: BLE001
        family = 0
    if family == 0x0009:
        return "indexed"
    if family == 0x0005:
        return "pbs"
    return "other"


def mat_col_to_row_np(mat: list):
    """列主序 16 float → 行主序 4x4 的 contiguous float32 ndarray。"""
    import numpy as np
    return np.ascontiguousarray(
        np.array(mat, dtype=np.float32).reshape(4, 4).T, dtype=np.float32)


def murmur3_32(data: bytes, seed: int = 0) -> int:
    """MurmurHash3_x86_32：Korabli 字符串哈希（渲染集 shape 名 ↔ geometry mapping_id）。"""
    c1 = 0xCC9E2D51
    c2 = 0x1B873593
    length = len(data)
    h1 = seed
    for i in range(length // 4):
        k1 = struct.unpack_from('<I', data, i * 4)[0]
        k1 = (k1 * c1) & 0xFFFFFFFF
        k1 = ((k1 << 15) | (k1 >> 17)) & 0xFFFFFFFF
        k1 = (k1 * c2) & 0xFFFFFFFF
        h1 ^= k1
        h1 = ((h1 << 13) | (h1 >> 19)) & 0xFFFFFFFF
        h1 = (h1 * 5 + 0xE6546B64) & 0xFFFFFFFF
    tail = data[length // 4 * 4:]
    k1 = 0
    if len(tail) >= 3:
        k1 ^= tail[2] << 16
    if len(tail) >= 2:
        k1 ^= tail[1] << 8
    if len(tail) >= 1:
        k1 ^= tail[0]
        k1 = (k1 * c1) & 0xFFFFFFFF
        k1 = ((k1 << 15) | (k1 >> 17)) & 0xFFFFFFFF
        k1 = (k1 * c2) & 0xFFFFFFFF
        h1 ^= k1
    h1 ^= length
    h1 ^= h1 >> 16
    h1 = (h1 * 0x85EBCA6B) & 0xFFFFFFFF
    h1 ^= h1 >> 13
    h1 = (h1 * 0xC2B2AE35) & 0xFFFFFFFF
    h1 ^= h1 >> 16
    return h1


def tech_family(fx_path: str, shader_id: str = "") -> str:
    """按 fx 路径判定渲染类别（渲染分支用）；路径缺失退回 shader_id 高 16 位族。

    渲染类别（geometry_renderer.paintGL 按此分支）：
      grid        → 网格/线网 alpha（透明 pass，无光照）
      indexed     → INDEXED 分块渲染（materialIdMap+albedoArray）
      emissive    → 自发光/燃烧（无光照亮色，u_mode=2 不透明）
      transparent → 半透明（玻璃/螺旋桨/水/云/旗帜等，blend + 贴图 alpha）
      decal       → 贴花叠加（普通光照）
      wire        → 细线/栏杆材质（普通）
      pbs         → 标准 PBS（光照实体）
      other       → shader_id 高 16 位兜底
    """
    low = (fx_path or "").lower()
    base = low.split("/")[-1]
    if "grid_alpha" in low:
        return "grid"
    if "indexed" in low:
        return "indexed"
    # 自发光/燃烧：无光照亮色
    if "emissive" in low or "blaze" in low:
        return "emissive"
    # 半透明：玻璃/透明/螺旋桨/水/瀑布/云平面/旗帜（blend + 贴图 alpha）
    if ("glass" in low or "transparent" in low or "propeller" in low
            or "water" in low or "waterfall" in low or "cloud_plane" in low
            or base == "flag.fx" or "dirty_glass" in low
            or "interior_behind_glass" in low):
        return "transparent"
    if "decal" in low or "mesh_decal" in low:
        return "decal"
    if "wire" in low:
        return "wire"
    if ("pbs" in low or "camo" in low or "ship_material" in low
            or "diffuse" in low or "base_material" in low):
        return "pbs"
    # 无 fx 路径/无法归类：退回 shader_id 高 16 位族
    return material_family(shader_id)
