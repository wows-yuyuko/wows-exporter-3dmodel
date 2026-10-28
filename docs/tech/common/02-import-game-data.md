# 02 · 导入 GameParams → `game_data*.db`

> 入口：`cli.py import` → `services/processor_service.run_process()`
> schema：`resources/database/{lesta,wargaming}/database_new.sql`

## 1. 输入文件与解码

`import` 先把 GameParams 复制进 `data/`（`cli.py:cmd_import`），再交给
`processor_service._process()`：

```python
gpd = open(data/GameParams*.data).read()
gpd = gpd[::-1]                      # 1) 整体反转
data = zlib.decompress(gpd)          # 2) zlib
pickle.loads(gpd, encoding='latin1') # 3) pickle → GPData / dict
```

两服的 pickle **顶层结构不同**，靠"有没有 `''` 键"分流
（`processor_service.py:255-273`）：

| 服务器 | 顶层结构 | 遍历方式 |
|---|---|---|
| Lesta | `list[dict]`，每个元素一个实体，`idx==0` 是表头 | `enumerate(data)`，`typeinfo` 由 `ti = idx` 标记 |
| Wargaming | `dict`，实体全在 `data['']` 下 | 取 `source_dict`，`typeinfo=None` |

`core/gameparams.py`（CLI 列船用）额外支持 WG 的 **deflate 变体**：
`zlib.decompressobj(-15)` 解出后整体反转再 zlib（`core/gameparams.py:40-44`）。
两条路径互不依赖：`list`/`export-keys` 走 `core.gameparams`，`import` 走
`processor_service`。

## 2. 拆分与入库

每个实体按 `typeinfo.type` 归类（`TYPE_CATEGORY_MAP`，`processor_service.py:180`）：

```python
{"Ship": "Ship", "Gun": "Gun", "Projectile": "Projectile",
 "Aircraft": "Aircraft", "Ability": "Ability",
 "Modernization": "Modernization", "Crew": "Crew",
 "Other": "Other", "Exterior": "Exterior"}
```

WG 的类型集合可能与 Lesta 不同，差异预留在 `services/wg_compat.py`
（`get_type_category_map(wows_type)`；当前**未填充** → 回退 Lesta 默认）。

落库实际只写这些：

| 目标 | 内容 | 说明 |
|---|---|---|
| `data/split/<Category>/*.json` | 每实体一个 JSON | **导入过程的临时产物**，收尾即删（没有开关控制）。只供同一次导入里的后续步骤读，跨进程不再存在 |
| `entity_snapshots` | 实体原始 JSON | **只写 `entity_type='ship'`**（见下） |
| `ship_models` | 模型目录名 | 导出期取 `model_folder` |
| `meta_import_state` / `data_version_registry` | build 号与版本状态 | `core/build_info.imported_build()` 读 |

> ⚠️ **分析层已裁剪**：`processor_service._run_analysis()` 第一行就是 `return`
> （函数体保留作参考）。所以 `ship_basic_info` / `projectile_*` / `crew_*` /
> `name_mappings` 等结构化表**新导入不再写入**，库里现有的行数是旧版导入的遗留。
> 不要再按"导入会刷新这些表"来做设计。

```python
# processor_service._finalize_import
db.save_entity_snapshots([s for s in snapshots_batch if s[1] == "ship"], version_code=...)
```

限定 ship 是 2026-10-26 的裁剪：其余 2.3 万条 Gun/Projectile/Aircraft/… 快照
无人读取，占约 100 MB。实测 `game_data_wg.db` 318 → 157 MB，`game_data.db`
162 → 120 MB。ship 快照平均 105 KB/条（`A_AirDefense` 26% / `A_Hull` 15% /
`A_ATBA` 10% 都是挂载 model 路径来源，**不可再裁**）。

## 3. 表结构与"实际被谁读"

`database_new.sql` 两服各有 **50+ 张表**，但导出链路只碰 4 张：

| 表 | 导出期用途 | 代码 |
|---|---|---|
| `meta_import_state` | 取 build 号（`8867689_8867689` → `8867689`） | `core/build_info.imported_build()` |
| `entity_snapshots`（`entity_type='ship'`） | 舰船 JSON：装甲厚度字典、各挂载组件 model 路径、`permoflages` | `geometry_service._ship_snapshot()`、`camo_service` |
| `ship_models` | 模型目录名（`model_folder`） | `database_service.load_ship_models()` |
| `entity_registry` | 实体 id / 名称 / 国籍 | 列船兜底 |

其余（`ship_basic_info`、`ship_module_*`、`projectile_*`、`crew_*`、`consumable_*`、
`name_mappings`、`enum_translations` …）属于原「数据浏览/对比」功能：导出不读，
**新导入也不再写**（分析层已裁剪，见 §2 的警告）。可以直接删表瘦身；
`assets_data` 侧的裁剪方案见 [../../known-issues.md](../../known-issues.md) 的「数据库精简」。

## 4. 版本与一致性

- `DB_SCHEMA_VERSION = 48`（`database_service.py:20`）。库里版本更低时
  `initialize()` **整库重建**（`_schema_rebuilt=True`），调用方据此提示"需重新加载数据"。
- 每次导入 `begin_version()` 写 `data_version_registry`，全部成功后才
  `mark_version_complete()` 写 `meta_import_state`。中途被杀 → 半成品不会被
  `get_latest_version_code()` 当成可用。
- 收尾 `checkpoint("TRUNCATE")` 回收 WAL（大 WAL 会拖慢后续开库）。
- `assets_data*.db` 由后台线程并行写，主流程 `assets_done.wait(timeout=900)`；
  超时或失败 → 整个 import 报错终止（不标完成）。

## 5. 两服差异（详表见 `../servers/`）

| 项 | Lesta | WG |
|---|---|---|
| 库文件名 | `game_data.db` | `game_data_wg.db` |
| schema 目录 | `resources/database/lesta/` | `resources/database/wargaming/` |
| 独有表 | — | `ship_module_atba_config`、`crew_skill_groups` |
| GameParams 文件名 | `GameParams_py3.data` 优先 | `GameParams.data` 优先 |
| 当前舰船数 | 1,165 | 1,246 |

SQL 里两服**同名表也可能列不同**（如 WG 的 `ship_module_atba_config`），
所以 schema 必须按服务器取目录，不能共用一份。
