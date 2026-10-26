# 07 · 装甲模型（GLB + armor.json/bin）

> 入口：`cli.py --armor` →
> `ArmorScene.build(geom.armor_meshes)` →
> `export_service.export_armor_glb()` + `armor_export.write_armor_files()`

## 1. 装甲从哪来

`.geometry` 的 `ModelPrototype` 里带 BVH 装甲条目（16 字节/条），解析成
`ArmorTriangle`（3 顶点 + 3 法线 + `material_id` + `layer_index`）。
两类来源：

- **船体装甲**：来自 `<folder>.geometry` 主文件（分段文件通常没有）；
- **挂载装甲**：炮塔/副炮等模型自带的装甲，用挂点矩阵变换到舰船空间。

`_build_armor_mesh()` 逐三角形：

1. `_match_thickness(armor_thickness, material_id, layer_index)` 查厚度
   （数据源：ship 快照里的装甲厚度字典）；
2. **厚度 ≤ 0 的三角形直接剔除**（0mm = 无厚度数据，游戏里也不显示，
   留着会与有效装甲叠色）；
3. 颜色 = `thickness_to_color(thickness)`（对齐游戏内装甲查看器配色，alpha 0.8）；
4. 材质名 = `collision_material_name(material_id)`；
5. zone = `zone_from_material_name(mat_name)`；
6. `plate_key = (zone, mat_name, round(thickness*10))`。

## 2. ArmorScene（渲染与导出共用）

`models/armor_scene.py` 把各 `ArmorMesh`（本地坐标）聚合成**世界空间三角形汤**：

| 字段 | 内容 |
|---|---|
| `world_positions` / `world_normals` / `colors` | 扁平化三角形数据（舰船空间） |
| `tri_info` | 每三角形的 `ArmorTriangleInfo`（厚度/材质/zone/layers/hidden/plate_key） |
| `zones` | 三级索引 `zone → 部件名 → 厚度(tenths) → [三角形下标]` |
| `plate_ids` / `plate_keys_by_id` | 每三角形的板块 id |
| `mesh_tri_range` / `mesh_names` / `mesh_aabb` | 按源 ArmorMesh 的区间与拾取 AABB |
| `edge_positions` / `edge_tris` | 板块边界描边（网格边界 ∨ 板块交界 ∨ 折角） |

边界线判据（对齐 wows-toolkit）：
- 顶点量化 `QUANT = 10000` 后按边去重；
- 相邻面法线点积 `< CREASE_DOT = 0.7`（≈45°）视为折角。

拾取：`ray_pick()` 用 Möller–Trumbore，先按 per-mesh AABB 预筛。

## 3. 装甲 GLB

`GlbArmorOptions`：

| 字段 | 默认 | 说明 |
|---|---|---|
| `visible_only` | `True` | 只导查看器当前可见板块（CLI 无 mask → 全部） |
| `thickness_colors` | `True` | 厚度颜色；False 时统一灰 `0.72` |
| `armor_alpha` | `1.0` | <1 时 `alphaMode=BLEND` |
| `double_sided` | `True` | |
| `flip_winding` | **`False`** | 装甲源绕序与渲染网格相反，负 Z 镜像后**已朝外**（实测 0.72），翻转反而朝内 |

结构：**扁平**，没有空容器节点。每块板一个节点 + 一个 mesh：

- 节点名 = `"<zone>|<部件> <厚度>mm"`；
- 节点矩阵平移到该板包围盒中心，顶点改写成局部坐标（世界位置不变）；
- 顶点色 = 厚度颜色（`COLOR_0`），无贴图、无 PBR 参数；
- 材质 extras / mesh extras 都带
  `{zone, material, thickness_mm, hidden}`，mesh 多一个 `triangle_count`；
- 顶层 `asset.extras = {game_key, display_name, model_folder, content:"armor",
  visible_only, wows_export_version: 2}`。

## 4. `armor/<ship>.json` + `.bin`

给网页用的独立索引（schema **`wows-armor/1`**）：

```json
{
  "schema": "wows-armor/1",
  "ship": "PBSD511_Waterloo",
  "build": "8867689",
  "space": "ship",
  "bounds": {"min": [...], "max": [...]},
  "triangle_count": 12345,
  "plates": [
    {"id": "Citadel|Belt 300mm", "zone": "Citadel", "material": "Belt",
     "thickness_mm": 300.0, "component": "船体", "hidden": false,
     "tri_start": 0, "tri_count": 120, "bounds": {"min": [...], "max": [...]}}
  ],
  "zones": {"Citadel": {"Belt": [200, 250, 300]}},
  "buffers": {
    "bin": "armor/<ship>.bin",
    "positions": {"offset": 0, "count": N},
    "normals":   {"offset": ..., "count": N},
    "plate_ids": {"offset": ..., "count": T}
  }
}
```

`.bin` 是**三段直接拼接**：`float32 positions` + `float32 normals` +
`uint32 plate_ids`。三角形顺序按 `zone → 部件 → 厚度` 排序（与 `plates`
的 `tri_start/tri_count` 对应），网页按 `offset` 切片即可。

## 5. 与渲染 GLB 的关系

- 两者**完全独立**：渲染 GLB 不含装甲，装甲 GLB 不含舰体；
- 共享同一 `ArmorScene` 与同一镜像约定，所以导入 Blender 后位置天然对齐；
- `--armor` 同时产出三种文件（glb / json / bin），网页用 json+bin，
  Blender 用 glb。
