"""纯网格装配：把解析出的 primitive 组装成 HullMesh / ArmorMesh。

这些函数只吃 numpy 数组与 dataclass、不碰文件/数据库/客户端包，所以能脱离
`GeometryService` 单独读、单独测。调用方仍是 `GeometryService`（类里保留了同名
staticmethod 别名），因为装配过程要拿它的解析缓存。

放置约定：
  - 形状名/LOD/调色板这类命名规则在这里（纯字符串 + 正则）；
  - 顶点拼接、绕序、包围盒这类数值活也在这里；
  - 读 .geometry / 查库 / 解贴图留在 geometry_service。
"""

from __future__ import annotations

import re

import numpy as np

from models.collision_materials import collision_material_name, thickness_to_color
from models.geometry_parser import ArmorModel
from models.ship_mesh import (
    COMPONENT_HULL, HIDDEN_GENERIC_MATERIALS,
    ArmorMesh, ArmorTriangleInfo, HullMesh, MountMesh, ShipGeometry,
)


#: shape 名里的 LOD 标记。两种命名并存，顺序不能换：
#: `Bow_lod2Shape`（数字紧跟 _lod）与 `JM039_lodShape1`（数字在 Shape 之后）
LOD_RES = (
    re.compile(r'_lod(\d+)', re.IGNORECASE),
    re.compile(r'_lodShape(\d+)', re.IGNORECASE),
)


def folder_from_model_path(model_path: str) -> str:
    """`.model` 路径 → 模型目录名（末段）。"""
    parts = [p for p in str(model_path).replace("\\", "/").split("/") if p]
    return parts[-2] if len(parts) >= 2 else ""

def vfs_entry(extractor, path: str):
    """O(1) 文件树查找（file_tree 是按路径索引的 dict，避免 list_files 全树扫描）。"""
    try:
        return extractor.file_tree.get(path)
    except Exception:  # noqa: BLE001
        return None

def model_family_token(folder: str) -> str:
    """提取型号 token（如 '5in54_Mark42'），用于同型号跨目录贴图共享匹配。

    型号段形如 `数字+字母+数字+_名称`（跳过目录名开头的编号段如 AGS542_）。
    """
    import re
    m = re.search(r"\d+[a-z]+\d+_\w+", folder)
    return m.group(0) if m else ""

def shape_lod(name: str) -> tuple[str, int]:
    """shape 名 → (去掉 LOD 标记的基名, LOD 级别)。无标记视为 0。"""
    s = name or ''
    for rx in LOD_RES:
        m = rx.search(s)
        if m:
            return s[:m.start()] + s[m.end():], int(m.group(1))
    return s, 0

def bake_rigid(prim, m) -> None:
    """把刚性父节点矩阵 m（4x4 行主序）烘焙进 primitive 的顶点（原地）。"""
    R = m[:3, :3].astype(np.float64)
    t = m[:3, 3].astype(np.float64)
    prim.positions = ((prim.positions.astype(np.float64) @ R.T) + t).astype(np.float32)
    new_nrm = prim.normals.astype(np.float64) @ R.T
    ln = np.linalg.norm(new_nrm, axis=1, keepdims=True)
    ln[ln == 0] = 1.0
    prim.normals = (new_nrm / ln).astype(np.float32)

def derived_mount_transform(hp: str, transforms: dict) -> np.ndarray | None:
    """无独立骨架挂点的 HP（如 `HP_JGM_2_HP_JGA_1` 主炮塔上防空炮）
    按最长前缀回退父挂点矩阵（`HP_JGM_2`）；找不到返回 None。"""
    parts = hp.split("_")
    for i in range(len(parts) - 1, 1, -1):
        cand = "_".join(parts[:i])
        if cand in transforms:
            return transforms[cand]
    return None

def material_layers(armor: dict, material_id: int) -> list[float]:
    """该材质跨 model_index 的所有非零层厚度（升序）。

    Dual 多层材质同一 material_id 可有多个 model_index 层；tooltip
    需要展示全部堆叠厚度（wows-toolkit lookup_all_layers 语义）。
    """
    vals = sorted({float(thk) for (mi, mat), thk in armor.items()
                   if mat == material_id and thk})
    return vals

def read_vfs(extractor, path: str) -> bytes:
    """按路径读取文件字节；不存在/失败返回 b""。"""
    e = vfs_entry(extractor, path)
    if e is None or e.is_directory or e.file_info is None:
        return b""
    try:
        return extractor.pkg_reader.read_file(e.volume.filename, e.file_info)
    except Exception:  # noqa: BLE001
        return b""

def match_thickness(armor: dict, material_id: int, layer_index: int) -> float:
    """按 (layer_index, material_id) 查厚度；未命中时取该材质任意非零层。"""
    v = armor.get((layer_index, material_id))
    if v is not None:
        return float(v)
    # 回退：该材质任一层（取最小非零，避免极端值）
    best = 0.0
    for (mi, mat), thk in armor.items():
        if mat == material_id and thk and (best == 0.0 or thk < best):
            best = thk
    return float(best)

def norm_shape_stem(name: str) -> str:
    """归一化 shape 名（仅去 .vertices 后缀；**不再剥尾部重复字母**）。

    ⚠️ 旧实现会把 TurretShapeff → TurretShape（剥 ff），导致 JGA180 的 TurretShape
    网格在渲染集里模糊匹配到 JGA181 的 TurretShapeff 行，错用 JGA010 贴图。
    实证 PT 26.9：TurretShapeff 是 JGA181.geometry 的真实网格名（mapping_id
    0xb6f4c31d），TurretShape 是 JGA180 的真实网格名（0x03432c0c）——二者是
    不同网格，必须严格区分，不得归并。模糊兜底保留（去后缀后同名才匹配），
    仅处理真正的大小写/后缀笔误。
    """
    return name[:-len('.vertices')] if name.endswith('.vertices') else name

def instanced_bounds(positions, matrix) -> tuple[np.ndarray, np.ndarray]:
    """局部顶点包围盒经实例矩阵变换后的世界包围盒（8 角点法）。"""
    mn = positions.min(axis=0)
    mx = positions.max(axis=0)
    corners = np.array([
        [mn[0], mn[1], mn[2]], [mn[0], mn[1], mx[2]],
        [mn[0], mx[1], mn[2]], [mn[0], mx[1], mx[2]],
        [mx[0], mn[1], mn[2]], [mx[0], mn[1], mx[2]],
        [mx[0], mx[1], mn[2]], [mx[0], mx[1], mx[2]],
    ], dtype=np.float32)
    hom = np.hstack([corners, np.ones((8, 1), dtype=np.float32)])
    w = (hom @ matrix.T)[:, :3]
    return w.min(axis=0), w.max(axis=0)

def palette_subgroups(prims) -> list[list]:
    """按蒙皮调色板把 primitive 再分组（保序）。

    一个网格只能带**一份** `skin_bones/skin_bind`，而同一材质下常混有调色板
    不同的 shape（如 `TurretShape` 4 骨骼 + `insideShape` 1 骨骼、`Dasha` 的
    手臂/腿各一套骨骼）。合并后顶点组索引会按错误的调色板翻译 → 权重落到
    别的骨骼上。故合并前按调色板名序列拆开，保证每个网格的调色板一致。
    """
    out: dict = {}
    for p in prims:
        key = tuple(getattr(p, "skin_bones", None) or ())
        out.setdefault(key, []).append(p)
    return list(out.values())

def snapshot_without_mounts(geom: ShipGeometry) -> ShipGeometry:
    """船体/装甲就绪时的浅拷贝快照（列表/字典复制、元素共享）。

    后台线程后续的挂载解析只改**原对象**，快照不会再变 ⇒ 调用方可安全使用。
    """
    import copy

    snap = copy.copy(geom)
    snap.hull_meshes = list(geom.hull_meshes)
    snap.armor_meshes = list(geom.armor_meshes)
    snap.mounts = []
    snap.texture_dds = dict(geom.texture_dds)
    snap.stats = dict(geom.stats)
    warns = snap.stats.get("warnings")
    if isinstance(warns, list):
        snap.stats["warnings"] = list(warns)
    return snap

def misc_name_from_node(node_name: str) -> str:
    """MP_ 节点名 → misc 模型名（去 MP_ 前缀 + 尾部实例索引）。

    对齐 wows-toolkit `misc_name_from_node`：去 MP_ 前缀，剥尾部
    `.NNN`（带点三位数）或 `_INDEX_N`（传统索引）后缀。
    例：MP_BM800_Ventilators_mushroom_INDEX_16 → BM800_Ventilators_mushroom
    """
    if not node_name.startswith("MP_"):
        return node_name
    rest = node_name[3:]
    if "." in rest:
        pos = rest.rfind(".")
        tail = rest[pos + 1:]
        if len(tail) == 3 and tail.isdigit():
            return rest[:pos]
    idx = rest.rfind("_INDEX_")
    if idx >= 0:
        return rest[:idx]
    return rest

def apply_declared_color(hm, bundle: dict, extractor):
    """按 .mfm **声明的贴图属性**设置 mesh 颜色贴图（只认声明，不按名补全）。

    - indexed：用 albedoArray（分块图集）
    - 标准：用 diffuseMap
    - 都没声明：has_color=False、texture_dds=None（无色 → 按透明/不参与颜色计算处理）

    bundle 为空（mfm 不在库/解析失败）同样按无色处理，绝不按文件名补全。
    """
    texs = (bundle or {}).get('textures') or {}
    hm.tech_family = (bundle or {}).get('tech_family', 'pbs')
    hm.material_textures = texs
    hm.indexed_params = (bundle or {}).get('indexed_params')
    if hm.tech_family == 'indexed' and 'albedoArray' in texs:
        hm.texture_path, hm.texture_dds = texs['albedoArray']
    elif 'diffuseMap' in texs:
        hm.texture_path, hm.texture_dds = texs['diffuseMap']
    # meshdecal 的 albedo（SHIP_MESHDECAL_ALBEDO_*）用 g_albedoMap：
    # 仍是该材质的颜色贴图（alpha 遮罩），应参与渲染，不能当无色 overlay。
    elif 'g_albedoMap' in texs:
        hm.texture_path, hm.texture_dds = texs['g_albedoMap']
    else:
        # ★ 只认 mfm 声明：没声明颜色贴图就当不存在，不再按名/前缀猜 diffuse
        hm.texture_path, hm.texture_dds = "", None
    hm.has_color = bool(hm.texture_dds)

def clone_prim(p):
    """复制一个 MeshPrimitive（顶点/法线/uv/索引各拷贝一份，供多节点分别烘焙）。"""
    from models.geometry_parser import MeshPrimitive
    return MeshPrimitive(
        name=p.name,
        positions=p.positions.copy(),
        normals=p.normals.copy(),
        uvs=None if p.uvs is None else p.uvs.copy(),
        indices=None if p.indices is None else p.indices.copy(),
        uv2=None if getattr(p, "uv2", None) is None else p.uv2.copy(),
        tangents=(None if getattr(p, "tangents", None) is None
                  else p.tangents.copy()),
        binormals=(None if getattr(p, "binormals", None) is None
                   else p.binormals.copy()),
        t4=None if getattr(p, "t4", None) is None else p.t4.copy(),
        extra=None if getattr(p, "extra", None) is None else p.extra.copy(),
        mapping_id=p.mapping_id,
        is_skinned=p.is_skinned,
        bone_indices=None if p.bone_indices is None else p.bone_indices.copy(),
        bone_weights=None if p.bone_weights is None else p.bone_weights.copy(),
        skin_bones=list(p.skin_bones),
        skin_bind=list(p.skin_bind),
        shape_name=getattr(p, "shape_name", ""),
        node_name=getattr(p, "node_name", ""),
    )

def dedupe_mount_instances(geom: ShipGeometry) -> None:
    """把 `geom.mounts` 中共享同一几何的 MountMesh 合并为**实例化**网格。

    所有节点类型（HP 挂载 / MP 甲板设备 / 挂载模型骨架上的 MP 子设备）统一收敛：
    同一模型目录被多个节点引用时，不再逐节点复制顶点，而是每个
    (model_folder, 几何, component) 只保留一份网格，把各节点的渲染矩阵存入
    instance_matrices。渲染时逐节点各画一次 → 大幅降低 GPU 内存。

    依据：`_load_mount_model` 按 folder 缓存，同一模型的 `positions` 是同一个
    ndarray 对象，故用 `id(positions)` 判同几何。
    """
    groups: dict = {}
    order: list = []
    for mm in geom.mounts:
        key = (getattr(mm, 'model_folder', ''), id(mm.positions),
               getattr(mm, 'component', ''), getattr(mm, 'tech_family', 'pbs'))
        if key not in groups:
            groups[key] = [mm, [], []]
            order.append(key)
        g = groups[key]
        labels = list(getattr(mm, 'instance_labels', None) or [])
        if mm.instance_matrices:
            g[1].extend(list(mm.instance_matrices))
            for _k in range(len(mm.instance_matrices)):
                g[2].append(labels[_k] if _k < len(labels) else str(mm.name))
        elif mm.model_matrix is not None:
            g[1].append(np.ascontiguousarray(mm.model_matrix, dtype=np.float32))
            g[2].append(labels[0] if labels else str(mm.name))
        else:
            g[1].append(np.eye(4, dtype=np.float32))
            g[2].append(labels[0] if labels else str(mm.name))
    new_mounts: list = []
    for key in order:
        rep, mats, labels = groups[key]
        n = len(mats)
        if n == 1:
            # 单节点：无需实例化，保留原样（不额外复制）
            if rep.instance_matrices and not rep.model_matrix:
                rep.instance_matrices = mats
            rep.instance_labels = labels
            new_mounts.append(rep)
            continue
        name = f"{getattr(rep, 'name', rep.model_folder)}+({n})"
        inst = MountMesh(
            name=name, component=rep.component,
            positions=rep.positions, normals=rep.normals,
            uvs=rep.uvs, indices=rep.indices,
            uv2=rep.uv2, tangents=rep.tangents, binormals=rep.binormals,
            t4=rep.t4,
            extra=rep.extra,
            model_matrix=None,
            texture_dds=rep.texture_dds, texture_path=rep.texture_path,
            model_folder=rep.model_folder, vertex_count=rep.vertex_count,
            is_wire=rep.is_wire, is_crack=rep.is_crack,
            tech_family=rep.tech_family,
            material_textures=rep.material_textures or {},
            indexed_params=rep.indexed_params,
            has_color=rep.has_color,
            bone_indices=rep.bone_indices, bone_weights=rep.bone_weights,
            skin_bones=rep.skin_bones, skin_bind=rep.skin_bind,
            shape_names=rep.shape_names, node_names=rep.node_names,
            shape_spans=rep.shape_spans,
            instance_matrices=mats,
            instance_labels=labels,
        )
        new_mounts.append(inst)
    geom.mounts = new_mounts

def build_armor_mesh(am: ArmorModel, armor_thickness: dict,
                      component: str = COMPONENT_HULL,
                      model_matrix: np.ndarray | None = None) -> ArmorMesh | None:
    """装甲模型 → 带厚度颜色/材质信息的 ArmorMesh（每三角形 3 顶点同色）。

    component 标记该装甲的归属分类（船体/主炮塔/副炮/...）；挂载装甲
    传入 model_matrix（挂点变换）用于定位到舰船空间。

    ★ 厚度 ≤0（0mm）的三角形**直接剔除**（游戏内装甲查看器不显示无厚度
    数据的碰撞面；保留会与有效装甲叠色干扰判读）。全部被剔除时返回 None。
    """
    tris = am.triangles
    if not tris:
        return None
    infos: list[ArmorTriangleInfo] = []
    layers_cache: dict[int, list[float]] = {}
    keep: list[int] = []   # 保留的三角形下标（thickness > 0）

    for t, tri in enumerate(tris):
        mat_id = tri.material_id
        layer = tri.layer_index
        thickness = match_thickness(armor_thickness, mat_id, layer)
        if thickness <= 0.0:
            continue   # 0mm：无厚度数据，彻底驱逐
        keep.append(t)
        color = thickness_to_color(thickness)
        mat_name = collision_material_name(mat_id)
        from models.collision_materials import zone_from_material_name
        zone = zone_from_material_name(mat_name)
        layers = layers_cache.get(mat_id)
        if layers is None:
            layers = material_layers(armor_thickness, mat_id)
            layers_cache[mat_id] = layers
        hidden = zone == "Hull" and mat_name in HIDDEN_GENERIC_MATERIALS
        infos.append(ArmorTriangleInfo(
            material_id=mat_id,
            material_name=mat_name,
            layer_index=layer,
            thickness_mm=thickness,
            color=color,
            zone=zone,
            layers=layers,
            hidden=hidden,
            plate_key=(zone, mat_name, round(thickness * 10)),
        ))

    if not keep:
        return None
    n = len(keep)
    positions = np.empty((n * 3, 3), dtype=np.float32)
    normals = np.empty((n * 3, 3), dtype=np.float32)
    colors = np.empty((n * 3, 4), dtype=np.float32)
    indices = np.arange(n * 3, dtype=np.uint32)
    for i, t in enumerate(keep):
        tri = tris[t]
        base = i * 3
        positions[base:base + 3] = tri.vertices
        normals[base:base + 3] = tri.normals
        colors[base:base + 3] = infos[i].color

    return ArmorMesh(
        name=am.name,
        positions=positions,
        normals=normals,
        colors=colors,
        indices=indices,
        triangles=infos,
        component=component,
        model_matrix=model_matrix,
    )

def merge_hull(part_name: str, primitives, shape_names: list | None = None,
                node_names: list | None = None,
                instance_matrices: list | None = None) -> HullMesh:
    """把同一部件的所有 primitive 合并成单个网格。

    ⚠️ 调用方必须先用 `_palette_subgroups` 按蒙皮调色板分好组：本函数只保留
    第一份非空调色板，混着不同调色板的顶点会被翻译成错误骨骼的权重。
    """
    v_total = sum(p.positions.shape[0] for p in primitives)
    i_total = sum(0 if p.indices is None else p.indices.size for p in primitives)
    has_uv = any(p.uvs is not None for p in primitives)
    has_uv2 = any(getattr(p, "uv2", None) is not None for p in primitives)
    has_tb = any(getattr(p, "tangents", None) is not None for p in primitives)
    has_t4 = any(getattr(p, "t4", None) is not None for p in primitives)
    has_extra = any(getattr(p, "extra", None) is not None for p in primitives)
    has_bone = any(p.bone_indices is not None for p in primitives)

    positions = np.empty((v_total, 3), dtype=np.float32)
    normals = np.empty((v_total, 3), dtype=np.float32)
    uvs = np.empty((v_total, 2), dtype=np.float32) if has_uv else None
    uv2 = np.empty((v_total, 2), dtype=np.float32) if has_uv2 else None
    tangents = np.empty((v_total, 3), dtype=np.float32) if has_tb else None
    binormals = np.empty((v_total, 3), dtype=np.float32) if has_tb else None
    t4 = np.empty((v_total, 4), dtype=np.uint8) if has_t4 else None
    extra = np.empty((v_total, 4), dtype=np.uint8) if has_extra else None
    indices = np.empty(i_total, dtype=np.uint32)
    bone_indices = np.empty((v_total, 4), dtype=np.uint8) if has_bone else None
    bone_weights = np.empty((v_total, 4), dtype=np.float32) if has_bone else None
    skin_bones: list = []
    skin_bind: list = []

    def _copy_attr(dst, src_val, fill=0.0):
        if dst is not None:
            if src_val is not None:
                dst[voff:voff + n] = src_val
            else:
                dst[voff:voff + n] = fill

    voff = 0
    ioff = 0
    shape_spans: list = []
    for p in primitives:
        n = p.positions.shape[0]
        i_start = ioff
        i_count = 0 if p.indices is None else int(p.indices.size)
        if n:
            shape_spans.append((
                getattr(p, "shape_name", "") or "",
                getattr(p, "node_name", "") or "",
                voff, n, i_start, i_count))
        positions[voff:voff + n] = p.positions
        normals[voff:voff + n] = p.normals
        _copy_attr(uvs, p.uvs)
        _copy_attr(uv2, getattr(p, "uv2", None))
        _copy_attr(tangents, getattr(p, "tangents", None))
        _copy_attr(binormals, getattr(p, "binormals", None))
        _copy_attr(t4, getattr(p, "t4", None), fill=0)
        _copy_attr(extra, getattr(p, "extra", None), fill=0)
        if bone_indices is not None:
            if p.bone_indices is not None:
                bone_indices[voff:voff + n] = p.bone_indices
                bone_weights[voff:voff + n] = p.bone_weights
            else:
                bone_indices[voff:voff + n] = 0
                bone_weights[voff:voff + n] = 0.0
        if not skin_bones and p.skin_bones:
            skin_bones = list(p.skin_bones)
            skin_bind = list(p.skin_bind)
        if p.indices is not None and p.indices.size:
            indices[ioff:ioff + p.indices.size] = p.indices.astype(np.uint32) + voff
            ioff += p.indices.size
        voff += n

    return HullMesh(
        name=part_name,
        positions=positions,
        normals=normals,
        uvs=uvs,
        uv2=uv2,
        tangents=tangents,
        binormals=binormals,
        t4=t4,
        extra=extra,
        indices=indices[:ioff] if ioff < i_total else indices,
        vertex_count=voff,
        bone_indices=bone_indices,
        bone_weights=bone_weights,
        skin_bones=skin_bones,
        skin_bind=skin_bind,
        shape_names=list(dict.fromkeys(shape_names or [])),
        node_names=list(dict.fromkeys(node_names or [])),
        instance_matrices=list(instance_matrices or []),
        shape_spans=shape_spans,
    )
