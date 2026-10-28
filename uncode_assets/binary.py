"""低级二进制读取工具 —— 与 wows-toolkit `parser_utils.rs` 一一对应。

实现 BigWorld 通用的相对指针、packed string、标量数组解析。
所有读取均为小端序（BWDB 固定小端）。
"""

from __future__ import annotations

import struct
from typing import List, Optional, Tuple

from .errors import OutOfBoundsError


# ── 标量读取 ──────────────────────────────────────────────────────────────

def read_u8(data: bytes, offset: int) -> int:
    _check(data, offset, 1)
    return data[offset]


def read_u16(data: bytes, offset: int) -> int:
    _check(data, offset, 2)
    return struct.unpack_from('<H', data, offset)[0]


def read_u32(data: bytes, offset: int) -> int:
    _check(data, offset, 4)
    return struct.unpack_from('<I', data, offset)[0]


def read_u64(data: bytes, offset: int) -> int:
    _check(data, offset, 8)
    return struct.unpack_from('<Q', data, offset)[0]


def read_i32(data: bytes, offset: int) -> int:
    _check(data, offset, 4)
    return struct.unpack_from('<i', data, offset)[0]


def read_i64(data: bytes, offset: int) -> int:
    _check(data, offset, 8)
    return struct.unpack_from('<q', data, offset)[0]


def read_f32(data: bytes, offset: int) -> float:
    _check(data, offset, 4)
    return struct.unpack_from('<f', data, offset)[0]


def _check(data: bytes, offset: int, size: int) -> None:
    if offset < 0 or offset + size > len(data):
        raise OutOfBoundsError(offset, size, len(data))


# ── 相对指针 ──────────────────────────────────────────────────────────────

def resolve_relptr(base: int, rel: int) -> int:
    """把相对指针解析为绝对偏移：base + rel（wows-toolkit `resolve_relptr`）。"""
    return base + rel


# ── Packed String ─────────────────────────────────────────────────────────


def read_null_terminated_string(data: bytes, offset: int) -> str:
    """读取以 null 结尾的字符串。"""
    if offset < 0 or offset > len(data):
        raise OutOfBoundsError(offset, 0, len(data), "null-terminated string")
    end = data.find(b'\x00', offset)
    if end == -1:
        end = len(data)
    return data[offset:end].decode('utf-8', errors='replace')


# ── 数组解析 ──────────────────────────────────────────────────────────────

def parse_u16_array(data: bytes, offset: int, count: int) -> List[int]:
    _check(data, offset, count * 2)
    return list(struct.unpack_from(f'<{count}H', data, offset))


def parse_u32_array(data: bytes, offset: int, count: int) -> List[int]:
    if count <= 0:
        return []
    _check(data, offset, count * 4)
    return list(struct.unpack_from(f'<{count}I', data, offset))


def parse_u64_array(data: bytes, offset: int, count: int) -> List[int]:
    """u64 数组（如 WG ModelPrototype 的 skelExtResIds / tintMaterialIds）。"""
    if count <= 0:
        return []
    _check(data, offset, count * 8)
    return list(struct.unpack_from(f'<{count}Q', data, offset))


def parse_matrix_array(data: bytes, offset: int, count: int) -> List[List[float]]:
    """解析 count 个 4×4 矩阵（每个 16×f32 = 64 字节）。"""
    if count <= 0:
        return []
    _check(data, offset, count * 64)
    return [
        list(struct.unpack_from('<16f', data, offset + i * 64))
        for i in range(count)
    ]


# ── 复合结构 ──────────────────────────────────────────────────────────────

def parse_vec2(data: bytes, offset: int) -> List[float]:
    return [read_f32(data, offset), read_f32(data, offset + 4)]


def parse_vec3(data: bytes, offset: int) -> List[float]:
    return [read_f32(data, offset), read_f32(data, offset + 4), read_f32(data, offset + 8)]


def parse_vec4(data: bytes, offset: int) -> List[float]:
    return [read_f32(data, offset), read_f32(data, offset + 4),
            read_f32(data, offset + 8), read_f32(data, offset + 12)]


def parse_matrix4x4(data: bytes, offset: int) -> List[float]:
    return list(struct.unpack_from('<16f', data, offset))
