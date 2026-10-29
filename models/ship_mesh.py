"""舰船网格的数据模型（纯数据 + numpy，不含解析/IO）。

`services.geometry_service` 负责读包与缓存，解析结果就是这里的几个 dataclass；
`services.mesh_assembly` 负责把它们装配/合并；`services.export_service` 负责写 GLB。
拆开是为了让「数据结构」与「解析流程」能各自单独读。

术语：
  - HullMesh    舰体分段（或船体上的一个材质分片），顶点在舰船空间；
  - MountMesh   挂载实例（炮塔/防空/小艇…），顶点在模型本地坐标 + model_matrix；
  - ArmorMesh   装甲三角形汤（每三角形三顶点同色，按厚度着色）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np


@dataclass
class ShipInfo:
    """发现到的舰船（用于舰船列表下拉框）。"""
    game_key: str          # GameParams 键，如 "PFSB510_Bourgogne"
    display_name: str      # 本地化名称，如 "勃艮第"
    model_folder: str      # 几何文件夹名，如 "FSB025_Bourgogne_1945"
    model_path: str        # 完整 model VFS 路径
    nation: str = ""
    ship_type: str = ""    # 舰种（shiptype），如 "Battleship"
    tier: int = 0          # 等级 1..11
    group: str = ""        # 船型分组 group_status_key（可研发/加值/特种）
    has_geometry: bool = False


#: 模型部件归属（component / 归属分类，用于挂载与装甲按归属筛选显示）
COMPONENT_HULL = "船体"
COMPONENT_MAIN = "主炮塔"
COMPONENT_SECONDARY = "副炮塔"
COMPONENT_TORPEDOES = "鱼雷发射管"
COMPONENT_DEPTH_CHARGE = "深弹发射器"
COMPONENT_AA = "防空"
COMPONENT_DIRECTOR = "指挥仪"
COMPONENT_FINDER = "测距仪"
COMPONENT_RADAR = "雷达"
COMPONENT_DECK = "甲板设备"
COMPONENT_OTHER = "其他"

#: 游戏内装甲查看器隐藏的「通用 Hull 材质」集合（wows-toolkit hidden 语义）：
#: 原本借鉴 wows-toolkit 把 Hull 区通用材质（Trans/Deck/Belt 等）标记为隐藏，
#: 但这些板有真实厚度数据，在我们的查看器中应默认显示。清空该集合即可。
HIDDEN_GENERIC_MATERIALS: set[str] = set()


def _is_magenta_placeholder(dds_bytes: bytes, sample: int = 256) -> bool:
    """检测贴图是否为「洋红占位纹理」（缺失贴图常用纯色洋红，BC7 全 mode0）。

    只抽前若干 BC7 块解码颜色端点求平均色；平均 RGB 偏洋红(R>180,G<110,B>90)
    即视为占位。非 BC7 / 解析失败返回 False（不误判正常 camo）。
    实例图集(CIA000_instances_atlas_art)实测全 mode0 纯洋红 (238,51,127)。
    """
    try:
        from services.texture_io import parse_dds
        dds = parse_dds(dds_bytes)
        if dds.bc_kind != 8 or not dds.mips:
            return False
        mip0 = dds.mips[0]
        w, h = dds.width, dds.height
        nblocks = (w // 4) * (h // 4)
        if nblocks == 0:
            return False

        def _unpack(b: bytes, off: int, n: int) -> int:
            v = 0
            for i in range(n):
                bit = (off + i) // 8
                biti = (off + i) % 8
                if bit < len(b):
                    v |= ((b[bit] >> biti) & 1) << i
            return v

        def _ep(b: bytes, offs) -> list:
            """按 (off, bits) 解码 3 通道端点色，返回 0~255 值。"""
            out = []
            for off, bits in offs:
                v = _unpack(b, off, bits)
                out.append(v / ((1 << bits) - 1) * 255.0)
            return out

        total = [0.0, 0.0, 0.0]
        cnt = 0
        stride = max(1, nblocks // sample)
        for bi in range(0, nblocks, stride):
            if cnt >= sample:
                break
            blk = mip0[bi * 16: bi * 16 + 16]
            if len(blk) < 16:
                break
            m = 0
            for k in range(8):
                if (blk[0] >> (7 - k)) & 1:
                    m += 1
                else:
                    break
            if m == 0:
                p0 = _ep(blk, [(7, 4), (11, 4), (15, 4)])
                p1 = _ep(blk, [(22, 4), (26, 4), (30, 4)])
            elif m == 1:
                p0 = _ep(blk, [(8, 6), (14, 6), (20, 6)])
                p1 = _ep(blk, [(50, 6), (56, 6), (62, 6)])
            elif m == 2:
                p0 = _ep(blk, [(8, 5), (17, 5), (26, 5)])
                p1 = _ep(blk, [(56, 5), (65, 5), (74, 5)])
            elif m == 3:
                p0 = _ep(blk, [(8, 7), (15, 7), (22, 7)])
                p1 = _ep(blk, [(50, 7), (57, 7), (64, 7)])
            elif m == 4:
                p0 = _ep(blk, [(8, 5), (19, 5), (30, 5)])
                p1 = _ep(blk, [(49, 5), (60, 5), (71, 5)])
            elif m == 5:
                p0 = _ep(blk, [(8, 7), (15, 7), (22, 7)])
                p1 = _ep(blk, [(49, 7), (56, 7), (63, 7)])
            elif m == 6:
                p0 = _ep(blk, [(8, 7), (15, 7), (22, 7)])
                p1 = _ep(blk, [(43, 7), (50, 7), (57, 7)])
            elif m == 7:
                p0 = _ep(blk, [(8, 5), (17, 5), (26, 5)])
                p1 = _ep(blk, [(56, 5), (65, 5), (74, 5)])
            else:
                continue
            total[0] += (p0[0] + p1[0]) * 0.5
            total[1] += (p0[1] + p1[1]) * 0.5
            total[2] += (p0[2] + p1[2]) * 0.5
            cnt += 1
        if cnt == 0:
            return False
        r, g, b = total[0] / cnt, total[1] / cnt, total[2] / cnt
        return r > 180.0 and g < 110.0 and b > 90.0
    except Exception:
        return False

#: GameParams 组件键 → 归属分类（按 base 组件名匹配任意前缀变体）
#: 键格式 {PREFIX}_{BASE}，如 AB1_Artillery / B_AirDefense / A1_Torpedoes
_COMPONENT_BY_BASE = {
    "Artillery": COMPONENT_MAIN,            # 主炮塔（A1_/AB1_/B_/C_/X_... 前缀）
    "ATBA": COMPONENT_SECONDARY,            # 副炮（A_/B_...）
    "SecondaryArtillery": COMPONENT_MAIN,
    "AirDefense": COMPONENT_AA,             # 防空（A_/B_...）
    "AirSupport": COMPONENT_OTHER,
    "Directors": COMPONENT_DIRECTOR,        # 指挥仪（A_/AB_...）
    "Finders": COMPONENT_FINDER,            # 测距仪（A_/AB_...）
    "Radars": COMPONENT_RADAR,              # 雷达（A_/AB_...）
    "AirArmament": COMPONENT_OTHER,         # 弹射器（HP_JC_*）
    "Torpedoes": COMPONENT_TORPEDOES,        # 鱼雷发射管（HP_*GT_*）
    "DepthChargeGuns": COMPONENT_DEPTH_CHARGE,  # 深水炸弹炮塔（HP_*GB_*）
}


def _component_base(comp_key: str) -> str:
    """组件键的 base 组件名：`AB1_Artillery` → `Artillery`。"""
    if not comp_key:
        return ""
    return comp_key.split("_", 1)[1] if "_" in comp_key else comp_key


def component_for_key(comp_key: str) -> str:
    """GameParams 组件键 → 归属分类（未知键归"其他"）。

    键格式 {PREFIX}_{BASE}，BASE 匹配任意前缀变体（A1_/AB_/B_/C_/X_...），
    避免固定键列表遗漏合法变体（如 PGSB207 的 AB1_Artillery、AB_Directors、
    B_AirDefense、A1_Torpedoes 等）。
    """
    return _COMPONENT_BY_BASE.get(_component_base(comp_key), COMPONENT_OTHER)


def is_known_component_key(comp_key: str) -> bool:
    """组件键是否已被识别（按 base 组件名匹配任意前缀变体）。"""
    return _component_base(comp_key) in _COMPONENT_BY_BASE


def iter_component_groups(data: dict):
    """遍历快照中所有组件分组，产出 (component_key, category, group_dict)。

    不依赖固定键列表：按 base 组件名匹配任意前缀变体，其余归「其他」。
    或归「其他」。调用方据此加载挂载引用/装甲厚度，并可选告警未知组件。
    """
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        yield key, component_for_key(key), value


# ── MP 甲板设备节点 → misc 模型映射（纯命名约定，数据驱动校验 99.99% 命中）──
# MP_{baseID}_{desc}_{instance}[_INDEX_N] → 去 MP_ 前缀 → 首个「字母+数字」token
# 即 baseID；国家目录由 baseID 首字母决定；模型目录 = baseID（几何在
# content/gameplay/{nation}/misc/{baseID}/{baseID}.geometry，与 HP 挂载同一
# 目录索引/渲染集/贴图管线加载）。
_MP_NATION_BY_LETTER = {
    "A": "usa", "B": "uk", "C": "common", "F": "france", "G": "germany",
    "H": "netherlands", "I": "italy", "J": "japan", "R": "russia",
    "S": "spain", "U": "commonwealth", "V": "panamerica", "W": "europe",
    "X": "events", "Z": "panasia",
}
_MP_BASE_RE = re.compile(r"^([A-Z][A-Z0-9]*?\d+)")


def mp_base_id(mp_name: str) -> str:
    """MP 节点名 → baseID（模型目录名）；无法解析返回空串。

    例：MP_AM003_Fairlead_1 / MP_AM003_Fairlead_1_INDEX_2 → "AM003"
    """
    body = mp_name[3:] if mp_name.startswith("MP_") else mp_name
    m = _MP_BASE_RE.match(body)
    return m.group(1) if m else ""


@dataclass
class HullMesh:
    """一个舰体部件网格（顶点/法线/UV/索引，已合并该部件全部 primitive）。

    若按材质拆分（不同模型使用不同贴图），material/texture_dds 记录该分片的
    材质与独立贴图；否则 material=None，使用舰体默认贴图。
    """
    name: str
    positions: np.ndarray          # (N,3) f32
    normals: np.ndarray            # (N,3) f32
    indices: np.ndarray            # (M,) u32
    vertex_count: int = 0
    uvs: np.ndarray | None = None  # (N,2) f32（贴图用）
    #: 第二套 UV（格式含 uv2 时）
    uv2: np.ndarray | None = None
    #: 切线/副切线（格式含 tb 时）
    tangents: np.ndarray | None = None
    binormals: np.ndarray | None = None
    #: 单独 t 属性的原始 4 字节（语义未知，原样保留）
    t4: np.ndarray | None = None
    #: ``r`` 属性原始 4 字节（(N,4) uint8，不做语义解释）
    extra: np.ndarray | None = None
    material: str | None = None    # 材质名（如 TL2_SHIPMAT_PBS_DeckHouse）
    texture_dds: bytes | None = None   # 该材质独立贴图（.mfm diffuseMap 或 INDEXED albedoArray）
    texture_path: str = ""
    #: 是否声明了颜色贴图（mfm 有 diffuseMap / indexed 有 albedoArray）。False=无色：
    #: 按透明/不参与颜色计算处理（如仅 normalMap 的贴花层），避免误渲成白色/串错贴图。
    has_color: bool = True
    tech_family: str = "pbs"           # shader 技术族：pbs(0x0005)/indexed(0x0009)/other
    material_textures: dict = field(default_factory=dict)  # {贴图键: (vfs_path, bytes)}
    indexed_params: dict | None = None  # INDEXED 分块参数 {arrays, grid, offset}
    #: emissive 自发光强度（mfm emissivePower 属性；None=默认 1.0）
    emissive_power: float | None = None
    opaque: bool = True                 # 半透明材质（玻璃等）用 alpha 混合
    is_wire: bool = False               # wire 线框辅助网格：用 GL_LINES 渲染（非实体面）
    #: crack/damage 损伤网格（查看器不显示，导出保留用）
    is_crack: bool = False
    #: bind pose 蒙皮已实际施加到顶点（Root_BlendBone 已烘焙进几何）；
    #: 挂载矩阵不得再乘 rb，否则双重镜像 → 朝向翻转（PASB111 副炮 AGS542 实证）
    skinned_applied: bool = False
    #: 蒙皮权重（GLB 导出 glTF skin 用；非蒙皮网格为 None）
    bone_indices: np.ndarray | None = None   # (N,4) uint8（调色板 slot × 3）
    bone_weights: np.ndarray | None = None   # (N,4) float32
    #: 蒙皮调色板骨骼名与 bind 世界矩阵（游戏空间），导出时转 glTF 空间
    skin_bones: list = field(default_factory=list)
    skin_bind: list = field(default_factory=list)
    #: 构成该网格的形状名（*.vertices 去后缀，如 BIA454_Bollard_bigShape）与
    #: 对应绑定的骨骼节点名（如 BIA454_Bollard_big_6）；调试模式 3 标签用。
    shape_names: list = field(default_factory=list)
    node_names: list = field(default_factory=list)
    #: 逐节点实例矩阵（渲染空间 4x4 行主序）；非空时该网格是一份原始几何，
    #: 渲染时按其每个矩阵各画一次（左右舷/多处实例，如切锯机 _0/_1）。
    instance_matrices: list = field(default_factory=list)
    #: 按 shape 切片的区间：[(shape_name, node_name, v_start, v_count, i_start,
    #: i_count), ...]，与 `_merge_hull` 的合并顺序一致。GLB 导出据此把合并网格
    #: 重新拆成 .visual 的每个 renderSet（一个 Shape 一个网格对象）。
    shape_spans: list = field(default_factory=list)


@dataclass
class MountMesh:
    """一个挂载实例（炮塔/副炮/防空/指挥仪等）。

    - 几何为该部件模型目录（本地坐标），经 model_matrix 变换到舰船空间
    - 每个部件使用自己的独立贴图（`{stem}_a.dd0` 等命名约定）
    - component 为归属分类（主炮塔/副炮/防空/...），支持按归属筛选显示
    """
    name: str                      # HP_JGM_1
    component: str                 # 归属分类
    positions: np.ndarray          # (N,3) f32 本地坐标
    normals: np.ndarray            # (N,3) f32
    indices: np.ndarray            # (M,) u32
    uvs: np.ndarray | None = None  # (N,2) f32
    #: 第二套 UV / 切线 / 副切线 / 未知 t4 / r 属性（与 HullMesh 同义）
    uv2: np.ndarray | None = None
    tangents: np.ndarray | None = None
    binormals: np.ndarray | None = None
    t4: np.ndarray | None = None
    extra: np.ndarray | None = None
    model_matrix: np.ndarray | None = None  # (4,4) f32 行主序（渲染空间）
    texture_dds: bytes | None = None
    texture_path: str = ""
    model_folder: str = ""         # 来源模型目录
    vertex_count: int = 0
    is_wire: bool = False          # wire 线框辅助网格：GL_LINES 渲染
    #: 是否声明了颜色贴图（mfm 有 diffuseMap / indexed 有 albedoArray）。False=无色：
    #: 按透明/不参与颜色计算处理（如仅 normalMap 的贴花层）。
    has_color: bool = True
    tech_family: str = "pbs"      # shader 技术族：pbs/indexed/other/grid
    #: 材质贴图集 {贴图键: (vfs_path, bytes)}（INDEXED：albedoArray/materialIdMap/artMap）
    material_textures: dict = field(default_factory=dict)
    #: INDEXED 分块参数 {arrays, grid, offset}（炮塔等分块涂装）
    indexed_params: dict | None = None
    #: emissive 自发光强度（mfm emissivePower 属性；None=默认 1.0）
    emissive_power: float | None = None
    #: crack/damage 损伤网格（查看器不显示，导出保留用）
    is_crack: bool = False
    #: 蒙皮权重（GLB 导出 glTF skin 用；非蒙皮为 None）
    bone_indices: np.ndarray | None = None
    bone_weights: np.ndarray | None = None
    skin_bones: list = field(default_factory=list)
    skin_bind: list = field(default_factory=list)
    #: 构成该网格的形状名与绑定的骨骼节点名（调试模式 3 标签用）。
    shape_names: list = field(default_factory=list)
    node_names: list = field(default_factory=list)
    #: 逐节点实例矩阵（渲染空间 4x4 行主序）；非空时该网格是一份原始几何，
    #: 渲染时按其每个矩阵各画一次（挂载模型内的多处实例）。
    instance_matrices: list = field(default_factory=list)
    #: 与 instance_matrices 对应的节点名（HP_/MP_ 挂点名）；GLB 导出用它命名
    #: 每个实例的空物体，缺省时回退到网格名。
    instance_labels: list = field(default_factory=list)
    #: 按 shape 切片的区间（同 HullMesh.shape_spans）。
    shape_spans: list = field(default_factory=list)

    def bounds_in_world(self) -> tuple[np.ndarray, np.ndarray]:
        """本地包围盒经 model_matrix 变换后的世界包围盒。"""
        pts = np.empty((8, 3), dtype=np.float32)
        if self.model_matrix is not None:
            mn = self.positions.min(axis=0)
            mx = self.positions.max(axis=0)
            corners = np.array([
                [mn[0], mn[1], mn[2]], [mx[0], mn[1], mn[2]],
                [mn[0], mx[1], mn[2]], [mx[0], mx[1], mn[2]],
                [mn[0], mn[1], mx[2]], [mx[0], mn[1], mx[2]],
                [mn[0], mx[1], mx[2]], [mx[0], mx[1], mx[2]],
            ], dtype=np.float32)
            hom = np.hstack([corners, np.ones((8, 1), dtype=np.float32)])
            w = hom @ self.model_matrix.T
            return w[:, :3].min(axis=0), w[:, :3].max(axis=0)
        return self.positions.min(axis=0), self.positions.max(axis=0)


@dataclass
class ArmorTriangleInfo:
    material_id: int
    material_name: str
    layer_index: int
    thickness_mm: float
    color: tuple[float, float, float, float]
    zone: str
    #: 该材质所有非零层厚度（Dual 多层材质，升序；tooltip 展示堆叠）
    layers: list[float] = field(default_factory=list)
    #: 游戏内装甲查看器隐藏的板（Hull 区通用材质 Trans/Deck/Belt 等）
    hidden: bool = False
    #: 板块键 (zone, material_name, thickness_tenths)；高亮/显隐/描边判别
    plate_key: tuple = ()


@dataclass
class ArmorMesh:
    name: str
    positions: np.ndarray          # (N,3) f32（N = 三角形数×3）
    normals: np.ndarray            # (N,3) f32
    colors: np.ndarray             # (N,4) f32（每个三角形三顶点同色）
    indices: np.ndarray            # (M,) u32
    triangles: list[ArmorTriangleInfo] = field(default_factory=list)
    #: 归属分类（船体/主炮塔/副炮/防空/...），用于按归属筛选显示
    component: str = COMPONENT_HULL
    #: 舰船空间变换（挂载装甲需经挂点矩阵定位；舰体装甲为 None）
    model_matrix: np.ndarray | None = None

    def bounds_in_world(self) -> tuple[np.ndarray, np.ndarray]:
        """装甲本地包围盒经 model_matrix 变换后的世界包围盒（舰体装甲恒等）。"""
        pts = self.positions
        if self.model_matrix is not None and pts.size:
            corners = np.array([
                [pts[:, 0].min(), pts[:, 1].min(), pts[:, 2].min()],
                [pts[:, 0].max(), pts[:, 1].min(), pts[:, 2].min()],
                [pts[:, 0].min(), pts[:, 1].max(), pts[:, 2].min()],
                [pts[:, 0].max(), pts[:, 1].max(), pts[:, 2].min()],
                [pts[:, 0].min(), pts[:, 1].min(), pts[:, 2].max()],
                [pts[:, 0].max(), pts[:, 1].min(), pts[:, 2].max()],
                [pts[:, 0].min(), pts[:, 1].max(), pts[:, 2].max()],
                [pts[:, 0].max(), pts[:, 1].max(), pts[:, 2].max()],
            ], dtype=np.float32)
            hom = np.hstack([corners, np.ones((8, 1), dtype=np.float32)])
            w = hom @ self.model_matrix.T
            return w[:, :3].min(axis=0), w[:, :3].max(axis=0)
        if pts.size:
            return pts.min(axis=0), pts.max(axis=0)
        return np.zeros(3, dtype=np.float32), np.zeros(3, dtype=np.float32)


@dataclass
class CollisionModelData:
    name: str
    size_in_bytes: int
    data: bytes = b""


@dataclass
class ShipGeometry:
    game_key: str
    display_name: str
    model_folder: str
    hull_meshes: list[HullMesh] = field(default_factory=list)
    mounts: list[MountMesh] = field(default_factory=list)
    armor_meshes: list[ArmorMesh] = field(default_factory=list)
    collision_models: list[CollisionModelData] = field(default_factory=list)
    bounds_min: np.ndarray | None = None
    bounds_max: np.ndarray | None = None
    stats: dict = field(default_factory=dict)
    #: 舰体漫反射贴图（.dds 原始字节）与 VFS 路径（可能为 None，未找到贴图）
    texture_dds: bytes | None = None
    texture_path: str = ""
