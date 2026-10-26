# 待办整理：对照 Wows_Paragrams_Unpack/todo_list

> 来源：`C:\Users\uuz\Documents\GitHub\Wows_Paragrams_Unpack\todo_list\`（7 篇，2026-08-18 ～ 10-26）。
> 那个项目是 **GUI + 自研 D3D11 渲染器 + 数据浏览**，本项目是**纯离线导出**，
> 所以它的待办只有一部分适用。本文只保留「导出侧」相关项，并逐条给出本仓库的
> 现状证据（文件:行）与验收口径。
>
> 与 [known-issues.md](known-issues.md) 的分工：那边记**问题**（现象/根因），
> 这边记**要做的事**（来源/做法/验收）。

## 一、参考文档适用性总览

| 参考文档 | 判定 | 说明 |
|---|---|---|
| `New_function_of_unpack_geo_and_display.md` | 🔶 部分 | 第二章（装甲 UI 重构）全部不适用；第一章的 **GLB 导出设计**已落地，只剩验收项 |
| `New_function_of_fix_indexed_render_and_export_glb.md` | 🔶 部分 | P1（渲染修正）不适用；P2（GLB 导出）我方已实现，但 **INDEXED 烘焙**未做 |
| `New_function_of_camo_switcher.md` | 🔶 部分 | UI/实时切换不适用；**涂装数据探针**与 **INDEXED 数组覆盖**适用 |
| `New_function_of_unpack_assets_bin.md` | ❌ 不适用 | PrototypeDatabase/粒子/VFS 浏览，导出链路不碰 |
| `New_function_of_d3d11_renderer.md` | 🔶 部分 | 渲染器迁移不适用；**§5.4 通道语义**与 **§7.4/7.5 验收规范**适用 |
| `New_function_of_indexed_material_rendering.md` | ❌ 不适用 | 已被上表第 2 篇取代，结论也已落地 |
| `Code_duplicate_and_perf_optimization.md` | 🔶 少量 | analysis/UI 层全不适用；`data_extractor` 与 `utils` 的收敛项共享同一份代码 |

---

## 二、P0：正确性问题（已定位根因，可直接动手）

### P0-1 artMap 没进 manifest —— INDEXED 涂装叠加层全程缺失 ✅ 已修复（2026-10-26）

**现象**：`docs/sample/PBSD511_Waterloo/manifests/PBSD511_Waterloo.json` 里唯一的
INDEXED 材质 `TL2_SHIPMAT_INDEXED_PBS_Hull`，`layers` 只有 `materialIdMap`，
没有 `artMap`。`docs/threejs-example.html` 的 `buildIndexedMaterial()` 读的是
`(m.layers || {}).artMap` → 拿到 `undefined` → `uArt` 为 null → 采样退化成
`(0,0,0,0)` → `am = art.a × 强度 = 0` → `mix(c, art.rgb, 0)` 恒等。
**不会变黑，但游戏里的艺术涂装图案完全不显示。**

**根因（已实测）**：`services/export_service.py:108 _is_dds_array()` 第 112 行

```python
if len(dds) > 8_000_000:
    return True          # ← 用文件大小当"数组"的快捷判据
```

`BSD100_Waterloo_1956_art.dd0` 是 **4096×2048 BC7 单图**，字节数 8,388,756 > 8 MB，
被误判成数组纹理，于是 `_emit()` 的 `layer_tex` 循环（`export_service.py:1179`）
`if _is_dds_array(picked[1]): continue` 直接跳过。

实测反证：

```
artMap   4096x2048 array=1 mips=1 bc=8   →  DX10 arraySize 字段 = 1，数据量正好 1 层
```

即：只要删掉那条大小阈值，后面的「DX10 arraySize > 1」和「(len-148) ≥ slice×2」
两条判据就能正确区分。影响面不止 artMap —— **任何 >8 MB 的单图 DX10 贴图**
（`_alpha_n`、4K PBS diffuse）都会被误判。

**做法**：删掉 `len > 8_000_000` 快捷分支，只保留 arraySize 与层数换算；补一条
4096² 单图 + 30 层数组的回归断言。

**验收**：重导 Waterloo 后 `layers.artMap` 存在；页面加 `?debug=alb` 之外再跑
art 覆盖强度（参考项目的 debug 5 语义），能看出图案区域。

### P0-1 修复与实测（2026-10-26）

改动：`services/export_service.py::_is_dds_array()` 删掉 `len(dds) > 8_000_000`
快捷分支，只保留三条结构判据（无 DX10→单图 / `arraySize>1`→数组 / 单层字节×2
→数组）。**对真实源贴图的判定对照**（Lesta 库，`JSB039_Yamato_1945_Hull.mfm`
与 `BSD100_Waterloo_1956.mfm`）：

| 贴图 | 尺寸 / dxgi | 体积 | 实测层数 | 旧规则 | 新规则 |
|---|---|---|---|---|---|
| Yamato `diffuseMap` | 4096² BC1 | 8.00 MB | 0.5（含 mip） | ❌ 判数组→丢失 | ✅ 单图 |
| Yamato `metallicGlossMap` | 4096² BC1 | 8.00 MB | 0.5 | ❌ 丢失 | ✅ 单图 |
| Yamato `normalMap` | 4096² BC7(98) | 16.00 MB | 1.00 | ❌ 丢失 | ✅ 单图 |
| Waterloo `artMap` | 4096×2048 BC7(98) | 8.00 MB | 1.00 | ❌ 丢失 | ✅ 单图 |
| Waterloo `normalMap` | 4096×2048 BC7(98) | 8.00 MB | 1.00 | ❌ 丢失 | ✅ 单图 |
| Waterloo `albedoArray` | 1024² BC1(71) | 20.00 MB | 40.00 | ✅ 数组 | ✅ 数组 |
| Waterloo `normalArray` | 1024² BC7(98) | 40.00 MB | 40.00 | ✅ 数组 | ✅ 数组 |
| Waterloo `rgbNoiseMap` | 1024² BC7(98) | 2.67 MB | 2.67 | ❌ 漏判为单图 | ✅ 数组 |

即：旧规则**双向都错**——大单图被当数组丢掉，小数组（`rgbNoiseMap` 2.67 MB）
被当单图导出。新规则两侧都对。

**六船重导验收**（默认参数 `--texture-size 1024 --lod 2`，decal 默认剔除）：

| 用例 | 船 | 材质模式 | meshes | 三角 | GLB | 关键槽位 |
|---|---|---|---|---|---|---|
| INDEXED Lesta | `PBSD511_Waterloo` | indexed 1 / pbs 14 | 731 | 297,425 | 18.02 MiB | `artMap` 1024×512 ✅ `materialIdMap` 1024×512 ✅ |
| INDEXED Lesta | `PRSB539_Ural` | indexed 1 / pbs 13 | 1,601 | 1,085,431 | 62.77 MiB | `artMap` 1024² ✅ `materialIdMap` 1024² ✅ |
| PBS Lesta | `PJSB018_Yamato_1944` | pbs 78 | 422 | 249,131 | 17.41 MiB | albedo/ao/mg/normal 均 1024² ✅ |
| PBS Lesta | `PRSB910_Kremlin` | pbs 74 | 371 | 145,532 | 9.86 MiB | 同上 ✅ |
| PBS WG | `PJSB018_Yamato_1944` | pbs 76 | 429 | 264,772 | 22.93 MiB | 同上 ✅ |
| PBS WG | `PRSB110_Sovetskaya_Rossiya` | pbs 74 | 368 | 145,494 | 12.17 MiB | 同上 ✅ |

（WG 无名为 Kremlin 的船，`PRSB110_Sovetskaya_Rossiya` 即克里姆林级。）
修复前后按材质统计（`docs/sample` 是修复前的产物，材质总数一致，可直接对位）：

| 船 | 修复前 | 修复后 |
|---|---|---|
| Lesta 大和 | 59 带 normal / 2 个 `albedo+ao+mg` 缺 normal / 6 / 11（共 78） | 61 带 normal / 6 / 11（共 78） |
| WG 大和 | 58 带 normal / 2 缺 / 5 / 11（共 76） | 60 带 normal / 5 / 11（共 76） |

即每船**恰好 2 个材质（4K 船体/甲板法线）找回 normal**；其余材质本来就没有
normalMap 或贴图小于阈值，不受影响。INDEXED 船的 `layers` 恢复
`artMap` + `materialIdMap`。

**遗留**：backlog 里写的「补一条 4096² 单图 + 30 层数组的回归断言」未做——
本仓库没有测试框架（见 known-issues P2 工程化），暂靠上表的人工实测，
探针脚本留在 `.workbuddy/tmp/probe_dim.py`（未纳入 git）。

### P0-2 贴图去重按 `id(bytes)` —— 假命中会静默串图

**位置**：`export_service.py:465 _GlbBuilder.add_texture_from_dds()`

```python
key = (id(dds_bytes), kind)
```

`bytes` 对象被回收后 `id` 会被下一张贴图复用 → 命中假缓存，**返回另一张纹理的
索引，且不报错**。参考项目在 `renderer/scene.py::_TexturePool` 的 docstring 里
专门写了这个坑（"实测阿基坦另有 8 对贴图内容相同但签名不同被漏去重，其中一对
各 20 MB"），我们是同一类实现。

**做法**：键改成内容签名 —— `(len(dds), hashlib.sha1(dds).digest(), kind)`；
`dds` 动辄几 MB，`sha1` 一次几十毫秒可接受，或先取 `(len, 首尾 4KB)` 做粗筛。

**验收**：同一船连续导两次，贴图文件数与 manifest 引用数一致；随机抽 5 个材质
把 GLB 里的 image 与源 DDS 解码后逐像素比对（MAE ≤ 2/255）。

---

## 三、P1：保真度 / 数据正确性

### P1-1 自发光通道 `_mg.B` 完全没导出

参考 `New_function_of_d3d11_renderer.md` §5.4 定版语义：

| 通道 | 语义 | 我方现状 |
|---|---|---|
| `_mg.R` | F0 混合权重（**不是 metallic**） | ✅ 当 metallic 近似写入（已记录偏差） |
| `_mg.G` | gloss（roughness = 1-gloss） | ✅ 已换算 |
| `_mg.B` | **emissive 掩码** | ❌ 直接丢弃 |

`_to_gltf_metallic_roughness()`（`export_service.py:353`）只处理 R/G，
`add_material(emissive_tex=...)` 这个参数存在（`export_service.py:604`）
但**全仓库没有任何调用点传它**。

**做法**：`emissive = baseColor × _mg.B`，写成 `emissiveTexture` + `emissiveFactor=[1,1,1]`。
注意 glTF 的 emissive 是颜色不是掩码，必须乘底色，否则会亮成白块。

**验收**：`PBSD511_Waterloo` 的舱室窗/航行灯在 three.js 里自发光（关掉环境光仍可见）。

### P1-2 `--skins` 对 INDEXED 船体只导纹理，不导数组覆盖

参考 `New_function_of_camo_switcher.md` §2.1 的 P-A3：**tint / artStrength 是否随
涂装一起变**。我方 `services/skin_export.py:85 export_mat_skins()` 产出的 json 只有：

```json
{ "textures": {...}, "uv_transforms": {...}, "use_color_scheme": true, "colors": [...] }
```

而函数自己的注释写着 "INDEXED arrays and model-replacement skins are handled
separately" —— **没有别处处理**。结果：INDEXED 船体换涂装时，editable 的只有贴图，
`albedoTintMatIdArr` / `artStrengthMatIdArr` 仍是主模型那份。

**做法**：先做 P1-3 的探针确认真实数据行为，再决定皮肤 json 是否加
`indexed_overrides: { artMapLayer, artStrength, tint }`。

### P1-3 涂装数据探针（参考的 P0 清单，直接可用）

| 编号 | 问题 | 为什么要它 |
|---|---|---|
| P-A1 | 一船有几个 camo 材质 / 几张 artMap | 决定 `--skins` 的粒度 |
| P-A2 | camo 是否只换 artMap（共用 albedoArray/normalArray/MGArray） | 决定皮肤能否只换一层 |
| P-A3 | `albedoTintMatIdArr` 是否随 camo 变 | 决定要不要导数组覆盖（P1-2） |
| P-B1 | camo 是否有专属 geometry 变体 | 我方已用 `peculiarityModels` 走这条（`--model-skins`），可对照 |
| P-B2 | 是否存在"camo 专属 mesh"（贴花/装饰按涂装显隐） | 我方**完全没处理**，`--skins` 只改材质 |
| P-C1 | 可读涂装名从哪来 | 我方现用 `IDS_<NAME>` 本地化 + `ext_index`，需确认覆盖率 |

**产出**：`docs/` 下一份探针报告（可直接查 `assets_data.db` 的 `material_full` /
`camouflages`，不需要开客户端）。

### P1-4 wire 网格（栏杆 / 索具 / 天线）完全不导出

`export_service.py:1077`：

```python
if getattr(src, "is_wire", False):
    report.skipped_meshes += 1
    return
```

游戏里 wire 是 `GL_LINES` 渲染。glTF 支持 `primitive.mode = 1 (LINES)`，
Blender / three.js 都能显示。现在 Waterloo 甲板栏杆、拉索全没了，只剩船体光板。

**做法**（二选一，先看数据）：

1. 确认 wire 的索引是不是退化三角形（两个索引重合）→ 能还原成线段对；
2. 若不成立，就按窄带三角形导出（可能偏粗，但比全丢好）。

**验收**：`?debug=wire` 下栏杆可见，且不与船体重叠闪烁。

### P1-5 导出验收缺一条硬标准

参考 `d3d11_renderer.md` §7.4（渲染行为锁定表）+ §7.5（A/B 像素规范：

- 允许：几何边缘 ≤1px 位移；通道 MAE ≤ 2/255，峰值 ≤ 6/255（仅边缘/高频区）
- 不允许：色相偏移、图块错层、条纹伪影、`_mg` 影响基础色、decal 法线消失

我方目前只有人工截图。建议脚本化三组对照（复用本机无头 Chrome 那套）：

| 对照组 | 验收点 |
|---|---|
| crack 剔除前 / 后（`--damage`） | 断面几何消失，其余不变 |
| decal 剔除前 / 后（`--decals`） | 只少贴花片，船体不变化 |
| `--lod 0` / `--lod 2` | 轮廓不塌陷（Bow/Stern 面数保护生效） |

---

## 四、P2：能力与性能（按需推进）

### P2-1 INDEXED → 标准 PBR 烘焙（`--bake-indexed`）

这是参考文档 P2 的核心诉求：**让 Blender 打开就能看到正确颜色**。我方走的是
"图集 + 6 数组 + 自带 shader"路线，代价是：**INDEXED 船体在标准 glTF 查看器里没有
baseColor**。实测 Waterloo 15 个材质，唯一 `baseColorTexture` 为空的就是
`TL2_SHIPMAT_INDEXED_PBS_Hull`。

烘焙规则（与页面 shader 必须一致，否则"看到的 ≠ 导出的"）：

| 输出 | 来源 |
|---|---|
| baseColor | `albedoArray[tileIdx.x]`（经 offsetScale+rotation 变换）→ tint 混合（用 `albedoToRemoveTintMatIdArr` 算权重）→ artMap 叠加（`art.a × artStrength`） |
| normal | `normalArray[tileIdx.y]` + `_alpha_n` 叠加 |
| MR | `MGArray[tileIdx.z]`（G→roughness=1-gloss，R→metallic） |

代价：4096×2048 matId 逐像素 + 30 层图集采样，单船几十秒；输出 2048² 控制体积。
建议作为**可选开关**，默认仍走 arrays 路线（体积小、可实时换涂装）。

### P2-2 decal 的处理策略（当前：默认整片丢弃）

参考项目用 **`mode 9`**：decal 只当**法线叠加层**（读场景法线缓冲，把 decal 法线
合成进去再写回），**不写颜色**，所以船体 albedo 完整保留。

我方 `include_decals=False` 直接丢，Waterloo 少了 960 个贴花 / 383,536 三角（62%）。
观感确实干净了，但舱门、航行灯、通风口、皇家徽章这些细节也没了。

可选路线（不急）：

- **A**：维持现状（默认丢），`--decals` 保留现状（会穿插）；
- **B**：新增 `--decals blend` —— 贴花导出为独立节点、材质改 `BLEND` + `alphaMode`，
  查看器侧加 `polygonOffset` / 排序，节点名带 `decal` 便于单独开关；
- **C**：烘焙进 baseColor（需要把 decal 投影到船体 UV，成本最高，不推荐首版）。

### P2-3 `--model-skins` 每套皮肤重新 `load_ship`

实测 8 套换模型皮肤耗时 **3 分 01 秒**，瓶颈是每套都完整加载一次几何
（与数据源无关）。做法：按 `(ship_key, lod, include_damage)` 缓存 `ShipGeometry`，
皮肤只替换 mfm / 纹理 / 模型替换节点。预期降到 1 分钟内。

### P2-4 batch 每船重启进程 / 无增量导出

已在 [known-issues.md](known-issues.md) 的 P1 里记录（每船重新打开 DB、
`--force` 只能全量重导）。参考项目无此需求，无参考实现。这里只做交叉引用。

### P2-5 参考页 INDEXED shader 的 5 处近似

`docs/threejs-example.html::indexedShader()`：

1. `uMatIdSize` 写死 `2048×1024`，而 `--texture-size 1024` 导出的 matIdMap 实际是
   **1024×512** → 四邻采样偏移错一倍（材质块边缘会串色）
2. 缺 matId 采样前的**噪声抖动 + texel 中心量化**（HLSL 是 `uv*48` 抖动 + `floor(x*size+0.5)`）
3. 缺 `_alpha_n` 法线叠加层
4. artMap 未下发（= P0-1）
5. 光照是手搓近似（`col *= 1.65`、混 6% 白），不是参考的 `pbr_light` / `studio_light`

### P2-6 判定逻辑分散（技术债，这一轮已经疼过）

`_lod` / `_crack` / `decal` 三套判定现在分散在：

- `geometry_service._lod_skip()`（LOD 级别 + 最小面数保护）
- `geometry_service._split_primitives_by_material()` 里的 `__crack__` 分流
- `export_service._is_decal()`（材质名 / 网格名 / shape_spans 三级）

最近四轮改动（crack → decal → 面数保护 → 遮挡体回退）每次都在这三处来回改。
建议抽 `services/mesh_filters.py`：`shape_lod()` / `is_crack()` / `is_decal()` /
`is_wire()`，几何层与导出层共用同一份判据。

### P2-7 补 `tools/blender_wows_vertex_groups.py`

`export_service.py:33 / :172` 的注释写着"需要顶点组但不要骨架时……再用
`tools/blender_wows_vertex_groups.py` 还原"，但**本仓库没有 `tools/` 目录**。
参考项目里是 130 行的独立脚本（读 `_JOINTS_0` / `_WEIGHTS_0` + `bone_palette`
重建顶点组，不建 Armature）。`export_skins=False` 模式下没有它，这套数据就是死的。

---

## 五、明确不做

| 参考项 | 出处 | 原因 |
|---|---|---|
| assets.bin PrototypeDatabase 全解析、VFS 浏览、粒子效果 | `unpack_assets_bin.md` | 导出只读 `render_sets` / `material_full` / `skeleton_*` |
| D3D11 渲染器、Qt 交互、拾取、HUD、设备丢失恢复 | `d3d11_renderer.md` | 本项目无 GUI |
| 装甲展示重构、分区高亮、穿透计算器 | `unpack_geo_and_display.md` 第二章 | 同上 |
| 涂装切换器 UI、实时换肤、缩略图 | `camo_switcher.md` 三～五章 | 我方只出离线产物，切换由前端做 |
| analysis/UI/name_mapping 层的重复与 N+1 | `Code_duplicate_and_perf_optimization.md` 1.1/1.3～1.5 | 我方已不生成 `game_data` 的大部分表，相关代码路径不在导出链路上 |
| `indexed_material_rendering.md` 全部 | — | 已被 `fix_indexed_render_and_export_glb.md` 取代，且其结论已落地 |

> 唯一共享的仍是 `data_extractor/`（Kraken 解压、pkg 读取）。那份文档里的
> `D4`（`CODE_PREFIX_ORG` 定义 3 次）、`P1`（`list_files` 每次全树 fnmatch）
> 对本项目的 `import` 耗时同样成立，但属"能跑就行"级别，暂不排期。

---

## 六、参考文档里仍标"待实现"、但本仓库已具备的能力

避免重复劳动，逐条核对过代码：

| 能力 | 参考文档状态 | 本仓库位置 |
|---|---|---|
| GLB 导出（hull/mount/armor/贴图） | 待实现 | `export_service.export_render_glb()` |
| 标准 skin/joints + 0.2m 根骨与 `_tip` | 待实现 | `GlbRenderOptions.export_skins / bone_length / tip_bones` |
| 调色板保序（禁 `sorted(set(nodes))`） | 踩坑记录 | `assets_cache_service.py:49-53` 注释 + 保序实现 |
| `_mg` 通道换算（G=1-gloss、R→metallic） | 待实现 | `_to_gltf_metallic_roughness()` |
| 法线补 Z | 待实现 | `dds_to_png(kind=_TEX_NORMAL)` |
| 装甲按 `plate_key` 分组 + 0mm 剔除 | 待实现 | `geometry_service._build_armor_mesh()` |
| 原子写（临时文件 + `os.replace`） | 要求 | `export_service.py:927` |
| INDEXED 图集 + 6 数组 bin 下发 | 待实现（P2） | `wows_indexed_textures` / `wows_indexed.arrays` |
| 洋红占位 artMap → artStrength 置 0 | 已实现 | `geometry_service._resolve_material_full()` |
| crack / 破损断面默认剔除 | 无 | `GlbRenderOptions.include_damage` |
| LOD 级别选择 + 最小面数保护 | 无（参考项目固定跳 `_lod`） | `geometry_service._lod_skip()` |
| decal 默认剔除 | 无（参考用 mode 9 渲染） | `GlbRenderOptions.include_decals` |

---

## 七、建议的推进顺序

1. **P0-1 / P0-2**（半天）：都是小改 + 各一条回归，改完 INDEXED 涂装才谈得上"对"。
2. **P1-1 自发光**（半天）：通道已定版，直接照 §5.4 做。
3. **P1-3 探针**（1 天）：产出报告，决定 P1-2 与 P2-1 的必要性。
4. **P1-4 wire**（1 天）：影响所有船的观感（栏杆/索具），性价比高。
5. **P1-5 验收脚本**（1 天）：之后每次改动都能自证，不用再靠截图。
6. P2 项按实际需求插队。
