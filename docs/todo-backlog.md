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

**现象**：`docs/sample/lesta/manifests/PBSD511_Waterloo/PBSD511_Waterloo.json` 里唯一的
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

### P0-2 贴图去重按 `id(bytes)` —— 假命中会静默串图 ✅ 已修复（2026-10-27）

**位置**：`export_service.py _GlbBuilder.add_texture_from_dds()`

```python
key = (id(dds_bytes), kind)
```

`bytes` 对象被回收后 `id` 会被下一张贴图复用 → 命中假缓存，**返回另一张纹理的
索引，且不报错**。参考项目在 `renderer/scene.py::_TexturePool` 的 docstring 里
专门写了这个坑（"实测阿基坦另有 8 对贴图内容相同但签名不同被漏去重，其中一对
各 20 MB"），我们是同一类实现。

**修复**：新增 `_GlbBuilder._content_key()`，键改为 **sha1(内容)**：

- `_texture_cache`：`(sha1(dds), kind)`（原来是 `(id(dds), kind)`）
- `_array_texture_cache`：`sha1(dds)`（原来是 `id(dds)`）
- `_bake_cache`：`(sha1(materialIdMap), bake_size)`（原来带 `id(indexed_params)`，
  同一类隐患：`ip` 被回收后 id 复用 → 两个不同材质共用一份烘焙结果）

`id` 仍留着，但只用来**跳过重复哈希**（贴图动辄几 MB，每网格重算 sha1 会慢到
不可用）：缓存里同时存 `(bytes, sha1)`，**持有对象引用**，对象活着 `id` 就不会
被复用；命中时再校验 `hit[0] is data`。上限 512 条，超了整体清空重算
（不影响正确性）。

**验收**（`probe_p02.py`，5 passed / 0 failed）：

| 断言 | 结果 |
|---|---|
| 内容相同、对象不同 → 命中同一纹理 | PASS（idx 0/0） |
| 人为塞一条指向别的对象的陈旧 id 条目 → 不被命中，重算 | PASS |
| 缓存持有 bytes 引用（`hit[0] is data`） | PASS |
| 数组纹理：内容相同→同一结果 / 内容不同→不同结果 | PASS / PASS |

真实导出（Waterloo 默认烘焙版，3m46s）：

| 指标 | 结果 |
|---|---|
| manifest 引用 / 磁盘文件 | 64 / 64（孤儿 0、缺失 0） |
| 同内容不同文件名的贴图 | 0 张 |
| 烘焙缓存 | 11 MISS / 639 hit（同一材质被上百个网格共用） |
| 材质数 | 24（旧 31：wire/pinst 等变体烘焙结果完全相同的被合并，网格数 731 不变） |
| GLB | 18.71 MiB（旧 18.72 MiB） |

**注意一个连带修正**：烘焙缓存键一开始只用了 `materialIdMap` 的哈希，结果把
「共用 mask 但图集/LUT 不同」的材质合并了（烘焙数 17 → 11 里混着真合并且误合并）。
已改为 `_material_identity()`：mask + albedo/normal/mg 图集 + artMap + 6 组 LUT
的**全量**内容哈希 —— 烘焙的全部输入都进身份，才不会串图。

---

## 三、P1：保真度 / 数据正确性

### P1-1 自发光通道 `_mg.B` 完全没导出 🔶 已实现但默认关闭（2026-10-27）

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

#### 实测结论（2026-10-27）：B 不是干净的掩码，默认关闭

已实现 `_emissive_texture()`（`底色 × _mg.B`，linear 空间相乘、输出 sRGB 贴图，
`emissiveFactor=[1,1,1]`）+ `--emissive` 开关。**但实测数据不支持「B=emissive」**：

| 对象 | `_mg.B` 分布 |
|---|---|
| 船体 `JSB039_Yamato_1945_Hull_mg` | 0.47 的像素 < 0.05，0.23 落在 0.9–1.0（连续分布） |
| `JM032_Electric_Deck_Winch` | 0.67 落在 0.6–0.9、0.28 落在 0.9–1.0 |
| `JM010_Deck_Lamp` | 0.53 落在 0.9–1.0（像灯） |
| `JM124_Signal_Lamp_2` | **全 0**（信号灯反而不发光） |

按掩码绑上去，船体自发光亮度是底色的 **52%**（DeckHouse 24%），整船会被抬成半自发光、
丢阴影层次；而真正的信号灯是 0。所以判定：**B 更像「反射/高光强度」之类的连续通道**，
参考文档 §5.4 的「B=emissive」在本批资产上不成立。

处理：`--emissive` 保留为**可选**（默认关），需要时能一键试；语义确认后再改默认。
ARP 大和（`PJSB700_ARP_Yamato`）也不靠这条通道发光——它的两套 ARP 皮肤
（`PJES340` / `PJES512`，model skin）才是涂装差异来源，`--skins` 对它返回 0 条。

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

**实测结论（2026-10-27，见 [camo-probe.md](camo-probe.md)）：P1-2 降级为「不做」**。
同船所有 INDEXED mfm 的 6 组数组哈希完全一致，DB 里没有 camo 专属数组可覆盖；
涂装真正能覆盖的是 **artMap 贴图 + color scheme**，而 `--skins` 现在导出的是按部件的
diffuse 替换，对 INDEXED 船体没有对应槽位。若将来要做「INDEXED 换涂装图案」，
正确形状是给 skin json 加 `indexed: {artMap, artStrength, tint}` 并由前端替换
`layers.artMap`（且与默认开启的烘焙互斥，需重烘）。

### P1-3 涂装数据探针（参考的 P0 清单，直接可用）✅ 已完成（2026-10-27）

报告：[camo-probe.md](camo-probe.md)。要点：涂装是**按部件换贴图**，DB 里没有
camo 专属的 INDEXED 材质，同船所有 INDEXED mfm 的数组哈希完全一致 —— 所以
「换涂装时 tint/artStrength 会变」不成立。


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

### P1-4 wire 网格（栏杆 / 索具 / 天线）完全不导出 ❌ 实测不成立（2026-10-27）

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

#### 实测结论：没有丢，导出的就是薄三角形

- 判定条件是 `hm.is_wire = (mat == '__wire__' or mat == 'WIRE')`，而现网资产用的是
  `TL2_SHIPWIRE_*` 命名 → **不会命中**，这些网格一直按普通三角面导出；
- WG 大和有 6 个 `TL2_SHIPWIRE_PBS_Hull` 网格（3422/547/2178/4169 顶点等），
  实测**退化三角形占比 0.000**，面积均值 6e-5 ~ 4e-4 m²（0.06–0.4 mm²）——是薄带，
  不是 line list；截图里也能看到桅索；
- Waterloo（Lesta 2.0）在 LOD0/LOD2 都**没有** wire 网格（366 个网格里 0 个）。

所以 P1-4 的「栏杆/索具完全没导出」不成立（应是旧版本现象）。真要更锐利可以后续
把 `*WIRE*` 识别出来改导 `LINES`，但当前薄带已经能看，且改 LINES 会丢掉 UV/材质，
收益不大 —— 先不动。

### P1-5 导出验收缺一条硬标准 ✅ 已完成（2026-10-27）

参考 `d3d11_renderer.md` §7.4（渲染行为锁定表）+ §7.5（A/B 像素规范：

- 允许：几何边缘 ≤1px 位移；通道 MAE ≤ 2/255，峰值 ≤ 6/255（仅边缘/高频区）
- 不允许：色相偏移、图块错层、条纹伪影、`_mg` 影响基础色、decal 法线消失

我方目前只有人工截图。建议脚本化三组对照（复用本机无头 Chrome 那套）：

| 对照组 | 验收点 |
|---|---|
| crack 剔除前 / 后（`--damage`） | 断面几何消失，其余不变 |
| decal 剔除前 / 后（`--decals`） | 只少贴花片，船体不变化 |
| `--lod 0` / `--lod 2` | 轮廓不塌陷（Bow/Stern 面数保护生效） |

#### 实现：`tools/ab_render_compare.py`

无头 Chrome + CDP 打开两个 URL（各自等「mapped」+ 网络静默 2 秒再截），出
`a.png` / `b.png` / `diff.png` 并给判定：

- **MAE ≤ 2/255** 且 **显著差异像素（|Δ|>24）≤ 0.5%** 才算一致
- 只看 MAE 不够：贴花消失这类变化面积小、MAE 很低（实测换 4° 机位 MAE 才 1.55，
  但显著差异像素 1.68%）→ 必须叠第二条判据
- 「预期会变」的对照加 `--expect-change`：只出报告与 diff，不判 PASS/FAIL

自校验：A=B 时 MAE 0.000 / 显著差异 0.000% → PASS；把 `pitch` 从 -22 改成 -18
→ MAE 1.551 / 显著差异 1.68% → FAIL（脚本对几何级差异敏感）。

用法：

```bash
python tools/ab_render_compare.py \
  --a "http://127.0.0.1:8767/threejs-example.html?ship=X&root=sample/X_before/&zoom=1.8&yaw=90&pitch=-22" \
  --b "http://127.0.0.1:8767/threejs-example.html?ship=X&root=sample/X/&zoom=1.8&yaw=90&pitch=-22" \
  --out .workbuddy/tmp/ab_lod
```

依赖 `websocket-client`（`pip install websocket-client`）+ 本机 Chrome/Edge。
三组真实对照（damage / decals / lod）需要先各导一份变体，尚未跑。

---

## 四、P2：能力与性能（按需推进）

### P2-1 INDEXED → 标准 PBR 烘焙（`--bake-indexed`）✅ 已实现（2026-10-27）

这是参考文档 P2 的核心诉求：**让 Blender 打开就能看到正确颜色**。我方走的是
"图集 + 6 数组 + 自带 shader"路线，代价是：**INDEXED 船体在标准 glTF 查看器里没有
baseColor**。实测 Waterloo 15 个材质，唯一 `baseColorTexture` 为空的就是
`TL2_SHIPMAT_INDEXED_PBS_Hull`。

落地：`services/indexed_bake.py` + `GlbRenderOptions.bake_indexed / bake_size`
+ `--bake-indexed / --bake-size`。**默认开启**（2026-10-27 用户要求「以后默认给烘焙版」），
`--no-bake-indexed` 回到图集数组路线。

**不需要光栅化** —— shader 的每个输入都只是 `vUv` 的函数，烘焙就是 UV 栅格上的逐像素
运算，分辨率任意。

| 输出 | 来源 |
|---|---|
| baseColor | `albedoArray[tileIdx.x]`（经 offsetScale+rotation 变换，按 matId 分块）→ tint 混合（`albedoToRemoveTintMatIdArr` 距离 / `albedoTintMatIdArr.w` 阈值）→ artMap 叠加（`art.a × artStrengthMatIdArr.x`） |
| normal | `normalArray[tileIdx.y]`，保持 XY 补 Z（切线空间） |
| MR | `MGArray[tileIdx.z]`（G→roughness=1-gloss，B→metallic=F0） |

实测（Waterloo，Lesta）：

- 4096×2048（跟 materialIdMap 原生一致）**12.9 s**；`--bake-size 1024` → 1024×512 **3.0 s**；
- 数值对拍：GLSL 字面标量实现 vs 向量化烘焙，4000 随机像素 **max |Δ| = 1/255**（100% 落在 ≤2）；
- 导出：17 个 INDEXED 材质全部变 `mode: "pbs"`，GLB 18.72 MiB（arrays 版 18.02 MiB）；
- 页面 A/B：烘焙版与图集版结构一致（迷彩带/甲板/水线），烘焙版是标准 PBR 光照。

坑与差异：

- **art 强度必须取 `artStrengthMatIdArr.x`**（实测恒 1.0）。技术文档原先写"存在
  `rotationMatIdArr.y`"是被 `threejs-example.html` 的错误实现带偏的 —— 实测
  `rotation.y` 恒 0，照它算 art 永远不叠加。示例页已改，并顺带把写死的
  `uMatIdSize=2048×1024` 改成读实际图像尺寸（`--texture-size 1024` 下 matId 是
  1024×512，写死会让四邻域偏移错一倍）。
- **ID 图不能插值**：materialIdMap 缩放必须最近邻；
- **mip 要按采样密度选**：38 次平铺下一个输出像素跨约 9.5 个源纹素，
  直接采 1024² 原图会摩尔纹；
- 法线只能输出切线空间（shader 里是 `normalize(vN + ntex.xy*0.6)` 的视图空间近似）；
- **丢失实时换涂装**（artMap 已混进底色）；`_alpha_n` 法线叠加层仍未参与烘焙。

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

### P2-8 跨皮肤/跨船的几何复用（内容哈希池）

**实测（WG 大和 10 份皮肤 GLB，逐图元按 attributes+indices 哈希）**：

| | 字节 |
|---|---|
| 几何合计 | 214.5 MiB |
| 去重后唯一（674 个图元） | **69.9 MiB** |
| 可省 | **144.6 MiB（67%）** |

也就是皮肤之间约 2/3 的几何逐字节相同（炮塔、防空、救生艇、弹射器、飞机），只有
舰体/涂装部位不同；跨船同理（同一门 127 mm 炮被几十条船引用）。

做法：`geometry/<ship>/` 只放该船独有几何，共用件按
`sha1(attributes+indices) + build` 落到 `geometry/pool/<sha1>.glb`，manifest 增加
`parts: [{hash, node, transform, material}]`，前端加载时按 hash 取并缓存（同船多处
可 `InstancedMesh`）。

前置：跨船命中率还没实测（手上只有 3 条船，且 Lesta/WG build 不同、字节天然不同）；
建议先写一个「只统计不导出」的探针，在同 build 的 20 条船上量一遍再决定值不值得做。

### P2-3 `--model-skins` 每套皮肤重新 `load_ship` 🔶 已优化 26%（2026-10-27）

实测 8 套换模型皮肤耗时 **3 分 01 秒**，瓶颈是每套都完整加载一次几何
（与数据源无关）。做法：按 `(ship_key, lod, include_damage)` 缓存 `ShipGeometry`，
皮肤只替换 mfm / 纹理 / 模型替换节点。预期降到 1 分钟内。

**实测结论：几何无法复用，只能省「重复解码/编码」**。ARP 大和的 model skin 会换
`hull_config.model`（`JSB015_Yamato_ARP2020` / `JSB402_Yamato_ARP2024`）以及一批
`peculiarity_models` / `nodes_config` 挂载模型 —— 几何本身就不一样，缓存 ShipGeometry
没有意义。于是改成两处缓存：

1. `GeometryService.keep_load_caches()`：皮肤批量导出期间**不清**按路径索引的
   缓存（贴图字节 / 材质信息 / 挂载模型 / 骨架 / 渲染集），退出时统一清一次。
   实测单次 load 14.2 s → **8.4 s**；
2. `export_service._TEXTURE_DISK_CACHE`：`(dds 内容哈希, kind, max_size) → 已落盘文件名`，
   跨 builder 复用已编码好的 webp。单测：同内容两次导出只编码一次、文件名一致；
   不同内容仍重新编码。

实测（WG 大和，3 套 model skin，同进程 A/B）：

| | 用时 | 每套 |
|---|---|---|
| 关缓存 | 84.9 s | 28.3 s |
| 开缓存 | 62.7 s | **20.9 s** |

8 套按此推算约 **226 s → 167 s**，**没到「1 分钟内」**：剩下的 20.9 s/套 里
load 7.2 s、GLB 导出 12–15 s、manifest 2.6–4.7 s，其中 GLB 那段是每套皮肤**独有的**
贴图编码 + 几何组装，不是重复劳动。要继续压只能并行导出多套（内存换时间），暂不做。

### P2-4 batch 每船重启进程 / 无增量导出 ✅ 已完成（2026-10-27）

已在 [known-issues.md](known-issues.md) 的 P1 里记录（每船重新打开 DB、
`--force` 只能全量重导）。参考项目无此需求，无参考实现。这里只做交叉引用。

**已落地**：

1. **增量导出**：新增 `services/export_index.py`，在输出目录写
   `.export_index.json`，键 = 船，值 = 签名
   `sha1(server | build | ship | lod | texture_size | 开关集合)`。
   batch 只有当「产物存在 **且** 签名一致」才跳过；任一参数或 build 变了就重导。
   `--force` 仍然强制全量。实测（Waterloo，Lesta）：
   | 场景 | 结果 |
   |---|---|
   | 首次 | `[ok]` 3m14s |
   | 同参数再跑 | `[skip]` **2.3 s** |
   | 加 `--no-textures` | 重导（签名变） |
   | 改回原参数 | 重导（签名又变回另一组） |
2. **不再每船重启进程**：`--jobs` 默认 1 现在也走 `batch-worker`（一个进程跑完整批，
   GameParams / 数据库只初始化一次）；`--jobs 0` 保留旧的「每船一个 `ship` 子进程」。
   并行分支同样吃增量指纹（先剔掉 unchanged，跑完按产物登记）。

**局限（写清楚免得误用）**：签名只看**输入**（build + 参数），不看产物内容。
同一 build 内游戏资产若被改动（例如换了个 mod 包但版本号没变），不会自动重导；
真正的「内容级增量」需要把 geom/tex 指纹写进 manifest 并由 index 比对，暂未做。

### P2-5 参考页 INDEXED shader 的 5 处近似 🔶 已修 3 处（2026-10-27）

`docs/threejs-example.html::indexedShader()`：

1. ✅ `uMatIdSize` 写死 `2048×1024` → 改为读 materialIdMap 实际尺寸
2. 🔶 texel 中心量化已做（`uv = (floor(vUv*size)+0.5)/size`），matId 贴图改成
   **NearestFilter**（原来线性过滤会在块边界插出不存在的 id）；**噪声抖动未做**
   —— 参考文档只写了"缺噪声抖动"，没给公式（`uv*48` 是我们自己的记录，无出处）
3. ❌ `_alpha_n` 叠加层未做：实测这张 2048×4096 图 RG 像法线 XY，但 **B/A 均值
   95/86，语义无出处**；照猜混进法线只会更糊，等拿到游戏 shader 再动
4. ✅ artMap 未下发（= P0-1，已随 P0-1 修复）
5. ❌ 光照仍是手搓近似（`×1.65` + 混 6% 白）：参考的 `pbr_light` 只有名字没有实现；
   而且**烘焙默认开启后这条路径只影响 `--no-bake-indexed`**（烘焙材质走
   MeshStandardMaterial），优先级已降低

### P2-6 判定逻辑分散（技术债，这一轮已经疼过）✅ 已收敛（2026-10-27）

`_lod` / `_crack` / `decal` 三套判定现在分散在：

- `geometry_service._lod_skip()`（LOD 级别 + 最小面数保护）
- `geometry_service._split_primitives_by_material()` 里的 `__crack__` 分流
- `export_service._is_decal()`（材质名 / 网格名 / shape_spans 三级）

最近四轮改动（crack → decal → 面数保护 → 遮挡体回退）每次都在这三处来回改。
建议抽 `services/mesh_filters.py`：`shape_lod()` / `is_crack()` / `is_decal()` /
`is_wire()`，几何层与导出层共用同一份判据。

**已落地**：`services/mesh_filters.py`

| 判据 | 入口 | 原散落位置 |
|---|---|---|
| LOD 级别 + 面数保护 + 辅助几何豁免 | `should_skip_lod(name, lod, levels_by_base, tri_by_base_lod, shape_lod)` | `geometry_service._lod_skip()` 闭包 |
| 破损断面 | `is_crack_material(mat)` | `mat_key.startswith('__crack__')` |
| 老式 wire | `is_wire_material(mat)` | `mat in ('__wire__','WIRE')` |
| 贴花 | `is_decal(obj)` | `export_service._is_decal()` |
| 辅助几何关键词 | `is_auxiliary_geometry(name)` | `_lod_skip` 里的内联元组 |

回归：Waterloo 重导与重构前**逐项一致**——731 meshes / 24 materials / 297,425 三角 /
18.71 MiB / 11 个烘焙材质；`tools/ab_render_compare.py` 同机位 A/B：MAE 0.000、
显著差异 0.000% → PASS。

注：`is_wire_material()` **只认** `__wire__`/`WIRE`，`TL2_SHIPWIRE_*` 不判 wire
（那些是薄三角形，判进去会丢几何，见 P1-4）。

**已落地**：`services/mesh_filters.py`

| 判据 | 入口 | 原散落位置 |
|---|---|---|
| LOD 级别 + 面数保护 + 辅助几何豁免 | `should_skip_lod(name, lod, levels_by_base, tri_by_base_lod, shape_lod)` | `geometry_service._lod_skip()` 闭包 |
| 破损断面 | `is_crack_material(mat)` | `mat_key.startswith('__crack__')` |
| 老式 wire | `is_wire_material(mat)` | `mat in ('__wire__','WIRE')` |
| 贴花 | `is_decal(obj)` | `export_service._is_decal()` |
| 辅助几何关键词 | `is_auxiliary_geometry(name)` | `_lod_skip` 里的内联元组 |

回归：Waterloo 重导与重构前**逐项一致**——731 meshes / 24 materials / 297,425 三角 /
18.71 MiB / 11 个烘焙材质；`tools/ab_render_compare.py` 同机位 A/B：MAE 0.000、
显著差异 0.000% → PASS。

注：`is_wire_material()` **只认** `__wire__`/`WIRE`，`TL2_SHIPWIRE_*` 不判 wire
（那些是薄三角形，判进去会丢几何，见 P1-4）。

### P2-7 补 `tools/blender_wows_vertex_groups.py` ❌ 不做（2026-10-27 用户确认）

`export_service.py` 的注释曾写着"需要顶点组但不要骨架时……再用
`tools/blender_wows_vertex_groups.py` 还原"，但**本仓库一直没有这个脚本**。
参考项目里是 130 行的独立脚本（读 `_JOINTS_0` / `_WEIGHTS_0` + `bone_palette`
重建顶点组，不建 Armature）。

决定：**不做**。`export_skins=True`（默认）本来就写标准 skin，Blender 直接有顶点组；
`export_skins=False` 只是"要空物体层级、不要 Armature"的少数场景，需要时用
自定义属性自己接即可。注释已同步改成说明数据位置，不再指向不存在的脚本。

---

## 五、明确不做

| 参考项 | 出处 | 原因 |
|---|---|---|
| assets.bin PrototypeDatabase 全解析、VFS 浏览、粒子效果 | `unpack_assets_bin.md` | 导出只读 `render_set` / `material_full` / `skeleton_*` |
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
| 原子写（临时文件 + `os.replace`） | 要求 | `export_service._GlbBuilder.save()` |
| INDEXED 图集 + 6 数组 bin 下发 | 待实现（P2） | `wows_indexed_textures` / `wows_indexed.arrays` |
| 洋红占位 artMap → artStrength 置 0 | 已实现 | `geometry_service._resolve_material_full()` |
| crack / 破损断面默认剔除 | 无 | `GlbRenderOptions.include_damage` |
| LOD 级别选择 + 最小面数保护 | 无（参考项目固定跳 `_lod`） | `geometry_service._lod_skip()` |
| decal 默认剔除 | 无（参考用 mode 9 渲染） | `GlbRenderOptions.include_decals` |

---

## 七、剩余工作总表（2026-10-28 全量排查）

### 已完成：`diff` 版本对比（2026-10-28）

主场景「版本更新只导变动的船」原先**没有工具支撑**（`batch` 的签名含 build，
版本一变全批失效），只能手工 `--filter` 猜。现在补上：

- `cli.py diff`：读两版 `bin/<build>/idx/*.idx`，按 `.geometry` 的 `crc32` 比对，
  **不加载 assets.bin、不解包几何**；映射到船 key（`ship_models` + 快照的
  `A_*.*.model`）后输出报告 / keys / json。
- `cli.py batch --keys-file`：直接吃 `diff --format keys` 的输出。
- 实测 WG `13187581 → 13357625`：两版共 6.4 万个同名 geometry，**99.9% 的 crc32 不变**，
  只有 25 个文件变 → 7 条船的船体（其中仅 1 条动了主文件）+ 12 个共享挂载（波及 21 条船）；
  整条命令 **14.6 s**。闭环验证：`diff --format keys` → `batch --keys-file` 导出成功。

口径「宽松」（与用户确认）：crc32 含顶点/索引，**只改模型细节也算变动**，
宁可多导不漏导。局限见 `10-cli.md` §4.5（纯 GameParams 变动不在此判据内）。

---

P0、P1 与 P2-1/3/4/5/6/7 已全部结清（逐项状态见各节标题）。下面是**还没做**的全部项，
按「收益 ÷ 风险」排序，括号内为实测数据来源。

### A0. 数据库重构 ✅ 已完成（2026-10-28）

`game_data.db` 从「多版本 50 张表」改成「单版本镜像 7 张表」（`ship` / `ship_data` /
`model` / `model_file` / `ship_model` / `meta` / `meta_schema_version`），
`assets_data.db` 去掉 `bin_folder` 维度改按 `model_id` 组织。

效果：同 build 重复 `import` 从「全清重建 ~4 min」变成 **18 s**；
只有内容变了的船/模型会被重写（快照 sha1 + geometry 签名判定）。
详见 [tech/common/02](tech/common/02-import-game-data.md) 与 [03](tech/common/03-import-assets.md)。

### A. 数据体积（纯本地，不改导出结果）

| # | 项 | 现状（实测） | 目标 | 风险 |
|---|---|---|---|---|
| A1 | `assets_data` 裁剪 | 555 MB（Lesta）/ 623 MB（WG）。`render_set` 399,008 行里 `/ship/` 283,908、`location` 34,748、`gun` 14,873、`aircraft` 9,430、其余 56,049；两张骨骼表 + 索引约占 300 MB/库 | 按路径类型裁 → 估计 350 / 400 MB | 口径必须按**路径类型**，不能按船名；裁 `aircraft` 会丢水上飞机挂载 |
| A2 | `game_data` 快照瘦身 | `ship_data` 占 118 / 154 MB 库的大头，1,165 / 1,230 条 × 均 105 KB。**增量后不再每次重写**（sha1 没变就跳过），但体积仍在 | 只存导出用到的字段（`components[*].model`、装甲厚度、`permoflages`、`hull/nodes_config`）→ 估计 40 / 55 MB | **有漏字段风险**：漏一个 `A_*` 组件就少一批挂载；需逐船 A/B 对比网格数 |
| A3 | 删二级索引 | 本轮去掉了 `idx_rs_geom` / `idx_bones_stem`（主键前缀已覆盖查询），只剩 `idx_rs_model`（按模型增量删行用，约 6 MB） | ✅ 已解决 | — |

### B. 保真度（观感/正确性，需要对着游戏截图验）

| # | 项 | 现状 |
|---|---|---|
| B1 | 颜色还原 | 整体偏灰，未与游戏内截图逐像素对齐。INDEXED 用近似光照 `c*(0.75+0.35*ndl)+spec*mg.r*0.35` 再 `*1.65`；PBS 的金属度/粗糙度是估算值 |
| B2 | decal（P2-2） | 默认整片丢弃（Waterloo 少 960 片 / 383,536 三角 = 62%）。方案 B（独立节点 + BLEND）未做 |
| B3 | `_alpha_n` 法线叠加层 | INDEXED 的 `normalMap` 是 alpha 叠加层，烘焙时未参与（P2-5 遗留） |
| B4 | 自发光 | 实现但默认关：实测 `_mg.B` 不是掩码（船体 0.47、信号灯 0），开着会让整船半自发光 |

### C. 性能

| # | 项 | 现状（实测） |
|---|---|---|
| C1 | 模型皮肤每套独立几何（P2-3） | 已优化 26%（3 套 84.9 → 62.7 s）；几何无法复用（皮肤换 `hull_config.model`），只剩解码/编码可省 |
| C2 | 跨船几何池（P2-8） | 同船 10 套皮肤 214.5 → 69.9 MiB（可省 67%），但**跨船命中率未测**；先写「只统计不导出」的探针 |

### D. 工程化

| # | 项 | 现状 |
|---|---|---|
| D1 | 自动化测试 | 没有 `tests/`。现有回归手段只有 `tools/ab_render_compare.py`（像素 A/B）和几个 `.workbuddy/tmp/probe_*.py` 一次性脚本 |
| D2 | CI | 没有 `.github/workflows` |
| D3 | NOTICE | 没有。`data_extractor/kraken.py` 是 **GPLv3**，其余 Apache-2.0，分发时必须带 NOTICE 说明 |
| D4 | 文档行号 | `文件:行号` 会漂（本轮核对 18 处里 8 处失准，最大偏 290 行）。已在 `tech/README.md` 注明「按符号 grep」，彻底改符号锚点**暂不做** |

### E. 已知不一致 / 预留骨架

| # | 项 | 现状 |
|---|---|---|
| E1 | ~~WG 库 build 过期~~ | ✅ 2026-10-28 重跑 `import` 修好：`meta.build = 13357625`，与客户端一致 |
| E2 | `wg_compat` 4 处 `TODO(手动)` | WG 的类型映射 / 弹夹炮识别目前复制 Lesta 版，未按 WG 实测校准 |

### F. 死代码 ✅ 已清理（2026-10-28）

用 AST 扫「只有定义、无调用点」的函数（全仓标识符计数），逐个确认后删除：

| 批次 | 内容 | 行数 |
|---|---|---|
| 配置/上下文 | `keep_split_json` 字段与 property、`AppContext.exe_dir/data_dir/split_dir`、`game_path_is_set`、`schema_mismatches`、`Application` 的 6 个死 setter、`ConfigManager.reset`、`signals.disconnect` | ~60 |
| 导入链 | `processor_service._run_analysis()`（首行 return 的不可达体）、`_write_one()` 与写 split 分支、`data/split` 两处回退 | ~110 |
| 涂装缩略图 | `camo_thumbs_dir` / `get_camo_thumb` / `put_camo_thumb` / `_camo_icon_map` / `_cache_camo_icon` / `_gui_ext_rel` / `_resolve_camo_icon` + `camo_thumbs` 表（两服 schema） | ~250 |
| 实时预览（GUI 遗留） | `apply_camo`、`live_skeleton`、`apply_live_material`、`release_live_material_caches`、`resolve_material_live`、`_live_skel_path_index`、`_section_render_sets`、`_mfm_index`、`_ensure_visual_geom_index`、`_apply_rigid_binding`、`_bake_camo_art`、`_composite_camo_over_base`、`_camo_attr`、`release_load_caches`、`_parse_visual_render_sets` | ~500 |
| 数据浏览查询 | `database_service` 的 18 个方法（`list_entities` / `count_entities` / `get_stats` / `get_entity` / `get_turret_arcs` / `has_turret_arcs` / `list_versions` / `ship_name_gaps` / `import_po_translations` / `vacuum` / `drop_all` / `check_schema_mismatches` / `save_ship_splash_protection` / `load_ship_splash_protection` / `backfill_ship_name_mapping_ids` / `read_latest_version_code` / `close_all_connections` / `schema_rebuilt` / `_read_schema_version`） | ~365 |
| 其它 | `armor_scene` 的 4 个查询、`collision_materials` 的 2 个、`name_mapping` 的 6 个（含 `rich_tooltip` / `format_modifier`）、`camo_service` 的 `list_orphan_skins` / `entry_by_name` / `collect_all_permoflages` / `_geometry_folder_names`、`assets_cache_service` 的 3 个 getter | ~250 |

顺手修掉两个**真问题**（不是死代码）：

- `ExportReport` 没有 `__str__`，而 CLI 是 `print(report)` → 实际打印的是 dataclass repr，
  `summary()` 白写了。已补 `__str__`，与文档描述一致。
- `assets_cache_service` 里硬编码 `D:\战舰世界汉化\bin` 猜本地化文件 → 已删；
  随后 `.po/.mo` 读取整条链路也删了（用不到，`display_name` 列保留但恒空）。

### 第二轮：确认后全部清掉（同日）

上一版「刻意不动」的 24 个，逐个复核后确认都是真死（均不在 `uncode_assets.__all__`、
无 `getattr`/字符串调用），已删；连带删掉调用它们的封装层，以及 6 个整模块：

| 内容 | 说明 |
|---|---|
| `kraken.py` 5 个（`_init_stream_reverse` / `peek_reverse` / `read_bits_nr` / `align_to_byte` / `refill_reverse`） | 反向位流支持，本项目的 `.geometry` 不走这条 |
| `bc7prep.packed_sub`、`pkg_reader.read_file_direct`、`geometry_parser.unpack_vertices` | 未被调用的替代实现 |
| `atlas_contour.encode_contours` / `format_contours_text`、`binary.parse_packed_string*`、`decoders.read_material_header` / `_extract_ascii_strings`、`fx.record_region_bounds` | 浏览/调试辅助 |
| `service.wows_type_auto` / `auto_rule` / `auto_rule_reason` / `find_files` / `decode_mfm_by_self_id` / `decode_material_by_path`、`space.parse_geometry_header` / `parse_space_bin`、`types.default_item_sizes`、`vfs.pkg_has` / `logical_path` | 子模块公开接口，本仓库无调用方 |
| **整模块删除**：`uncode_assets/{mfm_text, model_text, skel_ext_text, model_wg_text, visual_text, visual_wg_text}.py` | 「Prototype → 明文」实验性反导出，6 个模块共 688 行，全仓无引用 |
| 级联孤儿：`config._path_valid`、`geometry_service._rgba_to_dds_bgra` / `_get_assets_service` / `_load_texture_tier_live` | 因上游删除而失去调用者 |

> 删完后 `uncode_assets` 只剩实际用到的：`parser` / `decoders` / `types` / `vfs` /
> `service` / `binary` / `space` / `fx` / `atlas_contour`（后者仅经 `decoders` 调 `decode_contours`）。
>
> 两轮合计 **−2900 行代码**（全仓 diff 含文档是 50 文件 +420/−3227）。
> 验证：`compileall` 全包 + 三个核心包可导入 + 两次真实导出（`PJSD889_Kitakaze_PR`，
> `--no-textures --lod 2`）都是 **265 网格 / 89,771 三角形**，与清理前完全一致。

### 顺带：`data_extractor` 不再写死路径

`python -m data_extractor.cli` 的所有子命令：
`game_dir` 位置参数改为可选，新增 `--game/-g` 与 `--server lesta|wg`；
都没给时读项目 `config.json` 的 `game_path_lesta` / `game_path_wargaming`，
取不到就打印「怎么配」的三种做法后退出。省略目录、直接把 glob 写在第一个位置参数
（`list "content/**/*.data"`）也能正确识别 —— 不是已存在目录时还回 `patterns`。
