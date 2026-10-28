# Lesta（Korabli）专属事实

> 与 WG 的差异总表见 [../README.md](../README.md#服务器隔离总表)。
> 本文只写"Lesta 特有"的内容，共用实现看 `../common/`。

## 1. 客户端与文件

| 项 | 值 |
|---|---|
| 本机路径 | `D:\Games\Korabli` |
| `bin/` 目录 | `8867689`（库里记录的）、`8870710`（更新的一份） |
| GameParams | `data/GameParams_py3.data`（9.8 MB，优先）、`GameParams_py2.data`（13.3 MB） |
| GameParams 编码 | 反转 + zlib + pickle（`latin1`） |
| 数据库 | `data/game_data.db`、`data/assets_data.db` |
| SQL schema | `resources/database/lesta/` |

`cli._gameparams_path()` 的优先序是 **py3 → py2 → data**，所以 Lesta 走 py3。
注意 `bin/` 里已有比库里更新的 `8870710` —— 只要没重跑 `import`，
`assets_data.db` 的 `meta.bin_folder` 仍是 `8867689`。

## 2. 素材格式

| 项 | Lesta |
|---|---|
| assets.bin Prototype 类型 | **12 类**（多 `ModelFbxPrototype`，空 blob） |
| Material 条目尺寸 | `0x88` |
| Visual 条目尺寸 | `0x40` |
| Model 条目尺寸 | `0x20` |
| blob index → 类型 | 与 WG **完全不同**，必须按 magic 识别 |

## 3. INDEXED（视觉 2.0）只在这里

| 项 | 值 |
|---|---|
| `material_full.family='indexed'` | **192 个** |
| 典型材质 | `TL2_SHIPMAT_INDEXED_PBS_Hull`（Waterloo 船体） |
| 贴图 | `CIT000_1k_ship_tiles_{a,n,mg}.dds`（1024² × 30 层）+ `*_matid.dd0` |
| 数组 | 6 个 196×4 |

WG 侧 indexed = 0，所以 `05-materials-textures.md` 的 INDEXED 章节只对 Lesta 成立。

两艘样本船的材质模式：

| 船 | 材质族 |
|---|---|
| `PBSD511_Waterloo` | 1 个 indexed（船体）+ 14 pbs |
| `PJSB018_Yamato_1944` | 全 pbs（78 个） |

## 4. 数据规模（2026-10-26）

| 指标 | 值 |
|---|---|
| `assets_data.db` | 554 MB |
| `game_data.db` | 120 MB |
| `render_set` | 399,008 |
| `material_full` | pbs 16,357 / indexed 192 / other 12,772 |
| `skeleton_bones` / `skeleton_mounts` | 全量（含飞机/建筑/港口） |
| `ship_basic_info` | 1,165 |
| `exteriors` | 1,915（Ensign 800 / Skin 603 / Permoflage 159 / Camouflage 155 / Decal 59 / MSkin 56） |
| `camouflages` | 384 |

## 5. 本机样本

产物在 `docs/sample/lesta/`（默认参数、`--slim`、无皮肤）：

| 船 | key | 说明 |
|---|---|---|
| Waterloo | `PBSD511_Waterloo` | INDEXED 舰体；731 网格 / 297,425 三角 / 8.9 MiB |
| 大和 | `PJSB018_Yamato_1944` | 全 PBS；422 网格 / 245,767 三角 / 8.6 MiB |
| 星之北风 | `PJSD889_Kitakaze_PR` | PR 船；265 网格 / 89,771 三角 / 4.0 MiB，`base_skin = PJES509` |

## 6. Lesta 特有的坑

1. **INDEXED 船体是分段外壳，内部空腔无遮挡** —— 曾报「从船首能一路看到船尾螺旋桨」，
   2026-10-27 已确认不再出现（`../../known-issues.md` 状态表标记「舰体深色/透视块：已解决」）。
   原始分析与试过的三种做法只在 git 历史里，当前代码不含相关特判。
2. **`.visual` 与 WG 是两套二进制格式** —— 字段布局不同，解析时不能互相套用。
3. `data/GameParams_py2.data` 与 `py3` **同时存在**时优先 py3；
   若某版本 py3 缺失或解析失败，需要显式 `--gp`。

> 曾经的第 2 号坑「artMap 被 `_is_dds_array()` 的 8 MB 阈值误判」已修：
> 判据改为纯结构判定，不再看文件大小（见 `../../todo-backlog.md` P0-1）。
