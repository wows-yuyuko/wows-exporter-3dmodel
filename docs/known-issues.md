# 已知问题与剩余工作

更新时间：2026-10-26

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
| three.js 示例 | 完成 |
| skipped_textures | 0 |
| 白色主题 / 水下光照黑块 | 已修 |
| 破损断面几何（`*_crack_*`） | 已修：默认剔除，`--damage` 恢复 |
| 舰体深色/透视块 | 未解决 |
| 颜色还原 | 偏灰，未对齐游戏 |
| 贴图格式 | WebP（定稿，不做 KTX2） |
| 增量导出 | 未实现 |
| batch 启动性能 | 每船重新加载 DB |
| 数据库体积 | game_data 已精简（318→157 MB）；assets_data 待精简（619 MB） |
| 自动化测试 / CI / NOTICE | 未做 |

## P0：Waterloo 舰体深色 / 透视块

### 现象

- `PBSD511_Waterloo` 舰体侧面有一块深色/像透明洞的区域；
- 近看仍能看到纹理，不是完全空白；
- 其他船水下区域也曾出现类似黑块。

### 已排除

- 不是背景透出（背景白色，洞应该是白色）；
- 不是 alphaMode（manifest 全部 OPAQUE）；
- 不是 AO / 法线 / UV；
- 不是合并 hull 后的缺面（已回退到分段导出，问题仍在）。

### 当前判断

- 更可能是 Hull albedo 中黑色占位区，或 camo/colorScheme mask；
- 也可能是分段边界重叠面在同一位置反复遮挡，视觉上像透明；
- **2026-10-26 补：只在 Lesta 客户端「视觉 2.0」的船上出现**——即 Lesta 的
  **INDEXED 材质体系**（`TL2_SHIPMAT_INDEXED_PBS_*`），WG 侧目前没有这套。
  INDEXED 船体只有分段外壳（Bow/MidFront/…），内部空腔没有任何遮挡，
  从船首视角会一路看穿到船尾（能看到尾部螺旋桨）。游戏另有一个 `Full_*` 低模
  （整船封闭体，通常只有 LOD4 一份）负责挡这条视线。

### 试过但已回退：`Full_*` 当内部遮挡体

思路是把主 geometry 的 `Full_*` 一起导出当作遮挡体。试过三种做法，都不成立：

1. **直接导出** —— `Full_lod4` 在平坦处比分段外壳大，且 UV 是远景那套，
   结果整船被深色多边形盖住（涂装消失）；
2. **沿法线内缩 0.12** —— 仍然露出大半，LOD4 与 LOD2 外壳的偏差不止这个量级；
3. **单面材质 + 反绕序**（本意「从外看不见、从内挡视线」）—— 几何上有效，
   但 `docs/threejs-example.html` 把所有材质强制改成了 `THREE.DoubleSide`，
   把单面设置覆盖掉了；换任何同样强制的查看器都会失效。

已全部撤回，导出回到只含分段外壳的干净版本。真要解决得在前端做（尊重 GLB
的 `doubleSided`、或限制相机/用背面剔除渲染遮挡体），不在导出侧。

### 下一步

- crack 断面面已在导出时默认剔除，先重导一份排除该因素；
- 按材质名逐个隐藏，定位覆盖该区域的 mesh/material；
- 检查该 material 的 albedo 在对应 UV 区域是否本来就是黑色；
- 如果是 colorScheme mask，需要导出默认涂装或烘焙。

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

## P1：增量导出

尚未实现：

- 没有 `data/export_manifest.db`；
- 没有单船 geom_fp / tex_fp / skin_fp；
- `batch` 目前只按“文件是否存在”跳过；
- 没有把 assets.bin / GameParams / build 变化作为总闸。

## P1：batch 性能

- 每艘船都是独立 Python 进程；
- 每次都重新加载 GameParams、DB、VFS 索引；
- 四艘串行导出需要数分钟到十几分钟；
- 可在同一进程内批量导出，省掉重复初始化。

## P1：数据库精简（剩余）

本项目是**纯导出器**，但两个库里仍存着原 Wows_Paragrams_Unpack 数据浏览功能留下的数据。
导出链路实际只读这些表：

| 库 | 导出用到的表 |
|---|---|
| `assets_data*.db` | `render_sets`、`skeleton_mounts`、`skeleton_bones`、`material_full`、`mfm_textures`、`shape_names`、`camouflages`、`camouflage_color_schemes`、`camo_ship_groups`、`exteriors` |
| `game_data*.db` | `entity_snapshots`（只读 `entity_type='ship'`）、`meta_import_state`、`ship_models`、`entity_registry` |

### A. 已完成（2026-10-26）

`import` 时只写 ship 快照（其余 2.3 万条 Gun/Projectile/Aircraft/… 快照无人读取），
并清掉了遗留的大表数据。实测：

```
data/game_data_wg.db   318 MB -> 157 MB   （删 23,250 快照 + 51,004 crew + 596 buff）
data/game_data.db      162 MB -> 120 MB   （删 20,050 快照 + 1,301 buff）
```

`--skins` 已验证不受影响（10 个皮肤正常导出）。ship 快照保留 1,246（WG）/ 1,165（Lesta）条，
平均 105 KB/条 —— 体积大头是 `A_AirDefense`(26%)、`A_Hull`(15%)、`A_ATBA`(10%)，
这些正是导出挂载时要读的 model 路径，**不能再裁**。

### B. 待做：裁剪 assets_data（619 MB -> 估计 200 MB）

`dbstat` 实测分布：

| 项 | 体积 | 处理 |
|---|---|---|
| `skeleton_bones` + 2 个索引 | 207 MB | 只留「舰船 + 舰炮」stem（现有 27,016 个 stem，含飞机 AAB、建筑、港口） |
| `skeleton_mounts` + 2 个索引 | 161 MB | 同上（现有 7,219 个 stem） |
| `render_sets` + 2 个索引 | 214 MB | 只留 `/ship/` 与 `/gun/` 路径；丢掉 `other` 97k 条（location/建筑/飞机） |
| `material_full` | 18.5 MB | 可只留被 render_sets 引用到的 mfm |
| `camo_thumbs` | 0 行 | 直接删表（`exteriors` 已改为 GameParams 入库，WG 2,362 条） |

**约束**：必须按路径类型裁剪，不能按船名——否则以后导冷门船会缺数据。

### C. 待做：去掉二级索引

`idx_rs_geom`(48 MB)、`idx_bones_stem`(43 MB)、`idx_skel_stem`(31 MB) 合计约 122 MB。
导出是「打开库、按 geom_path 批量取一次」的一次性扫描，索引收益不如它占的空间；
可改成导出前临时建、用完丢。

## P2：工程化

- 没有自动化测试 / CI；
- 没有 NOTICE（kraken.py 为 GPLv3）；
- `docs/sample` 约 111 MB，未纳入 git（.gitignore 已忽略）；
- 文档中的样本清单需要随导出更新。

## 已修复（备查）

- `exteriors` 表恒为 0 行：入库原本读 `data/split/Exterior/*.json`，而 split 是导入临时产物
  （`keep_split_json=False` 会清）且后台任务与主流程写 split 有竞态，失败还被 `except: pass`
  吞掉 → 每次导出都要现场解 GameParams。改为直接从 GameParams 解析
  （`core.gameparams.exterior_entities`），split 仅作回退；`data_json` 只存涂装相关 10 个字段
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
- `GeometryService.load_ship` docstring 写 `lod=-1` 默认，实际签名是 `2`（导出默认 LOD2）；

## 测试样本

```text
docs/sample/                         默认参数（texture 1024 / LOD2）
  PBSD511_Waterloo/                    24 MB   Lesta，主 glb 18.0 MiB（已剔除 decal）
  PJSB018_Yamato_1944/                 93 MB   Lesta，主 glb 17.2 MiB（含 4 套 model skin）
  PJSB018_Yamato_1944_WG/             256 MB   WG，主 glb 22.8 MiB（含 8 套 model skin
                                               + before.glb 37.2 MiB，剔除 crack 前的对照件）
  PBSD511_Waterloo_before_array_fix/   23 MB   修复前的 Waterloo（无 artMap），对照用，可删
```

2026-10-26：数组误判修完后三份样本按「复现命令」重导（`--armor --skins`，大和另加
`--model-skins`，因此比旧的「不含皮肤」样本大不少；想要小样本就去掉这两个开关）。
Waterloo 的 `layers` 恢复 `artMap`（1024×512）+ `materialIdMap`；
两份大和各多出 2 个材质的 4K 法线（59→61 / 58→60）。

本地预览：

```powershell
cd docs
python -m http.server 8767
```

```text
http://127.0.0.1:8767/threejs-example.html?ship=PBSD511_Waterloo&root=sample/PBSD511_Waterloo/
```

WG 大和新旧对照（`before` 用的是备份的 manifest/glb，`geometry` 字段已指向 `*.before.glb`）：

```text
# 剔除 crack 后
http://127.0.0.1:8767/threejs-example.html?ship=PJSB018_Yamato_1944&root=sample/PJSB018_Yamato_1944_WG/

# 剔除前（含断面）
http://127.0.0.1:8767/threejs-example.html?ship=PJSB018_Yamato_1944.before&root=sample/PJSB018_Yamato_1944_WG/
```

## 复现命令

> 以下命令默认输出完好状态（`*_crack_*` 断面已剔除）；需要沉没断裂几何时追加 `--damage`。

Waterloo：

```bash
python cli.py ship --game D:\Games\Korabli --ship PBSD511_Waterloo \
  --out out --armor --skins
```

WG 大和：

```bash
python cli.py ship --server wg --game D:\Games\World_of_Warships \
  --ship PJSB018_Yamato_1944 --out out --armor --skins --model-skins
```

Lesta 大和：

```bash
python cli.py ship --game D:\Games\Korabli --ship PJSB018_Yamato_1944 \
  --out out --armor --skins --model-skins
```
