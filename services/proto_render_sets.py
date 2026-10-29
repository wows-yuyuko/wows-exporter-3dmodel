"""VisualPrototype → render_set 行（Lesta 与 WG 是两套完全不同的布局）。

`assets_cache_service.populate()` 扫全树时按服调用这里的解析器，逐条记录回调
`add_row` 写库（流式，内存有界）。拆出来的原因：两服布局差异大、且与
「连库、清表、增量过滤」无关，合在一个类里读不清。

⚠️ 两服**不可互相套用**：
  - Korabli：记录 0x40，渲染集数组从 `+0x38`(record-relative) 起、每项 0x50；
  - WG：记录 0x70，另有 0x28 渲染集项，字段偏移完全不同。
"""

from __future__ import annotations

import json
import struct

from utils.path_utils import model_id_of


def path_of(h: int, db, self_id_idx) -> str:
    if h in (0, 0xFFFFFFFFFFFFFFFF):
        return ''
    i = self_id_idx.get(h)
    if i is None:
        return ''
    try:
        return db.reconstruct_path(i, self_id_idx)
    except Exception:  # noqa: BLE001
        return ''

def name_of(sdict: dict, db, hid: int) -> str:
    """哈希 → 名字：先查批量字典 sdict，缺失时回退完整字符串表。

    sdict 是「全表扫描一次」的快速字典；若构建时被异常截断（部分条目缺失），
    回落 db.strings 避免整套名字为空（否则骨骼/渲染集会被全部跳过 → 入库 0 条）。
    """
    nm = sdict.get(hid & 0xFFFFFFFF)
    if nm:
        return nm
    try:
        return db.strings.get_string_by_id(hid) or ''
    except Exception:  # noqa: BLE001
        return ''

def render_sets_lesta_one(data, off, db, self_id_idx, sdict,
                           add_row, models=None) -> None:
    """Korabli VisualPrototype（0x40 布局）单条记录的渲染集解析。

    独立成方法是为了让 populate 的渲染集循环能**逐条记录**捕获异常
    （单条坏记录不应让整库渲染集为 0）。

    models 非 None 时只解析该集合里的模型（增量重建）。
    """
    cnt = struct.unpack_from('<Q', data, off + 0x30)[0]
    rel = struct.unpack_from('<Q', data, off + 0x38)[0]
    if not rel or cnt <= 0:
        return
    h = struct.unpack_from('<Q', data, off + 0x20)[0]
    gp = path_of(h, db, self_id_idx)
    if not (gp and gp.endswith(".geometry")):
        return
    model_id = model_id_of(gp)
    if models is not None and model_id not in models:
        return
    # 渲染集数组：+0x38 rel 为 **record-relative**（相对记录起始），
    # 数组从 rel 起、每项 0x50 步长；+0x30 count 为权威条数。
    # ⚠️ 2026-08-19 修正：VisualPrototype item_size=0x40（非 0x80）。
    # 每条记录唯一 geometry（+0x20）+ primitives（+0x28）。旧 0x80
    # 误把两条记录合并 → JGA180 误用 JGA181 的 TurretShapeff。
    # 无多 geometry 共用 → 无需广播归属，直接归本记录 geometry。
    base = off + rel
    if base + cnt * 0x50 > len(data):
        return
    for k in range(cnt):
        o = base + k * 0x50
        shp = name_of(sdict, db, struct.unpack_from('<I', data, o)[0])
        if not shp.endswith('.vertices'):
            continue
        mat = name_of(sdict, db, struct.unpack_from('<I', data, o + 8)[0])
        mfm_h = struct.unpack_from('<Q', data, o + 0x20)[0]
        mfm = path_of(mfm_h, db, self_id_idx)
        damage = int('_crack_' in shp or '_lod' in shp or 'Crack' in mat)
        # 蒙皮标志 + 调色板节点名（Korabli 渲染集项布局）：
        #   +0x0C u8 skinned / +0x0D u8 nodes_count /
        #   +0x28 u64 node_name_ids relptr（**item-relative**）→ u32 名ID 数组
        skinned = data[o + 0x0C]
        ncnt = data[o + 0x0D]
        nodes: list[str] = []
        # ★ 所有条目都读 nodes（含 skinned=0 的刚性父子模型，如
        #   BIA454_Bollard_bigShape → ['BIA454_Bollard_big_7']），
        #   供 geometry_service 按父节点矩阵定位到舰船空间。
        if ncnt:
            nrel = struct.unpack_from('<Q', data, o + 0x28)[0]
            if nrel:
                nabs = o + nrel
                if nabs + ncnt * 4 <= len(data):
                    for j in range(ncnt):
                        nid = struct.unpack_from('<I', data, nabs + j * 4)[0]
                        nm = name_of(sdict, db, nid)
                        if nm:
                            nodes.append(nm)
        add_row((gp, shp, model_id, mat, mfm, damage,
                 int(skinned), json.dumps(nodes)))

def render_sets_wg(data, off, db, self_id_idx, sdict,
                    add_row, models=None) -> None:
    """WG VisualPrototype 渲染集解析（0x70 记录 + 0x28 渲染集，wows-toolkit visual.rs）。

    add_row: 单行回调（= populate 里的 _add_rs，边解析边汇总，内存有界）。

    WG 记录布局（与 Korabli 完全不同，不可复用 Korabli 的 +0x30/+0x38/+0x20）：
      +0x30 u64 merged_geometry_path_id
      +0x38 u8 underwater_model / +0x39 u8 abovewater_model /
      +0x3A u16 render_sets_count / +0x3C u8 lods_count
      +0x60 i64 render_sets_relptr（**record-relative**，i64 有符号）
    RenderSet 0x28 步长（parser_utils.rs parse_render_set_fields）：
      +0x00 u32 name_id(.vertices) / +0x04 u32 material_name_id /
      +0x08 u32 vertices_mapping_id / +0x0C u32 indices_mapping_id /
      +0x10 u64 material_mfm_path_id / +0x18 u8 skinned /
      +0x19 u8 nodes_count / +0x20 i64 node_name_ids_relptr（item-relative）
    """
    if off + 0x70 > len(data):
        return
    gp = path_of(struct.unpack_from('<Q', data, off + 0x30)[0],
                       db, self_id_idx)
    if not (gp and gp.endswith(".geometry")):
        return
    model_id = model_id_of(gp)
    if models is not None and model_id not in models:
        return
    cnt = struct.unpack_from('<H', data, off + 0x3A)[0]
    rel = struct.unpack_from('<q', data, off + 0x60)[0]
    if not rel or cnt <= 0:
        return
    base = off + rel
    if base + cnt * 0x28 > len(data):
        return
    for k in range(cnt):
        o = base + k * 0x28
        shp = name_of(sdict, db, struct.unpack_from('<I', data, o)[0])
        # WG 渲染集名是形状名（xxShape，无后缀）；对应 .vertices 名 = xxShape.vertices。
        # 与 Korabli 不同（Korabli 渲染集 +0x00 直接就是 .vertices 名）。
        if shp and not shp.endswith('.vertices'):
            shp += '.vertices'
        if not shp.endswith('.vertices'):
            continue
        mat = name_of(sdict, db, struct.unpack_from('<I', data, o + 4)[0])
        mfm_h = struct.unpack_from('<Q', data, o + 0x10)[0]
        mfm = path_of(mfm_h, db, self_id_idx)
        damage = int('_crack_' in shp or '_lod' in shp or 'Crack' in mat)
        skinned = data[o + 0x18]
        ncnt = data[o + 0x19]
        nodes: list = []
        # ★ 所有条目都读 nodes（含 skinned=0 的刚性父子模型），供定位用。
        if ncnt:
            nrel = struct.unpack_from('<q', data, o + 0x20)[0]
            if nrel:
                nabs = o + nrel
                if nabs + ncnt * 4 <= len(data):
                    for j in range(ncnt):
                        nid = struct.unpack_from('<I', data, nabs + j * 4)[0]
                        nm = name_of(sdict, db, nid)
                        if nm:
                            nodes.append(nm)
        add_row((gp, shp, model_id, mat, mfm, damage,
                 int(skinned), json.dumps(nodes)))
