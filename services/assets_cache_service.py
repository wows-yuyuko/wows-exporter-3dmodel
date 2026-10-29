"""
assets.bin 数据缓存服务 —— 把导出/查看需要的数据预提取到独立数据库。

assets.bin（约 217 MB）现场解析要 90 s，所以 import 时把骨架挂点、视觉渲染集、
材质贴图映射写进 `assets_data.db`；之后 `ship`/`batch` 只查库。

单版本镜像：库只反映最后一次同步的客户端 build（记在 meta.build）。
元信息表按 model_id 组织、**不带 build 维度** —— 否则 build 一变，未变动的
那上百万行也要 UPDATE 到新 build，等于全量重写，增量就无从谈起。

表结构：
- meta:            库级元信息（build / game_version / wows_type / last_sync_*）
- skeleton_mounts: 模型骨架 HP_/MP_ 挂点矩阵（按 model_id）
- skeleton_bones:  模型完整骨骼世界矩阵（按 model_id）
- render_set:      visual 渲染集 shape → 材质/mfm（按 geom_path，model_id 冗余一列）
- mfm_textures:    材质 .mfm → diffuseMap 贴图路径（全局表）
- material_full:   材质完整信息（全局表）
- shape_names:     *.vertices 名哈希 → 名（全局表）
- exteriors / camouflages / camo_*: 涂装（全局表）
- meta_schema_version: schema 版本记录（同 database_new.sql 规则）

增量：`populate(models=...)` 只重建指定模型的骨架/渲染集；全局表（材质/贴图/
形状名/涂装，合计约 30 MB）每次全刷 —— 它们被多船共用，无法归属单个模型。
"""

from __future__ import annotations

import json
import sqlite3
import struct
import time
from pathlib import Path

from services.proto_render_sets import (
    name_of, path_of, render_sets_lesta_one, render_sets_wg,
)
from services.sqlite_store import SqliteStore
from utils.asset_utils import material_family
from utils.path_utils import model_id_of

#: assets_data.db schema 版本（与 database_new.sql 的 DB_SCHEMA_VERSION 规则一致：
#: 库内 meta_schema_version 记录已应用版本，低于本值则 initialize 重建全表）
#: v2（2026-08-19）：skeleton_bones 增加解码列（pos/四元数/scale）
#: v4（2026-08-19）：渲染集按 count(+0x70) 精确解析（根治跨模型串用）；新增 shape_names 表
#:    （*.vertices 名哈希→名，LOD/crack 兜底用；显示时只读 DB，绝不现场读 assets.bin）
#: v5（2026-08-20）：skeleton_mounts 纳入 MP_ 甲板设备挂点（此前仅 HP_，
#:    导致 MP 节点挂载的缆桩/小艇/探照灯等甲板设备模型不显示）
#: v6（2026-08-20）：render_sets 增加 skinned 标志 + nodes 调色板（JSON 数组），
#:    供蒙皮网格按渲染集调色板施加 bind pose 混合（修复 PASA111 天线/索具
#:    180° 朝向错误；Korabli 渲染集项 +0x0C skinned / +0x0D nodes_count /
#:    +0x28 item-relative relptr → u32 名ID 数组）
#: v8：material_full 新增 material_hash 列（= fx 资源 selfId，供 fx 识别）
#: v10（2026-08-25）：render_sets 同一 (geom_path, shape) 可对应**多个节点**（如
#:    BIA400_Motor_Cutter_25ftShape 同时在 _0/_1 两舷节点），此前 PK 折叠导致只渲染
#:    一个；现改为汇总节点调色板（并集），供 geometry_service 按多节点实例化。
#: v11（2026-08-25）：多节点并集**仅对刚性（skinned=0）件**生效；蒙皮 shape 的
#:    nodes 是蒙皮骨骼调色板，并集会污染（如 TurretShape 的 4 骨骼）。保留首条。
#: v17（2026-10-20）：render_sets.nodes 改为**保留 .visual 原始顺序**（此前
#:    `sorted(set(...))` 会重排调色板）。顶点骨骼索引 = 调色板 slot × 3，
#:    顺序错位会把权重绑到错误骨骼（如 Mk56Shape 的 Rotate_Y 权重落到 Root、
#:    炮塔 Rotate_Y 的权重落到 Rotate_X）。
#: v18（2026-10-20）：material_full 新增 fx_path 列（material_hash 即 fx 资源
#:    selfId，入库时在 assets.bin 路径树里解析；不再依赖固定映射表）。
#: v19（2026-10-28）：去掉 bin_folder 维度改按 model_id 组织（单版本镜像 + 增量同步）；
#:    render_sets 更名 render_set，并加 model_id 列供「按模型整片删」；
#:    meta 改为 key/value；删除 exteriors/camouflages 的 display_name。
ASSETS_SCHEMA_VERSION = 19

# ── 分批落库阈值（模块级常量，便于测试时覆盖）────────────────
# 背景：Lesta 有 39793 个骨架 / 119631 个 visual → 骨骼 100 万+ 行、渲染集 40 万行。
# 若先全部攒在 Python 列表里最后一次性 executemany，峰值可达 1.5GB+（叠在本服务自身
# ~800MB 之上）→ 触发换页，看起来像卡死。改为边解析边分批写库，峰值内存恒定。
SKEL_FLUSH = 1500          # 骨架：每 N 个骨架写一批
SKEL_LOG = 2000            # 骨架：每 N 个骨架报一次进度
RS_LOG = 20000             # 渲染集：每 N 条记录报一次进度
RS_CHUNK = 20000           # 渲染集：末尾写库时每批 N 条
MF_FLUSH = 3000            # 材质：行数超过 N 条写一批
MF_LOG = 4000              # 材质：每 N 个文件报一次进度
EXT_FLUSH = 400            # 外观：行数超过 N 条写一批（单条 data_json 可能很大）


class AssetsCacheService(SqliteStore):
    """assets.bin 数据缓存（独立数据库 assets_data.db，线程安全）。

    连接 / schema 版本 / meta 见 `SqliteStore`；这里只负责解析与查询。
    """

    DB_STEM = "assets_data"
    SQL_FILE = "assets_database.sql"
    SCHEMA_VERSION = ASSETS_SCHEMA_VERSION
    TIMEOUT = 15
    BUSY_TIMEOUT = 8000

    def _is_wg(self) -> bool:
        """当前缓存库是否 WG 服（WG 的 assets.bin 用 10 类型表，布局与 Korabli 不同）。"""
        return self._wows_type == "Wargaming"

    # ── VisualPrototype 解析（实现见 services/proto_render_sets.py）──
    # 两服的记录布局差异大，且与「连库/清表/增量过滤」无关，拆了出去；
    # 类里留别名，是为了不改 13 处 `self._path_of/self._name_of` 调用点。
    _path_of = staticmethod(path_of)
    _name_of = staticmethod(name_of)
    _render_sets_lesta_one = staticmethod(render_sets_lesta_one)
    _render_sets_wg = staticmethod(render_sets_wg)

    # ── 渲染集合并（同一 shape 的多个实例节点）──────────────

    @staticmethod
    def _merge_render_set_into(merged: dict, row: tuple) -> None:
        """把一条渲染集行并入汇总字典（按 PK = (geom_path, shape)）。

        行格式：``(geom_path, shape, model_id, material, mfm, damage, skinned, nodes)``

        原始资产里同一 shape 可绑定多个骨骼节点（左右舷实例化，如 motor_cutter），
        但 render_set 表 PK 是 (geom_path, shape)，INSERT OR REPLACE 会把
        多条折叠成一条 → 丢失其余节点 → 只渲染一个实例。此处按 PK 合并，把节点并集
        写回单行 nodes 数组，供 geometry_service 逐节点实例化。

        ★ 顺序必须保留 .visual 里的原始顺序：蒙皮网格的顶点骨骼索引 = 调色板
          slot × 3，重排（原先的 `sorted`）会把权重绑到错误骨骼。故用「去重但保序」
          的列表，绝不能排序。

        populate 现为**流式**调用本方法（内存有界）；_merge_render_set_rows 为批量包装。
        """
        key = (row[0], row[1])
        sk = int(row[6])
        names: list[str] = []
        if row[7]:
            try:
                names = [n for n in json.loads(row[7]) if n]
            except Exception:  # noqa: BLE001
                names = []
        m = merged.get(key)
        if m is None:
            # 首条：无论刚性/蒙皮都用它的节点调色板（蒙皮 shape 的调色板 = 蒙皮骨骼）
            merged[key] = [row[2], row[3], row[4], row[5], sk, names]
        else:
            m[4] = int(m[4]) or sk
            # ★ 仅**刚性（skinned=0）**项并入额外节点（多实例，如左右舷）；
            #   蒙皮项节点是蒙皮调色板，保留首条即可，并集会污染骨骼调色板。
            if sk == 0 and names:
                known = set(m[5])
                m[5].extend(n for n in names if n not in known)

    @staticmethod
    def _materialize_render_set(merged: dict) -> list[tuple]:
        """汇总字典 → 待写库行（nodes 保序 JSON，不得排序）。"""
        return [(k[0], k[1], v[0], v[1], v[2], v[3], int(v[4]),
                 json.dumps(v[5])) for k, v in merged.items()]

    @staticmethod
    def _merge_render_set_rows(rows: list[tuple]) -> list[tuple]:
        """批量汇总（等价于流式 _merge_render_set_into + _materialize_render_set）。"""
        merged: dict[tuple, list] = {}
        for r in rows:
            AssetsCacheService._merge_render_set_into(merged, r)
        return AssetsCacheService._materialize_render_set(merged)

    # ── 事务内清空（全量重建 / 按模型重建）─────────────────

    def _clear_all_rows(self) -> None:
        """清空全部表的数据行（**不提交**，与写入共用事务，失败可回滚）。

        与 `_drop_all_tables` 的区别：表结构保留、只 DELETE 行，因此
        “导入→中途失败→回滚”能把旧缓存原样恢复（DROP 会即时提交、无法回滚）。
        `meta_schema_version` 保留（记录已应用的 schema 版本）。
        """
        conn = self._conn
        try:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        except sqlite3.OperationalError:
            return
        for tname in tables:
            if tname == "meta_schema_version":
                continue
            try:
                conn.execute(f'DELETE FROM "{tname}"')
            except sqlite3.OperationalError:
                pass

    # ── 按模型删行（增量重建）───────────────────────────────

    def _delete_models(self, models: set[str]) -> None:
        """删掉指定模型目录在派生表里的全部行（增量重建前调用）。"""
        if not models:
            return
        items = list(models)
        for i in range(0, len(items), 400):
            chunk = items[i:i + 400]
            ph = ",".join("?" * len(chunk))
            self._conn.execute(
                "DELETE FROM skeleton_mounts WHERE model_id IN (%s)" % ph, chunk)
            self._conn.execute(
                "DELETE FROM skeleton_bones WHERE model_id IN (%s)" % ph, chunk)
            self._conn.execute(
                "DELETE FROM render_set WHERE model_id IN (%s)" % ph, chunk)

    # ── 入库（「加载数据」时调用）────────────────────────────


    @staticmethod
    def _scan_prototype_files(svc) -> dict:
        """一次遍历 VFS 全树，按 prototype 类型分桶（4 个阶段各取所需）。

        45 万条记录只扫一遍：骨架（Korabli）、Visual / SkeletonExtender（WG）、
        材质 .mfm。单次遍历约 10 s，分阶段各扫一遍会翻四倍。
        """
        buckets: dict[str, list] = {"skel": [], "vis": [], "ext": [], "mfm": []}
        for f in svc.vfs.all_files():
            try:
                pt = f.prototype_type
                if pt is None:
                    continue
            except Exception:  # noqa: BLE001
                continue
            name = pt.name
            if name == "SkeletonPrototype":
                buckets["skel"].append(f)
            elif name == "MaterialPrototype" and f.path.endswith(".mfm"):
                buckets["mfm"].append(f)
            elif name == "VisualPrototype" and f.path.endswith(".visual"):
                buckets["vis"].append(f)
            elif name == "SkeletonExtenderPrototype" and f.path.endswith(".skel_ext"):
                buckets["ext"].append(f)
        return buckets

    def _populate_skeletons(self, svc, db, sdict, self_id_idx,
                            skel_files, vis_files, ext_files,
                            models, full: bool, counts: dict, _p) -> None:
        """骨架挂点 + 完整骨骼世界矩阵 → skeleton_mounts / skeleton_bones。

        Korabli 走 SkeletonPrototype；WG 走 Visual nodes（HP_）+ SkeletonExtender（MP_/SP_/EP_）。
        必须分批落库：Lesta 有近 4 万个骨架，攒齐再写要吃约 800 MB。
        """
        # 1) 骨架挂点 + 完整骨骼世界矩阵（bind pose 按 parent 累积）
        #    Korabli：SkeletonPrototype；WG：Visual nodes（HP_）+ SkeletonExtender（MP_）
        #
        # ⚠️ 必须**分批落库**：Lesta 有 39793 个骨架 → 约 102 万骨骼行 + 66 万挂点行。
        #    若先把它们全部攒在 Python 列表里、最后再一次性 executemany，光这些 13 元组
        #    就要 ~800MB 常驻（叠在本服务自身 ~800MB 之上）→ 触发换页，整个阶段看起来
        #    像卡死（用户实测：日志停在「解析舰体骨架挂点与骨骼」不再前进）。
        #    每 SKEL_FLUSH 个骨架写一批并 clear()，峰值内存恒定在几十 MB。
        #    事务仍然只有一个（末尾统一 commit）→ 中途被强杀时整体回滚，不会留半成品。
        import numpy as np
        skel_rows: list[tuple] = []
        bone_rows: list[tuple] = []
        skel_failed: list[str] = []
        n_skel = len(skel_files)
        tot_mounts = 0
        tot_bones = 0
        _t_skel = time.time()

        def _flush_skel() -> None:
            """把已累积的挂点/骨骼行写库并清空（内存有界）。"""
            nonlocal tot_mounts, tot_bones
            if skel_rows:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO skeleton_mounts "
                    "(model_id, hp_name, "
                    "pos_x, pos_y, pos_z, rot_qx, rot_qy, rot_qz, rot_qw, "
                    "scale_x, scale_y, scale_z) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    skel_rows)
                tot_mounts += len(skel_rows)
                skel_rows.clear()
            if bone_rows:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO skeleton_bones "
                    "(model_id, bone_name, "
                    "pos_x, pos_y, pos_z, rot_qx, rot_qy, rot_qz, rot_qw, "
                    "scale_x, scale_y, scale_z) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    bone_rows)
                tot_bones += len(bone_rows)
                bone_rows.clear()

        for _si, f in enumerate(skel_files, 1):
            if _si % SKEL_FLUSH == 0:
                _flush_skel()
            if _si % SKEL_LOG == 0 and n_skel > SKEL_LOG:
                _p(f"骨架解析 {_si}/{n_skel}"
                   f"（骨骼 {tot_bones + len(bone_rows):,} / "
                   f"挂点 {tot_mounts + len(skel_rows):,}，"
                   f"已用 {time.time() - _t_skel:.0f}s）...")
            stem = model_id_of(f.path)
            if not stem:
                continue
            # 增量：只重建变更集里的模型，其余整个跳过（省掉解码 + 写库）
            if not full and stem not in models:
                continue
            try:
                sk = svc.decode_skeleton_path(f.path)
            except Exception as exc:  # noqa: BLE001
                skel_failed.append(f"{stem}({exc})")
                continue
            names = sk.get("name_ids") or []
            mats = sk.get("matrices") or []
            parents = sk.get("parent_ids") or []
            n = len(names)
            # 完整骨骼世界矩阵（与 geometry_service._skeleton_bone_world 一致）
            if n and n == len(mats):
                try:
                    local = [np.array(mats[i], dtype=np.float32).reshape(4, 4).T
                             for i in range(n)]
                    w: list = [None] * n
                    for i in range(n):
                        p = parents[i] if i < len(parents) else 65535
                        if p >= n or p == i or p == 65535:
                            w[i] = local[i]
                        else:
                            w[i] = (w[p] if w[p] is not None
                                    else np.eye(4, dtype=np.float32)) @ local[i]
                    for i, nm in enumerate(names):
                        if i < len(w) and w[i] is not None:
                            # 统一用 _decompose_mat 分解（含反射 det=-1 处理：
                            # 否则 Root_BlendBone 等 Z 镜像骨骼会被存成恒等 → 主炮/副炮方向翻转）
                            pos, quat, scale = self._decompose_mat(w[i])
                            bone_rows.append((
                                stem, str(nm),
                                float(pos[0]), float(pos[1]), float(pos[2]),
                                quat[0], quat[1], quat[2], quat[3],
                                scale[0], scale[1], scale[2]))
                except Exception:  # noqa: BLE001
                    pass
            # HP_/MP_ 挂点（舰船骨架定位用；存解码后坐标/四元数/缩放）
            # MP_ 为甲板设备挂点（缆桩/小艇/探照灯等 misc 模型），v5 起纳入；
            # 其 parent 恒为 Scene Root，原始矩阵即船空间 bind pose
            for i, n in enumerate(names):
                if not (isinstance(n, str)
                        and (n.startswith("HP_") or n.startswith("MP_"))):
                    continue
                if i >= len(mats) or not mats[i] or len(mats[i]) < 16:
                    continue
                m = np.array(mats[i], dtype=np.float32).reshape(4, 4).T
                pos, quat, scale = self._decompose_mat(m)
                skel_rows.append((stem, n,
                                  pos[0], pos[1], pos[2],
                                  quat[0], quat[1], quat[2], quat[3],
                                  scale[0], scale[1], scale[2]))
        # WG：Visual nodes（HP_ 挂点 + 骨骼）+ SkeletonExtender（MP_/SP_/EP_ 挂点）
        if self._is_wg():
            self._populate_wg_skeleton(
                svc, db, sdict, self_id_idx,
                vis_files, ext_files, skel_rows, bone_rows, skel_failed,
                flush_cb=_flush_skel,
                models=models, note_cb=_p,
                log_cb=lambda i, n: _p(
                    f"WG 骨架解析 {i}/{n}"
                    f"（骨骼 {tot_bones + len(bone_rows):,} / "
                    f"挂点 {tot_mounts + len(skel_rows):,}，"
                    f"已用 {time.time() - _t_skel:.0f}s）..."))
        _flush_skel()
        if skel_failed:
            sample = ", ".join(skel_failed[:5])
            more = f" 等 {len(skel_failed)} 个" if len(skel_failed) > 5 else ""
            _p(f"⚠️ {len(skel_failed)} 个骨架解码失败: {sample}{more}")
        counts["skeleton"] = tot_mounts
        counts["skeleton_bones"] = tot_bones
        skel_rows.clear()
        bone_rows.clear()

    def _populate_render_sets(self, db, sdict, self_id_idx, models, full: bool,
                              counts: dict, _p) -> None:
        """VisualPrototype 渲染集 → render_set（+ shape 名称哈希表）。

        40 万行级别，攒成列表要几百 MB，所以边解析边按 PK 汇总到字典
        （同一 (geom_path, shape) 可能出现在多条记录里，必须全阶段并集），
        解析完再一次分块写库。
        """
        _p("解析视觉渲染集（shape → 材质/mfm 引用）...")

        # 2) 视觉渲染集（VisualPrototype 渲染集区 → shape/材质/mfm）
        #    行数可达 40 万+，直接攒成列表要几百 MB → 改为**边解析边按 PK 汇总到字典**
        #    （字典比原始行省内存：同 PK 折叠），解析全部完成后分块写库。
        #    ★ 字典**不能中途清空**：同一 (geom_path, shape) 可能出现在多个记录里，
        #      需要全阶段并集，才与旧的批量 _merge_render_set_rows 完全等价。
        rs_merged: dict[tuple, list] = {}
        tot_rs = 0
        _t_rs = time.time()

        def _add_rs(row: tuple) -> None:
            """按 PK(geom_path, shape) 汇总节点调色板（流式，内存有界）。"""
            self._merge_render_set_into(rs_merged, row)

        def _write_rs() -> None:
            """汇总字典 → 分块写库（解析结束后调**一次**，字典内容不变）。"""
            nonlocal tot_rs
            if not rs_merged:
                return
            buf: list[tuple] = []
            for k, v in rs_merged.items():
                buf.append((k[0], k[1], v[0], v[1], v[2], v[3], int(v[4]),
                            json.dumps(v[5])))
                if len(buf) >= RS_CHUNK:
                    self._conn.executemany(
                        "INSERT OR REPLACE INTO render_set "
                        "(geom_path, shape, model_id, material, mfm, damage, "
                        "skinned, nodes) VALUES (?,?,?,?,?,?,?,?)", buf)
                    tot_rs += len(buf)
                    buf.clear()
            if buf:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO render_set "
                    "(geom_path, shape, model_id, material, mfm, damage, "
                    "skinned, nodes) VALUES (?,?,?,?,?,?,?,?)", buf)
                tot_rs += len(buf)
                buf.clear()

        try:
            from uncode_assets.parser import BLOB_HEADER_SIZE
            from uncode_assets.types import type_from_magic
            vis = next((b for b in db.databases if (lambda t: t and
                       t.name == "VisualPrototype")(
                type_from_magic(b.prototype_magic))), None)
            if vis is not None:
                data = vis.data
                isize = vis.item_size
                nrec = vis.record_count
                wg = self._is_wg()
                rs_failed: list[str] = []
                for ri in range(nrec):
                    if (ri + 1) % RS_LOG == 0 and nrec > RS_LOG:
                        _p(f"渲染集解析 {ri + 1}/{nrec}"
                           f"（已汇总 {len(rs_merged):,} 条，"
                           f"已用 {time.time() - _t_rs:.0f}s）...")
                    off = BLOB_HEADER_SIZE + ri * isize
                    # ★ 逐条记录隔离异常：旧实现把整个循环包在同一个 try 里，
                    #   任意一条坏记录都会让**全部**渲染集解析中断（入库 0 条）。
                    try:
                        if wg:
                            # WG VisualPrototype 0x70 布局（wows-toolkit visual.rs）：
                            # 渲染集 relptr 在 +0x60，记录头字段与 Korabli 完全不同，
                            # 走专用 WG 解析（每记录一 geometry → 渲染集 0x28 步长）
                            self._render_sets_wg(data, off, db, self_id_idx, sdict,
                                                 _add_rs, models)
                            continue
                        if off + 0x40 > len(data):
                            break
                        self._render_sets_lesta_one(data, off, db, self_id_idx,
                                                    sdict, _add_rs, models)
                    except Exception as exc:  # noqa: BLE001
                        # 只留前几条：整库为 0 时这就是唯一线索
                        if len(rs_failed) < 5:
                            rs_failed.append(repr(exc))
                        continue
                if rs_failed:
                    _p(f"⚠️ {len(rs_failed)}+ 条渲染集记录解析失败: {rs_failed[0]}")
        except Exception as exc:  # noqa: BLE001
            _p(f"⚠️ 渲染集解析中断，已入库的仍保留: {exc}")
        _write_rs()
        counts["render_sets"] = tot_rs
        _p(f"渲染集已入库: {tot_rs:,} 条")
        tot_sn = 0
        sn_rows: list[tuple] = []
        for h, nm in sdict.items():
            if nm.endswith(".vertices"):
                sn_rows.append((h, nm))
                if len(sn_rows) >= 50000:
                    self._conn.executemany(
                        "INSERT OR REPLACE INTO shape_names "
                        "(hash, name) VALUES (?,?)", sn_rows)
                    tot_sn += len(sn_rows)
                    sn_rows.clear()
        if sn_rows:
            self._conn.executemany(
                "INSERT OR REPLACE INTO shape_names (hash, name) "
                "VALUES (?,?)", sn_rows)
            tot_sn += len(sn_rows)
            sn_rows.clear()
        counts["shape_names"] = tot_sn

    def _populate_materials(self, svc, db, sdict, self_id_idx, mfm_files,
                            models, full: bool, counts: dict, _p) -> None:
        """材质 .mfm → material_full + mfm_textures（全局表，每次全刷）。

        一个 mfm 的贴图会被上百条船共用，无法归属到单个模型，所以增量同步
        也整表重写（约 3 万行 / 20 MB）。
        """
        _p(f"解析材质（{len(mfm_files)} 个 mfm 文件）...")

        # 3) 材质：mfm_textures（diffuseMap 原始路径）+ material_full（完整：
        #    shader_id / 所有贴图原始路径 / INDEXED vec4 数组）。
        #    只存路径与参数，贴图字节渲染时实时从客户端 pkg 解包。
        from uncode_assets import binary as B
        from uncode_assets.decoders import (
            _read_typed_value,
            material_array_lengths,
            material_ool_end,
        )
        from uncode_assets.service import resolve_fx_path
        mfm_rows: list[tuple] = []
        mf_rows: list[tuple] = []
        mfm_failed: list[str] = []
        wg_mat = self._is_wg()
        # 同样分批落库：material_full 每行带 INDEXED vec4 的 JSON（可达数 KB），
        # 3 万条全攒在内存里同样是几百 MB
        n_mfm = len(mfm_files)
        tot_mfm = 0
        tot_mf = 0
        _t_mf = time.time()

        def _flush_mf() -> None:
            nonlocal tot_mfm, tot_mf
            if mfm_rows:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO mfm_textures "
                    "(mfm_path, texture_path) VALUES (?,?)",
                    mfm_rows)
                tot_mfm += len(mfm_rows)
                mfm_rows.clear()
            if mf_rows:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO material_full "
                    "(mfm_path, shader_id, family, material_hash, "
                    "fx_path, textures, indexed) VALUES (?,?,?,?,?,?,?)", mf_rows)
                tot_mf += len(mf_rows)
                mf_rows.clear()

        for _mi, f in enumerate(mfm_files, 1):
            if len(mf_rows) >= MF_FLUSH:
                _flush_mf()
            if _mi % MF_LOG == 0:
                _flush_mf()
                if n_mfm > MF_LOG:
                    _p(f"材质解析 {_mi}/{n_mfm}"
                       f"（完整材质 {tot_mf:,} 条 / 贴图映射 {tot_mfm:,} 条，"
                       f"已用 {time.time() - _t_mf:.0f}s）...")
            try:
                data = svc.vfs.open_file_len(f.path, 0x78 if wg_mat else 0x90)
            except Exception as exc:  # noqa: BLE001
                mfm_failed.append(f"{f.path.rsplit('/', 1)[-1]}({exc})")
                continue
            if len(data) < (0x78 if wg_mat else 0x88):
                continue
            pc = B.read_u16(data, 0x00)
            if not pc:
                continue
            if wg_mat:
                # WG MaterialPrototype 0x78 布局（wows-toolkit material.rs）：
                # +0x00 u16 property_count / +0x02 u16 flags /
                # +0x04 u32 shader_id / +0x08 u64 reserved /
                # +0x10 names_ptr / +0x18 type_idx_ptr /
                # +0x20..+0x60 type_ptrs[9] / +0x68 material_hash
                shader_id = B.read_u32(data, 0x04)
                mat_hash = B.read_u64(data, 0x68)
                names_ptr = B.read_u64(data, 0x10)
                type_idx_ptr = B.read_u64(data, 0x18)
                type_ptrs = {t: B.read_u64(data, 0x20 + t * 8) for t in range(9)}
            else:
                # Korabli 0x88 布局
                shader_id = B.read_u32(data, 0x08)
                mat_hash = B.read_u64(data, 0x78)
                names_ptr = B.read_u64(data, 0x18)
                type_idx_ptr = B.read_u64(data, 0x20)
                type_ptrs = {t: B.read_u64(data, 0x30 + t * 8) for t in range(9)}
            tptr4 = type_ptrs.get(4) or 0
            vec4_ptr = type_ptrs.get(7) or 0
            # ⚠️ 指针**先做合理性校验**再参与切片长度计算：个别材质记录的
            #    OOL 指针是垃圾值（如 0x30000000E），旧实现直接拿去算 need →
            #    随后的 parse_u32_array 抛 OutOfBounds，把整个 3D 缓存入库打断。
            size = len(data)
            _PTR_LIMIT = 64 * 1024 * 1024   # OOL 区上限（材质记录尾部远小于此）

            def _plausible(p: int, n: int) -> bool:
                return 0 < p <= _PTR_LIMIT and p + n <= _PTR_LIMIT

            need = 0x90
            for _ptr, _n in ((names_ptr, pc * 4), (type_idx_ptr, pc * 2),
                             (tptr4, pc * 8), (vec4_ptr, 4096 * 16 + 64)):
                if _plausible(_ptr, _n):
                    need = max(need, _ptr + _n)
            # 至少读到下一条记录的头（数组长度推断要用后继记录的 OOL 起点界定
            # 「最后一组」的结束；见 uncode_assets.decoders.material_array_lengths）
            need = max(need, (0x78 if wg_mat else 0x88) + 0x90)
            if need + 8 > size:
                try:
                    data = svc.vfs.open_file_len(f.path, need + 8)
                except Exception:  # noqa: BLE001
                    continue
                size = len(data)

            def _ok_ptr(p: int, n: int) -> bool:
                return 0 < p and p + n <= size

            if not _ok_ptr(names_ptr, pc * 4) or not _ok_ptr(type_idx_ptr, pc * 2):
                mfm_failed.append(f"{f.path.rsplit('/', 1)[-1]}(属性表指针异常)")
                continue
            names = B.parse_u32_array(data, names_ptr, pc)
            type_idx = B.parse_u16_array(data, type_idx_ptr, pc)
            if not _ok_ptr(tptr4, pc * 8):
                tptr4 = 0
            if not _ok_ptr(vec4_ptr, 16):
                vec4_ptr = 0
            textures: dict = {}
            vec4s: list = []
            diff_base = ""
            # 每个属性的值数组元素数（最后一组按 OOL 边界界定，避免多读下一条
            # 材质的 OOL 数据 → 出现 1e28 / 1e36 之类的假数值）
            arr_lens = material_array_lengths(
                data, pc, type_idx, type_ptrs,
                material_ool_end(data, wg_mat), wg_mat)
            for i in range(pc):
                ti = type_idx[i] if i < len(type_idx) else 0
                ptype = ti & 0xF
                pidx = ti >> 4
                raw_name = names[i] if i < len(names) else 0
                # 数组名优先查 sdict（shape/贴图名缓存），缺失时回退 db.strings 完整表
                # （⚠️ tileIdxMatIdArr/offsetScaleMatIdArr/artStrengthMatIdArr 等在 sdict 缺失，
                #   必须用 db.strings 解析，否则 INDEXED 分块数组被漏提取）
                nm = (sdict.get(raw_name & 0xFFFFFFFF)
                      or db.strings.get_string_or_hex(raw_name)
                      or "")
                if ptype == 4:
                    value = _read_typed_value(data, tptr4, 4, pidx)
                    if isinstance(value, int) and value:
                        vp = self._path_of(value, db, self_id_idx)
                        if vp:
                            if nm == "diffuseMap" and not diff_base:
                                diff_base = vp
                            textures[nm or f"tex_{i}"] = vp
                elif ptype == 7 and nm:
                    vec4s.append((i, raw_name, pidx))
            if diff_base:
                mfm_rows.append((self._norm_vfs_path(f.path), diff_base))
            # INDEXED vec4 数组（长度按 material_array_lengths 推断）
            indexed: dict = {}
            if vec4s and vec4_ptr:
                for j, (pi, nid, pidx) in enumerate(vec4s):
                    nm = (sdict.get(nid & 0xFFFFFFFF)
                          or db.strings.get_string_or_hex(nid)
                          or f"vec4_{j}")
                    length = arr_lens.get(pi, 1)
                    off = vec4_ptr + pidx * 16
                    vals = []
                    for k in range(length):
                        o = off + k * 16
                        if o + 16 <= len(data):
                            vals.append([round(float(x), 6) for x in B.parse_vec4(data, o)])
                    if vals:
                        indexed[nm] = vals
            family = material_family(f"0x{shader_id:08X}")
            # fx 路径：material_hash（fx 资源 selfId）→ assets.bin 路径树。
            # 解析逻辑在 uncode_assets.service.resolve_fx_path（只依赖 PrototypeDatabase），
            # 不依赖任何随源码打包的固定映射表。
            fx_path = resolve_fx_path(db, f"0x{mat_hash:016X}")
            mf_rows.append((self._norm_vfs_path(f.path), f"0x{shader_id:08X}", family,
                            f"0x{mat_hash:016X}", fx_path,
                            json.dumps(textures), json.dumps(indexed)))
        if mfm_failed:
            sample = ", ".join(mfm_failed[:5])
            more = f" 等 {len(mfm_failed)} 个" if len(mfm_failed) > 5 else ""
            _p(f"⚠️ {len(mfm_failed)} 个材质文件读取失败: {sample}{more}")
        _flush_mf()
        counts["mfm_textures"] = tot_mfm
        counts["material_full"] = tot_mf

    def _populate_exteriors(self, game_dir, bin_folder: str, wows_type: str,
                            counts: dict, _p) -> None:
        """外观（GameParams Exterior）与涂装（camouflages.xml）→ 全局表。

        两张表被多船共用、无法归属到单个模型，所以**每次同步全刷**。
        这里失败不致命（涂装只影响命名/配色），捕获后打日志继续。
        """
        _p("写入外观（Exterior）与涂装（camouflages.xml）数据...")
        # 共享单个 GameExtractor（仅当需要从 pkg 读涂装时才建），惰性建一次复用。
        shared_ext = None


        def _ext_factory():
            nonlocal shared_ext
            if shared_ext is None and game_dir:
                from data_extractor import GameExtractor
                shared_ext = GameExtractor(str(game_dir), bin_folder=bin_folder)
            return shared_ext
        # 9) Exterior 实体（GameParams 直读）→ exteriors 表
        try:
            from core.gameparams import (EXTERIOR_DATA_KEYS, exterior_entities,
                                         locate as gp_locate)
            # 数据源只有 GameParams 一份（split 目录已不再产出）；
            # 这里不解包（extract=False）：import 链路上 GameParams 已由 CLI 备好
            ext_items: list[tuple[str, dict]] = []
            gp_file = gp_locate(game_dir, wows_type)
            if gp_file:
                try:
                    ext_items = exterior_entities(gp_file)
                    _p(f"外观数据来源: GameParams（{len(ext_items)} 条）")
                except Exception as exc:  # noqa: BLE001
                    _p(f"GameParams 解析外观失败，外观表将为空: {exc}")
            ext_rows: list[tuple] = []
            # data_json 只存涂装相关字段，不存整份 Exterior（数千条会撑爆库）
            ext_sql = ("INSERT OR REPLACE INTO exteriors "
                       "(ext_index, name, camouflage, species, nation, "
                       "cost_gold, can_buy, parts_json, custom_json, "
                       "unpeculiar_camouflage, color_scheme_id, icon_path, data_json) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)")
            tot_ext = 0
            ext_total = len(ext_items)
            # 外观条目极多，分批输出进度，避免长时间无任何日志（显得卡死）
            ext_batch = max(1, ext_total // 20 or 1)
            for _i, (ext_name, d) in enumerate(ext_items, 1):
                if not isinstance(d, dict):
                    continue
                ti = d.get("typeinfo")
                if not isinstance(ti, dict):
                    ti = getattr(ti, "__dict__", {}) or {}
                ext_index = d.get("index", ext_name)
                # icon_path 列保留但不再生产（缩略图链路已移除）
                icon_rel = ""
                camo_mat = d.get("camouflage", "") or ""
                ext_rows.append((
                    ext_index,
                    ext_name,
                    camo_mat,
                    ti.get("species", "") or "",
                    ti.get("nation", "") or "",
                    int(d.get("costGold", 0) or 0),
                    int(bool(d.get("canBuy"))),
                    json.dumps(d.get("parts", []), ensure_ascii=False),
                    json.dumps(d.get("custom", {}), ensure_ascii=False),
                    d.get("unpeculiarCamouflage", "") or "",
                    int(d.get("unpeculiarCamouflageColorSchemeId", 0) or 0),
                    icon_rel,
                    json.dumps({k: d.get(k) for k in EXTERIOR_DATA_KEYS
                                if d.get(k) is not None}, ensure_ascii=False),
                ))
                if len(ext_rows) >= EXT_FLUSH:
                    self._conn.executemany(ext_sql, ext_rows)
                    tot_ext += len(ext_rows)
                    ext_rows.clear()
                if ext_total > ext_batch and _i % ext_batch == 0:
                    _p(f"外观数据入库进度 {_i}/{ext_total} ...")
            if ext_rows:
                self._conn.executemany(ext_sql, ext_rows)
                tot_ext += len(ext_rows)
                ext_rows.clear()
            counts["exteriors"] = tot_ext
            _p(f"外观数据已入库: {counts['exteriors']} 条")
        except Exception as exc:  # noqa: BLE001
            _p(f"⚠️ 外观数据入库失败: {exc}")

        # 10) camouflages.xml（包根，含 shipgroups/colorschemes/camouflages 三段）
        #     → camouflages + camouflage_color_schemes 表，供涂装切换器按索引查询。
        try:
            from services.camo_service import parse_camouflages
            camo_xml: bytes | None = None
            if game_dir:
                rp = Path(game_dir) / "res_unpack" / "camouflages.xml"
                if rp.exists():
                    camo_xml = rp.read_bytes()
            if camo_xml is None and game_dir:
                ext = _ext_factory()
                if ext is not None:
                    try:
                        hits = ext.list_files(["camouflages.xml"])
                        if hits:
                            e = hits[0]
                            camo_xml = ext.pkg_reader.read_file(e.volume.filename, e.file_info)
                    except Exception:  # noqa: BLE001
                        pass
            if camo_xml:
                _p("解析 camouflages.xml（涂装/配色/船组）...")
                parsed = parse_camouflages(camo_xml)
                crows: list[tuple] = []
                csrows: list[tuple] = []
                for name, variants in (parsed.get("entries") or {}).items():
                    if not variants:
                        continue
                    entry = variants[0]
                    icon_rel = ""   # 缩略图链路已移除，列保留
                    crows.append((
                        name, entry.realm, int(entry.tiled),
                        int(entry.use_color_scheme),
                        ",".join(entry.color_schemes
                                 or ([entry.color_scheme] if entry.color_scheme else [])),
                        json.dumps(entry.textures, ensure_ascii=False),
                        json.dumps(
                            {k: {"scale": list(v.scale), "offset": list(v.offset)}
                             for k, v in entry.uv_transforms.items()},
                            ensure_ascii=False),
                        json.dumps(entry.ship_groups, ensure_ascii=False),
                        json.dumps(entry.target_ships, ensure_ascii=False),
                        icon_rel,
                    ))
                for cname, cs in (parsed.get("color_schemes") or {}).items():
                    cols = cs.colors or [[0.0] * 4 for _ in range(4)]
                    csrows.append((cname,) + tuple(
                        json.dumps(list(cols[i]), ensure_ascii=False) for i in range(4)))
                if crows:
                    self._conn.executemany(
                        "INSERT OR REPLACE INTO camouflages "
                        "(name, realm, tiled, use_color_scheme, "
                        "color_scheme, textures_json, uv_json, "
                        "ship_groups_json, target_ships_json, icon_path) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)", crows)
                if csrows:
                    self._conn.executemany(
                        "INSERT OR REPLACE INTO camouflage_color_schemes "
                        "(name, color0, color1, color2, color3) "
                        "VALUES (?,?,?,?,?)", csrows)
                # shipgroups.xml 全局船组映射
                sgrows: list[tuple] = []
                for gname, ships in (parsed.get("ship_groups") or {}).items():
                    for s in ships:
                        sgrows.append((gname, s))
                if sgrows:
                    self._conn.executemany(
                        "INSERT OR REPLACE INTO camo_ship_groups "
                        "(group_name, ship) VALUES (?,?)", sgrows)
                counts["camouflages"] = len(crows)
                counts["camouflage_color_schemes"] = len(csrows)
                counts["camo_ship_groups"] = len(sgrows)
                _p(f"涂装数据已入库: {counts['camouflages']} 条 / "
                   f"配色 {counts['camouflage_color_schemes']} 条 / "
                   f"船组 {counts['camo_ship_groups']} 条")
        except Exception:  # noqa: BLE001
            pass

        if shared_ext is not None:
            try:
                shared_ext.close()
            except Exception:  # noqa: BLE001
                pass

    def populate(self, assets_path: str | None = None, bin_folder: str = "",
                 game_version: str = "", wows_type: str = "",
                 game_dir: str | Path | None = None,
                 progress_cb=None, assets_bytes: bytes | None = None,
                 models: set[str] | None = None,
                 removed: set[str] | None = None) -> dict:
        """解析 assets.bin 并写入缓存数据库（全量或按模型增量）。

        assets_bytes: 内存中的 assets.bin 字节（**推荐**：加载流程全程不落盘）；
                      给了它就不读任何文件、也不写任何文件。
        assets_path:  兼容旧调用方的已解压文件路径（仅当 assets_bytes 为 None 时使用）。
        models:      要重建的模型目录集合；``None`` = 全量重建（首次/强制）。
        removed:     已从客户端消失的模型目录，删掉它们的行。
        progress_cb: 可选阶段进度回调（str 消息），用于把骨架/渲染集/材质各阶段
        显示到日志区。
        返回各表写入条数。

        增量语义：`render_set` / `skeleton_*` 只重建 `models` 里的模型；
        材质 / 贴图 / 形状名 / 外观这几张全局表（合计约 30 MB）每次全刷 ——
        它们被多船共用，无法归属到单个模型，全刷是唯一正确做法。
        """
        from uncode_assets.service import AssetsBinService

        if assets_bytes is None and not assets_path:
            raise ValueError("populate 需要 assets_bytes 或 assets_path 之一")

        self.initialize()
        # 全内存路径：assets_bytes 直接解析（不落盘、不读既有文件）
        # wows_type 传入：WG 服用 10 类型表 + WG 布局（渲染集 0x70/材质 0x78）
        if assets_bytes is not None:
            svc = AssetsBinService(assets_bytes=assets_bytes, wows_type=self._wows_type)
        else:
            svc = AssetsBinService(assets_path=assets_path, wows_type=self._wows_type)
        db = svc.db

        def _p(msg: str) -> None:
            if progress_cb:
                try:
                    progress_cb(msg)
                except Exception:  # noqa: BLE001
                    pass

        full = models is None

        try:
            counts = {"skeleton": 0, "render_sets": 0, "mfm_textures": 0,
                      "material_full": 0, "skeleton_bones": 0, "shape_names": 0,
                      "exteriors": 0, "camouflages": 0, "camouflage_color_schemes": 0,
                      "camo_ship_groups": 0, "models": 0, "mode": "full" if full else "incremental"}
            # 共用：字符串 dict + self_id 索引（渲染集/材质解析都要用）
            sdict = self._strings_dict(db)
            self_id_idx = db.build_self_id_index()

            if full:
                # 全量：清空全部行（同一事务，失败可回滚 → 旧缓存原样保留，
                # 不会出现「导入失败 → 库被清空 → 3D 数据全丢」的连带故障）
                self._clear_all_rows()
            else:
                # 增量：只删要重建的模型 + 已消失的模型；全局表全刷
                drop = set(models or ()) | set(removed or ())
                self._delete_models(drop)
                for t in ("material_full", "mfm_textures", "shape_names",
                          "exteriors", "camouflages", "camouflage_color_schemes",
                          "camo_ship_groups"):
                    self._conn.execute('DELETE FROM "%s"' % t)

            self.set_meta({
                "build": bin_folder or "",
                "game_version": game_version or "",
                "wows_type": wows_type or self._wows_type,
                "last_sync_mode": "full" if full else "incremental",
                "last_sync_models": str(len(models or ())),
            }, commit=False)

            _p("清空目标模型（事务内，失败可回滚）并扫描 VFS 全树...")

            # 一次遍历全树，按 prototype 类型分桶（4 个阶段各取所需）
            files = self._scan_prototype_files(svc)
            skel_files, vis_files = files["skel"], files["vis"]
            ext_files, mfm_files = files["ext"], files["mfm"]

            if self._is_wg():
                _p(f"解析舰体骨架挂点与骨骼（{len(skel_files)} 个骨架文件，"
                   f"Visual {len(vis_files)} / SkeletonExtender {len(ext_files)}）...")
                _p("WG 服：骨架来自 Visual nodes（HP_ 挂点/骨骼）+ "
                   "SkeletonExtender（MP_/SP_/EP_ 挂点）")
            else:
                _p(f"解析舰体骨架挂点与骨骼（{len(skel_files)} 个骨架文件）...")

            self._populate_skeletons(svc, db, sdict, self_id_idx,
                                     skel_files, vis_files, ext_files,
                                     models, full, counts, _p)
            self._populate_render_sets(db, sdict, self_id_idx, models, full, counts, _p)
            self._populate_materials(svc, db, sdict, self_id_idx, mfm_files,
                                     models, full, counts, _p)
            self._populate_exteriors(game_dir, bin_folder, wows_type, counts, _p)

            counts["models"] = len(models or ())
            self.set_meta({
                "synced_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "built_models": str(counts["models"]),
            }, commit=False)
            self._conn.commit()
            return counts
        except Exception:
            # ★ 失败回滚：DELETE 与全部写入都在同一事务内，回滚后旧缓存完整保留，
            #   不会把查看器的骨架/材质数据“清空且重建不出来”。
            try:
                self._conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            raise
        finally:
            svc.close()

    # ── 查询（3D 查看器用）─────────────────────────────────


    def get_exteriors(self) -> list[dict]:
        """全部外观/涂装（Exterior 索引）：[{index,name,species,nation,icon_path,...}]。"""
        out: list[dict] = []
        try:
            rows = self._conn.execute(
                "SELECT ext_index, name, camouflage, species, nation, cost_gold, can_buy, "
                "parts_json, custom_json, unpeculiar_camouflage, color_scheme_id, "
                "icon_path, data_json FROM exteriors ORDER BY ext_index").fetchall()
            for r in rows:
                out.append({
                    "index": r["ext_index"],
                    "name": r["name"],
                    "camouflage": r["camouflage"],
                    "species": r["species"],
                    "nation": r["nation"],
                    "cost_gold": r["cost_gold"],
                    "can_buy": bool(r["can_buy"]),
                    "parts": json.loads(r["parts_json"] or "[]"),
                    "custom": json.loads(r["custom_json"] or "{}"),
                    "unpeculiar_camouflage": r["unpeculiar_camouflage"],
                    "color_scheme_id": r["color_scheme_id"],
                    "icon_path": r["icon_path"] or "",
                    "data": json.loads(r["data_json"] or "{}"),
                })
        except (sqlite3.OperationalError, ValueError):
            out = []
        return out

    def get_all_camouflages(self) -> list[dict]:
        """全部 camouflages.xml 条目。"""
        out: list[dict] = []
        try:
            rows = self._conn.execute(
                "SELECT name, realm, tiled, use_color_scheme, color_scheme, textures_json, "
                "uv_json, ship_groups_json, target_ships_json, icon_path "
                "FROM camouflages ORDER BY name").fetchall()
        except sqlite3.OperationalError:
            return []
        for r in rows:
            out.append({
                "name": r["name"], "realm": r["realm"],
                "tiled": bool(r["tiled"]), "use_color_scheme": bool(r["use_color_scheme"]),
                "color_scheme": r["color_scheme"] or "",
                "textures": json.loads(r["textures_json"] or "{}"),
                "uv": json.loads(r["uv_json"] or "{}"),
                "ship_groups": json.loads(r["ship_groups_json"] or "[]"),
                "target_ships": json.loads(r["target_ships_json"] or "[]"),
                "icon_path": r["icon_path"] or "",
            })
        return out

    def get_all_camo_color_schemes(self) -> dict[str, list]:
        """全部颜色方案：{name: [color0..3] 各 4 float}。"""
        out: dict[str, list] = {}
        try:
            rows = self._conn.execute(
                "SELECT name, color0, color1, color2, color3 "
                "FROM camouflage_color_schemes").fetchall()
        except sqlite3.OperationalError:
            return {}
        for r in rows:
            cols = []
            for i in range(4):
                txt = r[f"color{i}"]
                try:
                    cols.append(json.loads(txt) if txt else [0.0] * 4)
                except Exception:  # noqa: BLE001
                    cols.append([0.0] * 4)
            out[r["name"]] = cols
        return out

    def get_all_camo_ship_groups(self) -> dict[str, set]:
        """全部船组映射：{group_name: set(ship)}。"""
        out: dict[str, set] = {}
        try:
            rows = self._conn.execute(
                "SELECT group_name, ship FROM camo_ship_groups").fetchall()
        except sqlite3.OperationalError:
            return {}
        for r in rows:
            out.setdefault(r["group_name"], set()).add(r["ship"])
        return out

    def get_skeleton_mounts(self, model_id: str) -> dict:
        """某模型骨架的 HP_ 挂点：{hp_name: (4,4) 行主序渲染空间矩阵（由解码值重建）}。"""
        out: dict = {}
        try:
            rows = self._conn.execute(
                "SELECT hp_name, pos_x, pos_y, pos_z, "
                "rot_qx, rot_qy, rot_qz, rot_qw, scale_x, scale_y, scale_z "
                "FROM skeleton_mounts WHERE model_id=?",
                (model_id,)).fetchall()
            for r in rows:
                out[r["hp_name"]] = self._quat_to_mat(
                    r["rot_qx"], r["rot_qy"], r["rot_qz"], r["rot_qw"],
                    r["scale_x"], r["scale_y"], r["scale_z"],
                    r["pos_x"], r["pos_y"], r["pos_z"])
        except (sqlite3.OperationalError, ValueError):
            out = {}
        return out

    def get_skeleton_bones(self, model_id: str) -> dict:
        """某模型骨架的全部骨骼世界矩阵：{bone_name: (4,4)（由解码值重建）}。"""
        out: dict = {}
        try:
            rows = self._conn.execute(
                "SELECT bone_name, pos_x, pos_y, pos_z, "
                "rot_qx, rot_qy, rot_qz, rot_qw, scale_x, scale_y, scale_z "
                "FROM skeleton_bones WHERE model_id=?",
                (model_id,)).fetchall()
            for r in rows:
                out[r["bone_name"]] = self._quat_to_mat(
                    r["rot_qx"], r["rot_qy"], r["rot_qz"], r["rot_qw"],
                    r["scale_x"], r["scale_y"], r["scale_z"],
                    r["pos_x"], r["pos_y"], r["pos_z"])
        except (sqlite3.OperationalError, ValueError):
            out = {}
        return out

    def get_render_sets(self, geom_paths: list[str]) -> list[dict]:
        """某批 geometry 路径的全部渲染集（含整合模型共享记录）。

        返回 [{geom_path, shape, material, mfm, damage, skinned, nodes}]。
        nodes 为蒙皮调色板节点名列表（JSON 反序列化）。
        """
        out: list[dict] = []
        if not geom_paths:
            return out
        try:
            ph = ",".join("?" * len(geom_paths))
            rows = self._conn.execute(
                f"SELECT geom_path, shape, material, mfm, damage, skinned, nodes "
                f"FROM render_set "
                f"WHERE geom_path IN ({ph}) "
                f"ORDER BY geom_path, shape", tuple(geom_paths)).fetchall()
            for r in rows:
                nodes: list[str] = []
                if r["nodes"]:
                    try:
                        nodes = json.loads(r["nodes"])
                    except (ValueError, TypeError):
                        nodes = []
                out.append({
                    "geom_path": r["geom_path"], "shape": r["shape"],
                    "material": r["material"], "mfm": r["mfm"],
                    "damage": bool(r["damage"]),
                    "skinned": bool(r["skinned"]),
                    "nodes": nodes,
                })
        except sqlite3.OperationalError:
            out = []
        return out

    def get_shape_names(self) -> dict:
        """*.vertices 名字哈希 → 名字（LOD/crack 兜底跳过用；**只从 DB 读取**）。"""
        out: dict = {}
        try:
            rows = self._conn.execute("SELECT hash, name FROM shape_names").fetchall()
            for r in rows:
                out[r["hash"]] = r["name"]
        except sqlite3.OperationalError:
            out = {}
        return out

    def get_mfm_textures(self) -> dict:
        """全部材质贴图映射：{mfm_path: texture_path（原始路径，含扩展名）}。"""
        out: dict = {}
        try:
            rows = self._conn.execute(
                "SELECT mfm_path, texture_path FROM mfm_textures").fetchall()
            for r in rows:
                out[r["mfm_path"]] = r["texture_path"]
        except sqlite3.OperationalError:
            out = {}
        return out

    def get_material_full(self, mfm_path: str) -> dict | None:
        """材质完整信息：{shader_id, family, material_hash, fx_path, textures, indexed}。"""
        try:
            row = self._conn.execute(
                "SELECT shader_id, family, material_hash, fx_path, textures, indexed "
                "FROM material_full WHERE mfm_path=?", (mfm_path,)).fetchone()
            if row is None:
                return None
            return {
                "shader_id": row["shader_id"],
                "family": row["family"],
                "material_hash": row["material_hash"],
                "fx_path": row["fx_path"],
                "textures": json.loads(row["textures"] or "{}"),
                "indexed": json.loads(row["indexed"] or "{}"),
            }
        except (sqlite3.OperationalError, ValueError, TypeError):
            return None

    @staticmethod
    def _norm_vfs_path(p: str) -> str:
        """vfs 路径（/@content/... 或 /content/...）→ 规范化（content/...，与 render_sets 一致）。"""
        if p.startswith("/@"):
            return p[2:]
        if p.startswith("/"):
            return p[1:]
        return p

    # ── 工具 ────────────────────────────────────────────────


    def _populate_wg_skeleton(self, svc, db, sdict, self_id_idx,
                              vis_files, ext_files, skel_rows, bone_rows,
                              skel_failed, flush_cb=None, log_cb=None,
                              note_cb=None, models=None) -> None:
        """WG 骨架：VisualPrototype nodes（基础骨架，含 HP_ 挂点）+ SkeletonExtender（MP_/SP_/EP_）。

        数据来源（wows-toolkit）：
          - Visual nodes（visual.rs）：+0x00 u32 nodes_count / +0x18 name_ids relptr /
            +0x20 matrices relptr / +0x28 parent_ids relptr（u16 索引，0xFFFF=根）。
            HP_ 炮塔挂点与全部骨架骨骼都在这（舰船按段 Bow/MidFront/MidBack/Stern 分 .visual）。
          - SkeletonExtender（skeleton_extender.rs，0x20 记录）：+0x00 u16 flag /
            +0x02 u16 node_count / +0x08 name_ids relptr / +0x18 matrices relptr。
            parent 为名字哈希（通常 Scene Root）→ local 即船空间；节点含 MP_/SP_/EP_。
        矩阵均为列主序 4x4 → 转行主序 → _decompose_mat（含反射 det=-1 处理）。

        flush_cb/log_cb：分批落库 + 进度回调（与 Korabli 分支同样的内存有界策略，
        本服有数万 visual/skel_ext 文件，全攒在内存里同样会撑爆）。
        """
        import numpy as np

        stats: dict = {}

        def _tick(i: int, n: int, every: int = 2000) -> None:
            if i % every:
                return
            if flush_cb is not None:
                flush_cb()
            if log_cb is not None and n > every:
                log_cb(i, n)

        # 1) VisualPrototype nodes → HP_ 挂点 + 全部骨骼
        for _i, f in enumerate(vis_files, 1):
            _tick(_i, len(vis_files))
            stem = model_id_of(f.path)
            if not stem:
                continue
            try:
                names, mats, parents = self._wg_visual_nodes(svc, f, sdict, stats)
                if not names or not mats:
                    continue
                n = len(names)
                local = [np.array(mats[i], dtype=np.float32).reshape(4, 4).T
                         for i in range(len(mats))]
                w: list = [None] * len(local)
                for i in range(len(local)):
                    p = parents[i] if i < len(parents) else 65535
                    if p >= len(local) or p == i or p == 65535:
                        w[i] = local[i]
                    else:
                        w[i] = (w[p] if w[p] is not None
                                else np.eye(4, dtype=np.float32)) @ local[i]
                for i in range(n):
                    nm = names[i]
                    if not nm or i >= len(w) or w[i] is None:
                        continue
                    pos, quat, scale = self._decompose_mat(w[i])
                    bone_rows.append((stem, nm,
                                      float(pos[0]), float(pos[1]), float(pos[2]),
                                      quat[0], quat[1], quat[2], quat[3],
                                      scale[0], scale[1], scale[2]))
                    if nm.startswith("HP_"):
                        skel_rows.append((stem, nm,
                                          float(pos[0]), float(pos[1]), float(pos[2]),
                                          quat[0], quat[1], quat[2], quat[3],
                                          scale[0], scale[1], scale[2]))
            except Exception as exc:  # noqa: BLE001
                skel_failed.append(f"{f.path.rsplit('/', 1)[-1]}({exc})")

        # 2) SkeletonExtender → MP_/SP_/EP_ 挂点（local 即船空间）
        for _i, f in enumerate(ext_files, 1):
            _tick(_i, len(ext_files))
            stem = model_id_of(f.path)
            if not stem:
                continue
            try:
                names, mats = self._wg_extender_nodes(svc, f, sdict, stats)
                for i, nm in enumerate(names):
                    if not nm or i >= len(mats):
                        continue
                    mtx = np.array(mats[i], dtype=np.float32).reshape(4, 4).T
                    pos, quat, scale = self._decompose_mat(mtx)
                    bone_rows.append((stem, nm,
                                      float(pos[0]), float(pos[1]), float(pos[2]),
                                      quat[0], quat[1], quat[2], quat[3],
                                      scale[0], scale[1], scale[2]))
                    if nm.startswith(("MP_", "SP_", "EP_")):
                        skel_rows.append((stem, nm,
                                          float(pos[0]), float(pos[1]), float(pos[2]),
                                          quat[0], quat[1], quat[2], quat[3],
                                          scale[0], scale[1], scale[2]))
            except Exception as exc:  # noqa: BLE001
                skel_failed.append(f"{f.path.rsplit('/', 1)[-1]}({exc})")

        if note_cb is not None:
            note_cb("WG 骨架节点统计：成功 "
                    f"{stats.get('ok', 0)} / 无节点 {stats.get('no_nodes', 0)} / "
                    f"指针异常 {stats.get('bad_ptr', 0)} / "
                    f"计数异常 {stats.get('bad_count', 0)} / "
                    f"头过短 {stats.get('short_header', 0)}")

    def _wg_visual_nodes(self, svc, f, sdict, stats: dict | None = None):
        """WG VisualPrototype nodes → (names, matrices, parents)。有界读取（不拷整段 blob）。

        ⚠️ **指针校验**：个别 visual 记录的 OOL 指针不是合法的 record-relative 偏移
        （LOD/特效类记录），旧实现直接拿去 unpack_from → 巨量偏移异常（整个骨架阶段
        报 2 万个解码失败）。校验失败返回空，由调用方计为 skipped。
        stats：可选，统计跳过原因（诊断日志用）。
        """
        import struct

        def _bump(k: str) -> None:
            if stats is not None:
                stats[k] = stats.get(k, 0) + 1

        hdr = svc.vfs.open_file_len(f.path, 0x30)
        if len(hdr) < 0x30:
            _bump("short_header")
            return [], [], []
        ncnt = struct.unpack_from('<I', hdr, 0)[0]
        if ncnt <= 0:
            _bump("no_nodes")
            return [], [], []
        if ncnt > 5000:
            _bump("bad_count")
            return [], [], []
        nids_ptr = struct.unpack_from('<q', hdr, 0x18)[0]
        mats_ptr = struct.unpack_from('<q', hdr, 0x20)[0]
        pids_ptr = struct.unpack_from('<q', hdr, 0x28)[0]
        need = 0x30
        for ptr, esize in ((nids_ptr, 4), (mats_ptr, 64), (pids_ptr, 2)):
            if ptr > 0:
                need = max(need, ptr + ncnt * esize)
        data = svc.vfs.open_file_len(f.path, need + 16)
        size = len(data)

        def _fits(ptr: int, esize: int) -> bool:
            return 0 <= ptr and ptr + ncnt * esize <= size

        if not _fits(nids_ptr, 4) or not _fits(mats_ptr, 64):
            _bump("bad_ptr")
            return [], [], []
        names = []
        for i in range(ncnt):
            h = struct.unpack_from('<I', data, nids_ptr + i * 4)[0]
            names.append(self._name_of(sdict, svc.db, h))
        mats = [list(struct.unpack_from('<16f', data, mats_ptr + i * 64))
                for i in range(ncnt)]
        if _fits(pids_ptr, 2):
            parents = [struct.unpack_from('<H', data, pids_ptr + i * 2)[0]
                       for i in range(ncnt)]
        else:
            parents = [0xFFFF] * ncnt   # 父链缺失 → 全部当根，仍可导出骨骼
        _bump("ok")
        return names, mats, parents

    def _wg_extender_nodes(self, svc, f, sdict, stats: dict | None = None):
        """WG SkeletonExtender → (names, matrices)。列主序矩阵，local 即船空间。

        与 `_wg_visual_nodes` 同样做指针校验（坏记录直接跳过，不抛异常）。
        """
        import struct

        def _bump(k: str) -> None:
            if stats is not None:
                stats[k] = stats.get(k, 0) + 1

        hdr = svc.vfs.open_file_len(f.path, 0x20)
        if len(hdr) < 0x20:
            _bump("short_header")
            return [], []
        ncnt = struct.unpack_from('<H', hdr, 0x02)[0]
        if ncnt <= 0:
            _bump("no_nodes")
            return [], []
        if ncnt > 5000:
            _bump("bad_count")
            return [], []
        nids_ptr = struct.unpack_from('<q', hdr, 0x08)[0]
        mats_ptr = struct.unpack_from('<q', hdr, 0x18)[0]
        need = 0x20
        for ptr, esize in ((nids_ptr, 4), (mats_ptr, 64)):
            if ptr > 0:
                need = max(need, ptr + ncnt * esize)
        data = svc.vfs.open_file_len(f.path, need + 16)
        size = len(data)
        if not (0 <= nids_ptr and nids_ptr + ncnt * 4 <= size) \
                or not (0 <= mats_ptr and mats_ptr + ncnt * 64 <= size):
            _bump("bad_ptr")
            return [], []
        names = []
        for i in range(ncnt):
            h = struct.unpack_from('<I', data, nids_ptr + i * 4)[0]
            names.append(self._name_of(sdict, svc.db, h))
        mats = [list(struct.unpack_from('<16f', data, mats_ptr + i * 64))
                for i in range(ncnt)]
        _bump("ok")
        return names, mats


    @staticmethod
    def _mat3_to_quat(rot) -> tuple:
        """3x3 旋转矩阵（行主序）→ 单位四元数 (qx, qy, qz, qw)。

        标准矩阵→四元数（Shepperd 方法，数值稳定，避免大角度分支问题）。
        """
        import numpy as np
        m = np.asarray(rot, dtype=np.float64)
        t = m[0, 0] + m[1, 1] + m[2, 2]
        if t > 0.0:
            s = np.sqrt(t + 1.0) * 2.0
            qw = 0.25 * s
            qx = (m[2, 1] - m[1, 2]) / s
            qy = (m[0, 2] - m[2, 0]) / s
            qz = (m[1, 0] - m[0, 1]) / s
        elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
            s = np.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2.0
            qw = (m[2, 1] - m[1, 2]) / s
            qx = 0.25 * s
            qy = (m[0, 1] + m[1, 0]) / s
            qz = (m[0, 2] + m[2, 0]) / s
        elif m[1, 1] > m[2, 2]:
            s = np.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2.0
            qw = (m[0, 2] - m[2, 0]) / s
            qx = (m[0, 1] + m[1, 0]) / s
            qy = 0.25 * s
            qz = (m[1, 2] + m[2, 1]) / s
        else:
            s = np.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2.0
            qw = (m[1, 0] - m[0, 1]) / s
            qx = (m[0, 2] + m[2, 0]) / s
            qy = (m[1, 2] + m[2, 1]) / s
            qz = 0.25 * s
        # 归一化（浮点误差防护）
        n = float(np.sqrt(qx * qx + qy * qy + qz * qz + qw * qw))
        if n > 0.0:
            qx, qy, qz, qw = qx / n, qy / n, qz / n, qw / n
        return (float(qx), float(qy), float(qz), float(qw))

    @staticmethod
    def _decompose_mat(mtx) -> tuple:
        """行主序 4x4 矩阵 → (pos(x,y,z), quat(qx,qy,qz,qw), scale(x,y,z))。

        pos 取平移列；scale 取三个基列的长度；旋转 = 归一化基列组成 3x3 再转四元数。

        ⚠️ 反射矩阵（det=-1，如挂载骨架的 Root_BlendBone = Z 镜像 diag(1,1,-1)）
        不能表示为旋转四元数（Shepperd 会给出伪结果，重建后丢失镜像 → 主炮/副炮
        方向翻转）。检测到 det<0 时翻转第一基列并把 scale_x 取负，使旋转部分为
        有效旋转（det=+1），重建时 _quat_to_mat 用负 scale 恢复镜像。
        """
        import numpy as np
        m = np.asarray(mtx, dtype=np.float64).reshape(4, 4)
        pos = (float(m[0, 3]), float(m[1, 3]), float(m[2, 3]))
        c0, c1, c2 = m[0:3, 0], m[0:3, 1], m[0:3, 2]
        sx = float(np.linalg.norm(c0))
        sy = float(np.linalg.norm(c1))
        sz = float(np.linalg.norm(c2))
        eps = 1e-8
        r0 = c0 / (sx if sx > eps else 1.0)
        r1 = c1 / (sy if sy > eps else 1.0)
        r2 = c2 / (sz if sz > eps else 1.0)
        r = np.column_stack([r0, r1, r2])
        # 反射矩阵：翻转第一基列 + scale_x 取负，恢复有效旋转（重建时负 scale 还原镜像）
        if np.linalg.det(r) < 0.0:
            sx = -sx
            r = np.column_stack([-r0, r1, r2])
        q = AssetsCacheService._mat3_to_quat(r)
        return pos, q, (sx, sy, sz)

    @staticmethod
    def _quat_to_mat(qx, qy, qz, qw, sx, sy, sz, px, py, pz):
        """解码值（四元数 + 缩放 + 位置）→ 行主序 4x4（渲染器/挂载定位用）。"""
        import numpy as np
        x, y, z, w = float(qx), float(qy), float(qz), float(qw)
        m = np.array([
            [1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - z * w), 2.0 * (x * z + y * w), 0.0],
            [2.0 * (x * y + z * w), 1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - x * w), 0.0],
            [2.0 * (x * z - y * w), 2.0 * (y * z + x * w), 1.0 - 2.0 * (x * x + y * y), 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ], dtype=np.float32)
        m[0:3, 0] *= float(sx)
        m[0:3, 1] *= float(sy)
        m[0:3, 2] *= float(sz)
        m[0, 3] = float(px)
        m[1, 3] = float(py)
        m[2, 3] = float(pz)
        return np.ascontiguousarray(m, dtype=np.float32)

    @staticmethod
    def _strings_dict(db) -> dict:
        """字符串表 → {hash: name}（渲染集 shape 反查）。"""
        from utils.asset_utils import build_strings_dict
        return build_strings_dict(db)

