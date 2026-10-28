# Wargaming（WG）专属事实

> 与 Lesta 的差异总表见 [../README.md](../README.md#服务器隔离总表)。
> 本文只写"WG 特有"的内容，共用实现看 `../common/`。

## 1. 客户端与文件

| 项 | 值 |
|---|---|
| 本机路径 | `D:\Games\World_of_Warships` |
| `bin/` 目录 | `13187581`、`13357625` |
| GameParams | `data/GameParams.data`（17.0 MB，**唯一一份**） |
| GameParams 编码 | 反转 + zlib + pickle（`latin1`）；`core.gameparams` 另有 deflate 变体回退 |
| 数据库 | `data/game_data_wg.db`、`data/assets_data_wg.db` |
| SQL schema | `resources/database/wargaming/` |

⚠️ **build 号不一致（当前已知问题）**：
`assets_data_wg.db` 的 `meta.bin_folder = 13488861`，而客户端 `bin/` 只有
`13187581` / `13357625`。后果与修法：

- `meta` 只用于展示，不影响导出；
- `shape_names` 按 `bin_folder` **精确过滤** → 查空；
  几何侧已改用 `render_sets` 自带 `shape` 做 LOD/crack 判定，所以导出结果仍正确；
- 修法：重跑 `python cli.py import --game D:\Games\World_of_Warships --server wg`。

## 2. 素材格式

| 项 | WG |
|---|---|
| assets.bin Prototype 类型 | **10 类**（无 `ModelFbxPrototype`） |
| Material 条目尺寸 | `0x78` |
| Visual 条目尺寸 | `0x70` |
| Model 条目尺寸 | `0x28` |
| blob index → 类型 | 与 Lesta **完全不同**，按 magic 识别 |

> 两服 **Material/Visual/Model 的 magic 相同，但 item_size 不同**。
> 这是最容易踩的坑：按 Lesta 的 `0x88/0x40/0x20` 去切 WG 的 blob 会整体错位。
> 服务器上下文由 `AssetsBinService(wows_type=...)` 设置（thread-local）。

## 3. 没有 INDEXED

| 项 | 值 |
|---|---|
| `material_full.family='indexed'` | **0 个** |
| 舰体材质 | 全部 `pbs`（`PBS_ship*.fx` / `ship_material.fx`） |
| 颜色来源 | `diffuseMap`（`*_a.dds`，2048 及以下） |

实测样船 `PJSB018_Yamato_1944`（WG）：76 个材质**全 pbs**。
所以：

- 不存在"分段外壳 + 空腔"问题（`../../known-issues.md` 的 Waterloo 段不适用）；
- `docs/threejs-example.html` 的 `indexedShader` 分支在 WG 船上不会命中；
- INDEXED 相关的 6 个数组、图集下发对 WG 都是空跑。

## 4. 数据规模（2026-10-26）

| 指标 | 值 |
|---|---|
| `assets_data_wg.db` | 623 MB |
| `game_data_wg.db` | 157 MB |
| `render_sets` | 392,305 |
| `material_full` | pbs 19,708 / other 12,040 / indexed 0 |
| `ship_basic_info` | 1,246 |
| `exteriors` | 2,362（Ensign 930 / Skin 667 / MSkin 246 / Permoflage 221 / Camouflage 131 / VisualCustomization 51） |
| `camouflages` | 515 |

WG 比 Lesta 大：材质更多（19.7k vs 16.4k）、涂装条目更多（2.3k vs 1.9k）。

## 5. SQL 与表的差异

`resources/database/wargaming/database_new.sql` 比 Lesta 多两张表：

| 表 | 说明 |
|---|---|
| `ship_module_atba_config` | WG 的 ATBA（自动防空）配置，Lesta 无 |
| `crew_skill_groups` | 舰长技能分组，Lesta 无 |

其余同名表的**列集合也可能不同**，所以 schema 必须按服务器目录取，
不能共用一份 SQL。导出链路只用 4 张表（见 `../common/02-import-game-data.md` §3），
不受这些差异影响。

## 6. GameParams 类型映射

`services/wg_compat.py` 是 WG 的 GameParams 差异**预留骨架**：

- `WG_TYPE_CATEGORY_MAP` / `WG_CATEGORIES` / `WG_CAT_LABELS` / `WG_NORMALIZE_ENTITY`
  全部为 `None`；
- 调用方（`processor_service`）在 `None` 时回退 Lesta 默认映射；
- 当前 WG 导入正常，说明 `typeinfo.type` 命名与 Lesta 一致；
- 若将来 WG 改了类型命名，只改这一个文件（不要往业务代码里加 `if wows_type`）。

## 7. WG 特有的坑

1. **`.visual` 格式与 Lesta 完全不同** —— WG 是
   `<skeleton>/<properties>/<boundingBox>/<renderSets>` 结构。
2. **item_size 不同** —— 套错表会静默读出错位数据，表现为材质/节点全错。
3. **build 号过期** —— 见 §1，`shape_names` 会查空。
4. **没有 INDEXED** —— 写 INDEXED 相关代码时注意：WG 路径下永远走不到，
   但也不能假设"所有船都有 albedoArray"。
