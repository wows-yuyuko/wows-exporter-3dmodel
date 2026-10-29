# 02 · 导入 GameParams → `game_data*.db`

> 入口：`cli.py import` → `services/processor_service.run_process()`
> schema：`resources/database/{lesta,wargaming}/database_new.sql`
>
> **单版本镜像 + 增量同步**（2026-10-28 重构）：库只反映最后一次同步的客户端 build；
> 内容没变的行不重写，只刷 `last_checked`。

## 1. 输入文件与解码

`import` 先把 GameParams 复制进 `data/`（`cli.py:cmd_import`），再交给
`processor_service._process()`：

```python
gpd = open(data/GameParams*.data).read()
gpd = gpd[::-1]                      # 1) 整体反转
gpd = zlib.decompress(gpd)           # 2) zlib
pickle.loads(gpd, encoding='latin1') # 3) pickle → GPData / dict
```

两服的 pickle **顶层结构不同**，靠"有没有 `''` 键"分流：

| 服务器 | 顶层结构 | 遍历方式 |
|---|---|---|
| Lesta | `list[dict]`，每个元素一批实体 | 逐个 `elem.items()` |
| Wargaming | `dict`，实体全在 `data['']` 下 | 取 `source_dict` |

`core/gameparams.py`（CLI 列船用）额外支持 WG 的 **deflate 变体**：
`zlib.decompressobj(-15)` 解出后整体反转再 zlib。两条路径互不依赖：
`diff` 走 `core.gameparams`，`import` 走 `processor_service`。

## 2. 只保留舰船

`typeinfo.type == "Ship"` 的实体才进库 —— 导出链路只读舰船（其余 2.3 万条
Gun/Projectile/Aircraft 快照无人读取，曾占 100+ MB）。

> ⚠️ 顺序很重要：**先按 `typeinfo.type` 过滤，再 `json.dumps`**。
> 2.3 万实体全做一次 json 往返要几十秒，而最终只留 1.1 千条。
> 实体在 pickle 里是自定义对象不是 dict，取类型要走 `_entity_type()`（兼容两者）。

每艘船产出两样东西：

| 产物 | 内容 | 去向 |
|---|---|---|
| 元信息 | `id` / `index` / `name` / `nation` / `level` / `typeinfo.species` / `group` / 船体模型目录 | `ship` |
| 规范化快照 | `json.dumps(entity, sort_keys=True, ensure_ascii=False)` + `sha1` | `ship_data` |

快照是**装甲厚度、挂载 model 路径、`permoflages`** 的唯一来源，导出期必读。
平均约 100 KB/条，占整库 84%，所以增量同步的第一个优化就是「sha1 没变就不重写它」。

## 3. 表结构（7 张表）

```text
ship          主表：一艘船一行（数字 id 主键，GameParams 的 $.id）
ship_data     从表：快照 JSON（1:1，单独存放，避免整表扫描拖入大 blob）
model         从表：一个模型目录一行（21k）
model_file    从表：模型目录下的 .geometry 清单（crc32 + size）
ship_model    关联：船 ↔ 模型 N:N（挂载模型多船共用，40k 行）
meta          key/value：build / game_version / synced_at / assets_build …
meta_schema_version  schema 版本（低版本 → 整库重建）
```

为什么「船 : 模型」不是 1:1：一条船有 1 个船体 + N 个挂载；而
`JGM055_100mm65_Type98` 这类挂载被 13 条船共用。所以必须经 `ship_model`
关联，`role` 记位置（`hull` / `A_Artillery.HP_JGM_1`）。

> `ship.name` 与船体模型目录**不是同一个字符串**：
> `PBSD511_Waterloo`（带 P 前缀 + 编号）vs `BSD100_Waterloo_1956`（另一套编号）。
> 两者只能靠快照里 `A_Hull.model` 的路径关联。

## 4. 增量规则

每个元信息行带三个时间戳 + 两个 build：

| 字段 | 什么时候写 |
|---|---|
| `first_build` / `first_seen` | 首次入库，之后不变 |
| `last_build` / `last_changed` | 内容**真变了**（快照 sha1 或模型签名变化） |
| `last_checked` | 每次同步都刷（没变也刷） |

判据：

| 对象 | 判据 | 代码 |
|---|---|---|
| 船 | `ship_data.sha1`（规范化 JSON 的哈希） | `_build_ship_rows()` |
| 模型 | `model.sig` = sha1(目录名 + 每个 `.geometry` 的 `(文件名, crc32, size)`) | `services/model_index.py` |

`model.sig` 只读 `bin/<build>/idx/*.idx`（21k 目录 / 27k geometry，实测 **5.5 s**），
**不加载 assets.bin、不解包几何**。crc32 是 idx 里本来就存的「解压后内容 CRC32」。

传播：模型变了 → 引用它的船也算变动（挂载是共享件）：

```sql
UPDATE ship SET last_build=?, last_changed=?
WHERE id IN (SELECT ship_id FROM ship_model WHERE model_id IN (...))
```

实测（同一 build 重复 import）：

```text
战舰 新增 0 / 变动 0 / 未变 1165 / 下架 0
模型 新增 0 / 变动 0 / 未变 21232 / 消失 0
→ 18 s（全量首次约 4 min，其中 92 s 是读 assets.bin）
```

下架的船**不删行**，只置 `status='removed'` —— 「这船曾经存在」这个事实要留着，
否则下次又出现时无法区分「回归」和「新增」。

## 5. 一致性

- `DB_SCHEMA_VERSION = 49`。库里版本更低时 `initialize()` **整库重建**
  （`_schema_rebuilt=True`），之后就一直增量。
- 全部写在一个事务里，最后一次 `commit()`；中途被强杀不会留半成品
  （`meta.build` 仍是上一次成功同步的值）。
- 收尾 `checkpoint("TRUNCATE")` 回收 WAL。
- `assets_data*.db` 由后台线程并行同步，主流程在工作线程里等 `assets_done`
  （最长 900 s）。成功后写 `meta.assets_build`；失败则不写，下次 `import`
  发现 `assets_build != build` 会自动重试。

## 6. 两服差异

| 项 | Lesta | WG |
|---|---|---|
| 库文件名 | `game_data.db` | `game_data_wg.db` |
| schema 目录 | `resources/database/lesta/` | `resources/database/wargaming/` |
| GameParams 文件名 | `GameParams_py3.data` 优先 | `GameParams.data` 优先 |
| 当前舰船数 | 1,165 | 1,246 |
| 当前模型目录 | 21,232 | —（未实测） |

两服的库**绝不合并**：`PJSB018_Yamato_1944` 在两边同名但内容不同。
schema 文件也各一份（当前两份内容一致，保留分目录是为了以后能按服调）。
