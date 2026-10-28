# 04 · `.geometry` → HullMesh / MountMesh

> 入口：`GeometryService.load_ship()`（`services/geometry_service.py`）
> 解析器：`models/geometry_parser.py`；空间变换：`models/geometry_transform.py`

## 1. 文件发现

1. 由 `ShipInfo.model_folder`（如 `BSD100_Waterloo_1956`）拼 VFS 模式
   `content/**/<folder>/<folder>*.geometry`；
2. 同路径+大小的条目去重；
3. **主文件**（`<folder>.geometry`，无分段后缀）单独挑出：它常含烟囱等多余模型，
   不作为船体来源，但**可能带船体装甲**（`CM_PA_*.armor`，如大和 12,487 三角），
   所以主文件仍会被解析，只取 `armor_models`；
4. 其余分段文件（`_Bow` / `_MidFront` / `_MidBack` / `_Stern` …）逐个解析合并；
5. 只有主文件、没有分段 → 明确报错（不回退用主文件当船体）。

`ShipGeometry` 的渲染集索引来自 `assets_data.db` 的 `render_sets`，
按 `model_folder` 取该船全部记录（含跨分段的同名 shape）。

## 2. 二进制格式

```text
MergedGeometryPrototype
  0x48  Header（mapping 数、各段 relptr）
  0x10  MappingEntry × N        (mapping_id, merged_buffer_index, texel_density, items_offset, items_count)
  0x20  VerticesPrototype × N   (format 字符串指针, stride, count, is_skinned, is_bumped, data relptr)
  0x10  IndicesPrototype × N    (index_size, count, data relptr)
  0x20  ModelPrototype × N      (碰撞/装甲 BVH)
```

- **顶点压缩**：数据块以 `ENCD` 魔数开头时用 `meshoptimizer` 解码（失败才提示）；
- **顶点格式字符串**形如 `set3/xyznuvtpc` / `xyznuviiiwwtb_encd`，逐字符解析布局：

| 语义 | 字节 | 说明 |
|---|---|---|
| `x`（含 `yz`） | 12 | POSITION，3×f32 |
| `n` | 4 | NORMAL，packed |
| `uv` / `uv2` | 4 / 4 | 2×f16；格式里跟 `2..9` 时多一套 UV |
| `t`（`tb`） | 4 / 8 | `t` 单独时是未知 4 字节（原样保留为 `t4`）；`tb` 是 tangent+binormal |
| `iiiww` | 8 | 骨骼：3×u8 索引（= 调色板 slot × 3）+ 2×u8 权重，第三权重 = 1-w0-w1 |
| `i` | 4 | 单骨骼索引 |
| `r` | 4 | 未知 4 字节（原样保留为 `extra`） |
| `pc` / `oi` | 0 | 标记位，不占空间 |

> `_encd` 后缀必须在解析前剥掉，否则末尾的 `n`/`d` 会被当成 normal 导致偏移错位。
> Korabli 的 `is_skinned` 标志**恒为 0**，蒙皮只能按顶点格式串判定。

- **装甲**：`ModelPrototype` 的 BVH 是 16 字节条目流 → `ArmorTriangle`
  （3 顶点 + 3 法线 + `material_id` + `layer_index`）。
- **碰撞**：原始 blob，原样保留在 `CollisionModel.data`，不解析。

## 3. 解析空间 → 渲染空间

`.geometry` 输出的是游戏左手系（Z 朝向观察者），渲染与 glTF 需要右手系 Y 向上，
只差一个 Z 镜像 `negz = diag(1,1,-1,1)`（det=-1）。全部变换集中在
`geometry_transform.prepare_render_mesh()`：

1. 位置：`(x, y, -z)`；
2. 法线：按"源绕序 vs 源法线"一致性 `c` 判定——
   - `c > 0.65`：法线取 `-negz`（抵消镜像导致的绕序翻转）；
   - `0.35 ≤ c ≤ 0.65`（绕序混合）：逐三角形拆点、复制顶点并翻转坏面法线；
   - 否则：法线取 `negz`；
3. 索引：拆点时重映射；`uv2 / t4 / extra / tangent / binormal` 随顶点同步复制。

**渲染器与导出共用这一个函数**，保证"查看器看到的 = 导出的"。
这是行为敏感区，改动必须做 A/B 回归（见 `../../todo-backlog.md` P1-5）。

## 4. 组装：HullMesh / MountMesh

### 船体分段

每个分段文件 → 若干 `HullMesh`：

```text
primitives → _split_primitives_by_material()
            ├─ 正常材质 → 按 material 分组 → _merge_hull() → HullMesh
            ├─ damage=1 且名字/材质含 crack → __crack__ 组（is_crack=True）
            ├─ _lodN 变体 → 按 LOD 规则跳过
            └─ __wire__ 材质 → is_wire=True
```

- `HullMesh.name` = `<部件>#<材质>`（导出时按 `#` 前缀拆节点）；
- `positions/normals/indices/uvs` 已是渲染空间；
- `instance_matrices`：同一 shape 的多个节点实例组（`__inst__#K`）→ 一次导出多份；
- 同一材质下不同 shape 的**骨骼调色板不同**时按调色板再拆（`#bonesN`），
  否则顶点组索引会按别人的调色板翻译，权重绑错骨骼。

### 挂载

来源是舰船快照（`entity_snapshots` 的 ship JSON）里的各组件分组
（`A_Hull / A_Artillery / A_SecondaryArtillery / A_ATBA / A_Torpedoes / …`，
遍历全部组，不依赖固定键列表），每个 `HP_*` 键含 `model` 路径。流程：

1. `model` → 挂载模型目录 → 该目录的 `.geometry`；
2. 解析出 primitives → 同样按材质分组 → `MountMesh`；
3. 挂点矩阵来自 `skeleton_mounts`（`HP_*`）与 `SkeletonExtender`（`MP_/SP_/EP_`），
   换算到渲染空间后作为 `model_matrix` / `instance_matrices`；
4. 相同模型+相同矩阵的实例去重（`_dedupe_mount_instances`），其余按实例化导出；
5. 炮塔内部零件用 `Root_BlendBone` 相对炮塔根定位（`_mount_root_blend`）；
6. `miscFilter` 是该挂点的 MP 子设备白名单（全名匹配）——
   **空列表 = 空白名单 = 一个都不显示**（对齐游戏 MiscsController）；
   皮肤可用 `nodesConfig` 覆盖 `model / miscFilter / customMiscs`。

**蒙皮烘焙**：带 `iiiww` 的顶点在加载阶段就按 bind pose 混合到最终位置
（`_apply_skinning`），`skinned_applied=True`。因此：

- 挂载矩阵**不能再乘** `rb`（否则双重镜像 → 朝向翻转，PASB111 的 AGS542 实证）；
- 导出的 `JOINTS_0/WEIGHTS_0` 仍保留原始索引/权重，供 Blender 做顶点组。

## 5. 材质解析（每段一次）

```text
render_sets 里该 (geom_path, mapping_id) → mfm 路径
   ↓ _resolve_material_full(mfm, extractor)
{tech_family, shader_id, material_hash, fx_path,
 textures: {键: (VFS 路径, DDS 字节)}, indexed_params}
   ↓ _apply_declared_color()
HullMesh.tech_family / material_textures / indexed_params / texture_dds
```

- `tech_family` 由 `fx_path` 判定，fx 缺失时退回 `shader_id` 高 16 位；
- INDEXED 用 `albedoArray` 当颜色源，PBS 用 `diffuseMap`；
- **都不声明 → `has_color=False`、`texture_dds=None`**（按透明叠加处理，
  绝不按文件名补全）。

材质细节见 `05-materials-textures.md`。

## 6. 四条过滤规则（都在几何侧，导出只是消费标记）

> 目标 LOD 由 `load_ship(lod=...)` 传入：**默认 2**（CLI 未指定时即为 LOD2，
> 几何体积约为 LOD0 的 60%）；`-1` = 只用 LOD0 高模。

| 规则 | 判据 | 结果 |
|---|---|---|
| **LOD** | `_shape_lod(name)`：`_lodN`（`Bow_lod2Shape`）与 `_lodShapeN`（`JM039_lodShape1`）两种命名都识别 | 目标级无此件 → 回退 LOD0 |
| **最小面数保护** | 目标级三角数 `< max(50, LOD0×5%)` | 回退 LOD0（Waterloo 的 Bow/Stern_lod2Shape 只有 2 个三角形） |
| **辅助几何例外** | 名字含 `wire / patch / perforation / glass / _net` | 不参与面数保护（否则 623 面的线材会被换成 13,015 面） |
| **crack** | `damage=1` 且名字含 `_crack_` 或材质含 `Crack` | 进 `__crack__` 组、`is_crack=True`，导出默认剔除 |

`damage=1` 同时涵盖"真断面"与"LOD 变体"，所以必须先按名字/材质区分再套 LOD：

```python
# _split_primitives_by_material
if p.mapping_id in damage_mids and ('_crack_' in shape or 'Crack' in material):
    → __crack__（导出默认剔除，--damage 保留）
# 其余 damage=1 的走正常 LOD 判定
```

> `wire` / `decal` 在几何侧只打标记，**是否导出由导出层决定**
> （`GlbRenderOptions.include_decals` / `is_wire` 直接跳过）。

## 7. 内存与取消

- 每个 `.geometry` "读取 → 解析 → 合并 → `del data`"，峰值只与单文件相关；
- 共享图集（`CIT000_1k_ship_tiles_*` 20 MB+）按 base 路径缓存，
  否则同一材质反复 Kraken 解压（实测 30 s/个，会让加载看起来卡死）；
- `cancel_event` 在文件、图元、装甲三角形批次边界检查，抛 `TaskCancelled` 正常退出；
- `partial_cb` 在船体解析完、挂载开始前回传快照（`_snapshot_without_mounts`）。

## 8. 数据结构速查

| 对象 | 关键字段 |
|---|---|
| `ShipGeometry` | `hull_meshes / mounts / armor_meshes / collision_models / bounds_min / bounds_max / stats` |
| `HullMesh` | `name / positions / normals / indices / uvs / uv2 / tangents / t4 / extra / material / tech_family / material_textures / indexed_params / is_crack / is_wire / instance_matrices / skinned_applied` |
| `MountMesh` | 同上 + `model_folder / component / model_matrix` |
| `ArmorMesh` | `positions / normals / indices / component / plate_key` |
