# 05 · 材质与贴图（.mfm / INDEXED / DDS → WebP）

> 材质解析：`GeometryService._resolve_material_full()`
> 入库：`assets_cache_service` 步骤 3；导出：`export_service.dds_to_png/_to_gltf_*`
> 参考语义：[Wows_Paragrams_Unpack `docs/materials-format.md`] 与 `d3d11_renderer.md` §5.4

## 1. `.mfm` → 材质参数

`.mfm` 是 MaterialPrototype 的明文形态（本仓库有 `uncode_assets/mfm_text.py`
做反向导出，实验性）。入库时只取程序真正需要的三样：

| 字段 | 来源 | 用途 |
|---|---|---|
| `shader_id` | MaterialPrototype 头 | 技术族兜底判定（`0x0009` / `0x0005`） |
| `material_hash` | SelfId | 解析 fx 资源路径（`material_hash` → assets.bin 路径树） |
| 属性表 | `property` 列表 | 贴图（type 4 → 路径）与 vec4 数组（type 7） |

fx 路径**不用固定映射表**：`uncode_assets.mfm_fx.resolve_fx_path(db, hash)`
在 assets.bin 的路径树里按 selfId 反查，客户端更新后自动跟着变。

## 2. 技术族判定

```text
fx_path = resolve_fx_path(...)
family  = fx_tech_family(fx_path, shader_id)     # 优先按 fx
        ↳ fx 解析失败时看 shader_id 高 16 位：0x0009 → indexed，0x0005 → pbs，其余 other
```

| 族 | 判定 | 颜色来源 |
|---|---|---|
| `pbs` | `PBS_ship*.fx` / `ship_material.fx` | `diffuseMap` / `g_albedoMap` |
| `indexed` | `ship_material_indexed.fx`（shader_id `0x0009`） | `albedoArray`（2D 数组图集） |
| `other` | 线材、玻璃、网格等 | 见 §4 |

## 3. 贴图键（稀疏材质原则）

只认 `.mfm` **声明**的键，绝不按文件名补全。INDEXED 材质的 9 张：

| 键 | 典型文件 | 格式 | 用途 |
|---|---|---|---|
| `materialIdMap` | `*_matid.dd0` | BC4 4096²（实际 4096×2048） | R = 材质 ID 0–195 |
| `albedoArray` | `CIT000_1k_ship_tiles_a.dds` | BC1 1024² × 30 层 | 漫反射图集（真正主纹理） |
| `normalArray` | `..._tiles_n.dds` | BC1 1024² × 30 | 法线图集 |
| `MGArray` | `..._tiles_mg.dds` | BC1 1024² × 30 | 金属/光泽图集 |
| `artMap` | `*_art.dd0` | BC7 4096×2048 **单图** | 艺术涂装叠加层 |
| `normalMap` | `*_alpha_n.dd0` | BC7 4096×2048 | **alpha 法线叠加层**（不是标准法线） |
| `ambientOcclusionMap` | `*_ao.dd0` | BC1 | AO |
| `rgbNoiseMap` | `CIC001_splice_rgb_noise_a.dds` | BC1 | 材质 ID 采样抖动用的噪声 |
| `diffuseMap` | `*_a.dds` | BC1 **32²** | 占位（INDEXED 下不是主纹理） |

PBS 材质是另一套 5 张：`diffuseMap / normalMap / metallicGlossMap /
ambientOcclusionMap / detailMap`。

> ⚠️ INDEXED 的 `normalMap` 是 `_alpha_n` 叠加层，与 PBS 的 `normalMap`
> **不是同一个槽**。导出的 PBR 映射会跳过 INDEXED 的 normal（见 `06-glb-render.md`）。

## 4. INDEXED 的 6 个 196×4 数组

`material_full.indexed` 存 `{名: [[x,y,z,w] × 196]}`，渲染/导出按 matId 下标取用：

| 数组 | 语义 | 用法 |
|---|---|---|
| `offsetScaleMatIdArr` | xy = UV 偏移，zw = UV 缩放 | `uv = uv * zw + xy` |
| `rotationMatIdArr` | x = UV 旋转角（弧度）；y/z/w 实测恒 0 | 只用 `x` 旋转 |
| `tileIdxMatIdArr` | x / y / z = albedo / normal / MG 的数组层号 | 图集选层 |
| `albedoTintMatIdArr` | rgb = tint 颜色，w = 褪色阈值（含 999 特殊值） | tint 混合 |
| `albedoToRemoveTintMatIdArr` | rgb = 参考色 | 算曼哈顿距离决定混合权重 |
| `artStrengthMatIdArr` | x = BaseStrength（实测恒 1.0；洋红占位时置 0） | art 叠加强度取 `x` |

上色公式（与游戏/查看器一致）：

```text
k = clamp((|albedo − remove.rgb|₁ + 0.001) / max(tint.w, 1e-4), 0, 1)
c = lerp(tint.rgb, albedo, k)
c = lerp(c, art.rgb, clamp(art.a × artStrength, 0, 1))
```

**洋红占位规则**：无涂装时 `artMap` 是洋红占位图。加载阶段检测到就把
`artStrengthMatIdArr[:,0]` 全部置 0（`geometry_service.py:2825`），否则整船会泛粉
（`CIA000_instances_atlas` 实测）。占位图仍会导出，只是强度为 0。

## 5. DDS 分层读取

同一张贴图在客户端有多个质量层，按 **`.dd0`（最高）→ `.dd1` → `.dd2` → `.dds`**
依次尝试（`_load_texture_tier`）：

- 每个候选都要满足：前 4 字节是 `DDS `，且 `parse_dds()` 能解出 mip 或 layer；
- `.dd0/.dd1/.dd2` 是 **bc7prep** 压缩容器，读取时先由
  `data_extractor/bc7prep.py` 解码成标准 BC7（否则 `mips` 为空 → 上传成黑图）；
- 结果按 base 路径缓存：共享图集 20 MB+ 被大量 INDEXED 材质复用，
  不缓存会每个 mfm 重新 Kraken 解压（30 s/个）。

## 6. DDS → RGBA → WebP

```text
parse_dds(bytes)                       # 头解析：bc_kind / width / height / mips / array_size
  ↓ 取 mip0
texture2ddecoder.decode_bc1/2/3/4/5/7  # 硬件格式 → BGRA
  ↓ 交换通道
(H,W,4) uint8 RGBA
  ↓ 按 kind 变换
color: 原样
normal: 补 Z（WoWS 只存 XY，否则 Blender 里法线全平）
mr:     G ← 255−G（roughness = 1−gloss），B ← R（F0 权重当 metallic），R = 255
  ↓ PIL：超长边缩到 texture_max_size（默认 1024），LANCZOS
WebP quality=85 method=2
  ↓ sha1(webp) 当文件名
textures/<sha1>.webp
```

- 文件按**内容哈希**去重，同图不重复落盘；
- `_write_external_texture` 里还会再按 `texture_max_size` 限一次尺寸
  （`dds_to_png` 已限，这里是兜底）；
- 解码失败返回 `None`：几何照常导出，`report.skipped_textures += 1`。

## 7. INDEXED 数组贴图 → 图集

`add_texture_array_from_dds()`：DX10 头里 `arraySize > 1` 的 DDS 数组 →
逐层解码 → 每层缩到 **256²**（超过 256 才缩）→ 按 `cols × rows` 拼成 WebP 图集。
当前 30 层 → `6 × 5`、1536×1280。

manifest 里记录 `{uri, tile: 256, cols, rows, count}`，网页用
`atlasUV(slice, uv) = (cell + fract(uv)) / grid` 换算。

> 判"是不是数组"只看结构：无 DX10 头 → 单图；`arraySize > 1` → 数组；
> 否则按「单层字节 × 2」兜底。**不能用文件大小**——4096×2048 的单图
> artMap 就有 8 MB，按大小判会把它当数组丢掉（已修，见
> [../../todo-backlog.md](../../todo-backlog.md) P0-1）。

## 7.5 烘焙成标准 PBR（默认开启，`--no-bake-indexed` 关闭）

INDEXED 的正确颜色只有那套 shader 算得出来，Blender / 标准 glTF 查看器打开就是
没有底色的灰模。`--bake-indexed` 把它烘成 baseColor / normal / MR 三张常规贴图
（`services/indexed_bake.py`）。

**不需要光栅化几何**：shader 的每个输入（materialIdMap、artMap、图集采样）都只是
`vUv` 的函数，所以烘焙就是 UV 栅格上的逐像素运算，分辨率可任意选。

| 输出 | 来源 |
|---|---|
| baseColor | 图集 `tile.x` 层 → `offsetScale`/`rotation` 变换 → tint 混合（`albedoTintMatIdArr` 阈值）→ artMap 叠加（`art × artStrength.x`） |
| normal | 图集 `tile.y` 层，保持 XY、补 Z（切线空间） |
| MR | 图集 `tile.z` 层，G=1-gloss、B=F0 权重 |

实现要点：

- **按 matId 分块**：196 个 id 各自一套 UV 缩放，按 id 分组做向量化采样；
- **mip 按采样密度选**：`38` 次平铺 → 一个输出像素跨约 9.5 个源纹素，
  直接用 1024² 原图会摩尔纹，所以按 `1024 / footprint` 先缩对应层再双线性采；
- **materialIdMap 用最近邻缩放**：ID 图做插值会造出不存在的材质 id；
- **分辨率**：默认跟随 materialIdMap 原生尺寸（Waterloo = 4096×2048，实测 13 s），
  `--bake-size 1024` 可降到 1024×512（3 s）；
- 三张图**不再被 `--texture-size` 二次压缩**；材质在 manifest 里变成
  `mode: "pbs"` + `baked: {from: "indexed", size, notes}`。

**代价**：artMap 已混进底色 → 失去实时换涂装能力；shader 末尾的手搓光照
（`×1.65`、`mix(white, 0.06)`）属于渲染，不烘。

**默认开启**（`--no-bake-indexed` 回到图集数组路线）。非 INDEXED 材质不受影响，
`--no-textures` 时不烘。

## 8. 其它技术族

| 族 | 特征 | 导出处理 |
|---|---|---|
| `wire` | 材质名 `__wire__`，GL_LINES 渲染 | 几何侧标 `is_wire`，导出直接跳过（栏杆/索具缺失） |
| `decal` | `SHIP_MESHDECAL_*`，平面贴片 + alpha | 导出默认剔除（`--decals` 保留） |
| `glass` / `grid` / `transparent` | `opaque=False` | 材质 `alphaMode=BLEND`，`doubleSided=True` |
| `perforation` | 穿孔板（INDEXED 图集 `CIT001_perforated_*`） | 不特殊处理，按普通网格导出 |
