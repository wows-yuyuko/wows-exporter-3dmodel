# 06 · 渲染模型 GLB（舰体 + 挂载）

> 入口：`export_service.export_render_glb(geometry, path, options)`
> 产物：`geometry/<ship>.glb`（+ 外置 `textures/*.webp`）

## 1. 选项与默认值

`GlbRenderOptions`（`export_service.py:120` 起）：

| 字段 | 默认 | 作用 |
|---|---|---|
| `embed_textures` | `True` | 为 False 时不写贴图（`--no-textures`） |
| `textures_dir` / `texture_uri_prefix` | `""` / `../textures` | 外置贴图目录与 glTF 里的相对路径 |
| `texture_format` | `webp` | 仅 WebP（KTX2 已弃用） |
| `texture_max_size` | `1024` | 贴图最大边长（`--texture-size`） |
| `export_mounts` | `True` | 是否导出挂载 |
| `include_damage` | `False` | 破损/断面几何（`--damage`） |
| `include_decals` | `False` | 平面贴花（`--decals`） |
| `double_sided` | `True` | 全部材质双面（绕序不统一也不丢面） |
| `flip_winding` | `True` | 统一翻转绕序（镜像后 CW 需转 CCW） |
| `export_skins` | `True` | 标准 glTF skin（顶点组 + 骨架） |
| `bone_length` | `0.2` | 合成根骨骼的骨尖长度（0 = 不建根骨） |
| `tip_bones` | `False` | 每根骨骼单独加 `_tip`（顶点组翻倍） |
| `split_shapes` | `True` | 按 `shape_spans` 拆网格（默认；`--merge-shapes` 关闭） |

> CLI 的 `--merge-shapes` / `--split-shapes` 是互斥组，默认走拆分；`--split-shapes` 只是把默认行为写明确，见 `10-cli.md`。

## 2. 坐标与变换

- 顶点在几何阶段已转成渲染空间（右手系），导出不再做空间变换；
- 网格顶点**烘焙到最终世界坐标**，网格节点自身不带变换；
- 空物体节点携带真实变换：`HP_*` / `MP_*` 用挂点矩阵，
  舰体分段恒等（源数据本就在舰船坐标系）；
- 装甲单独一套规则（`07-glb-armor.md`）。

## 3. 节点层级与命名（对齐 BigWorld 插件的导入样式）

```text
Scene Root                                wows_export_version = 5
├─ Bow                                    ← 舰体分段（部件空物体）
│  ├─ Bow_wire                            ← .visual <node>（按名称前缀嵌套）
│  │  └─ Bow_wireShape                    ← 网格名 = <vertices> 去 ".vertices"
│  └─ BowShape
├─ MidFront / MidBack / Stern …
├─ HP_JGM_1                               ← 挂载节点（挂点矩阵）
│  └─ Roll_Back1_BlendBone
│     └─ TurretShape
└─ <部件>_bones                           ← skins 模式：骨骼容器（恒等变换）
```

- 网格名唯一化：重名自动追加 `#2`、`#3`；
- 分段内按 `shape_spans` 拆（默认），`--merge-shapes` 时整段一个网格
  （接缝变少，但会丢面，见 known-issues）。

## 4. 蒙皮（默认开启）

| 项 | 做法 |
|---|---|
| 属性 | `JOINTS_0`（4×u8 → 骨骼索引，= 调色板 slot × 3）+ `WEIGHTS_0`（4×f32，已归一化） |
| skin | `skin.joints` 按 `.visual` **原始调色板顺序**排；joint 节点名 = visual 骨骼名 |
| bind | bind 矩阵先 `M4 @ bind @ M4` 共轭到渲染空间（M4 = Z 镜像对角阵） |
| 骨骼容器 | `<部件>_bones` 空物体，恒等变换 —— 挂在带挂点矩阵的部件下会让 Blender 重复施加父变换，骨骼跑到船外（实测偏差 6–10 单位） |
| 骨骼显示尺寸 | 每个 skin 加一根 `<网格名>_root`（恒等）+ `_tip`（+Y 0.2 m）→ 根骨长 = 0.2，其余继承 → Blender 的骨骼球尺寸统一 |

`export_skins=False` 时改为写自定义属性 `_JOINTS_0` / `_WEIGHTS_0`（FLOAT，
避免 uint8 被 Blender 当 BYTE_COLOR 归一化）+ `mesh.extras.bone_palette`，
文件里没有 skin/joints，Blender 导入后是干净的空物体层级。

## 5. 材质映射

按 `.mfm` **声明的键**映射，不做伪造（`export_service.py:1145` 起）：

| WoWS 键 | glTF 槽 | 变换 |
|---|---|---|
| `diffuseMap` / `g_albedoMap` | `baseColorTexture` | 原样 |
| `normalMap` / `g_normalMap` | `normalTexture` | 补 Z |
| `metallicGlossMap` / `g_mgMap` | `metallicRoughnessTexture` | G ← 1−gloss，B ← F0 权重，R = 255 |
| `ambientOcclusionMap` | `occlusionTexture` | 原样 |
| `detailMap` / `artMap` / 图集数组 | ❌ 只记 extras | glTF 无等价物 |

**INDEXED 族例外**：整块 PBR 通道（normal / MR / AO）**全部跳过**，不是只跳 normal。
需要标准 PBR 时改用 `--bake-indexed`（烘出 baseColor/normal/MR，见
`05-materials-textures.md` §7.5）—— 那时不再走"跳过"分支。
原因是这些贴图是 `*Array` 图集，必须按 `materialIdMap` 逐像素选层才能取对，
硬套 `normalTexture` 只会得到错误的一层。所以 INDEXED 材质的
`slots` 是**空对象**（实测 Waterloo 的 `TL2_SHIPMAT_INDEXED_PBS_Hull`：`slots = {}`），
颜色由 `wows_indexed*` 的图集 + 数组在网页侧自行合成。

其它固定行为：

- `alphaMode`：`opaque=False` → `BLEND`，否则 `OPAQUE`；
- `doubleSided = True`（全部材质）；
- `baseColorFactor = [1,1,1,1]`（颜色只在贴图里）。

### 材质 extras（网页侧靠它还原）

```json
{
  "component": "hull",
  "texture_path": "content/.../xxx_a.dds",
  "wows_textures": { "正常贴图键": "VFS 路径" },
  "wows_layer_textures": { "artMap|materialIdMap|composite": <texture index> },
  "wows_indexed": { "arrays": {...}, "offset": [x,y], "grid": [w,h] },
  "wows_indexed_textures": { "albedoArray": {uri, tile, cols, rows, count}, ... }
}
```

- `wows_layer_textures` 是**图集/单图**（artMap、materialIdMap），
  与 `wows_textures`（原始 DDS 路径清单）不是一回事；
- INDEXED 的 6 个数组落盘为 `textures/indexed_<sha1>.bin`，
  `wows_indexed.arrays[name] = {bin, dtype: "float32", shape: [196,4]}`。

## 6. 顶点属性

| 属性 | 说明 |
|---|---|
| `POSITION` / `NORMAL` | f32×3 |
| `TEXCOORD_0` | f32×2（主 UV） |
| `TEXCOORD_1` | 格式含第二套 UV 时 |
| `TANGENT` | 格式含 `tb` 时（f32×4，W 由法线×切线×副法线符号算出） |
| `_T4` / `_EXTRA` | 原始 4 字节按 VEC4 uint8 原样保留（glTF 无语义，供追溯） |

## 7. 跳过规则与统计

| 条件 | 结果 |
|---|---|
| `is_crack` 且未开 `--damage` | 跳过（破损断面） |
| 材质/网格/shape 名含 `decal` 且未开 `--decals` | 跳过（平面贴花） |
| `is_wire` | 跳过（记录 `skipped_meshes`） |
| 名字含 `perforation` | 跳过 |
| `p.size == 0` 或 非 3 的倍数 | 跳过并计数 |
| 贴图解码失败 | 保留几何，`skipped_textures += 1`，`warnings` 记账 |

## 8. 写盘方式

1. 先写 `<path>.tmp`，成功后 `os.replace()` 原子替换（`export_service.py:927`）；
2. `pygltflib.save_binary()` 返回 False 视为失败并抛 `RuntimeError`；
3. 所有 bufferView 都追加到单一二进制 blob（GLB 单文件，无外链 bin）。

## 9. 实测（默认参数）

| 船 | 平台 | GLB | meshes | 三角形 | 材质 | 贴图 |
|---|---|---|---|---|---|---|
| Waterloo | Lesta | 18.0 MiB | 731 | 297,425 | 15 | 58 |
| 大和 | Lesta | 17.2 MiB | 422 | 245,767 | 78 | 256 |
| 大和 | WG | 22.8 MiB | 429 | 261,748 | 76 | 253 |

体积构成（Waterloo）：几何 18.0 MiB + 外置贴图 3.7 MB；
大和贴图 3.3–3.5 MB。**体积大头是几何**，不是贴图。
