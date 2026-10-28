# 10 · CLI 参考（`python cli.py …`）

> 代码：`cli.py`（单文件，argparse 六个子命令）
> 本文按 **2026-10-27 的代码**校对；默认值、产物布局、行为都以代码为准。

## 1. 命令总览

| 命令 | 用途 | 需要先 `import` 建库 | 典型耗时 |
|---|---|---|---|
| `list` | 列船（GameParams 直读） | ❌ | < 5 s |
| `diff` | **版本对比**：找出两版之间 geometry 变动的船（只读 idx 的 crc32） | ❌（有库才能映射到船 key） | ~15 s（两版各扫一次 idx） |
| `import` | 建库（GameParams + assets.bin → `data/*.db`） | — | 分钟级 |
| `ship` | **单船导出**（主力） | ✅ | 35 s ~ 4 min |
| `batch` | 批量导出（默认单进程 `batch-worker` 跑完整批） | ✅ | 按船数 |
| `export-keys` | 只写船 key 列表 | ❌ | < 5 s |
| `batch-worker` | 内部：`batch` 的子进程，一般不手跑 | ✅ | — |

## 2. 通用约定

| 参数 | 默认 | 说明 |
|---|---|---|
| `--game DIR` | 必填 | 游戏安装根（下面要有 `bin/`、`data/`），如 `D:\Games\Korabli` |
| `--server {lesta,wg}` | **必填** | Lesta = `Korabli`，wg = `World_of_Warships`。**没有默认值**，必须显式指定 |
| `--gp FILE` | 空 | 显式指定 GameParams（默认 `<game>/data/GameParams*.data`） |

- `bin_folder`（build 号）优先用 `import` 写进 `data/*.db` 的值；库里没有或该目录已不存在时，
  取 `<game>/bin/` 下**数字最大**的目录（`8867689` vs `8870710` 按数值比，不按字符串）。
  build 号**不写进 config.json**，客户端更新后无需改配置。
- **一个 `--out` 只能放一个服务器 + 一个 build 的产物**：船 key 相同会互相覆盖
  （Lesta 与 WG 都有 `PJSB018_Yamato_1944`）。导出时若发现已有 manifest 的 build 不同，
  会打印 `[warn] … 将被覆盖`，请分根输出（`out/lesta`、`out/wg`）。

## 3. `ship` —— 单船导出

```text
ship --game DIR --ship KEY --out DIR
     --server {lesta,wg} [--gp FILE]
     [--no-textures] [--zstd] [--armor] [--damage] [--decals]
     [--lod N=2] [--merge-shapes | --split-shapes]
     [--slim | --no-slim]
     [--bake-indexed | --no-bake-indexed] [--bake-size PX=0]
     [--emissive | --no-emissive] [--texture-size N=1024]
     [--skins] [--permoflage] [--model-skins]
     [--model-skin ID|auto|none] [--no-model-skin]
```

`--ship` 接受 GameParams key（`PJSB018_Yamato_1944`），也接受模型目录名
（`JSB039_Yamato_1945`），取第一个匹配。

### 3.1 输出与内容

| 参数 | 默认 | 说明 |
|---|---|---|
| `--out DIR` | 必填 | 输出根（布局见 §8） |
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

## 4. `list` / `export-keys`

```text
list        --game DIR --server {lesta,wg} [--filter TEXT] [--gp FILE]
export-keys --game DIR --out FILE --server {lesta,wg} [--filter TEXT] [--limit N] [--gp FILE]
```

- `list` 输出四列（Tab 分隔）：`key  model_folder  T<tier>  nation`；
- `--filter` 是**子串匹配**（大小写不敏感，同时匹配 key 与 model_folder）；空串 = 全部；
- `export-keys` 只写 key，一行一个；`--limit` 截断（`batch --jobs ≥ 1` 靠它工作）。

## 4.5 `diff` —— 版本对比（找变动的船）

```text
diff --game DIR --server {lesta,wg} [--old BUILD] [--new BUILD]
     [--format text|keys|json] [--out FILE]
```

`--old` / `--new` 缺省时取 `<game>/bin/` 下**数值最大的两个**（新 = 最大）。

**判据是 idx 里本来就存的 `crc32`**（解压后内容的 CRC32，实测与 `zlib.crc32(内容)` 一致），
所以**完全不加载 assets.bin、不解包几何**：

| 层 | 做什么 | 成本 |
|---|---|---|
| 读两版 idx | 各 ~3.2 万个 `.geometry` 的 `(crc32, size)` | 两版合计 ~12 s |
| 比对 | crc+size 一致 → 视为没变 | 毫秒级 |
| 映射 | 目录名 → 船（`ship_models` + ship 快照的 `A_*.*.model`） | ~2 s |

实测（WG `13187581 → 13357625`，共 2.4 万个改动点里只有 25 个文件变）：

```text
对比 13187581 → 13357625
  变动 19 个模型目录（25 个文件）

船体变动 7 条（其中 1 条动了主文件，可能含装甲）：
  PASC597_Nueve_de_Julio_1951   ASC028_Boise_1951        主文件
  PWSC016_Tre_Kronor            WSC305_Tre_Kronor_1947   仅分段 + 1 分段
  ...

挂载模型变动 12 个，波及 21 条船：
  JGM055_100mm65_Type98         PJSB508_Kii、PJSD210_Harugumo、…
  JGT023_610mm4_Type_93R        PJSB539_Daisen、PJSC517_Maya、…
```

字段含义：
- **主文件** = `<folder>.geometry`（**装甲只在这里**，分段实测恒 0）；
- **仅分段** = 只动了 `_Bow` / `_MidFront` 等，属于模型细节，装甲没变；
- 挂载是**多船共享**的，改一个会影响一批船 —— 所以输出按「模型目录 → 引用它的船」列。

`--format keys` 输出一行一个船 key，可直接给 `batch --keys-file`：

```bash
python cli.py diff --server wg --game D:\Games\World_of_Warships --format keys --out changed.txt
python cli.py batch --server wg --game D:\Games\World_of_Warships --out out\wg --keys-file changed.txt
```

**局限（口径：宽松）**：
- crc32 覆盖整个 `.geometry`（含顶点/索引）→ **只改模型细节也会判为变动**，宁可多导不漏导；
- 只比 `.geometry`。**纯 GameParams 变动**（改装甲厚度、改挂载配置、改涂装归属）不会被发现；
- 新增/删除的 geometry 会单独列出（新船通常伴随新增 geometry），但不自动转成船 key；
- 没有 `game_data*.db` 时退化为只报模型目录名，映射不到船。

## 5. `import` —— 建库

```text
import --game DIR --server {lesta,wg} [--gp FILE] [--bin BUILD] [--version TEXT]
```

1. 设 `wows_type / game_path / bin_folder`；
2. 把 GameParams 复制进 `<project>/data/`；
3. `bin_folder` = `--bin`，否则 `<game>/bin/` 下**数字最大**的目录；
4. 同步跑（后台线程 + `data_processed` 信号），超时 **1800 s**；
5. 失败 `SystemExit(1)`；成功打印 `import result: {'ok': True}`。

客户端更新后必须重跑（否则 build 号与资产对不上）。

## 6. `batch` —— 批量导出

```text
batch --game DIR --out DIR --server {lesta,wg} [--filter TEXT] [--keys-file FILE]
      [--jobs N=1] [--limit N] [--force] [--gp FILE]
      + ship 的全部开关
```

`--keys-file` 直接指定要导哪些船（一行一个 key，`#` 开头当注释），
与 `--filter` 是**交集**；不在当前 GameParams 里的 key 会打印 `[warn]` 跳过。
配 `diff --format keys` 就是「版本更新只导变动船」的闭环。

### 6.1 `--jobs` 三档

| 值 | 行为 |
|---|---|
| **1（默认）** | 单进程跑 `batch-worker`：GameParams / DB 只初始化一次，逐船调 `_export_ship()` |
| `> 1` | `export-keys` 写 `.keys.txt` → 按 key **交错**切成 N 份 → N 个 `batch-worker` 并行 |
| `0` | 旧行为：每艘船起一个 `ship` 子进程（进程隔离，但每条船都重新加载 GameParams/DB） |

### 6.2 增量导出

输出根下维护 `.export_index.json`：

```text
签名 = sha1(server | build | ship | lod | texture_size | 所有开关)
```

- 产物存在 **且** 签名一致 → `[skip]`（实测同参数复跑 3m14s → **2.3 s**）；
- 任一开关或 build 变化 → 重导（换皮肤、换 LOD、关烘焙都会触发）；
- `--force` 忽略签名全量重导；
- `--limit N` 只导前 N 条（在 `export-keys` 阶段截断，两档 `--jobs` 都生效）。

**局限**：签名是**输入级**（build + 参数），不含产物内容哈希。同一 build 内资产被改动
（换了 mod 包但版本号没变）不会自动重导。

### 6.3 输出行

```text
[skip] 12 unchanged ships
[worker done] 3
[done] 3 exported to out/lesta
```

`--jobs 0` 时逐船打印 `[ok]/[skip]/[fail]`。

## 7. `batch-worker`（内部）

```text
batch-worker --game DIR --keys-file FILE --out DIR
             --server {lesta,wg} [--no-textures] [--zstd] [--armor] [--damage] [--decals]
             [--bake-indexed | --no-bake-indexed] [--bake-size PX]
             [--slim | --no-slim] [--emissive | --no-emissive]
             [--texture-size N] [--lod N]
             [--skins] [--permoflage] [--model-skins]
             [--model-skin ID|auto|none] [--no-model-skin]
             [--merge-shapes] [--gp FILE]
```

只为 `batch --jobs ≥ 1` 服务；`--force` / `--limit` 由父进程处理，这里不接受。

**历史坑**：这些参数曾只在父进程解析、子命令没定义，`batch --jobs 2` 会直接
`unrecognized arguments` 退出。**以后加新开关必须两处都加**（`ship` 与 `batch-worker`，
外加 `batch` 的透传），否则并行分支会静默丢参数。

## 8. 产物布局

```text
<out>/
  geometry/<ship>/<ship>.glb          渲染模型（可再带 <ship>__<skin>.glb）
  manifests/<ship>/<ship>.json        入口索引（materials / meshes / skins / hashes / armor / base_skin）
  armor/<ship>/<ship>.{glb,json,bin}  装甲（仅 --armor）
  skins/<ship>/*.json                 皮肤元数据（仅 --skins / --model-skins）
  textures/<sha1>.webp                贴图池（扁平、按内容哈希去重、跨船共享）
  .export_index.json                  增量索引（仅 batch）
```

GLB 内的贴图 URI 是相对 GLB 的 `../../textures/…`；manifest 里的路径统一相对输出根。

## 9. 退出码与排错

| 情况 | 表现 |
|---|---|
| 正常 | exit 0 |
| `ship` 失败（船找不到、加载异常） | 抛异常 / `SystemExit(...)`，**非零** |
| `import` 失败或超时 | `SystemExit(1)` |
| `batch` 单船失败 | 只打印 `[fail] <key>: <末 300 字>`，**进程仍退 0** —— 要看日志或产物，不能只看退出码 |

常见问题：

- `unrecognized arguments` → 用了子命令没定义的开关（尤其手跑 `batch-worker`）；
- 页面 404 / 空白 → `--out` 根与页面 `root=` 参数不一致；改了布局或文件名要硬刷新；
- 同名船被覆盖 → 两个服务器导进了同一个 `--out`；
- 贴图缺失 → 用了 `--no-textures`，或换了 `--texture-size` 却沿用旧的 `textures/`。

## 10. 已修正的历史问题（备查）

| 问题 | 现状 |
|---|---|
| `--split-shapes` 死参数（解析了但不用） | 已接线，且与 `--merge-shapes` 互斥 |
| `--merge-shapes` 帮助写 "(default)"，与实现相反 | 帮助改为实际语义 |
| `batch` 没有 `--split-shapes` | 已补齐（互斥组） |
| `load_ship` docstring 写 `lod=-1` 默认 | 改为“默认 2”，并说明 `-1` = LOD0 |
| `batch --jobs 2` 报 `unrecognized arguments` | 子命令补齐全部开关 |
| `batch` 只按“文件存在”跳过 | 改为 build+参数的签名索引（§6.2） |
| `batch --limit` 在 `--jobs ≥ 1` 下被忽略 | `export-keys` 支持 `--limit` 并透传 |

## 11. 典型命令

```bash
# 列船 / 建库（客户端更新后必须重跑 import）
python cli.py list   --server lesta --game D:\Games\Korabli --filter Yamato
python cli.py import --server lesta --game D:\Games\Korabli

# 单船：渲染 + 装甲 + zst
python cli.py ship --server lesta --game D:\Games\Korabli --ship PJSB520_Raiju --out out\lesta --armor --zstd

# 快速预览：低模 + 小贴图
python cli.py ship --server lesta --game D:\Games\Korabli --ship PBSD511_Waterloo --out out\lesta \
  --lod 2 --texture-size 512

# 联动船本来的样子（默认 auto 已自动套模型皮肤）；要原版就关掉
python cli.py ship --server wg --game D:\Games\World_of_Warships \
  --ship PJSB700_ARP_Yamato --out out\wg
python cli.py ship --server wg --game D:\Games\World_of_Warships \
  --ship PJSB700_ARP_Yamato --out out\wg --no-model-skin

# 批量：先小规模验证，再全量；--jobs 4 并行
python cli.py batch --server lesta --game D:\Games\Korabli --filter 10 --out out\lesta --armor --jobs 4 --limit 5
python cli.py batch --server lesta --game D:\Games\Korabli --out out\lesta --force
```
