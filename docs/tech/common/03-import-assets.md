# 03 · 导入 assets.bin → `assets_data*.db`

> 入口：`import` 的后台任务 `_populate_assets_cache()`
> → `services/assets_cache_service.populate()`
> schema：`resources/database/{lesta,wargaming}/assets_database.sql`（`ASSETS_SCHEMA_VERSION = 19`）
>
> **单版本镜像 + 按模型增量**（2026-10-28 重构）：表里**不再有 `bin_folder` 主键列**，
> 改按 `model_id` 组织。原因：`bin_folder` 进主键意味着 build 一变，未变动的那上百万行
> 也要 UPDATE 到新 build，等于全量重写 —— 增量就无从谈起。build 号记在 `meta.build`。

## 1. 为什么要这个库

`content/assets.bin` 约 **217 MB**（Lesta）/ 更大（WG），是 PrototypeDatabase：
材质、视觉、骨架、模型路径全在里面。每次导出都现场解析要几十秒到几分钟，
所以导入时**一次性提取成 SQLite**，导出只读库（`assets_data*.db`）。

派生库体积：Lesta 554 MB / WG 623 MB —— 大头是两张骨骼表与 `render_set`
的 `nodes` JSON（见 §3）。按数据类型分两类：

| 类 | 表 | 体量 | 增量策略 |
|---|---|---|---|
| 按模型 | `render_set`、`skeleton_mounts`、`skeleton_bones` | 274 MB | **只重建 `models` 集合里的模型**（先按 `model_id` DELETE，再只解析这些记录） |
| 全局 | `material_full`、`mfm_textures`、`shape_names`、`exteriors`、`camouflages`、`camo_*` | 约 30 MB | 每次同步全刷 —— 一个 mfm / 一个 shape 哈希被多船共用，无法归属到单个模型 |

`populate(models=None)` 是全量；给了集合就是增量。判定在上层
（`processor_service` 拿 idx 签名比 `model.sig` 得出变更集）。

**只存路径与参数，不存贴图字节**：贴图在导出时按 `.dd0 → .dd1 → .dd2 → .dds`
分层从 pkg 实时读。

## 2. 十个步骤

| # | 步骤 | 产出表 |
|---|---|---|
| 1 | 骨架挂点 + 完整骨骼（bind pose 按 parent 累积到世界） | `skeleton_mounts`、`skeleton_bones` |
| 2 | 视觉渲染集：`shape → 材质 / mfm` | `render_set` |
| 3 | 材质：贴图路径 + 完整参数 | `mfm_textures`、`material_full` |
| 9 | Exterior 实体（**GameParams 直读**，split 仅回退） | `exteriors` |
| 10 | 包根 `camouflages.xml`（三段） | `camouflages`、`camouflage_color_schemes`、`camo_ship_groups` |

> 步骤号直接取自源码注释（4–8 未使用，历史上属于武器/涂装段，导出不需要）。

另有一步独立于编号流程：从 assets.bin 字符串表提取 `*.vertices` 名哈希 →
`shape_names`（LOD/crack 按名字兜底时用）。

## 3. 关键表与语义

### `render_set`（核心，约 40 万行）

> 记录解析代码在 `services/proto_render_sets.py`：`render_sets_lesta_one()` 与
> `render_sets_wg()` 是两套**互不通用**的布局（0x40/0x50 vs 0x70/0x28），
> `populate()` 只负责扫树、过滤模型、调 `add_row` 写库。


| 列 | 含义 |
|---|---|
| `geom_path` + `shape` | 主键（`geom_path` 前缀索引足够快：带模型过滤的 IN 查询 4.65 ms） |
| `model_id` | 模型目录名（父目录），**冗余存一份**：增量重建要按模型整片删，SQL 里没法从路径反推 |
| `material` | 材质名（如 `TL2_SHIPMAT_INDEXED_PBS_Hull`） |
| `mfm` | 材质文件 VFS 路径 |
| `damage` | 1 = 破损/断面或 LOD 变体（两者混在一起，靠 `shape` 名再分） |
| `skinned` | 该渲染集是否蒙皮（Korabli 渲染集项 +0x0C） |
| `nodes` | **蒙皮调色板节点名 JSON 数组，保留 `.visual` 原始顺序** |

> `nodes` 顺序是硬约束：顶点骨骼索引 = 调色板 slot × 3，顺序被打乱会把权重
> 绑到别的骨骼（历史 bug：`Mk56Shape` 的 `Rotate_Y` 权重落到 `Root`）。
> 早期实现用 `sorted(set(...))` 入库，v17 起改为保序。

写入用**流式合并**：边解析边按主键折叠到字典，解析完分块 `INSERT`，
避免 40 万行攒列表占几百 MB。

### `material_full`（材质完整信息）

| 列 | 含义 |
|---|---|
| `shader_id` | `0x0009xxxx` = INDEXED，`0x0005xxxx` = PBS |
| `family` | `pbs` / `indexed` / `other`（由 fx 路径判定，shader_id 兜底） |
| `material_hash` | fx 资源 selfId → 解析成 `fx_path` |
| `fx_path` | 如 `shaders/std_effects/PBS_ship.fx` |
| `textures` | `{贴图键: VFS 路径}` JSON（最多 9 张） |
| `indexed` | `{数组名: [[x,y,z,w] × 196]}` JSON |

INDEXED 数组提取要点（`assets_cache_service.populate()` 的材质入库段）：
- 数组长度由 `material_array_lengths` 推断（不能按固定 196 读）；
- 每项 `parse_vec4` 后 `round(..., 6)`，控制 JSON 体积；
- 字段名来自字符串表（`sdict.get(nid)`），拿不到才退化成 `vec4_<i>`。

### `exteriors`（涂装/皮肤条目）

数据源已改为 **GameParams 直读**（`core.gameparams.exterior_entities`），
`data/split/Exterior/*.json` 仅作回退 —— 因为 split 是导入临时产物，
且后台线程与主流程写 split 有竞态，失败还会被 `except: pass` 吞掉。

`data_json` 只存 10 个涂装相关键：`camouflage / unpeculiarCamouflage /
unpeculiarCamouflageColorSchemeId / hullConfig / nodesConfig /
peculiarityModels / renderSetsStates / miscFilter / planesConfig / decalsEnabled`
（2.5 MB vs 整份实体 7.7 MB）。

### `skeleton_mounts` / `skeleton_bones`

解码后的 `pos (x,y,z)` + `quat (qx,qy,qz,qw)` + `scale`，**行主序渲染空间**。
挂点用 `skeleton_mounts`（`HP_*`），骨骼用 `skeleton_bones`（已按 parent 累积）。
`SkeletonExtender` 的 `MP_/SP_/EP_` 挂点由第二步补入（local 即船空间）。

## 4. 两服差异

| 项 | Lesta | WG |
|---|---|---|
| 库文件名 | `assets_data.db` | `assets_data_wg.db` |
| Prototype 类型数 | **12**（多 `ModelFbxPrototype`） | **10** |
| Material item_size | `0x88` | `0x78` |
| Visual item_size | `0x40` | `0x70` |
| Model item_size | `0x20` | `0x28` |
| blob index → 类型映射 | 与 WG **完全不同** | 参考 wowsunpack |
| INDEXED 材质数 | 192 | **0** |

类型表按 magic 识别，**不能按 blob index 套用另一服的表**
（`uncode_assets/types.py` 顶部注释）。类型查询走 thread-local 的当前服务器上下文。

## 5. 实测规模（2026-10-30，全量 `--rebuild`）

| 指标 | Lesta | WG |
|---|---|---|
| `assets_data*.db` | 554 MB | 623 MB |
| `render_set` | 402,395 | 388,129 |
| `skeleton_mounts` | 483,925 | 630,354 |
| `skeleton_bones` | 812,395 | 876,588 |
| `material_full` | 29,688 | 31,487 |
| `mfm_textures` | 27,475 | 30,770 |
| `exteriors` | 1,915 | 2,362 |
| `camouflages` | 385 | 505 |
| `camo_ship_groups` | 1,024 | 1,076 |

> 涂装三项（`camouflages` / `camouflage_color_schemes` / `camo_ship_groups`）
> 在 2026-10-30 之前**恒为 0** —— `populate()` 里 `Path(game_dir)` 的 `Path`
> 被误删（`from pathlib import Path` 缺失），异常被 `except` 吞掉。修好后两服
> 分别入库 385 / 868 / 1,024 与 505 / 1 / 1,076 条。

## 6. 已解决：库与客户端 build 不一致

旧版把 `bin_folder` 当主键维度，库里的 build 与客户端 `bin/` 对不上时
`shape_names` 会查空。schema v19 起 build 只记在 `meta.build`，不再参与过滤，
「库过期」只会让 `import` 走一次全量，不会查空。
