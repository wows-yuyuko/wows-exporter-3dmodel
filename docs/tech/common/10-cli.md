# 10 · CLI 参考（`python cli.py …`）

> 代码：`cli.py`（单文件）。**默认动作 = 增量同步数据库 + 导出选中的船**，
> 只有 `import`（只同步）和 `diff`（只对比）是子命令。
> 本文按 2026-10-28 的代码校对；默认值、产物布局、行为都以代码为准。

## 1. 一个入口

```text
python cli.py [--server {lesta,wg}] [--game DIR] [--gp FILE] [--out DIR]
              [--ship SPEC]... [--jobs N] [--limit N] [--force] [--no-sync]
              [--rebuild] [--bin BUILD] [--version TEXT]
              [全部导出开关，见 §3]
```

不带子命令就是「同步 + 导出」。`--server` 必填（决定读哪一服、默认游戏路径取哪个）。

| 命令 | 干什么 | 需要先建库 |
|---|---|---|
| `python cli.py --server lesta` | **同步 + 导出全部船** | 自动（首次建库、之后增量） |
| `python cli.py --server lesta --ship 10` | 同步 + 只导 10 级 | 自动 |
| `python cli.py import --server lesta` | 只建库 / 同步，不导出 | — |
| `python cli.py diff --server lesta` | 只对比两个 build（只读 idx，不建库） | ❌ |

`--game` 不给就取 `config.json` 里该服的路径（`game_path_lesta` / `game_path_wargaming`）；
`--out` 默认 `out`。

## 2. `--ship` 筛选（可重复，取并集）

| 写法 | 例子 | 匹配 |
|---|---|---|
| 完整 key | `PJSB018_Yamato_1944` | `ship.key` 精确（大小写不敏感） |
| idx | `PJSB018` | key 的第一段（`PJSB018_...`） |
| 模型目录 | `JSB039_Yamato_1945` | `ship.model_folder` |
| 等级 | `10` / `3,4,7` / `5-11` | `ship.tier` |
| 子串（兜底） | `Yamato` | 精确都没命中时按 key 子串匹配 |

```bash
python cli.py --server lesta --ship 10              # 232 条
python cli.py --server lesta --ship 3,4,7           # 189 条
python cli.py --server lesta --ship 5-11            # 943 条
python cli.py --server lesta --ship PJSB018 --ship PJSD889   # 2 条
```

一个精确写法都没命中的 spec 会打印 `[warn] --ship 'x' 没匹配到任何船`。

## 3. 导出开关

### 3.1 输出与内容

| 参数 | 默认 | 说明 |
|---|---|---|
| `--out DIR` | `out` | 输出根（布局见 §6） |
| `--no-textures` | 关 | 不导贴图：GLB 里没有 image，也不写 `textures/` |
| `--zstd` | 关 | 额外写 `.glb.zst`（zstd level 10，仅传输压缩） |
| `--armor` | 关 | 额外导 `armor/<ship>/`（`.glb` + `.json` + `.bin`） |
| `--damage` | 关 | 保留破损断面 `*_crack_*`（默认剔除） |
| `--decals` | 关 | 保留平面贴花 `SHIP_MESHDECAL_*`（默认剔除；Waterloo 有 202 片 / 169k 三角） |

### 3.2 几何

| 参数 | 默认 | 说明 |
|---|---|---|
| `--lod N` | **2** | `-1` = LOD0；`0–3` 指定级别。目标级面数不足（< LOD0 的 5% 且 < 50 面）的部件回退 LOD0；`wire/patch/perforation/glass/_net` 不参与面数保护 |
| `--merge-shapes` | 关 | 每段合成一个网格（接缝少，但**会丢 shape**） |
| `--split-shapes` | 关 | 按 `shape_spans` 拆分 —— **这就是默认行为**，只为显式声明 |

`--merge-shapes` / `--split-shapes` 是 argparse **互斥组**，同时给会直接报错。

### 3.3 材质与贴图

| 参数 | 默认 | 说明 |
|---|---|---|
| `--bake-indexed` | **开** | Lesta INDEXED（视觉 2.0）材质烘成标准 baseColor/normal/MR，标准查看器直接可看 |
| `--no-bake-indexed` | — | 保留原始图集数组 + 6 组 LUT（只有自带 shader 的页面画得对；可实时换涂装） |
| `--bake-size PX` | **0** | 烘焙贴图最长边；`0` = 跟随 materialIdMap 原生尺寸（Waterloo 4096×2048，实测 13 s） |
| `--emissive` | 关 | 自发光 = `底色 × _mg.B`。默认关：实测 B 是连续通道（船体 ≈0.5、甲板灯 ≈0.9、信号灯 =0），当掩码用会让整船半自发光 |
| `--texture-size N` | **1024** | 贴图最大边长（小图不放大）；改这个值会生成一整套新的贴图文件名 |
| `--slim` / `--no-slim` | **开** | 顶点属性量化（`KHR_mesh_quantization`）：位置 u16、法线 i8、UV u16、索引 u16，并丢掉 `_T4`/`TEXCOORD_1`/`TANGENT`。Waterloo GLB 18.7 → 8.8 MiB（-53%），A/B MAE 0.111 |

### 3.4 皮肤

**皮肤不是默认产物**：不加 `--skins` / `--model-skins` 时不会写 `skins/`，
`manifest.skins` 是空数组。

| 参数 | 默认 | 说明 |
|---|---|---|
| `--skins` | 关 | 导出材质皮肤（mskin / skin），每套一个 `skins/<ship>/<id>.json` |
| `--permoflage` | 关 | 连同永久涂装一起导（需同时给 `--skins`） |
| `--model-skins` | 关 | 把**所有**换模型皮肤各导一套 GLB（`<ship>__<id>.glb` + manifest + skin json） |
| `--model-skin ID\|auto` | **auto** | 把某套模型皮肤当**基准外观**，产物仍是 `<ship>.glb`，manifest 记 `base_skin` |
| `--no-model-skin` | — | 永不套模型皮肤，直接用 `ship.model` |

`auto` 的规则：**只有该船 `permoflages[0]` 是模型皮肤时才套用那一套**。

| 船 | 首个 permoflage | `auto` 结果 |
|---|---|---|
| 大和 `PJSB018`（科技树） | `PCEP120_Permo_Upgr_10_lvl`（Permoflage） | 不套，导原版 |
| 北风 `PJSD219`（科技树） | `PCEP119_Permo_Upgr_9_lvl`（Permoflage） | 不套 |
| ARP 大和 `PJSB700`（联动） | `PJES340_Yamato_ARP`（Skin） | 套 `PJES340` |
| 星之北风 `PJSD889`（PR） | `PJES509_Kitakaze_ranger`（Skin） | 套 `PJES509` |

指定具体 ID 时按 `ext_index` / `raw_name` / `display_name` 匹配，找不到会列出可选项后退出。

## 4. `import` —— 建库 / 增量同步

```text
import --server {lesta,wg} [--game DIR] [--gp FILE] [--bin BUILD] [--version TEXT] [--rebuild]
```

默认动作里**自动执行**这一步（`--no-sync` 可跳过）。流程：

1. 设 `wows_type / game_path / bin_folder`；
2. 把 GameParams 复制进 `<project>/data/`；
3. `bin_folder` = `--bin`，否则 `<game>/bin/` 下**数字最大**的目录；
4. 扫 idx 建模型清单（**5.5 s**，不解包几何）→ 与库里的 `model.sig` 比对；
5. 只重写内容变了的行（快照 sha1 / 模型签名），其余仅刷 `last_checked`；
6. 有模型变动才同步 3D 缓存（`assets_data.db`），否则直接跳过；
7. 超时 **1800 s**；失败 `SystemExit(1)`。

| 场景 | 耗时（Lesta 实测） |
|---|---|
| 首次 / `--rebuild` | 约 4 min（其中 92 s 是读 assets.bin） |
| 同 build 无变更 | **15–18 s**（只扫 idx + 刷时间戳，不碰 assets.bin） |
| 版本更新（几条船变了） | 18 s + 重建那几个模型的 3D 缓存 |

`--rebuild` 忽略增量判据，强制全量重解析 3D 缓存 —— 解析逻辑改过
（或怀疑缓存脏了）时用。它不会重建 `game_data.db` 的行，只重写内容变化的。

客户端更新后必须重跑；不做增量校验的话 build 号与资产会对不上
（旧版直接全清重建，现在是 upsert + 变更集）。

## 5. `diff` —— 版本对比（诊断用）

```text
diff --server {lesta,wg} [--game DIR] [--old BUILD] [--new BUILD]
     [--format text|json] [--out FILE]
```

`--old` / `--new` 缺省时取 `<game>/bin/` 下**数值最大的两个**（新 = 最大）。

**判据是 idx 里本来就存的 `crc32`**（解压后内容的 CRC32，实测与 `zlib.crc32(内容)` 一致），
所以**完全不加载 assets.bin、不解包几何**：

| 层 | 做什么 | 成本 |
|---|---|---|
| 读两版 idx | 各 ~3 万个 `.geometry` 的 `(crc32, size)` | 两版合计 ~12 s |
| 比对 | crc+size 一致 → 视为没变 | 毫秒级 |
| 映射 | 目录名 → 船（`ship` + `ship_model`） | ~2 s |

实测（WG `13187581 → 13357625`，3.2 万个同名 geometry 里只有 25 个文件变）：

```text
对比 13187581 → 13357625
  .geometry：旧 32050 / 新 32103，同名 63686
  变动 19 个模型目录（25 个文件）

船体变动 7 条（其中 1 条动了主文件，可能含装甲）：
  PASC597_Nueve_de_Julio_1951   ASC028_Boise_1951        主文件
  PWSC016_Tre_Kronor            WSC305_Tre_Kronor_1947   仅分段 + 1 分段
  ...

挂载模型变动 12 个，波及 21 条船：
  JGM055_100mm65_Type98         PJSB508_Kii、PJSD210_Harugumo、…
```

字段含义：
- **主文件** = `<folder>.geometry`（**装甲只在这里**，分段实测恒 0）；
- **仅分段** = 只动了 `_Bow` / `_MidFront` 等，属于模型细节，装甲没变；
- 挂载是**多船共享**的，改一个会影响一批船 —— 所以输出按「模型目录 → 引用它的船」列。

**它和默认流程的关系**：默认动作已经自动同步，所以 diff 不是必经步骤。
它的价值是「不碰数据库就能先看影响面」—— 比如只想确认这次更新动了几条船。

**局限（口径：宽松）**：
- crc32 覆盖整个 `.geometry`（含顶点/索引）→ **只改模型细节也会判为变动**，宁可多导不漏导；
- 只比 `.geometry`。**纯 GameParams 变动**（改装甲厚度、改挂载配置、改涂装归属）不会被发现
  （但 `import` 的快照 sha1 能发现，见 §4）；
- 新增/删除的 geometry 会单独列出，但不自动转成船 key；
- 没有 `game_data*.db` 时退化为只报模型目录名，映射不到船。

## 6. 增量导出与 `--jobs`

输出根下维护 `.export_index.json`：

```text
签名 = sha1(server | build | ship | lod | texture_size | 所有开关)
```

- 产物存在 **且** 签名一致 → `[skip]`（实测同参数复跑 3m14s → **2.3 s**）；
- 任一开关或 build 变化 → 重导（换皮肤、换 LOD、关烘焙都会触发）；
- `--force` 忽略签名全量重导；
- `--limit N` 只导前 N 条（在选船之后截断）。

### `--jobs`

| 值 | 行为 |
|---|---|
| **1（默认）** | 进程内串行：GameParams / DB 只初始化一次，逐船调 `_export_ship()` |
| `> 1` | 按 key **交错**切成 N 份 → N 个 `--worker` 子进程并行（内部开关，不出现在 `--help`） |

**局限**：签名是**输入级**（build + 参数），不含产物内容哈希。同一 build 内资产被改动
（换了 mod 包但版本号没变）不会自动重导。

### 输出行

```text
[sync] build 8870710 ← GameParams_py3.data
  📦 战舰 新增 0 / 变动 0 / 未变 1165 / 下架 0；模型 新增 0 / 变动 0 / 未变 21232 …
[sync] done
[plan] 1 / 1070 艘 → out
[skip] 12 艘未变（签名命中），直接跳过
[done] 1 / 1 exported to out
```

## 7. 产物布局

```text
<out>/
  geometry/<ship>/<ship>.glb          渲染模型（可再带 <ship>__<skin>.glb）
  manifests/<ship>/<ship>.json        入口索引（materials / meshes / skins / hashes / armor / base_skin）
  armor/<ship>/<ship>.{glb,json,bin}  装甲（仅 --armor）
  skins/<ship>/*.json                 皮肤元数据（仅 --skins / --model-skins）
  textures/<sha1>.webp                贴图池（扁平、按内容哈希去重、跨船共享）
  .export_index.json                  增量索引
```

GLB 内的贴图 URI 是相对 GLB 的 `../../textures/…`；manifest 里的路径统一相对输出根。

## 8. 退出码与排错

| 情况 | 表现 |
|---|---|
| 正常 | exit 0 |
| 船找不到 / 加载异常 | `SystemExit(...)` 或单船 `[fail]`，见下 |
| 同步失败或超时 | `SystemExit(1)` |
| 单船失败 | 打印 `[fail] <key>: <异常>`，**进程仍退 0** —— 要看日志或产物，不能只看退出码 |

常见问题：

- `--server 必填` → 没给 `--server`；`--game` 也没配进 `config.json`；
- `没有匹配的船` → `--ship` 的 spec 一个都没命中（注意等级是 `10` 不是 `T10`）；
- 页面 404 / 空白 → `--out` 根与页面 `root=` 参数不一致；改了布局或文件名要硬刷新；
- 同名船被覆盖 → 两个服务器导进了同一个 `--out`（`PJSB018_Yamato_1944` 两边都有）；
- 贴图缺失 → 用了 `--no-textures`，或换了 `--texture-size` 却沿用旧的 `textures/`。

## 9. 已修正的历史问题（备查）

| 问题 | 现状 |
|---|---|
| `--split-shapes` 死参数（解析了但不用） | 已接线，且与 `--merge-shapes` 互斥 |
| `--merge-shapes` 帮助写 "(default)"，与实现相反 | 帮助改为实际语义 |
| `load_ship` docstring 写 `lod=-1` 默认 | 改为"默认 2"，并说明 `-1` = LOD0 |
| `--jobs 2` 报 `unrecognized arguments` | 子进程补齐全部开关（现共用 `_add_export_flags`） |
| 只按"文件存在"跳过 | 改为 build+参数的签名索引（§6） |
| `--limit` 在并行分支被忽略 | 选船后统一截断，两条分支都生效 |
| 命令面 7 个子命令、6 个帮助空白 | 收敛为「默认动作 + `import`/`diff`」，全部带说明 |

## 10. 典型命令

```bash
# 建库 / 更新（客户端更新后跑一次；日常不用单独调，默认动作会自动做）
python cli.py import --server lesta

# 全部导出（首次 ~4 min 同步 + 按船数导出）
python cli.py --server lesta --out out/lesta

# 只导 10 级
python cli.py --server lesta --ship 10 --out out/lesta

# 指定几条（idx + 完整 key 混用）
python cli.py --server lesta --ship PJSB018 --ship PJSD889 --out out/lesta

# 渲染 + 装甲 + zst
python cli.py --server lesta --ship PJSB520_Raiju --out out/lesta --armor --zstd

# 快速预览：低模 + 小贴图
python cli.py --server lesta --ship PBSD511_Waterloo --out out/lesta --lod 2 --texture-size 512

# 联动船本来的样子（默认 auto 已自动套模型皮肤）；要原版就关掉
python cli.py --server wg --ship PJSB700_ARP_Yamato --out out/wg
python cli.py --server wg --ship PJSB700_ARP_Yamato --out out/wg --no-model-skin

# 只想知道这次更新动了哪些船（不碰数据库）
python cli.py diff --server wg

# 多进程
python cli.py --server lesta --ship 10 --jobs 4 --out out/lesta
```
