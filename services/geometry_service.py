"""
geometry_service.py —— 舰船 3D 几何与装甲提取服务。

职责：
  1. 发现舰船（GameParams 键、model 路径、装甲厚度字典、显示名）——DB-first：
     优先主库 ship_models/entity_snapshots 表；data/split JSON 仅作旧库回退
  2. 用 data_extractor.GameExtractor 从游戏客户端读取该舰所有 .geometry 部件文件
  3. 解析并合并为可直接上传 GPU 的 HullMesh / ArmorMesh / 碰撞模型

内存约定：逐文件读取→解析→合并，随即释放原始 bytes；大船（数十万顶点）
总占用控制在合理范围（遵守 2GB 红线）。后台线程中调用，通过 progress_cb 汇报。
"""

from __future__ import annotations

import contextlib
import threading

import numpy as np

from app.application import app as app_ctx
from app.signals import bus
from models.geometry_parser import GeometryError, parse_geometry
from models.ship_mesh import (  # noqa: F401  （ShipInfo 等仍从这里对外）
    COMPONENT_DECK, COMPONENT_HULL,
    ArmorMesh, CollisionModelData, MountMesh, ShipGeometry, ShipInfo,
    _is_magenta_placeholder, is_known_component_key, iter_component_groups,
    mp_base_id,
)
from services import mesh_filters
from services.mesh_assembly import (
    LOD_RES,
    apply_declared_color, bake_rigid, build_armor_mesh, clone_prim,
    dedupe_mount_instances, derived_mount_transform, folder_from_model_path,
    instanced_bounds, match_thickness, material_layers, merge_hull,
    misc_name_from_node, model_family_token, norm_shape_stem, palette_subgroups,
    read_vfs, shape_lod, snapshot_without_mounts, vfs_entry,
)
from utils.asset_utils import murmur3_32, tech_family as fx_tech_family
from utils.threading_utils import TaskCancelled




# ────────────────────────────────────────────────────────────────────────────
# 服务入口
# ────────────────────────────────────────────────────────────────────────────

class GeometryService:
    """舰船几何服务（单例用法：GeometryService.instance）。"""

    _instance: "GeometryService | None" = None
    _lock = threading.Lock()

    # ── 纯装配函数（实现见 services/mesh_assembly.py）──────────────
    # 留在类里当别名，是为了不改动 30+ 处 `self._x(...)` / `GeometryService._x(...)`
    # 调用点；这些函数不碰 self，所以等价于模块级函数。
    _folder_from_model_path = staticmethod(folder_from_model_path)
    _vfs_entry = staticmethod(vfs_entry)
    _model_family_token = staticmethod(model_family_token)
    _shape_lod = staticmethod(shape_lod)
    _bake_rigid = staticmethod(bake_rigid)
    _derived_mount_transform = staticmethod(derived_mount_transform)
    _material_layers = staticmethod(material_layers)
    _read_vfs = staticmethod(read_vfs)
    _match_thickness = staticmethod(match_thickness)
    _norm_shape_stem = staticmethod(norm_shape_stem)
    _instanced_bounds = staticmethod(instanced_bounds)
    _palette_subgroups = staticmethod(palette_subgroups)
    _snapshot_without_mounts = staticmethod(snapshot_without_mounts)
    _misc_name_from_node = staticmethod(misc_name_from_node)
    _apply_declared_color = staticmethod(apply_declared_color)
    _clone_prim = staticmethod(clone_prim)
    _dedupe_mount_instances = staticmethod(dedupe_mount_instances)
    _build_armor_mesh = staticmethod(build_armor_mesh)
    _merge_hull = staticmethod(merge_hull)
    _LOD_RES = LOD_RES

    def __init__(self):
        self._extractor = None  # 懒创建
        # assets.bin 骨架/材质服务（懒加载，跨船复用）
        self._assets_svc = None
        self._assets_tried = False
        # AC11: AssetsCacheService 单实例（替代 9 处每次 new → 新 sqlite 连接）
        self._assets_cache = None
        self._assets_cache_wows_type = None  # 创建时服务器，切服后重建（分库隔离）
        self._mfm_index_cache: dict | None = None  # mfm 名(去 .mfm) -> 完整路径
        self._mfm_diffuse_cache: dict = {}       # stem -> 贴图基础名（.mfm 识别结果）
        #: 几何目录索引 {文件夹名: [VfsEntry]}，一次构建跨船复用（避免重复全树扫描）
        self._geom_folder_index: dict | None = None
        #: 挂载模型加载缓存 {文件夹名: 结果 or None}，跨船复用（多船共享同一炮塔/副炮模型）
        self._mount_model_cache: dict = {}
        #: 舰体骨架挂点变换缓存 {model_stem: {hp: matrix}}
        self._stem_mount_cache: dict = {}
        #: 骨架骨骼世界矩阵缓存 {stem: {bone_name: (4,4)}}（蒙皮 bind pose 用，跨船复用）
        self._skeleton_bones_cache: dict = {}
        #: 分段渲染集缓存 {geometry_path: [{shape, material, mfm}]}
        self._visual_rs_cache: dict = {}
        #: visual blob 一次性索引 {geometry_path: [record_index]}（避免每分段全量扫描）
        self._visual_geom_index: dict | None = None
        self._assets_self_id_index = None
        #: 材质贴图缓存 {mfm_path: (texture_path, texture_bytes)}，跨船复用
        self._material_texture_cache: dict = {}
        #: 贴图字节缓存 {base路径: bytes}（同次加载内共享图集只解压一次；
        #: INDEXED 的 albedoArray/normalArray/MGArray 是 20MB+ 共享图集，
        #: 每 mfm 现场解压 30s/个 → 多个 mfm 复用同一图集时严重卡死）
        self._texture_bytes_cache: dict = {}
        #: 材质完整信息缓存 {mfm_path: dict}（技术族 + 贴图集 + INDEXED 分块参数）
        self._material_full_cache: dict = {}
        #: 批量导出皮肤时不清上面这批按路径索引的缓存（见 keep_load_caches()）
        self._keep_load_caches: bool = False
        #: 全局渲染集索引 {murmur3(shape.vertices): {shape, material, mfm, damage}}
        #: 整合模型的高模 shape 渲染集可能在别的分段记录里，需全局扫描一次
        self._global_rs_index: dict | None = None
        #: 字符串表 {hash: name} dict（加速渲染集扫描，避免逐次哈希查表）
        self._strings_dict_cache: dict | None = None
        #: shape 名哈希表（**只从 assets_data.db 读取**）{hash: name} + 已尝试标记
        self._shape_names_cache: dict | None = None
        self._shape_names_tried = False
        #: assets_data.db 材质贴图映射缓存 {mfm_path: diffuse_base}（数据库优先，一次加载）
        self._mfm_textures_db: dict | None = None
        #: 舰船实体 JSON 快照缓存 {game_key: dict}（DB-first：只读主库 entity_snapshots）
        self._ship_snapshot_cache: dict = {}
        #: 实时骨架读取（assets.bin，独立模型查看器用）：stem → 骨架文件路径索引
        self._live_skel_index: dict | None = None
        #: 实时骨架缓存 {stem: {bone_name: (4,4)}}（assets.bin bind pose 世界矩阵）
        self._live_skel_cache: dict = {}
        #: 实时材质缓存 {mfm_path: bundle}（独立模型查看器用，**不随 load_ship 清理**）
        self._live_material_cache: dict = {}
        #: 实时贴图字节缓存 {base路径: bytes}（跨模型复用共享图集，带总量上限）
        self._live_texture_cache: dict = {}
        self._live_texture_bytes_total = 0

    #: 实时贴图缓存总量上限（512 MB；超出按插入顺序淘汰，避免看多个模型把内存吃满）。
    #: INDEXED 图集（albedoArray 20MB + normalArray 41MB…）一套材质约 140MB，
    #: 上限太小会导致来回切换模型时反复解压（单次数十秒）。
    _LIVE_TEXTURE_CACHE_LIMIT = 512 * 1024 * 1024

    @classmethod
    def instance(cls) -> "GeometryService":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ── 提取器 ──────────────────────────────────────────

    def _get_extractor(self):
        if self._extractor is None:
            from data_extractor import GameExtractor
            self._extractor = GameExtractor(
                app_ctx.ctx.game_path,
                bin_folder=app_ctx.ctx.bin_folder,
            )
        return self._extractor

    def get_extractor(self):
        """公开访问共享的客户端提取器（独立工具复用，避免重复解析 IDX）。

        只读用途（枚举 VFS / 读取单个文件），不要在调用方持有或关闭它。
        """
        return self._get_extractor()

    def _release_extractor(self):
        if self._extractor is not None:
            try:
                self._extractor.close()
            except Exception:  # noqa: BLE001
                pass
            self._extractor = None

    # ── 舰船列表 ────────────────────────────────────────

    @staticmethod
    def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
        """协作式取消检查点：取消已请求则抛出 TaskCancelled（正常结束，非错误）。

        供 load_ship 及挂载加载等长循环在批次边界调用，使后台任务能在
        关闭 3D 查看器后尽快退出，而不是继续跑完整艘船。
        """
        if cancel_event is not None and cancel_event.is_set():
            raise TaskCancelled


    # ── 舰船几何加载 ────────────────────────────────────

    def load_ship(self, ship: ShipInfo, progress_cb=None,
                  cancel_event: threading.Event | None = None,
                  model_replace: dict | None = None,
                  skin: dict | None = None,
                  partial_cb=None, lod: int = 2) -> ShipGeometry:
        """加载一艘船的船体网格 + 挂载模型 + 装甲网格 + 碰撞模型。

        model_replace：皮肤变体（origin="model"）的「原始 .model 路径 → 替换路径」映射
        （Exterior.peculiarityModels），用于把船体/挂载模型替换成皮肤变体。
        skin：Exterior 皮肤的完整数据（hull_config/nodes_config/peculiarity_models），
        用于整体替换船体模型 + 各挂载模型 + miscFilter 过滤。
        partial_cb：**分阶段交付**回调（在后台线程调用）。船体（+装甲）解析完、挂载还没开始时
        先回传一份几何**快照**，调用方可据此提前出图，不必等挂载全部解析完。
        lod：目标 LOD 级别。**默认 2**（CLI 未显式指定时走 LOD2，体积约为 LOD0 的 60%）；
        -1 = 只用 LOD0（最高模）；0..3 = 尽量用该级，该级缺失/退化的部件回退 LOD0。
        """
        try:
            return self._load_ship_impl(ship, progress_cb=progress_cb,
                                        cancel_event=cancel_event,
                                        model_replace=model_replace, skin=skin,
                                        partial_cb=partial_cb, lod=lod)
        finally:
            if not self._keep_load_caches:
                self._clear_per_load_caches()

    @contextlib.contextmanager
    def keep_load_caches(self):
        """批量导出皮肤时跨次复用已解码的贴图 / 材质信息 / 挂载模型。

        皮肤（尤其 `--model-skins`）每套都要 `load_ship` 一遍，而它们之间大部分
        贴图与挂载模型是同一份：清缓存会让 pkg 解包 + BC7 解码从头再来一遍
        （实测 ARP 大和第二次 load 14.2s → 8.4s）。这里只是**不清**这些按路径
        索引的缓存，正确性不变（键都是 vfs 路径，内容随 build 固定）。
        退出时若外层没嵌套再统一清一次，避免把重内存留在服务里。
        """
        prev = self._keep_load_caches
        self._keep_load_caches = True
        try:
            yield
        finally:
            self._keep_load_caches = prev
            if not prev:
                self._clear_per_load_caches()

    def _load_ship_impl(self, ship: ShipInfo, progress_cb=None,
                        cancel_event: threading.Event | None = None,
                        model_replace: dict | None = None,
                        skin: dict | None = None,
                        partial_cb=None, lod: int = 2) -> ShipGeometry:
        """加载一艘船的船体网格 + 挂载模型 + 装甲网格 + 碰撞模型（实现）。

        - 舰体：模型目录内全部 .geometry 分段（已处于舰船坐标系，直接合并）
        - 挂载：GameParams 各 HP_* 挂载模型 + assets.bin 骨架挂点变换定位，
          每个部件使用自己的独立贴图（以骨架信息为基准区分不同模型贴图）
        - 装甲：按归属分类标记（船体/主炮塔/副炮/防空/指挥仪/测距/雷达）

        progress_cb(pct: float, message: str) —— 0..100
        cancel_event: 协作式取消事件；在长循环边界检查，取消时抛 TaskCancelled。
        partial_cb: 见 :meth:`load_ship`（船体/装甲就绪时回传快照）
        """
        if progress_cb:
            progress_cb(2, "定位几何文件...")
        self._raise_if_cancelled(cancel_event)
        extractor = self._get_extractor()

        # 读取该舰的装甲厚度字典（A_Hull.armor + 各挂载 HP_*.armor）
        armor_thickness = self._load_armor_thickness(ship)

        # 皮肤变体（origin="model"）：把船体 model 目录替换成皮肤变体目录
        #   优先 hullConfig 的 model；其次 peculiarityModels 的船体替换；最后原 model_folder
        use_mf = ship.model_folder
        skin_nodes: dict = {}
        if skin:
            if not model_replace:
                model_replace = skin.get("peculiarity_models") or None
            for hv in (skin.get("hull_config") or {}).values():
                hv_model = hv.get("model") if isinstance(hv, dict) else (hv if isinstance(hv, str) else "")
                if hv_model:
                    use_mf_override = hv_model.rsplit("/", 2)[-2] if "/" in hv_model else ""
                    if use_mf_override:
                        use_mf = use_mf_override
                    break
            for nodes in (skin.get("nodes_config") or {}).values():
                if isinstance(nodes, dict):
                    for hp, nv in nodes.items():
                        if isinstance(nv, dict):
                            skin_nodes[hp] = nv
        if use_mf == ship.model_folder and model_replace:
            for _k, _v in model_replace.items():
                _d = _k.rsplit("/", 2)[-2] if "/" in _k else ""
                if _d and _d == ship.model_folder:
                    use_mf = _v.rsplit("/", 2)[-2] if "/" in _v else use_mf
                    break

        # 舰体分段：模型目录内全部 .geometry（已处于舰船坐标系）
        entries = self._geometry_folder_index(extractor).get(use_mf) or []
        if not entries:
            # 回退：按 model_folder 全树匹配任意路径下的 geometry
            pattern = f"content/**/{use_mf}/{use_mf}*.geometry"
            try:
                entries = [
                    e for e in extractor.list_files([pattern])
                    if not e.is_directory
                ]
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"列出几何文件失败: {exc}") from exc

        if not entries:
            raise RuntimeError(f"未找到 {use_mf} 的 .geometry 文件")

        # 去重（相同路径+大小只加载一次）
        seen = set()
        uniq = []
        for e in sorted(entries, key=lambda x: x.path):
            key = (e.path, e.file_info.size)
            if key not in seen:
                seen.add(key)
                uniq.append(e)

        # 跳过「不带分段后缀」的主文件（如 JSB039_Yamato_1945.geometry）：
        # 多为 base/聚合文件，含烟囱等多余模型；真正高模在 *_Bow/_MidBack/... 分段。
        # 但**主文件可能含船体装甲**（CM_PA_*.armor，如 Yamato 12487 tris）→ 单独保留。
        main_entries = []
        seg_uniq = []
        for e in uniq:
            stem = e.path.rsplit('/', 1)[-1][:-len('.geometry')]
            if stem == use_mf:
                main_entries.append(e)
                continue
            seg_uniq.append(e)
        if seg_uniq:
            uniq = seg_uniq
        elif uniq:
            # 只有主文件（无分段模型）：明确报错提示，而非静默用主文件回退
            raise RuntimeError(
                f"未找到 {use_mf} 的分段模型文件"
                f"（仅存在主文件 {use_mf}.geometry）")

        # 该船全部渲染集索引（含整合模型：高模 shape 渲染集可能在别的分段记录）
        ship_rs = self._ship_render_sets(use_mf)

        geom = ShipGeometry(
            game_key=ship.game_key,
            display_name=ship.display_name,
            model_folder=use_mf,
        )

        # 蒙皮 bind pose 骨架 stem（舰船模型目录；_apply_skinning 用它查骨骼矩阵）
        self._current_skinning_stem = use_mf

        bmin = np.full(3, np.inf, dtype=np.float32)
        bmax = np.full(3, -np.inf, dtype=np.float32)
        total_parts = len(uniq)

        for idx, e in enumerate(uniq):
            self._raise_if_cancelled(cancel_event)
            if progress_cb:
                progress_cb(5 + 80 * idx / total_parts, f"解析 {e.path.rsplit('/', 1)[-1]}")
            try:
                data = extractor.pkg_reader.read_file(e.volume.filename, e.file_info)
            except Exception as exc:  # noqa: BLE001
                geom.stats.setdefault("warnings", []).append(f"{e.path}: 读取失败 {exc}")
                continue
            try:
                parsed = parse_geometry(data, file_path=e.path, cancel_event=cancel_event)
            except GeometryError as exc:
                geom.stats.setdefault("warnings", []).append(f"{e.path}: {exc}")
                continue
            finally:
                del data

            part_name = e.path.rsplit("/", 1)[-1].rsplit(".", 1)[0]

            # 合并该部件的全部 primitive → 按材质拆分的 HullMesh（不同模型不同贴图）
            if parsed.primitives:
                # ship_rs 含 LOD/crack 的 damage 标记，但部分船的 LOD/crack 渲染集
                # 不在数据库（如 ST 服 Yamato MidFront 无 LOD1/LOD3 visual 记录）——
                # 用 shape_names 表（populate 时写入 DB）按名兜底跳过 _lod/_crack_；
                # ★ 只从数据库读取，绝不现场读 assets.bin
                sdict = self._shape_names_sdict()
                groups = self._split_primitives_by_material(parsed.primitives, ship_rs, sdict,
                                                            geom_path=e.path, instancing=True,
                                                            lod=lod)
                if groups:
                    for mat_key, g in groups.items():
                        if not g.get('prims'):
                            continue
                        inst = list(g.get('instance_matrices') or [])
                        # __inst__# 组：真实材质在 g['material']，带逐节点实例矩阵
                        if isinstance(mat_key, str) and mat_key.startswith('__inst__#'):
                            real = g.get('material')
                            base = part_name if real is None else f"{part_name}#{real}"
                        else:
                            real = mat_key
                            base = part_name if mat_key is None else f"{part_name}#{mat_key}"
                        # 同一材质的蒙皮分片可能用不同骨骼调色板 → 必须拆成多个网格，
                        # 否则合并后顶点组索引按别人的调色板翻译（权重绑错骨骼）。
                        subs = self._palette_subgroups(g['prims'])
                        for si, sub in enumerate(subs, 1):
                            nm = base if len(subs) == 1 else f"{base}#bones{si}"
                            hm = self._merge_hull(nm, sub,
                                                  shape_names=g.get('shape_names', []),
                                                  node_names=g.get('node_names', []),
                                                  instance_matrices=inst)
                            if real is not None:
                                hm.material = real
                            if mesh_filters.is_wire_material(mat_key):
                                hm.is_wire = True
                            if mesh_filters.is_crack_material(mat_key):
                                hm.is_crack = True
                            if real is not None:
                                # 完整材质信息：技术族 + 贴图集 + INDEXED 分块参数（fx 区分渲染）
                                # ★ 只认 mfm 声明的贴图属性；未声明颜色贴图 → 无色（不按名补全）
                                bundle = self._resolve_material_full(g.get('mfm') or '', extractor)
                                self._apply_declared_color(hm, bundle, extractor)
                            geom.hull_meshes.append(hm)
                            if hm.positions.size:
                                if inst:
                                    # 实例化网格：包围盒按各个节点矩阵求并集
                                    for _m in inst:
                                        mn, mx = self._instanced_bounds(hm.positions, _m)
                                        bmin = np.minimum(bmin, mn)
                                        bmax = np.maximum(bmax, mx)
                                else:
                                    pmin = hm.positions.min(axis=0)
                                    pmax = hm.positions.max(axis=0)
                                    bmin = np.minimum(bmin, pmin)
                                    bmax = np.maximum(bmax, pmax)
                else:
                    subs = self._palette_subgroups(parsed.primitives)
                    for si, sub in enumerate(subs, 1):
                        hm = self._merge_hull(
                            part_name if len(subs) == 1 else f"{part_name}#bones{si}", sub)
                        geom.hull_meshes.append(hm)
                        if hm.positions.size:
                            pmin = hm.positions.min(axis=0)
                            pmax = hm.positions.max(axis=0)
                            bmin = np.minimum(bmin, pmin)
                            bmax = np.maximum(bmax, pmax)

            # 装甲网格（舰体装甲 → 归属"船体"，已在舰船坐标系）
            for am in parsed.armor_models:
                mesh = self._build_armor_mesh(am, armor_thickness, component=COMPONENT_HULL)
                if mesh is not None:
                    geom.armor_meshes.append(mesh)
                    if mesh.positions.size:
                        pmin = mesh.positions.min(axis=0)
                        pmax = mesh.positions.max(axis=0)
                        bmin = np.minimum(bmin, pmin)
                        bmax = np.maximum(bmax, pmax)

            # 碰撞模型
            for cm in parsed.collision_models:
                geom.collision_models.append(CollisionModelData(cm.name, cm.size_in_bytes, cm.data))

            geom.stats[f"parts"] = len(uniq)
            geom.stats[f"prim_{idx}"] = {
                "file": part_name,
                "vertices": sum(p.positions.shape[0] for p in parsed.primitives),
                "triangles": sum(0 if p.indices is None else p.indices.size // 3 for p in parsed.primitives),
                "armor_tris": sum(len(a.triangles) for a in parsed.armor_models),
            }

        if np.isfinite(bmin).all():
            geom.bounds_min = bmin
            geom.bounds_max = bmax

        # 提取船体漫反射贴图（尽力而为：单贴图应用到全部舰体分段）
        self._raise_if_cancelled(cancel_event)
        try:
            tex_path, tex_bytes = self._find_hull_diffuse(ship, extractor)
            if tex_bytes:
                geom.texture_dds = tex_bytes
                geom.texture_path = tex_path
        except Exception:  # noqa: BLE001
            pass

        # ── 主文件装甲（几何已跳过渲染，但船体装甲 CM_PA_*.armor 在此） ──
        for e in main_entries:
            try:
                data = extractor.pkg_reader.read_file(e.volume.filename, e.file_info)
            except Exception as exc:  # noqa: BLE001
                geom.stats.setdefault("warnings", []).append(f"{e.path}: 装甲读取失败 {exc}")
                continue
            try:
                parsed = parse_geometry(data, file_path=e.path, cancel_event=cancel_event)
            except GeometryError as exc:  # noqa: BLE001
                geom.stats.setdefault("warnings", []).append(f"{e.path}: 装甲解析失败 {exc}")
                continue
            finally:
                del data
            for am in parsed.armor_models:
                mesh = self._build_armor_mesh(am, armor_thickness, component=COMPONENT_HULL)
                if mesh is not None:
                    geom.armor_meshes.append(mesh)
                    if mesh.positions.size:
                        pmin = mesh.positions.min(axis=0)
                        pmax = mesh.positions.max(axis=0)
                        bmin = np.minimum(bmin, pmin)
                        bmax = np.maximum(bmax, pmax)

        # ── 分阶段交付：船体 + 装甲已就绪，挂载还没开始 ──
        # 大船（中途岛那类）挂载阶段还要十几秒；先把「船体/装甲」交给调用方提前显示。
        if partial_cb is not None:
            try:
                partial_cb(self._snapshot_without_mounts(geom))
            except Exception:  # noqa: BLE001 - 提前交付失败不影响完整加载
                pass

        # ── 挂载模型（骨架定位 + 每个部件独立贴图） ──
        self._raise_if_cancelled(cancel_event)
        self._load_mounts(geom, ship, extractor, armor_thickness, progress_cb,
                          cancel_event=cancel_event, model_replace=model_replace,
                          skin_nodes=skin_nodes, lod=lod)

        if progress_cb:
            progress_cb(100, "加载完成")
        return geom

    # ── 挂载模型 ────────────────────────────────────────

    def _emit_instanced_mount(self, geom: ShipGeometry, src: dict, folder: str,
                              comp: str, group_items: list,
                              bmin: np.ndarray, bmax: np.ndarray):
        """为一个 (folder, comp) 组创建**实例化**挂载网格（一份几何 + 各节点矩阵）。

        同一模型目录被多个挂载节点引用（如 MP_... / MP_..._INDEX2、多座同型炮塔）时，
        不再为每个节点复制一份几何（N×顶点内存爆炸）；而是只创建每个材质分片一份网格，
        把各节点的渲染空间矩阵存入 instance_matrices，渲染时逐节点各画一次。
        group_items: [(label, mtx, armor_mtx), ...]。
        """
        for hm in src["meshes"]:
            mats = [(_armor_mtx if hm.skinned_applied else _mtx)
                    for (_label, _mtx, _armor_mtx) in group_items]
            mm = MountMesh(
                name=folder, component=comp,
                positions=hm.positions, normals=hm.normals,
                uvs=hm.uvs, indices=hm.indices,
                uv2=hm.uv2, tangents=hm.tangents, binormals=hm.binormals,
                t4=hm.t4,
                extra=hm.extra,
                model_matrix=None,
                instance_matrices=[np.ascontiguousarray(m, dtype=np.float32)
                                   for m in mats],
                texture_dds=hm.texture_dds, texture_path=hm.texture_path,
                model_folder=folder, vertex_count=hm.vertex_count,
                is_wire=hm.is_wire, is_crack=hm.is_crack,
                tech_family=hm.tech_family,
                material_textures=hm.material_textures or {},
                indexed_params=hm.indexed_params,
                has_color=hm.has_color,
                bone_indices=hm.bone_indices, bone_weights=hm.bone_weights,
                skin_bones=hm.skin_bones, skin_bind=hm.skin_bind,
                shape_names=hm.shape_names, node_names=hm.node_names,
                shape_spans=hm.shape_spans,
                instance_labels=[str(_label) for (_label, _mtx, _amtx) in group_items],
            )
            geom.mounts.append(mm)
            if mm.positions.size:
                for _m in mm.instance_matrices:
                    mn, mx = self._instanced_bounds(mm.positions, _m)
                    bmin = np.minimum(bmin, mn)
                    bmax = np.maximum(bmax, mx)
        return bmin, bmax

    def _load_mounts(self, geom: ShipGeometry, ship: ShipInfo, extractor,
                     armor_thickness: dict, progress_cb=None,
                     cancel_event: threading.Event | None = None,
                     model_replace: dict | None = None,
                     skin_nodes: dict | None = None, lod: int = -1):
        """加载 GameParams 各 HP_* 挂载模型并按骨架挂点定位。

        同一模型目录（如 JGM178 炮塔）只解析一次几何，按每个挂点实例化；
        每个挂载使用该模型自己的贴图（`{stem}_a.dd0` 命名约定）。
        """
        refs = self._load_mount_refs(
            ship, warnings=geom.stats.setdefault("warnings", []), skin_nodes=skin_nodes)
        transforms = self._load_mount_transforms(ship)
        if not refs and not transforms:
            geom.stats["mounts"] = 0
            return
        if not transforms:
            geom.stats["mounts"] = 0
            geom.stats.setdefault("warnings", []).append(
                "未找到 assets.bin 挂点骨架，挂载模型未定位（舰体仍正常显示）")
            return

        bmin = geom.bounds_min
        bmax = geom.bounds_max
        if bmin is None or bmax is None:
            bmin = np.full(3, np.inf, dtype=np.float32)
            bmax = np.full(3, -np.inf, dtype=np.float32)

        model_cache: dict = {}   # model_folder -> 加载结果 or None
        hp_groups: dict = {}     # (folder, comp) -> [(label, mtx, armor_mtx), ...]
        placed = 0
        sub_placed = 0   # 挂载模型骨架上的 MP 子设备（炮塔测距仪/炮上防空炮等）
        _negz = np.diag([1.0, 1.0, -1.0, 1.0])   # 几何(左手系)→渲染(右手系) 共轭
        n_refs = len(refs)
        for idx, (hp, comp, model_path, misc_filter, custom_battle) in enumerate(refs):
            self._raise_if_cancelled(cancel_event)
            if model_replace:
                model_path = model_replace.get(model_path, model_path)
            # 挂载加载阶段同样报进度（66%→99%），避免进度条停在分段解析末尾
            if progress_cb:
                progress_cb(66 + 33 * idx / max(1, n_refs), f"加载挂载 {hp}")
            folder = self._folder_from_model_path(model_path)
            if not folder:
                continue
            m_raw = transforms.get(hp)
            if m_raw is None:
                # 引用小零件（如 HP_JGM_2_HP_JGA_1 主炮塔上防空炮）：
                # 父挂点(舰船) × 父模型骨架内子挂点(bind pose 世界)
                m_raw = self._child_mount_transform(hp, refs, transforms)
            if m_raw is None:
                # 兜底：继承父挂点矩阵（HP_JGM_2），随炮塔定位
                m_raw = self._derived_mount_transform(hp, transforms)
            if m_raw is None:
                continue
            # 基于原始骨骼组匹配：几何空间 = HP 挂点 × 模型 Root_BlendBone(静息朝向)，
            # 再 negz 共轭转渲染空间（等价于顶点几何 Z 取反）
            rb = self._mount_root_blend(folder)
            mtx = np.ascontiguousarray(_negz @ (m_raw @ rb) @ _negz, dtype=np.float32)
            # ★ 装甲几何在制作时已与挂点对齐（wows-toolkit ship.rs：armor 用
            # 原始 hp_transform，不做 Root_BlendBone 旋转修正）；若套用 mtx 会
            # 多转一次 rb → 主炮塔装甲方向反转。装甲单独用原始挂点矩阵。
            armor_mtx = np.ascontiguousarray(_negz @ m_raw @ _negz, dtype=np.float32)
            if folder not in model_cache:
                model_cache[folder] = self._load_mount_model(
                    folder, extractor, armor_thickness, comp, cancel_event=cancel_event,
                    lod=lod)
            src = model_cache[folder]
            if src is None:
                continue

            # ★ 不逐节点复制几何：把该节点的 (label, mtx, armor_mtx) 累积到分组，
            #   循环后按 (folder, comp) 一次性实例化（一份几何 + 各节点矩阵）。
            hp_groups.setdefault((folder, comp), []).append((hp, mtx, armor_mtx))

            # 该模型的装甲（本地坐标 + 原始挂点矩阵定位 + 归属分类）
            for am in src["armor_meshes"]:
                mesh = ArmorMesh(
                    name=am.name, positions=am.positions, normals=am.normals,
                    colors=am.colors, indices=am.indices, triangles=am.triangles,
                    component=comp, model_matrix=armor_mtx,
                )
                geom.armor_meshes.append(mesh)
                if mesh.positions.size:
                    mn, mx = mesh.bounds_in_world()
                    bmin = np.minimum(bmin, mn)
                    bmax = np.maximum(bmax, mx)
            placed += 1

            # 该挂载模型骨架上的 MP 子设备（炮塔测距仪/炮管防空炮/弹药箱等，
            # 全库约 1% MP 父节点非 Scene Root）。骨架空间→船 = m_raw。
            # miscFilter 白名单：**恒生效**（空列表 = 空白名单 = 不显示任何
            # MP 子设备），对齐游戏 MiscsController；customMiscs.battle 预设
            # 按 miscName 兜底显示。同一模型在不同炮位显示不同挂件（PGSB106）。
            mp_allow = frozenset(misc_filter)
            battle_customs = frozenset(custom_battle)
            sub_n, bmin, bmax = self._place_skeleton_mps(
                geom, folder, m_raw, model_cache, extractor, armor_thickness,
                comp, bmin, bmax, 0, frozenset(), cancel_event=cancel_event,
                mp_allow=mp_allow, battle_customs=battle_customs)
            sub_placed += sub_n

        # ★ 一次性实例化 HP 挂载（同模型多节点 → 一份几何 + 各节点矩阵）
        for (folder, comp), items in hp_groups.items():
            src = model_cache.get(folder)
            if src is None:
                continue
            bmin, bmax = self._emit_instanced_mount(geom, src, folder, comp,
                                                    items, bmin, bmax)

        # ── MP 甲板设备挂载（缆桩/小艇/探照灯/救生筏等 misc 模型）──
        # MP 节点不在 GameParams，由 populate 纳入 skeleton_mounts（v5）；
        # 模型目录由命名约定推导：MP_{baseID}_... → misc/{baseID}/{baseID}.geometry
        mp_placed = 0
        mp_items = [(n, m) for n, m in transforms.items() if n.startswith("MP_")]
        mp_groups: dict = {}   # folder -> [(label, mtx, armor_mtx), ...]
        n_mp = len(mp_items)
        for idx, (mp_name, m_raw) in enumerate(mp_items):
            self._raise_if_cancelled(cancel_event)
            if progress_cb:
                progress_cb(66 + 33 * (n_refs + idx) / max(1, n_refs + n_mp),
                            f"加载甲板设备 {mp_name}")
            folder = self._mp_model_folder(mp_name)
            if not folder:
                continue
            rb = self._mount_root_blend(folder)
            mtx = np.ascontiguousarray(_negz @ (m_raw @ rb) @ _negz, dtype=np.float32)
            # 装甲用原始挂点矩阵（同 HP 挂载：不做 Root_BlendBone 修正）
            armor_mtx = np.ascontiguousarray(_negz @ m_raw @ _negz, dtype=np.float32)
            if folder not in model_cache:
                model_cache[folder] = self._load_mount_model(
                    folder, extractor, armor_thickness, COMPONENT_DECK,
                    cancel_event=cancel_event, lod=lod)
            src = model_cache[folder]
            if src is None:
                continue
            # ★ 不逐节点复制几何：累积到分组，循环后一次性实例化
            mp_groups.setdefault(folder, []).append((mp_name, mtx, armor_mtx))
            for am in src["armor_meshes"]:
                mesh = ArmorMesh(
                    name=am.name, positions=am.positions, normals=am.normals,
                    colors=am.colors, indices=am.indices, triangles=am.triangles,
                    component=COMPONENT_DECK, model_matrix=armor_mtx,
                )
                geom.armor_meshes.append(mesh)
                if mesh.positions.size:
                    mn, mx = mesh.bounds_in_world()
                    bmin = np.minimum(bmin, mn)
                    bmax = np.maximum(bmax, mx)
            mp_placed += 1
            # 甲板设备模型自身骨架上的 MP 子设备（递归，防环）
            sub_n, bmin, bmax = self._place_skeleton_mps(
                geom, folder, m_raw, model_cache, extractor, armor_thickness,
                COMPONENT_DECK, bmin, bmax, 0, frozenset(), cancel_event=cancel_event)
            sub_placed += sub_n

        # ★ 一次性实例化 MP 甲板设备（同模型多节点 → 一份几何 + 各节点矩阵）
        for folder, items in mp_groups.items():
            src = model_cache.get(folder)
            if src is None:
                continue
            bmin, bmax = self._emit_instanced_mount(geom, src, folder, COMPONENT_DECK,
                                                    items, bmin, bmax)

        # ★ 统一收敛：把所有节点类型（含挂载模型骨架上的 MP 子设备）中共享同一
        #   几何的 MountMesh 合并为实例化网格（一份几何 + 各节点矩阵），省 GPU 内存。
        self._dedupe_mount_instances(geom)

        if np.isfinite(bmin).all():
            geom.bounds_min = bmin
            geom.bounds_max = bmax
        geom.stats["mounts"] = placed
        geom.stats["deck_equipment"] = mp_placed
        geom.stats["sub_equipment"] = sub_placed
        geom.stats["unique_mount_models"] = len(model_cache)

    def _skeleton_bone_world(self, folder: str, bone_name: str) -> np.ndarray | None:
        """读挂载模型骨架某 bone 的世界矩阵（bind pose，**只从 assets_data.db 读取**）。

        按 folder 缓存整棵骨架的世界矩阵表。"""
        key = f"skelworld:{folder}"
        if key not in self._mount_model_cache:
            world: dict = {}
            try:
                c = self._get_assets_cache()
                world = c.get_skeleton_bones(folder)
            except Exception:  # noqa: BLE001
                world = {}
            self._mount_model_cache[key] = world
        return self._mount_model_cache[key].get(bone_name)

    def _child_mount_transform(self, hp: str, refs: list, transforms: dict) -> np.ndarray | None:
        """引用小零件（如 `HP_JGM_2_HP_JGA_1` 主炮塔上防空炮）的精确挂点：

        = 父挂点矩阵(舰船骨架 HP_JGM_2) × 父模型骨架内子挂点(HP_JGA_1) 的 bind pose 世界矩阵。
        无法解析（找不到父模型/子挂点）返回 None（调用方回退 _derived_mount_transform）。
        """
        if "_HP_" not in hp:
            return None
        parent, child = hp.split("_HP_", 1)
        child = "HP_" + child
        pm = transforms.get(parent)
        if pm is None:
            return None
        parent_folder = None
        for _hp, _comp, _mp, *_ in refs:
            if _hp == parent:
                parent_folder = self._folder_from_model_path(_mp)
                break
        if not parent_folder:
            return None
        child_w = self._skeleton_bone_world(parent_folder, child)
        if child_w is None:
            return None
        return pm @ child_w

    def _ship_snapshot(self, ship: ShipInfo) -> dict:
        """舰船实体 JSON（DB-first：只读主库 entity_snapshots，绝不读 data/split）。

        快照在「加载数据」入库时写入；按 game_key 缓存（同船多次查询复用）。
        数据库无记录返回 {}（装甲厚度/挂载引用为空，舰体仍正常显示）。
        """
        cached = self._ship_snapshot_cache.get(ship.game_key)
        if cached is not None:
            return cached
        data: dict = {}
        try:
            from services.database_service import get_db
            db = get_db(app_ctx.ctx.wows_type)
            if db.exists:
                data = db.load_ship_snapshot(ship.game_key) or {}
        except Exception:  # noqa: BLE001
            data = {}
        self._ship_snapshot_cache[ship.game_key] = data
        return data

    def _load_mount_refs(self, ship: ShipInfo,
                         warnings: list | None = None,
                         skin_nodes: dict | None = None) -> list[tuple[str, str, str, list, list]]:
        """从舰船实体快照收集所有 HP_ 挂载引用：
        [(hp_name, component, model_path, misc_filter, custom_battle)]。

        遍历全部组件分组（不依赖固定键列表），按 base 组件名归类；
        未知组件若含 HP_ + model 挂载引用，记录到 warnings（可见诊断而非静默丢失）。

        misc_filter：该挂点的 `miscFilter` 白名单（MP 节点**全名**；**空列表 =
        空白名单 = 不显示任何**骨架 MP 子设备，对齐游戏 MiscsController）。
        custom_battle：`customMiscs.battle` 预设的 misc 名集合（按 miscName 兜底显示）。
        """
        out: list[tuple[str, str, str, list, list]] = []
        data = self._ship_snapshot(ship)
        if not data:
            return out
        for key, category, group in iter_component_groups(data):
            has_mount = False
            for hp, v in group.items():
                if not hp.startswith("HP_"):
                    continue
                if not isinstance(v, dict):
                    continue
                model = v.get("model")
                if not model:
                    continue
                has_mount = True
                mf = v.get("miscFilter") or []
                if not isinstance(mf, list):
                    mf = []
                cm = v.get("customMiscs") or {}
                if not isinstance(cm, dict):
                    cm = {}
                cb = cm.get("battle") or []
                if not isinstance(cb, list):
                    cb = []
                # 皮肤 nodesConfig 覆盖：替换挂载 model / miscFilter / customMiscs
                if skin_nodes and hp in skin_nodes:
                    ov = skin_nodes[hp]
                    if ov.get("model"):
                        model = ov["model"]
                    if ov.get("miscFilter"):
                        mf = ov["miscFilter"]
                        if not isinstance(mf, list):
                            mf = []
                    if ov.get("customMiscs"):
                        cm = ov["customMiscs"]
                        if not isinstance(cm, dict):
                            cm = {}
                        cb = cm.get("battle") or []
                        if not isinstance(cb, list):
                            cb = []
                out.append((hp, category, str(model),
                            [str(x) for x in mf if isinstance(x, str)],
                            [str(x) for x in cb if isinstance(x, str)]))
            if has_mount and not is_known_component_key(key):
                if warnings is not None:
                    warnings.append(
                        f"组件 {key} 含挂载引用但未被识别，已按「其他」归属加载")
        return out

    # ── assets.bin 骨架挂点 ─────────────────────────────

    def load_assets_bytes(self) -> bytes | None:
        """现场从当前客户端 .pkg 读取 assets.bin 原始字节（3D 查看器骨架挂点权威来源）。

        只使用**当前加载客户端**的 assets.bin，绝不用别的客户端/来源不明的缓存：
        **全内存流转** —— 不读任何既有解包产物（如 data/assets.bin），也不把产物写盘
        供下次复用。失败返回 None（调用方按「无 assets 数据」降级）。
        """
        if not app_ctx.ctx.game_path:
            return None
        try:
            ext = self._get_extractor()
            candidates = [
                e for e in ext.list_files(["content/assets.bin"]) if not e.is_directory
            ]
            if not candidates:
                return None
            entry = candidates[0]
            return ext.pkg_reader.read_file(entry.volume.filename, entry.file_info) or None
        except Exception:  # noqa: BLE001
            return None


    def _get_assets_cache(self):
        """AC11: AssetsCacheService 单实例惰性缓存（替代 9 处每次 new 新 sqlite 连接）。

        按服务器分库：切服（Lesta↔WG）后缓存失效重建，避免读到上一服务器的 assets_data.db。
        """
        from app.application import app as app_ctx
        cur = app_ctx.ctx.wows_type
        if self._assets_cache is None or self._assets_cache_wows_type != cur:
            from services.assets_cache_service import AssetsCacheService
            self._assets_cache = AssetsCacheService(wows_type=cur)
            self._assets_cache_wows_type = cur
        return self._assets_cache


    def _mount_root_blend(self, folder: str) -> np.ndarray:
        """挂载模型骨架的 `Root_BlendBone` 矩阵（静息朝向修正，仅用于**视觉**网格）。

        从 assets_data.db 该模型的 SkeletonPrototype 读取；找不到回退恒等。按 folder 缓存。
        实测值因模型而异（大和主炮塔 JGM178 = diag(1,1,-1,1) Z 镜像，自逆）。
        ⚠️ 装甲几何不使用此修正（装甲已与挂点对齐，见 armor_mtx）。
        """
        key = f"rblend:{folder}"
        cached = self._mount_model_cache.get(key)
        if cached is not None:
            return cached
        # 回退**恒等**：找不到 Root_BlendBone 的模型（多数防空/火控，骨架只有
        # 2-4 bone）几何已含自身朝向，不施加任何补偿（用户要求删除 R180 回退，
        # 否则这些模型整体翻转）
        rb = np.eye(4, dtype=np.float32)
        try:
            c = self._get_assets_cache()
            bones = c.get_skeleton_bones(folder)
            if bones and "Root_BlendBone" in bones:
                rb = bones["Root_BlendBone"]
        except Exception as exc:  # noqa: BLE001
            bus.log_message.emit(f"⚠️ Root_BlendBone 读取失败({folder}): {exc}，已回退恒等矩阵")
        self._mount_model_cache[key] = rb
        return rb

    def _skeleton_bones_all(self, folder: str) -> dict:
        """某模型骨架的**全部骨骼世界矩阵**表（bind pose，只读 assets_data.db）。

        与 `_skeleton_bone_world` 共用 `skelworld:{folder}` 缓存；返回
        {bone_name: (4,4)}（含 MP_/HP_ 挂点节点，供子设备递归放置用）。
        """
        key = f"skelworld:{folder}"
        cached = self._mount_model_cache.get(key)
        if cached is not None:
            return cached
        world: dict = {}
        try:
            world = self._get_assets_cache().get_skeleton_bones(folder)
        except Exception:  # noqa: BLE001
            world = {}
        self._mount_model_cache[key] = world
        return world


    def _mp_model_folder(self, mp_name: str) -> str:
        """MP_ 节点 → 模型目录：WG 用完整 miscName（如 AM003_Fairlead_1），Lesta 用 baseID（AM003）。

        WG 的甲板设备模型目录按 misc 全名命名（wows-toolkit misc_name_from_node：
        去 MP_ 前缀 + 去实例后缀），Lesta 按 baseID（首个字母+数字 token）命名；
        用错则 `misc/{folder}/{folder}.geometry` 找不到 → 甲板设备不显示。
        """
        if app_ctx.ctx.wows_type == "Wargaming":
            return self._misc_name_from_node(mp_name)
        return mp_base_id(mp_name)

    def _place_skeleton_mps(self, geom, folder: str, skel_to_ship_game: np.ndarray,
                            model_cache: dict, extractor, armor_thickness: dict,
                            component: str, bmin: np.ndarray, bmax: np.ndarray,
                            depth: int, visited: frozenset,
                            cancel_event: threading.Event | None = None,
                            mp_allow: frozenset | None = None,
                            battle_customs: frozenset | None = None) \
            -> tuple[int, np.ndarray, np.ndarray]:
        """递归放置挂载模型骨架上的 MP 子设备（炮塔测距仪/炮管防空炮/弹药箱等）。

        全库约 1% 的 MP 节点父节点不是 Scene Root，而是挂在炮塔/火控等挂载模型
        自己的骨架里（`Rotate_Y`/`Roll_Back1` 等骨骼下）。这些子设备此前被遗漏。

        参数：
          folder: 当前挂载模型目录（读其骨架的 MP_ 节点）
          skel_to_ship_game: 当前模型**骨架空间 → 舰船游戏空间**矩阵（未 negz 共轭）。
              挂载模型骨架→船 = 其 HP 挂点矩阵 m_raw（几何→骨架 = Root_BlendBone 已约去）。
          component: 归属分类（继承父挂载，如主炮塔；甲板设备为 COMPONENT_DECK）
          visited: 递归路径上已出现的模型目录（防环）
          mp_allow: 当前层 MP 节点白名单（来自挂点 miscFilter；None=不过滤
              （甲板设备/递归子设备），空集合=空白名单=不显示任何节点）。
          battle_customs: customMiscs.battle 预设的 misc 名集合（按 miscName 兜底）。

        变换链（与舰体级 MP 同一规律：几何→自身骨架 = Root_BlendBone）：
          子设备骨架→船 = skel_to_ship_game @ W_sub（MP 节点世界矩阵）
          子设备几何→船（视觉）= 上式 @ rb_sub，再 negz 共轭转渲染空间。
          子设备装甲 = 上式（不乘 rb_sub）再 negz 共轭（装甲已与挂点对齐）。
        返回 (放置数, bmin, bmax)。
        """
        if depth > 3 or folder in visited:
            return 0, bmin, bmax
        bones = self._skeleton_bones_all(folder)
        mp_nodes = {k: v for k, v in bones.items() if k.startswith("MP_")}
        if not mp_nodes:
            return 0, bmin, bmax
        _negz = np.diag([1.0, 1.0, -1.0, 1.0])
        placed = 0
        visited_next = visited | {folder}
        for mp_name, W in mp_nodes.items():
            self._raise_if_cancelled(cancel_event)
            # miscFilter 白名单过滤：仅放置白名单内节点（空白名单 = 不放置任何），
            # 或 miscName 命中 customMiscs.battle 预设的兜底显示
            if mp_allow is not None and mp_name not in mp_allow \
                    and self._misc_name_from_node(mp_name) not in (battle_customs or ()):
                continue
            sub_folder = self._mp_model_folder(mp_name)
            if not sub_folder or sub_folder in visited_next:
                continue
            sub_skel_to_ship = skel_to_ship_game @ W
            rb_sub = self._mount_root_blend(sub_folder)
            mtx = np.ascontiguousarray(
                _negz @ (sub_skel_to_ship @ rb_sub) @ _negz, dtype=np.float32)
            # 装甲用原始变换（不做 Root_BlendBone 修正，同 HP 挂载）
            armor_mtx = np.ascontiguousarray(
                _negz @ sub_skel_to_ship @ _negz, dtype=np.float32)
            if sub_folder not in model_cache:
                model_cache[sub_folder] = self._load_mount_model(
                    sub_folder, extractor, armor_thickness, component,
                    cancel_event=cancel_event)
            src = model_cache[sub_folder]
            if src is None:
                continue
            for hm in src["meshes"]:
                mm = MountMesh(
                    name=mp_name, component=component,
                    positions=hm.positions, normals=hm.normals,
                    uvs=hm.uvs, indices=hm.indices,
                    uv2=hm.uv2, tangents=hm.tangents, binormals=hm.binormals,
                    t4=hm.t4,
                    extra=hm.extra,
                    model_matrix=(armor_mtx if hm.skinned_applied else mtx),
                    texture_dds=hm.texture_dds, texture_path=hm.texture_path,
                    model_folder=sub_folder, vertex_count=hm.vertex_count,
                    is_wire=hm.is_wire,
                    is_crack=hm.is_crack,
                    has_color=hm.has_color,
                    bone_indices=hm.bone_indices, bone_weights=hm.bone_weights,
                    skin_bones=hm.skin_bones, skin_bind=hm.skin_bind,
                    shape_names=hm.shape_names, node_names=hm.node_names,
                    shape_spans=hm.shape_spans,
                    instance_labels=[mp_name],
                )
                geom.mounts.append(mm)
                if mm.positions.size:
                    mn, mx = mm.bounds_in_world()
                    bmin = np.minimum(bmin, mn)
                    bmax = np.maximum(bmax, mx)
            for am in src["armor_meshes"]:
                mesh = ArmorMesh(
                    name=am.name, positions=am.positions, normals=am.normals,
                    colors=am.colors, indices=am.indices, triangles=am.triangles,
                    component=component, model_matrix=armor_mtx,
                )
                geom.armor_meshes.append(mesh)
                if mesh.positions.size:
                    mn, mx = mesh.bounds_in_world()
                    bmin = np.minimum(bmin, mn)
                    bmax = np.maximum(bmax, mx)
            placed += 1
            # 子设备自身骨架可能还有 MP（递归，防环）
            sub_n, bmin, bmax = self._place_skeleton_mps(
                geom, sub_folder, sub_skel_to_ship, model_cache, extractor,
                armor_thickness, component, bmin, bmax, depth + 1, visited_next,
                cancel_event=cancel_event)
            placed += sub_n
        return placed, bmin, bmax

    def _load_mount_transforms(self, ship: ShipInfo) -> dict:
        """收集舰体骨架 HP_ 挂点矩阵（**只从 assets_data.db 读取**）。

        返回 {hp_name: (4,4) 行主序渲染空间矩阵}；数据库无数据返回 {}（不读 assets.bin）。
        按 model stem 缓存（姊妹舰/共享船体复用）。
        """
        stem = ship.model_folder
        cached = self._stem_mount_cache.get(stem)
        if cached is not None:
            return cached
        out: dict[str, np.ndarray] = {}
        try:
            c = self._get_assets_cache()
            out = c.get_skeleton_mounts(stem)
        except Exception:  # noqa: BLE001
            out = {}
        self._stem_mount_cache[stem] = out
        return out

    # ── 挂载模型几何/贴图加载 ───────────────────────────


    def _geometry_folder_index(self, extractor) -> dict:
        """懒构建：{模型目录名: [VfsEntry .geometry]}，一次构建跨船复用。

        `list_files` 对每个模式全树 fnmatch（数万条目×多次）极慢，这里改用
        一次性目录索引做 O(1) 查找。排除 LOD 子目录与 `_lodN` 后缀文件。
        """
        if self._geom_folder_index is not None:
            return self._geom_folder_index
        idx: dict = {}
        try:
            for path, entry in extractor.file_tree.items():
                if entry.is_directory or not path.endswith(".geometry"):
                    continue
                # 排除 LOD 子目录与损伤/残骸变体几何（主模型目录常含 *_dead/_broken.geometry，
                # 不过滤会被当独立网格重复渲染 → 同一挂点出现多个炮塔）
                fname = path.rsplit("/", 1)[-1]
                if "/lods/" in path or "_lod" in fname:
                    continue
                if any(x in fname for x in ("_dead", "_broken", "_destroyed", "_burn")):
                    continue
                parts = path.split("/")
                if len(parts) < 2:
                    continue
                folder = parts[-2]
                idx.setdefault(folder, []).append(entry)
        except Exception as exc:  # noqa: BLE001
            bus.log_message.emit(f"⚠️ 几何目录索引构建失败: {exc}")
        self._geom_folder_index = idx
        return idx

    def _load_mount_model(self, folder: str, extractor, armor_thickness: dict,
                          component: str,
                          cancel_event: threading.Event | None = None,
                          lod: int = -1) -> dict | None:
        """加载一个挂载模型目录的几何+贴图+装甲（本地坐标）。失败返回 None。

        使用目录索引 O(1) 查找；结果按 folder 缓存（跨船复用，多船共享炮塔等）。
        返回 {meshes: [HullMesh...], texture_dds, texture_path, armor_meshes}
        - **按材质拆分成多个 mesh**（每材质分片独立贴图）：挂载模型常含多个
          材质（如雷达 Turret=PBS + Net=GRID、指挥仪玻璃、天线网），合并成单
          网格单贴图会让其他分片 UV 错位 → 贴图错误。
        """
        cached = self._mount_model_cache.get(folder)
        if cached is not None:
            return cached
        entries = self._geometry_folder_index(extractor).get(folder) or []
        if not entries:
            bus.log_message.emit(f"⚠️ 挂载模型 {folder}: 未找到几何文件，该挂载不显示")
            self._mount_model_cache[folder] = None
            return None
        # 蒙皮 bind pose 骨架 stem（舰船模型目录；_apply_skinning 用它查骨骼矩阵）
        self._current_skinning_stem = folder

        all_armor = []
        meshes: list = []
        # 挂载模型也按船体逻辑：渲染集（visual→mfm→贴图）+ LOD/低模/wire 兜底跳过
        mount_rs = self._ship_render_sets(folder)
        # mount_rs 含 damage 标记，但部分 LOD/crack 渲染集不在数据库时用
        # shape_names 表（DB）按名兜底跳过；★ 只从数据库读取，绝不现场读 assets.bin
        sdict = self._shape_names_sdict()
        main_tex = ("", b"")
        first_geom_path = entries[0].path
        for e in entries:
            self._raise_if_cancelled(cancel_event)
            try:
                data = extractor.pkg_reader.read_file(e.volume.filename, e.file_info)
            except Exception as exc:  # noqa: BLE001
                bus.log_message.emit(f"⚠️ 挂载模型读取失败 {e.path}: {exc}")
                continue
            try:
                parsed = parse_geometry(data, file_path=e.path, cancel_event=cancel_event)
            except GeometryError as exc:  # noqa: BLE001
                bus.log_message.emit(f"⚠️ 挂载模型解析失败 {e.path}: {exc}")
                continue
            finally:
                del data
            all_armor.extend(parsed.armor_models)
            if parsed.primitives:
                groups = self._split_primitives_by_material(parsed.primitives, mount_rs, sdict,
                                                            geom_path=e.path, lod=lod)
                for mat, g in groups.items():
                    # 保留所有分组（含 None 组：shape 无渲染集材质时用默认/文件名约定贴图，
                    # 如指挥仪 ControlTowerShape —— 否则指挥仪/雷达等会整组丢失不显示）
                    if not g.get('prims'):
                        continue
                    mfm = g.get('mfm') or ''
                    # 同一材质的蒙皮分片可能用不同骨骼调色板 → 拆成多个网格，
                    # 保证每个网格的顶点组索引只对应自己那份调色板。
                    subs = self._palette_subgroups(g['prims'])
                    for si, sub in enumerate(subs, 1):
                        base = f"{folder}|{mat or 'default'}"
                        hm = self._merge_hull(base if len(subs) == 1 else f"{base}#bones{si}",
                                              sub,
                                              shape_names=g.get('shape_names', []),
                                              node_names=g.get('node_names', []),
                                              instance_matrices=g.get('instance_matrices') or [])
                        hm.material = mat
                        hm.is_wire = mesh_filters.is_wire_material(mat)
                        hm.is_crack = bool(g.get('damage'))
                        hm.skinned_applied = bool(g.get('skinned_applied'))
                        if mfm:
                            # ★ 只认 mfm 声明的贴图属性（indexed→albedoArray，否则 diffuseMap）；
                            #   未声明颜色贴图 → has_color=False（无色，不按名补全 diffuse）。
                            #   INDEXED 数组不变，继续按老样子参与渲染。
                            _bundle = self._resolve_material_full(mfm, extractor)
                            self._apply_declared_color(hm, _bundle, extractor)
                        else:
                            # 无 mfm 的默认组：取模型自身 mfm 声明的 diffuseMap（只认声明）
                            tex_path, tex_bytes = self._find_model_diffuse(
                                first_geom_path, folder, extractor)
                            hm.texture_path, hm.texture_dds = tex_path, tex_bytes
                            hm.has_color = bool(tex_bytes)
                        meshes.append(hm)
                        if not main_tex[1] and hm.texture_dds:
                            main_tex = (hm.texture_path, hm.texture_dds)

        if not meshes and not all_armor:
            bus.log_message.emit(f"⚠️ 挂载模型 {folder}: 解析后无网格/装甲，该挂载不显示")
            self._mount_model_cache[folder] = None
            return None

        armor_meshes = []
        for am in all_armor:
            mesh = self._build_armor_mesh(am, armor_thickness, component=component)
            if mesh is not None:
                armor_meshes.append(mesh)
        result = {
            "meshes": meshes,
            "texture_dds": main_tex[1], "texture_path": main_tex[0],
            "armor_meshes": armor_meshes,
        }
        self._mount_model_cache[folder] = result
        return result


    def _mfm_diffuse_base(self, stem: str, prefer: tuple[str, ...] = ()) -> str:
        """基于 .mfm 识别贴图（**只从 assets_data.db 读取**）：diffuseMap 原始路径。

        数据由「加载数据」时预提取入库（assets_cache_service.populate）。
        按 stem 前缀匹配材质（排除 _wire/_dead/_blaze 变体），优先 prefer 精确名；
        找不到返回 ""（调用方回退到文件名约定）。
        """
        cached = self._mfm_diffuse_cache.get(stem)
        if cached is not None:
            return cached
        result = ""
        try:
            if self._mfm_textures_db is None:
                c = self._get_assets_cache()
                self._mfm_textures_db = c.get_mfm_textures() or {}
            if self._mfm_textures_db:
                cands = []
                for mfm_path, tex_path in self._mfm_textures_db.items():
                    if not tex_path:
                        continue
                    name = mfm_path.rsplit("/", 1)[-1][:-4]
                    # ★ 只认精确 mfm 名：不再 `startswith(stem + "_")` 前缀补全，
                    #   避免把一个变体(如 _decal_tech/_wire/_alpha)声明的 diffuse 串到主材质。
                    if name == stem or name in prefer:
                        if any(x in name for x in ("_wire", "_dead", "_blaze", "_alpha")):
                            continue
                        # 库存原始路径（含扩展名），渲染分级仍用基础名
                        base = tex_path[:-4] if tex_path.endswith(".dds") else tex_path
                        cands.append((name, base))
                if cands:
                    def _key(item):
                        name, _b = item
                        for i, pref in enumerate(prefer):
                            if name == pref:
                                return (i, 0)
                        return (len(prefer), 1)
                    cands.sort(key=_key)
                    result = cands[0][1]
        except Exception as exc:  # noqa: BLE001
            result = ""
            bus.log_message.emit(f"⚠️ mfm 贴图识别失败({stem}): {exc}")
        self._mfm_diffuse_cache[stem] = result
        return result

    def _load_texture_tier(self, base: str, extractor) -> tuple[str, bytes]:
        """按 .dd0/.dd1/.dd2/.dds 分级读取贴图（.dd0 最高清），返回 (vfs_path, bytes)。

        ⚠️ 按 base 路径缓存解压字节：共享图集（CIT000_1k_ship_tiles_*.dds 等 20MB+）
        被大量 INDEXED 材质复用，若不缓存则每个 mfm 都重新 Kraken 解压（~30s/个）
        导致加载卡死。缓存只跨同一次 load_ship（_clear_per_load_caches 清理）。
        """
        if base in self._texture_bytes_cache:
            return base + ".dds", self._texture_bytes_cache[base]
        for tier in (".dd0", ".dd1", ".dd2", ".dds"):
            cand = base + tier
            data = GeometryService._read_vfs(extractor, cand)
            if data and data[:4] == b"DDS ":
                # 校验可解析出 mip/层（bc7prep 的 .dd0 若未正确解码会得到空 mip，
                # 上传后是空纹理 → 采样 (0,0,0,1) 黑不透明 → 全黑），回退下一层级
                try:
                    from services.texture_io import parse_dds
                    d = parse_dds(data)
                    if not (d.mips or d.layers):
                        continue
                except Exception:
                    continue
                # 只缓存非空解压结果（共享图集多为 .dds；.dd0 大文件也缓存）
                if len(data) > 256 * 1024:
                    self._texture_bytes_cache[base] = data
                return cand, data
        return "", b""

    def _find_model_diffuse(self, geometry_path: str, folder: str, extractor) -> tuple[str, bytes]:
        """挂载模型贴图：**只认 .mfm 声明**的 diffuseMap（不再按文件名约定补全）。"""
        # 1) .mfm 识别：{folder}.mfm / {folder}_skinned.mfm → diffuseMap → _a 基础名
        base = self._mfm_diffuse_base(folder, prefer=(folder, f"{folder}_skinned"))
        if base:
            path, data = self._load_texture_tier(base, extractor)
            if data:
                return path, data
        # ★ 不再按文件名约定补全（_a.dd0/_Hull_camo 等）：只认 mfm 声明的 diffuseMap，
        #   缺失就当不存在。下面走渲染集 mfm 兜底（同型号变体共享材质）仍是只认声明。
        # 3) 渲染集 mfm 兜底：同型号变体共享材质（如 JGA181→JGA010、JGA020 无自有时）
        try:
            seen_mfm: set = set()
            mfms: list = []
            for r in self._ship_render_sets(folder).values():
                m = r.get("mfm") or ""
                if not m or m in seen_mfm:
                    continue
                seen_mfm.add(m)
                mfms.append(m)
            # 优先与当前 folder 型号 token 匹配的 mfm：TurretShape 等通用 shape 名的
            # 渲染集只存在于**同型号主炮/主模型**记录里（如 AGS542_5in54_Mark42 副炮
            # 无独立 TurretShape 渲染集，共享 AGM127_5in54_Mark42 主炮的贴图；
            # 若按记录顺序取第一个 mfm 会串到别的型号 → 副炮贴图错误）
            tok = self._model_family_token(folder)
            if tok:
                mfms.sort(key=lambda m: 0 if tok in m else 1)
            for m in mfms:
                p2, d2 = self._resolve_material_texture(m, extractor)
                if d2:
                    return p2, d2
        except Exception:  # noqa: BLE001
            pass
        return "", b""

    def _find_hull_diffuse(self, ship: ShipInfo, extractor) -> tuple[str, bytes]:
        """舰体贴图：**只认 .mfm 声明**的 diffuseMap（Hull.mfm 权威，不再按文件名约定补全）。

        颜色贴图以 `_a` 后缀为准，优先 `.dd0`（4096×4096 高清 DDS）。
        """
        # 1) .mfm 识别：{stem}_Hull.mfm / {stem}.mfm → diffuseMap（只认声明）
        stem = ship.model_folder
        base = self._mfm_diffuse_base(stem, prefer=(f"{stem}_Hull", stem))
        if base:
            path, data = self._load_texture_tier(base, extractor)
            if data:
                return path, data
        # ★ 不再按文件名约定补全（_Hull_a.dd0/_Hull_camo 等）：只认 mfm 声明的 diffuseMap。
        return "", b""

    #: shape 名里的 LOD 标记。两种命名并存：
    #: `Bow_lod2Shape`（数字在前）与 `JM039_lodShape1`（数字在后）
    def _split_primitives_by_material(self, primitives, global_rs: dict,
                                      sdict: dict | None = None,
                                      geom_path: str | None = None,
                                      instancing: bool = False,
                                      lod: int = -1) -> dict:
        """按渲染集索引把 primitives 分组：{material_key: {material, mfm, prims}}。

        - 用 murmur3(shape.vertices) == primitive.mapping_id 连接渲染集与几何
        - **damage/crack 渲染集**（断面、`*_in` 盖板等）归 `__crack__` 组并标
          is_crack：查看器不渲染，GLB 导出默认剔除（`--damage` 才导出）
        - LOD：`lod<0` 只用 LOD0；`lod>=0` 尽量用该级，部件没有该级时回退 LOD0
        - 匹配到材质的按材质分组；未匹配的归 None（用舰体默认贴图）
        - sdict 兜底：无渲染集匹配的 primitive 用字符串表反查 shape 名，
          LOD 低模跳过；`_crack_`/Crack 名归 `__crack__` 组
        - 多个 shape 同材质（如 Hull 本体 + DeckHouse）自动合并
        """
        # 目标 LOD 过滤：名字里的 `_lodN` 决定级别（`Bow_lod2Shape` → 2，`BowShape` → 0）。
        # 级别表取自 render_sets（DB），不依赖 shape_names 表——后者按 bin_folder 存储，
        # 换版本后可能查不到（会导致 LOD 参数失效）。
        levels_by_base: dict = {}
        if lod >= 0:
            for _rs in global_rs.values():
                _base, _lv = GeometryService._shape_lod(_rs.get('shape') or '')
                if _base:
                    levels_by_base.setdefault(_base, set()).add(_lv)

        mat_by_mid: dict = {}
        damage_mids: set = set()
        alt_mat_by_mid: dict = {}  # 目标 LOD 变体的材质（它们被 DB 标 damage，不在 mat_by_mid 里）
        norm_to_rs: dict = {}   # 归一化 shape stem → 渲染集（模糊兜底：处理视觉/几何笔误）
        rs_by_mid: dict = {}    # 任意一个 entry（按 mid 兜底）
        rs_by_gp_mid: dict = {} # (geom_path, mid) → 分段精确（刚性绑定用）
        for key, rs in global_rs.items():
            mid = key[1] if isinstance(key, tuple) else key
            _mm = (rs.get('material') or '', rs.get('mfm') or '')
            mat_by_mid[mid] = _mm
            if lod >= 0 and GeometryService._shape_lod(rs.get('shape') or '')[1] == lod:
                alt_mat_by_mid[mid] = _mm
            rs_by_mid.setdefault(mid, rs)
            if isinstance(key, tuple):
                rs_by_gp_mid[key] = rs
            if rs.get('damage'):
                damage_mids.add(mid)
                continue
            nk = GeometryService._norm_shape_stem(rs.get('shape') or '')
            if nk and nk not in norm_to_rs:
                norm_to_rs[nk] = rs

        def _rs_for(mid: int):
            """优先当前几何分段的渲染集条目（保留各自骨骼节点），否则任一兜底。"""
            if geom_path:
                rs = rs_by_gp_mid.get((geom_path, mid))
                if rs is not None:
                    return rs
            return rs_by_mid.get(mid)

        # 每个 (基名, LOD 级) 的三角数：用于「最小面数保护」。某些船的低级 LOD 会把
        # 船首/船尾压成 2 个三角形的占位块（Waterloo Bow/Stern_lod2Shape），
        # 目标级面数远低于 LOD0 时回退 LOD0，否则正面会直接看穿船壳。
        tri_by_base_lod: dict = {}
        if lod >= 0:
            _tri_by_mid: dict = {}
            for _p in primitives:
                _n = 0 if _p.indices is None else int(_p.indices.size) // 3
                _tri_by_mid[_p.mapping_id] = _tri_by_mid.get(_p.mapping_id, 0) + _n
            for _mid, _n in _tri_by_mid.items():
                _nm = (sdict or {}).get(_mid) or ((_rs_for(_mid) or {}).get("shape") or "")
                _base, _lv = GeometryService._shape_lod(_nm)
                _k = (_base, _lv)
                tri_by_base_lod[_k] = tri_by_base_lod.get(_k, 0) + _n

        def _lod_skip(name: str) -> bool:
            # 判据统一在 services/mesh_filters.py（LOD 级别 + 面数保护 + 辅助几何豁免）
            return mesh_filters.should_skip_lod(name, lod, levels_by_base,
                                                tri_by_base_lod,
                                                GeometryService._shape_lod)

        def _annotate(p) -> None:
            """写入该 primitive 的 .visual 形状名与绑定节点名（GLB 导出命名用）。

            只在这里做一次：后续所有分支（蒙皮/实例化/克隆）都直接读这两个字段。
            """
            if getattr(p, "shape_name", ""):
                return
            if sdict is not None:
                p.shape_name = GeometryService._norm_shape_stem(
                    sdict.get(p.mapping_id) or "")
            rs = _rs_for(p.mapping_id) or {}
            nodes = rs.get('nodes') or []
            if nodes:
                p.node_name = str(nodes[0])

        groups: dict = {}

        def _new_group(mat: str, mfm: str, damage: bool = False) -> dict:
            return {'material': mat, 'mfm': mfm, 'prims': [],
                    'shape_names': [], 'node_names': [], 'damage': damage}

        def _crack_group(mat: str, mfm: str) -> dict:
            """damage/crack 网格组（按材质分键，避免多种 crack 材质混入同一网格）。"""
            key = f"__crack__#{mat}" if mat else "__crack__"
            return groups.setdefault(key, _new_group(mat, mfm, damage=True))

        def _add_names(g: dict, p, rs=None) -> None:
            """收集该 primitive 的形状名（sdict 反查）与**全部**骨骼节点名到分组，供调试标签用。"""
            if sdict is not None:
                _sn = sdict.get(p.mapping_id) or ''
                if _sn and _sn not in g['shape_names']:
                    g['shape_names'].append(_sn)
            nodes = (rs or {}).get('nodes') or []
            for _nn in nodes:
                if _nn and _nn not in g['node_names']:
                    g['node_names'].append(_nn)

        for p in primitives:
            _annotate(p)
            _rs0 = _rs_for(p.mapping_id) or {}
            _sname0 = _rs0.get('shape') or ''
            if _lod_skip(_sname0):
                continue
            # DB 把 LOD 变体也标 damage=1；按名字/材质区分真 crack 与普通 LOD 变体
            if p.mapping_id in damage_mids:
                _mat0, _mfm0 = mat_by_mid.get(p.mapping_id) or ('', '')
                # 名字取不到时按旧行为当 damage 处理（宁可少导不可混入断面）
                if not _sname0 or '_crack_' in _sname0 or 'Crack' in _mat0:
                    # 损伤网格：真 crack（含其 LOD 变体）进 __crack__ 组，导出时剔除
                    _crack_group(_mat0, _mfm0)['prims'].append(p)
                    continue
            entry = mat_by_mid.get(p.mapping_id) or alt_mat_by_mid.get(p.mapping_id)
            if entry is None:
                # 无渲染集匹配：字符串表反查 shape 名，LOD 低模跳过；crack 名保留
                if sdict is not None:
                    _name = sdict.get(p.mapping_id) or ''
                    if _lod_skip(_name):
                        continue
                    if '_crack_' in _name or 'Crack' in _name:
                        _crack_group('', '')['prims'].append(p)
                        continue
                    # 模糊兜底：归一化 shape 名匹配渲染集（游戏数据笔误，
                    # 如 JGA180 视觉写 TurretShapeff.vertices、几何是 TurretShape.vertices）
                    if _name:
                        fuz = norm_to_rs.get(GeometryService._norm_shape_stem(_name))
                        if fuz is not None:
                            mat = fuz.get('material') or ''
                            mfm = fuz.get('mfm') or ''
                            g = groups.setdefault(mat, _new_group(mat, mfm))
                            applied = self._apply_skinning(p, fuz)
                            if applied:
                                g['prims'].append(p)
                                g['skinned_applied'] = True
                                _add_names(g, p, fuz)
                            elif instancing:
                                bake, instmats = self._rigid_instance_matrices(p, fuz)
                                if instmats:
                                    ig = groups.setdefault(
                                        f"__inst__#{mat}#{p.mapping_id}",
                                        _new_group(mat, mfm))
                                    ig['prims'].append(p)
                                    ig['instance_matrices'] = instmats
                                    ig['skinned_applied'] = True
                                    _add_names(ig, p, fuz)
                                elif bake is not None:
                                    self._bake_rigid(p, bake)
                                    g['prims'].append(p)
                                    g['skinned_applied'] = True
                                    _add_names(g, p, fuz)
                                else:
                                    g['prims'].append(p)
                                    _add_names(g, p, fuz)
                            else:
                                clones = self._apply_rigid_multi(p, fuz)
                                if clones:
                                    g['prims'].extend(clones)
                                    g['skinned_applied'] = True
                                else:
                                    g['prims'].append(p)
                                _add_names(g, p, fuz)
                            continue
                groups.setdefault(None, _new_group(None, ''))['prims'].append(p)
            else:
                mat, mfm = entry
                rs = _rs_for(p.mapping_id)
                applied = self._apply_skinning(p, rs)
                g = groups.setdefault(mat, _new_group(mat, mfm))
                if applied:
                    g['prims'].append(p)
                    g['skinned_applied'] = True
                else:
                    if instancing:
                        # 只存各节点坐标：网格一份原始几何，渲染时逐节点各画一次
                        bake, instmats = self._rigid_instance_matrices(p, rs)
                        if instmats:
                            ig = groups.setdefault(
                                f"__inst__#{mat}#{p.mapping_id}", _new_group(mat, mfm))
                            ig['prims'].append(p)
                            ig['instance_matrices'] = instmats
                            ig['skinned_applied'] = True
                            _add_names(ig, p, rs)
                        elif bake is not None:
                            self._bake_rigid(p, bake)
                            g['prims'].append(p)
                            g['skinned_applied'] = True
                            _add_names(g, p, rs)
                        else:
                            g['prims'].append(p)
                            _add_names(g, p, rs)
                    else:
                        # 非蒙皮：按绑定节点实例化（同一 shape 可绑多节点 → 左右舷多实例）
                        clones = self._apply_rigid_multi(p, rs)
                        if clones:
                            g['prims'].extend(clones)
                            g['skinned_applied'] = True
                        else:
                            g['prims'].append(p)
                        _add_names(g, p, rs)
        return groups

    def _strings_dict(self, db) -> dict:
        """字符串表 → {hash: name} Python dict（一次构建，O(1) 查询）。

        assets.bin 的 StringsSection 是哈希表（offsetsMap），get_string_by_id 每次
        线性探测较慢；构建全局渲染集索引时调用数百万次，改为先转成 Python dict。
        """
        if self._strings_dict_cache is not None:
            return self._strings_dict_cache
        from utils.asset_utils import build_strings_dict
        self._strings_dict_cache = build_strings_dict(db)
        return self._strings_dict_cache

    def _shape_names_sdict(self) -> dict:
        """渲染 shape 名哈希表 {hash32: name}（**只从 assets_data.db 读取**）。

        populate 时把 assets.bin 字符串表里 *.vertices 的名字哈希写入 shape_names 表；
        显示时 _split_primitives_by_material 用它按名跳过缺失渲染集的 LOD/crack 低模。
        数据库缺失/异常返回空 dict（= 不兜底），**绝不现场读 assets.bin**。
        """
        if self._shape_names_tried:
            return self._shape_names_cache or {}
        self._shape_names_tried = True
        out: dict = {}
        try:
            c = self._get_assets_cache()
            out = c.get_shape_names() or {}
        except Exception as exc:  # noqa: BLE001
            out = {}
            bus.log_message.emit(f"⚠️ shape_names 数据读取失败: {exc}")
        self._shape_names_cache = out
        return out


    def _ship_render_sets(self, model_folder: str) -> dict:
        """该船全部记录的渲染集索引（**只从 assets_data.db 读取**）。

        {murmur3(shape.vertices): {shape, material, mfm, damage, skinned, nodes}}。
        数据由「加载数据」时预提取入库（render_sets 表，含整合模型共享记录）；
        数据库缺失时返回空（不再从任何 assets.bin 现场扫描）。
        """
        key = f"ship_rs:{model_folder}"
        cached = self._visual_rs_cache.get(key)
        if cached is not None:
            return cached
        idx: dict = {}
        try:
            c = self._get_assets_cache()
            ext = self._get_extractor()
            entries = self._geometry_folder_index(ext).get(model_folder) or []
            if entries:
                paths = [e.path for e in entries]
                rows = c.get_render_sets(paths)
                for r in rows:
                    mid = murmur3_32(r["shape"].encode())
                    mat, mfm, damage = r["material"], r["mfm"], r["damage"]
                    entry = {'shape': r["shape"], 'material': mat, 'mfm': mfm,
                             'damage': damage, 'skinned': r.get('skinned', False),
                             'nodes': r.get('nodes') or []}
                    # ★ 按 (geom_path, mid) 索引：同一 shape 名可能出现在多个几何分段
                    #   且各自绑定不同骨骼节点（如 BIA454_Bollard_bigShape 在 MidBack 绑
                    #   _6、MidFront 绑 _0），必须保留各分段的节点，否则只渲染一个。
                    gp = r.get('geom_path') or ''
                    key = (gp, mid)
                    if key in idx:
                        # 跨模型同名 shape：优先当前模型自己的 mfm
                        if mfm and model_folder in mfm:
                            idx[key] = entry
                        continue
                    idx[key] = entry
        except Exception as exc:  # noqa: BLE001
            idx = {}
            bus.log_message.emit(f"⚠️ 渲染集数据读取失败({model_folder}): {exc}，已回退默认贴图")
        self._visual_rs_cache[key] = idx
        return idx

    def _resolve_material_texture(self, mfm_path: str, extractor) -> tuple[str, bytes]:
        """材质 .mfm 声明的 diffuseMap 贴图（只认 mfm 里声明的属性；未声明 → 不存在）。

        绝不再按 mfm 文件名/前缀自动补全（旧版 _mfm_diffuse_base 前缀匹配会把
        无颜色贴花/变体的 diffuse 串到别的材质，indexed 也受影响）。找不到返回 ("", b"")。
        """
        if not mfm_path or not mfm_path.endswith('.mfm'):
            return "", b""
        cached = self._material_texture_cache.get(mfm_path)
        if cached is not None:
            return cached
        result = ("", b"")
        try:
            bundle = self._resolve_material_full(mfm_path, extractor)
            result = (bundle.get('textures') or {}).get('diffuseMap', ("", b""))
        except Exception:  # noqa: BLE001
            result = ("", b"")
        self._material_texture_cache[mfm_path] = result
        return result

    # ── 材质技术族 + INDEXED 分块渲染 ───────────────────


    def _resolve_material_full(self, mfm_path: str, extractor) -> dict:
        """完整解析材质渲染信息：技术族 + fx + 贴图集 + INDEXED 参数。

        - shader_id / material_hash / fx_path / textures / indexed：来自
          assets_data.db 的 material_full 表（「加载数据」时预提取入库；fx_path
          由 uncode_assets.service.resolve_fx_path 从 material_hash 解析）
        - tech_family：由 fx_path 判定；fx_path 缺失时才退回 shader_id 高 16 位族
        - textures: {贴图键: (vfs_path, bytes)}（diffuseMap/albedoArray/materialIdMap/artMap/...）
        - indexed_params: {arrays: {名: (N,4)f32}, grid:(rows,cols), offset:(x,y)}
        """
        if not mfm_path or not mfm_path.endswith(".mfm"):
            return {}
        cached = self._material_full_cache.get(mfm_path)
        if cached is not None:
            return cached
        result: dict = {}
        try:
            c = self._get_assets_cache()
            info = c.get_material_full(mfm_path)
            if info:
                sid = info.get("shader_id") or "0x0"
                mh = info.get("material_hash") or ""
                # fx 路径入库时由 uncode_assets.service.resolve_fx_path 解析（material_hash = fx 资源
                # selfId），随客户端更新；不依赖任何固定映射表。
                fx_path = info.get("fx_path") or ""
                family = fx_tech_family(fx_path, sid)
                # 贴图属性 → 实时从客户端 pkg 解包字节（.dds/.dd0 分级）
                textures: dict = {}
                for name, vp in (info.get("textures") or {}).items():
                    if not vp:
                        continue
                    base = vp[:-4] if vp.endswith(".dds") else vp
                    tp, td = self._load_texture_tier(base, extractor)
                    if td:
                        textures[name] = (tp, td)
                result = {"tech_family": family, "shader_id": sid,
                          "material_hash": mh, "fx_path": fx_path,
                          "textures": textures}
                # INDEXED 分块参数（material_full.indexed 已含 vec4 数组）
                indexed = info.get("indexed") or {}
                if indexed:
                    try:
                        arrs = {k: np.array(v, dtype=np.float32) for k, v in indexed.items()}
                        # 洋红占位 artMap = 缺失/无 camo：把艺术涂装 BaseStrength 置 0，
                        # 否则占位洋红会被满强度混入 → 雷达/炮塔粉红（CIA000_instances_atlas 实测）。
                        art_tx = textures.get("artMap")
                        if art_tx and _is_magenta_placeholder(art_tx[1]):
                            a_arr = arrs.get("artStrengthMatIdArr")
                            if a_arr is not None and a_arr.ndim == 2 and a_arr.shape[1] >= 1:
                                a_arr[:, 0] = 0.0
                        params: dict = {"arrays": arrs}
                        off = arrs.get("offsetScaleMatIdArr")
                        if off is not None and len(off):
                            params["offset"] = (float(off[0, 0]), float(off[0, 1]))
                            params["grid"] = (int(round(off[0, 2])), int(round(off[0, 3])))
                        result["indexed_params"] = params
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as exc:  # noqa: BLE001
            result = {}
            bus.log_message.emit(f"⚠️ 材质数据读取失败({mfm_path}): {exc}")
        self._material_full_cache[mfm_path] = result
        return result

    # ── 实时材质（assets.bin，独立模型文件查看器）──────────


    def _skeleton_bones(self, stem: str) -> dict:
        """某模型骨架全部骨骼世界矩阵 {bone_name: (4,4)}（DB 读取，跨船缓存）。"""
        cached = self._skeleton_bones_cache.get(stem)
        if cached is not None:
            return cached
        out: dict = {}
        try:
            c = self._get_assets_cache()
            out = c.get_skeleton_bones(stem) or {}
        except Exception:  # noqa: BLE001
            out = {}
        self._skeleton_bones_cache[stem] = out
        return out

    def _apply_skinning(self, prim, rs: dict | None) -> bool:
        """对蒙皮图元施加 bind pose 混合（原地修改 positions/normals）。

        返回是否**实际变换了顶点**（True → Root_BlendBone 已烘焙进几何，
        挂载矩阵不得再乘 rb，否则双重镜像导致朝向翻转）。

        背景（PASA111 实证）：Korabli 的蒙皮网格（如 Bow_Antenna / Bow_Antenna_wire，
        材质 *_skinned）顶点存于 **BlendBone 局部帧**，渲染集调色板节点（*_BlendBone /
        Root_BlendBone）的 bind pose 世界矩阵多为 **原点 Z 镜像 diag(1,1,-1)**（det=-1）。
        游戏引擎按权重混合这些矩阵把顶点摆到舰船空间；我们此前直接渲染原始坐标，
        导致 Z 镜像骨骼的网格整体前后（艏艉）翻转 —— 即用户报告的 180° 朝向错误。

        修正：按渲染集调色板 + 顶点骨骼索引/权重做标准线性混合蒙皮：
            p' = Σ_j w_j · (R_j · p + t_j)
        顶点组索引 = 调色板 slot × 3（Korabli 实测）；iiiww 的 8B 布局为
        ``u8 idx[3] + 2B pad + u8 w[2] + 1B pad``：i0↔w0、i1↔w1，
        第三个顶点组权重 = 1-w0-w1（解析层已转换为 4 列）。

        - 调色板缺失 / 骨架缺失 / 全部骨骼为恒等 → 不修改（保持原样，安全回退）。
        - 法线用旋转部分（忽略平移）混合后归一化。
        - 反射骨骼（det=-1）会翻转三角形绕序，但渲染器不启用背面剔除，无碍。
        """
        if prim is None or not prim.is_skinned:
            return False
        if prim.bone_indices is None or prim.bone_weights is None:
            return False
        if rs is None or not rs.get('skinned'):
            return False
        palette = rs.get('nodes') or []
        if not palette:
            return False
        # 骨架 stem：渲染集 shape 名 → 模型目录（如 Bow_AntennaShape → ASA031_...）
        # 用当前舰船 model_folder 的骨架（调色板节点名在舰船骨架里）
        stem = getattr(self, '_current_skinning_stem', None)
        if not stem:
            return False
        bones = self._skeleton_bones(stem)
        if not bones:
            return False
        # 收集调色板骨骼世界矩阵（缺失的跳过）
        mats: list[np.ndarray | None] = []
        any_non_identity = False
        for nm in palette:
            m = bones.get(nm)
            mats.append(m)
            if m is not None and not np.allclose(m, np.eye(4), atol=1e-4):
                any_non_identity = True
        if not any_non_identity:
            # 骨骼全为恒等：无需变换顶点，但仍保留 skin 数据（供 GLB 导出 skin）
            prim.skin_bones = list(palette)
            prim.skin_bind = mats
            return False

        idx = prim.bone_indices  # (N,4) uint8
        wts = prim.bone_weights  # (N,4) float32
        pos = prim.positions
        nrm = prim.normals
        n = pos.shape[0]
        # 顶点索引 → 调色板 slot（Korabli：slot × 3）
        slots = (idx.astype(np.int32) // 3)
        # 越界 slot 置 -1（权重归零，不参与混合）
        valid = slots < len(mats)
        slots = np.where(valid, slots, -1)

        new_pos = np.zeros_like(pos, dtype=np.float64)
        new_nrm = np.zeros_like(nrm, dtype=np.float64)
        for j in range(4):
            s = slots[:, j]
            w = wts[:, j]
            for slot in np.unique(s):
                if slot < 0 or slot >= len(mats):
                    continue
                m = mats[slot]
                if m is None:
                    continue
                mask = s == slot
                if not mask.any():
                    continue
                R = m[:3, :3].astype(np.float64)
                t = m[:3, 3].astype(np.float64)
                wm = w[mask, None]
                new_pos[mask] += wm * ((pos[mask].astype(np.float64) @ R.T) + t)
                new_nrm[mask] += wm * (nrm[mask].astype(np.float64) @ R.T)
        prim.positions = new_pos.astype(np.float32)
        ln = np.linalg.norm(new_nrm, axis=1, keepdims=True)
        ln[ln == 0] = 1.0
        prim.normals = (new_nrm / ln).astype(np.float32)
        # 保留蒙皮调色板与 bind 世界矩阵（GLB 导出 skin 用；bones 保持原样）
        prim.skin_bones = list(palette)
        prim.skin_bind = mats
        return True


    def _apply_rigid_multi(self, prim, rs: dict | None) -> list:
        """对**非蒙皮但刚性绑定**的子模型：按每个绑定节点烘焙一份顶点，返回列表。

        同一 shape 可绑定多个骨骼节点（左右舷实例化，如 BIA400_Motor_Cutter_25ftShape
        在 `_0`/`_1` 两节点各一份）。每节点烘焙一份 → `_merge_hull` 合并为一网格，
        从而在两个位置各渲染一个实例。
        - 单节点：原地烘焙（保留既有行为），返回 [prim]。
        - 多节点：每节点克隆一份并烘焙，返回克隆列表（不改原 prim）。
        - 无法定位（无骨骼/全恒等/蒙皮）：返回 []（调用方追加原始 prim）。
        """
        if prim is None or rs is None:
            return []
        if rs.get('skinned'):
            return []
        nodes = rs.get('nodes') or []
        if not nodes:
            return []
        stem = getattr(self, '_current_skinning_stem', None)
        if not stem:
            return []
        bones = self._skeleton_bones(stem)
        if not bones:
            return []
        mats: list[tuple[str, object]] = []
        for nd in nodes:
            m = bones.get(nd)
            if m is None:
                continue
            if np.allclose(m, np.eye(4), atol=1e-4):
                continue
            mats.append((nd, m))
        if not mats:
            return []
        if len(mats) == 1:
            self._bake_rigid(prim, mats[0][1])
            return [prim]
        out = []
        for _nd, m in mats:
            clone = self._clone_prim(prim)
            self._bake_rigid(clone, m)
            out.append(clone)
        return out

    def _rigid_instance_matrices(self, prim, rs: dict | None):
        """刚性绑定：返回 (bake_matrix, instance_matrices)。

        只存各骨架节点坐标，不复制顶点——网格只含一份原始几何，渲染时按每个节点
        矩阵各画一次（左右舷/多处实例）。比逐节点烘焙复制顶点更省内存。

        返回：
          (None, None)                  —— 不可刚性绑定（蒙皮/无节点/无骨骼/全恒等），
                                            调用方保留原始几何（原样放入网格）。
          (M_game, [])                  —— 恰一个有效节点：调用方用 M_game 原地烘焙
                                            （既有单节点行为）。
          (None, [M_render_i, ...])     —— 多节点：调用方保留原始几何，并把 M_render_i
                                            写入网格 instance_matrices；渲染时逐实例绘制。
        M_render = MirrorZ @ M_game @ MirrorZ（游戏空间 → 渲染空间模型矩阵）。
        """
        if prim is None or rs is None or rs.get('skinned'):
            return None, None
        nodes = rs.get('nodes') or []
        if not nodes:
            return None, None
        stem = getattr(self, '_current_skinning_stem', None)
        if not stem:
            return None, None
        bones = self._skeleton_bones(stem)
        if not bones:
            return None, None
        negz = np.diag([1.0, 1.0, -1.0, 1.0]).astype(np.float32)
        game_mats: list[np.ndarray] = []
        for nd in nodes:
            m = bones.get(nd)
            if m is None:
                continue
            if np.allclose(m, np.eye(4), atol=1e-4):
                continue
            game_mats.append(np.asarray(m, dtype=np.float32))
        if not game_mats:
            return None, None
        if len(game_mats) == 1:
            return game_mats[0], []
        inst = [(negz @ m @ negz).astype(np.float32) for m in game_mats]
        return None, inst

    # ── 装甲厚度字典 ────────────────────────────────────

    def _load_armor_thickness(self, ship: ShipInfo) -> dict:
        """读取 A_Hull.armor + 炮塔/副炮 armor，构建 {(model_index, material_id): mm}。

        DB-first：数据源为主库 entity_snapshots 舰船快照（加载数据时入库），
        不读 data/split JSON。
        """
        out: dict[tuple[int, int], float] = {}
        data = self._ship_snapshot(ship)
        if not data:
            return out

        def _collect(d):
            if not isinstance(d, dict):
                return
            armor = d.get("armor")
            if isinstance(armor, dict):
                for k, v in armor.items():
                    try:
                        raw = int(k)
                    except (TypeError, ValueError):
                        continue
                    mi = raw >> 16
                    mat = raw & 0xFFFF
                    try:
                        thk = float(v)
                    except (TypeError, ValueError):
                        continue
                    out[(mi, mat)] = thk

        hull = data.get("A_Hull") or {}
        _collect(hull)
        # 炮塔 / 副炮 / 防空等各挂载 HP_*.armor（含 A_Artillery / AB1_Artillery 等主炮变体）
        for key, _category, group in iter_component_groups(data):
            if key == "A_Hull":
                continue
            for sub in group.values():
                if isinstance(sub, dict):
                    _collect(sub)
        return out

    # ── 生命周期 ────────────────────────────────────────

    def clear(self):
        self._ships = None
        self._ship_snapshot_cache.clear()
        self._release_extractor()

    def _clear_per_load_caches(self) -> None:
        """释放单次加载产生的全部重内存缓存（本服务**不跨船复用**缓存）。

        挂载模型几何/贴图、材质贴图字节/信息、整船快照、骨架/挂点变换、
        渲染集、mfm 识别结果均为单次加载的中间产物；返回的 ShipGeometry
        已按引用共享这些数组（geom 持有引用），清空缓存不影响已加载场景。
        全局参考数据（目录索引 / 材质名表 / mfm 映射 / shape 名表 / 渲染集
        全局索引）保留，它们不是按船复用的加载结果。
        """
        self._mount_model_cache.clear()
        self._material_texture_cache.clear()
        self._texture_bytes_cache.clear()
        self._material_full_cache.clear()
        self._ship_snapshot_cache.clear()
        self._skeleton_bones_cache.clear()
        self._stem_mount_cache.clear()
        self._visual_rs_cache.clear()
        self._mfm_diffuse_cache.clear()

