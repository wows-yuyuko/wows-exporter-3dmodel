"""网格取舍判据（LOD / crack / decal / wire / 辅助几何）—— 单一出处。

这些判定原先散在三处，每次改动都要来回对：
  - `geometry_service._lod_skip()`：LOD 级别 + 面数保护
  - `geometry_service._split_primitives_by_material()` 里的 `__crack__` 分流
  - `export_service._is_decal()`：材质名 / 网格名 / shape 名三级
  - wire：`hm.is_wire = (mat == '__wire__' or mat == 'WIRE')`

现在几何层与导出层共用这一份，改口径只改这里。
"""
from __future__ import annotations

#: 辅助几何关键词：面数天然很少（线材/补丁/穿孔/玻璃/网格），
#: 不参与「目标 LOD 面数过少就回退 LOD0」的保护，否则会把低模线材换成高模几万面。
AUXILIARY_KEYWORDS = ("wire", "patch", "perforation", "glass", "_net")

#: 破损/断面几何的材质前缀（`__crack__#<mat>`）
CRACK_PREFIX = "__crack__"


def is_auxiliary_geometry(name: str | None) -> bool:
    """名字里带线材/补丁/穿孔/玻璃/网格关键词 → 辅助几何。"""
    low = (name or "").lower()
    return any(k in low for k in AUXILIARY_KEYWORDS)


def is_wire_material(material: str | None) -> bool:
    """老式 wire 材质名（`__wire__` / `WIRE`）。

    注意：现网资产用 `TL2_SHIPWIRE_*` 命名，那些是**薄三角形**（不是 line list），
    不在这里判为 wire —— 判进来反而会丢几何（见 docs/todo-backlog.md P1-4）。
    """
    return (material or "").strip().lower() in ("__wire__", "wire")


def is_crack_material(material: str | None) -> bool:
    """破损断面材质（`__crack__#...`）。"""
    return str(material or "").startswith(CRACK_PREFIX)


def is_decal(obj) -> bool:
    """贴花平面片：材质名 / 网格名 / shape 名任一带 `decal`。"""
    if "decal" in str(getattr(obj, "material", "") or "").lower():
        return True
    names = [str(getattr(obj, "name", "") or "")]
    names += [str(x) for x in (getattr(obj, "shape_names", None) or [])]
    return any("decal" in n.lower() for n in names)


def should_skip_lod(name: str, lod: int, levels_by_base: dict,
                    tri_by_base_lod: dict, shape_lod) -> bool:
    """该 shape 在当前 LOD 设置下是否跳过。

    - `lod < 0`：只在 LOD0，其它级别全跳过
    - 否则取 `want = lod`（该级不存在则回退 0），并做面数保护：
      目标级三角数 < max(50, LOD0 的 5%) 视为占位几何 → 回退 LOD0
      辅助几何（线材/补丁/穿孔/玻璃/网格）不参与该保护
    """
    if not name:
        return False
    base, lv = shape_lod(name)
    if lod < 0 or not levels_by_base:
        return lv > 0
    want = lod if lod in levels_by_base.get(base, ()) else 0
    if want and not is_auxiliary_geometry(name):
        tri0 = tri_by_base_lod.get((base, 0), 0)
        tri_want = tri_by_base_lod.get((base, want), 0)
        if tri0 and tri_want < max(50, tri0 * 0.05):
            want = 0
    return lv != want
