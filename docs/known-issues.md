# 已知问题与剩余工作

更新时间：2026-10-27

本文档记录当前导出器仍未解决的问题、已经排除的方向，以及下一步建议。
设计说明见 [web-render-design.md](web-render-design.md)；
待办清单（对照 Wows_Paragrams_Unpack 的 todo_list 逐条判定）见 [todo-backlog.md](todo-backlog.md)。

## 状态总览

| 模块 | 状态 |
|---|---|
| 双服 GameParams / 建库 / 列船 | 完成 |
| PBS 几何 + 外置 WebP + manifest | 完成 |
| 装甲 JSON / bin / GLB | 完成 |
| mat 皮肤 | 完成 |
| model 皮肤导出 | 完成 |
| INDEXED 数组 + atlas + materialIdMap + artMap | 完成 |
| INDEXED → 标准 PBR 烘焙 | 完成，**默认开启**（`--no-bake-indexed` 关闭） |
| three.js 示例 | 完成 |
| skipped_textures | 0 |
| 白色主题 / 水下光照黑块 | 已修 |
| 破损断面几何（`*_crack_*`） | 已修：默认剔除，`--damage` 恢复 |
| 舰体深色/透视块 | 已解决（2026-10-27） |
| 颜色还原 | 偏灰，未对齐游戏 |
| 贴图格式 | WebP（定稿，不做 KTX2） |
| 增量导出 | 完成（`.export_index.json`，签名 = build + 参数，**输入级**） |
| batch 启动性能 | 完成（`--jobs 1` 默认单进程跑完整批） |
| 数据库体积 | game_data 已精简（318→157 MB）；assets_data 待精简（555 / 623 MB） |
| 自动化测试 / CI / NOTICE | 未做（已有 `tools/ab_render_compare.py` 做 A/B 像素对照） |

## P1：颜色还原

- 当前 INDEXED shader 使用近似光照：
  `col = c * (0.75 + 0.35*ndl) + spec * mg.r * 0.35;`
  `col *= 1.65;`
  `col = pow(col, 1/2.2);`
- PBS 使用 MeshStandardMaterial，金属度/粗糙度为估算值；
- 水下光照黑块已通过提高 HemisphereLight groundColor + AmbientLight 修复；
- 整体仍偏灰，未与游戏内截图逐像素对齐。

### 下一步

- 用游戏内同角度截图做颜色对比；
- PBS 的 composite/artMap 叠加公式需要完整实现；
- INDEXED 的 tint/remove/art 强度需要校准。

## P1：增量导出 ✅ 已实现（2026-10-27）

`services/export_index.py` + `<out>/.export_index.json`：签名 =
`sha1(server | build | ship | lod | texture_size | 开关集合)`，签名一致且产物存在才跳过。
实测同参数复跑 3m14s → **2.3 s**，改参数会重导。

仍是**输入级**增量：签名只含 build 与参数，不含产物内容哈希。要「资产变了也能感知」
还需把 geom/tex 指纹写进 manifest 再比对（未做）。

> 注意与「数据库增量」的区别：这里的签名含 `build`，所以客户端一更新，整批签名失效 →
> 导出会重跑全量（未变的船仍是本地读盘 + 写盘，不是「跳过」）。数据库那一层才是真增量：
> 只有快照/几何变了的行会被重写（见 [tech/common/02](tech/common/02-import-game-data.md)）。

## P1：batch 性能 ✅ 已实现（2026-10-27）

- ~~每艘船都是独立 Python 进程~~ → `--jobs 1`（默认）在进程内串行，
  GameParams / DB 只初始化一次；`--jobs > 1` 才切分给 `--worker` 子进程；
- 每次加载仍会重开 VFS 索引（按船必需），但不再重复 bootstrap。

## P1：数据库结构与体积

本项目是**纯导出器**。2026-10-28 把两个库都改成「单版本镜像 + 增量同步」，
删掉了原 Wows_Paragrams_Unpack 数据浏览功能留下的 46 张空表。

| 库 | 表 | 体积（Lesta / WG） |
|---|---|---|
| `game_data*.db` | `ship` / `ship_data` / `model` / `model_file` / `ship_model` / `meta` / `meta_schema_version` | 118 / 154 MB |
| `assets_data*.db` | `render_set` / `skeleton_mounts` / `skeleton_bones` / `material_full` / `mfm_textures` / `shape_names` / `exteriors` / `camouflages` / `camo_*` / `meta` | 554 / 623 MB |

设计取舍见 [tech/common/02](tech/common/02-import-game-data.md) 与
[03](tech/common/03-import-assets.md)。要点：表里**不再有 `version_code` / `bin_folder`**，
否则 build 一变，未变动的上百万行也要 UPDATE 到新 build，增量无从谈起。

### A. 已完成

| 项 | 结果 |
|---|---|
| 只写 ship 快照（2026-10-26） | 其余 2.3 万条 Gun/Projectile/Aircraft 快照无人读取，`game_data_wg` 318 → 157 MB |
| 删分析层 46 张表（2026-10-28） | `_run_analysis()` 早已 `return`，这些表只写不读；schema 从 1017 行降到 180 行 |
| 删 `name_mappings` / 显示名（2026-10-28） | 产物不需要中文名；`_resolve_ship_name()` 与 `.po/.mo` 读取一起移除 |
| 增量同步（2026-10-28） | 同 build 重复 `import` 从 ~4 min 降到 **18 s**；快照 sha1 没变就不重写（占整库 84% 的那部分） |
| 二级索引 | 去掉 `idx_rs_geom` / `idx_bones_stem`（主键前缀已覆盖），只留 `idx_rs_model` |

### B. 待做：assets_data 按路径类型裁剪

| 项 | 体积 | 处理 |
|---|---|---|
| `skeleton_bones` | 812,395 行 | 只留「舰船 + 舰炮」stem（现有含飞机 AAB、建筑、港口） |
| `skeleton_mounts` | 483,925 行 | 同上 |
| `render_set` | 402,395 行 | 只留 `/ship/` 与 `/gun/` 路径；丢掉 `other` 9 万条（location/建筑/飞机） |
| `material_full` | 29,688 行 / 20 MB | 可只留被 `render_set` 引用到的 mfm |

**约束**：必须按路径类型裁剪，不能按船名 —— 否则以后导冷门船会缺数据。

## P2：工程化

- 没有自动化测试 / CI；
- 没有 NOTICE（kraken.py 为 GPLv3）；
- `docs/sample` 约 43 MB，未纳入 git（.gitignore 已忽略）；
- 文档中的样本清单需要随导出更新。

## 已修复（备查）

- 顶点属性不压缩：GLB 里 POSITION/NORMAL 全 f32、索引全 u32，还带着没人读的
  `_T4` / `TEXCOORD_1` / `TANGENT`（Waterloo 属性合计 17.1 MiB / 60.1 B/tri）。
  现在默认 `--slim` 走 `KHR_mesh_quantization`：位置 u16（按图元 AABB + 节点矩阵还原）、
  法线 i8、UV u16、索引 u16，并丢掉上述属性 → 7.25 MiB / 25.6 B/tri，**GLB 18.7 → 8.8 MiB**；
  位置往返误差 1.5 mm、法线方向误差 0.05°、同机位 A/B MAE 0.11/255。`--no-slim` 可退回；
- 输出目录扁平：`geometry/<ship>.glb`、`manifests/<ship>.json`… 多船混在一起，
  与设计文档的 `web/<server>/<build>/` 也对不上。改为除 `textures/` 外**每类产物一层船名目录**
  （`geometry/<ship>/<ship>.glb`），GLB 内贴图 URI 变成 `../../textures/…`，manifest 里
  仍统一用相对输出根的路径；示例页同步改到 `manifests/<ship>/<ship>.json`；
- 默认导出会写 `skins/<ship>/default.json` 并在 manifest 里列 `skins: ["default"]`，
  即使根本没要皮肤。现在皮肤**不是默认产物**：只有 `--skins` / `--model-skins` 才写
  `skins/`，`manifest.skins` 为空数组，页面把皮肤下拉置灰。
- `exteriors` 表恒为 0 行：入库原本读 `data/split/Exterior/*.json`，而 split 是导入临时产物
  （收尾就清）且后台任务与主流程写 split 有竞态，失败还被 `except: pass`
  吞掉 → 每次导出都要现场解 GameParams。改为直接从 GameParams 解析
  （`core.gameparams.exterior_entities`），split 仅作回退
  （2026-10-28 起 split 已完全不产出，该回退只对旧残留有效）；
  `data_json` 只存涂装相关 10 个字段
  （2.5 MB vs 整份实体 7.7 MB）。入库结果 WG 2,362 条 / Lesta 1,915 条；
- 破损断面几何混入 GLB：`*_crack_*` 渲染集原本只标 `is_crack` 却照样写进 GLB，而它们
  与正常船体表面共面、只在战舰沉没断裂时才该出现；同一份 `.geometry` 同时含完好船体与
  断裂面，观感是船体断成几截（WG 大和 3 处断口 × 两侧 = 6 片 `*_in` 断面盖板）。现在导出
  默认按 `is_crack` 剔除，`--damage` 才包含；见 `GlbRenderOptions.include_damage`。
  实测 WG 大和：mesh 403→381、三角 451421→425734、体积 37.2→35.2 MiB、
  节点/网格名含 crack 者 66→0。排查时已排除 AO、法线贴图 / roughness、UV wrap、
  alphaMode、MESHDECAL、log depth / polygonOffset、皮肤模型加载错误；
- 双服 GameParams 解析；
- Lesta INDEXED 数组 / atlas / materialIdMap；
- skipped_textures 658 → 0（DX10 texture array 判断）；该功能后来发现**双向误判**：
  判据里有一条 `len(dds) > 8_000_000 → 数组` 的大小捷径，导致 4096² BC1/BC7 单图
  （Yamato 的 diffuse/mg/normal、Waterloo 的 artMap/normalMap）被当数组整张丢弃；
  而 2.67 MB 的 `rgbNoiseMap` 又是真数组却漏判。已删掉大小捷径，改为纯结构判定
  （`arraySize>1` + 单层字节×2）。实测与六船重导数据见
  [todo-backlog.md](todo-backlog.md) 的 P0-1；
- ARP 皮肤 hull_config 字符串解析；
- 水下光照黑块（HemisphereLight groundColor + AmbientLight）；
- three.js 白底主题；
- model 皮肤独立 geometry/manifest 与 switchModel()；
- `--merge-shapes` 保留为可选，但不推荐（会丢 shape）；
- `--split-shapes` 曾是死参数（解析了但 `_export_ship` 不读），`--merge-shapes` 帮助还写着
  "(default)"（与实现相反）。现在两者是 argparse 互斥组：默认拆分，`--split-shapes` 只是显式声明；
- `manifest.hashes.textures` 只统计 `slots`，INDEXED 船换涂装（artMap）/换图集时指纹不变，
  前端缓存读旧图。改为覆盖 `slots + layers + indexed 图集 + 数组 bin`（`_asset_uris()`）。
  实测 Waterloo：geometry / materials 哈希不变，textures 从 `ac2839a2…` → `f4491ec4…`；
- 贴图去重键原本是 `id(bytes)`：bytes 被回收后 `id` 会被下一张贴图复用 → 假命中、
  **静默返回另一张纹理的索引**。现在统一走 `_GlbBuilder._content_key()`（sha1 内容），
  `_texture_cache` / `_array_texture_cache` / `_bake_cache` 全部换键；`id` 只用于跳过
  重复哈希（缓存同时持 bytes 引用防 id 复用，按 96 MB 预算 FIFO 淘汰，命中时校验
  `hit[0] is data`）。回归：`probe_p02.py` 5 passed；
- `GeometryService.load_ship` docstring 写 `lod=-1` 默认，实际签名是 `2`（导出默认 LOD2）；
- INDEXED 涂装叠加层在页面里**永远不生效**：`threejs-example.html` 的
  `am = clamp(art.a * rot.y, 0, 1)` 取的是 `rotationMatIdArr.y`，实测该列恒 0，
  真正的强度在 `artStrengthMatIdArr.x`（洋红占位时被置 0 的也是这一列）。
  已改绑 `uArtStr`；`docs/tech/common/05-materials-textures.md` 里"存在 rotation.y"
  的旧描述同步纠正。实测 art 贡献不小（Waterloo：平均 |Δ| 5.5/255，23.7% 像素 >8，
  峰值 150）；
- 同一处 `uMatIdSize` 写死 `2048×1024`，而 `--texture-size 1024` 导出的 matId 是
  1024×512 → 四邻域偏移错一倍（材质块边缘串色）。改为读 `materialIdMap` 实际尺寸；
- INDEXED 材质默认烘成 baseColor/normal/MR（`--no-bake-indexed` 关闭）。数值对拍与实测见
  [todo-backlog.md](todo-backlog.md) P2-1；关闭时才是原始图集数组路线（可实时换涂装）；

## 测试样本

```text
docs/sample/                       一个服务器一个输出根（船 key 同名会互相覆盖）
  lesta/                          34 MB   Lesta build 8867689
    geometry/PBSD511_Waterloo/PBSD511_Waterloo.glb        8.8 MiB（烘焙 + 量化）
    geometry/PJSB018_Yamato_1944/PJSB018_Yamato_1944.glb  8.5 MiB
    geometry/PJSD889_Kitakaze_PR/PJSD889_Kitakaze_PR.glb  3.9 MiB（--model-skin auto，
                                                          base_skin = PJES509「星之北风」）
    armor/…  manifests/…           装甲 1.7 MB / manifest 276 KB
    textures/                     396 张 / 9.1 MB
  wg/                             26 MB   WG build 13357625
    geometry/PJSB018_Yamato_1944/PJSB018_Yamato_1944.glb  9.0 MiB
    geometry/PJSB700_ARP_Yamato/PJSB700_ARP_Yamato.glb    9.1 MiB（--model-skin auto，
                                                          base_skin = PJES340）
    textures/                     205 张 / 4.7 MB
```

样本用默认参数（`--texture-size 1024 --lod 2`，烘焙开、`--slim` 开），**不含皮肤**
（皮肤已改为非默认，需要时加 `--skins` / `--model-skins`）。整套样本 374 MB → **43 MB**：
一半来自去掉模型皮肤，一半来自顶点属性量化（GLB -53%）。

本地预览：

```text
cd docs
python -m http.server 8767
```

```text
http://127.0.0.1:8767/threejs-example.html?ship=PBSD511_Waterloo&root=sample/lesta/
http://127.0.0.1:8767/threejs-example.html?ship=PJSB018_Yamato_1944&root=sample/wg/
```

## 复现命令

> 以下命令默认输出完好状态（`*_crack_*` 断面已剔除）；需要沉没断裂几何时追加 `--damage`。

Waterloo：

```bash
python cli.py --server lesta --ship PBSD511_Waterloo --out out/lesta --armor
```

WG 大和：

```bash
python cli.py --server wg --ship PJSB018_Yamato_1944 --out out/wg --armor
```

Lesta 大和：

```bash
python cli.py --server lesta --ship PJSB018_Yamato_1944 --out out/lesta --armor
```
