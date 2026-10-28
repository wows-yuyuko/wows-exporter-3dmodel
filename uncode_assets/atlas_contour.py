"""AtlasContourProto（粒子图集轮廓）二进制 ⇄ 可读 XML。

`/particles/animated/Smoke_run_7x7_LM.dds_7_7_0_8.contours` 这类记录给**动态粒子
图集**（sprite sheet）记录每帧的轮廓多边形。客户端按
``%s_%u_%u_%u_%u.contours`` 的命名规则去取同名文件（exe 里的
``fx::unpackBinaryContours: did not found %s for %s`` / ``{}: failed to load contours``），
名字里的 4 个数字是运行期结构里的 4 个字节字段（栅格帧数在前两位，实测与
``<贴图>_<framesPerX>_<framesPerY>_…`` 的格子数一致）。

记录布局（16 字节）：

    +0x00  u32  frameCount
    +0x04  u32  pad
    +0x08  i64  rel（基准 = 记录起点）→ frames 数组

frames 数组元素（0x10 字节）：

    +0x00  u32  pointCount
    +0x04  u32  pad
    +0x08  i64  rel（基准 = **本元素起始**）→ pointCount 个 vec2（f32 x / f32 y）

实测（莱服 285 条、寄服 458 条）：``frameCount`` 恰好等于图集格子数
（6×6→36、7×7→49、8×8→64、1×1→1），每帧 8 个点，坐标落在 [-1, 1]；
载荷内**没有**未解析字节（285/285、458/458 往返逐字节一致）。

⚠️ 记录头（16 字节）与载荷之间那段不是本记录的数据：assets.bin 把各条记录的载荷
顺序码在同一个 blob 里，所以 ``open_file()`` 切出的 `[记录起点, blob 末尾)`
经常有几 MB 是别的记录 —— 本模块只认 ``[0,16)`` 与 ``[rel, regionSize)`` 两段。
"""

from __future__ import annotations

import struct
from typing import Any, Dict, List, Optional, Tuple

#: 记录头长度
RECORD_SIZE = 0x10
#: 单个帧元素长度
FRAME_SIZE = 0x10
#: 单个轮廓点长度（vec2）
POINT_SIZE = 8

_T = "\t"


class ContourFormatError(ValueError):
    """轮廓数据不符合预期结构。"""


class _Cover:
    """记录已解码字节的位图（未覆盖的字节原样保留，保证可无损回写）。"""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.cover = bytearray(len(data))

    def check(self, off: int, size: int) -> None:
        if off < 0 or size < 0 or off + size > len(self.data):
            raise ContourFormatError(
                f"越界读取 off=0x{off:X} size=0x{size:X} len=0x{len(self.data):X}")

    def mark(self, off: int, size: int) -> None:
        self.check(off, size)
        self.cover[off:off + size] = b"\x01" * size


def _raw_segments(cv: _Cover) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    i = 0
    data, cover = cv.data, cv.cover
    while i < len(data):
        if cover[i]:
            i += 1
            continue
        j = i
        while j < len(data) and not cover[j]:
            j += 1
        out.append({"at": i, "hex": data[i:j].hex()})
        i = j
    return out


def decode_contours(data: bytes, name: str = "") -> Dict[str, Any]:
    """把一条 AtlasContourProto 记录（记录起点 → blob 末尾的切片）解码为 dict。

    ``regionSize`` 只算到结构实际占用（最后一个被引用的数组末尾），
    超出部分是同一 blob 里下一条记录的数据，不参与回写；
    ``[16, payloadAt)`` 同样是别的记录的载荷（blob 把各条载荷顺序码在一起）。
    """
    if len(data) < RECORD_SIZE:
        raise ContourFormatError(f"AtlasContourProto 数据过短: {len(data)}")
    frame_count, _pad, rel = struct.unpack_from("<IIq", data, 0)
    if frame_count > 0x2000:
        raise ContourFormatError(f"frameCount 异常: {frame_count}")
    fps_at = rel
    cv = _Cover(data)
    end = RECORD_SIZE
    frames: List[Dict[str, Any]] = []
    if frame_count:
        if not (RECORD_SIZE <= fps_at < len(data)
                and fps_at + frame_count * FRAME_SIZE <= len(data)):
            raise ContourFormatError(
                f"frames 数组越界: count={frame_count} rel=0x{fps_at:X}")
        cv.mark(0, RECORD_SIZE)
        cv.mark(fps_at, frame_count * FRAME_SIZE)
        end = max(end, fps_at + frame_count * FRAME_SIZE)
        for i in range(frame_count):
            pos = fps_at + i * FRAME_SIZE
            point_count, _ppad, prel = struct.unpack_from("<IIq", data, pos)
            if point_count > 0x2000:
                raise ContourFormatError(f"frame[{i}] pointCount 异常: {point_count}")
            node: Dict[str, Any] = {"index": i, "pointCount": point_count}
            if prel:
                at = pos + prel
                need = point_count * POINT_SIZE
                if at < 0 or at + need > len(data):
                    raise ContourFormatError(
                        f"frame[{i}] 轮廓点越界: rel=0x{prel:X} count={point_count}")
                node["at"] = at
                node["points"] = [
                    {"x": struct.unpack_from("<f", data, at + k * POINT_SIZE)[0],
                     "y": struct.unpack_from("<f", data, at + k * POINT_SIZE + 4)[0]}
                    for k in range(point_count)]
                cv.mark(at, need)
                end = max(end, at + need)
            frames.append(node)
    else:
        cv.mark(0, RECORD_SIZE)

    region = end
    # raw 只统计本记录自己的载荷（[framesAt, region)）：记录头与载荷之间那些字节属于
    # **同一 blob 里的其它记录**（assets.bin 把各条记录的载荷顺序码在一起），
    # 所以 open_file() 切出来的 [记录起点, blob 末尾) 往往有几百 KB 是别人的数据。
    sub = _Cover(data[:region])
    sub.cover = cv.cover[:region]
    sub.cover[: min(fps_at, region)] = b"\x01" * min(fps_at, region)
    return {
        "_type": "AtlasContourProto",
        "name": name,
        "frameCount": frame_count,
        "payloadAt": fps_at,
        "regionSize": region,
        "frames": frames,
        "raw": _raw_segments(sub),
    }


def _num(value: Any) -> str:
    v = float(0.0 if value is None else value)
    return f"{v:.4f}"


def _xml_escape(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))
