# 03 · 导入 assets.bin → `assets_data*.db`

> 入口：`import` 的后台任务 `_populate_assets_cache()`
> → `services/assets_cache_service.populate()`
> schema：`resources/database/{lesta,wargaming}/assets_database.sql`（`ASSETS_SCHEMA_VERSION = 18`）

## 1. 为什么要这个库

`content/assets.bin` 约 **217 MB**（Lesta）/ 更大（WG），是 PrototypeDatabase：
材质、视觉、骨架、模型路径全在里面。每次导出都现场解析要几十秒到几分钟，
所以导入时**一次性提取成 SQLite**，导出只读库（`assets_data*.db`）。

派生库体积：Lesta 554 MB / WG 623 MB —— 大头是两张骨骼表与 `render_sets`
的 `nodes` JSON（见 §3）。

**只存路径与参数，不存贴图字节**：贴图在导出时按 `.dd0 → .dd1 → .dd2 → .dds`
分层从 pkg 实时读。

## 2. 十个步骤

| # | 步骤 | 产出表 |
|---|---|---|
| 1 | 骨架挂点 + 完整骨骼（bind pose 按 parent 累积到世界） | `skeleton_mounts`、`skeleton_bones` |
| 2 | 视觉渲染集：`shape → 材质 / mfm` | `render_sets` |
| 3 | 材质：贴图路径 + 完整参数 | `mfm_textures`、`material_full` |
| 9 | Exterior 实体（**GameParams 直读**，split 仅回退） | `exteriors` |
| 10 | 包根 `camouflages.xml`（三段） | `camouflages`、`camouflage_color_schemes`、`camo_ship_groups`、`camo_thumbs` |

> 步骤号直接取自源码注释（4–8 未使用，历史上属于武器/涂装段，导出不需要）。

另有一步独立于编号流程：从 assets.bin 字符串表提取 `*.vertices` 名哈希 →
`shape_names`（LOD/crack 按名字兜底时用）。

## 3. 关键表与语义

### `render_sets`（核心，39～40 万行）

| 列 | 含义 |
|---|---|
| `bin_folder` + `geom_path` + `shape` | 主键 |
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

INDEXED 数组提取要点（`assets_cache_service.py:905-935`）：
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
| Model item_size | `0x28` | `0x20` |
| blob index → 类型映射 | 与 WG **完全不同** | 参考 wowsunpack |
| INDEXED 材质数 | 192 | **0** |

类型表按 magic 识别，**不能按 blob index 套用另一服的表**
（`uncode_assets/types.py` 顶部注释）。类型查询走 thread-local 的当前服务器上下文。

## 5. 实测规模（2026-10-26）

| 指标 | Lesta | WG |
|---|---|---|
| `assets_data*.db` | 554 MB | 623 MB |
| `render_sets` | 399,008 | 392,305 |
| `material_full` | pbs 16,357 / indexed 192 / other 12,772 | pbs 19,708 / other 12,040 / indexed 0 |
| `exteriors` | 1,915 | 2,362 |
| `camouflages` | 384 | 515 |
| `shape_names` | 有（受 `bin_folder` 精确匹配影响） | 同上 |

## 6. 已知不一致（导入后要复查）

`assets_data*.db` 的 `meta.bin_folder` 必须与 `bin/` 实际目录一致。
WG 当前库里是 **13488861**，而客户端 `bin/` 是 `13187581` / `13357625`：
- `meta` 只用来展示，不影响导出；
- 但 `shape_names` 按 `bin_folder` 精确过滤 → **查空**，LOD/crack 的名字兜底失效
  （几何侧改用 `render_sets` 自带 `shape`，所以导出的 LOD 选择仍然正确）。

客户端更新后重跑一次 `import` 即可修好。
