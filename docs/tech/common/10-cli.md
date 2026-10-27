# 10 · CLI 参考（`python cli.py …`）

> 代码：`cli.py`（单文件，argparse 六个子命令）

## 1. 命令总览

| 命令 | 用途 | 需要数据库 |
|---|---|---|
| `list` | 列船（GameParams 直读） | ❌ |
| `import` | 建库（GameParams + assets.bin） | 写 |
| `ship` | 单船导出（主力） | ✅ |
| `batch` | 批量导出（逐船起子进程） | ✅ |
| `export-keys` | 只输出船 key 列表 | ❌ |
| `batch-worker` | 内部：`batch --jobs` 的子进程 | ✅ |

通用参数：`--game DIR`（必填）、`--server {lesta,wg}`（默认 **lesta**）、`--gp FILE`（显式 GameParams）。

## 2. `ship`

```text
--game DIR  --server {lesta,wg}  --ship KEY  --out DIR
[--no-textures] [--zstd] [--armor]
[--damage] [--decals]
[--bake-indexed | --no-bake-indexed] [--bake-size PX=0]
[--slim | --no-slim]
[--texture-size N=1024] [--lod N=2]
[--merge-shapes | --split-shapes]   # 互斥；默认就是拆分
[--skins] [--permoflage] [--model-skins] [--model-skin ID|auto]
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--no-textures` | 关 | 不导贴图（GLB 无图、不写 `textures/`） |
| `--zstd` | 关 | 额外写 `.glb.zst`（zstd level 10） |
| `--armor` | 关 | 额外导 `armor/*.glb` + `.json` + `.bin` |
| `--damage` | 关 | 保留破损断面（默认剔除） |
| `--decals` | 关 | 保留平面贴花（默认剔除） |
| `--bake-indexed` | **开** | 把 INDEXED 材质烘成标准 baseColor/normal/MR（关闭后丢失实时换涂装换不回来，见下） |
| `--no-bake-indexed` | — | 保留原始图集数组 + 6 LUT（只有自带 shader 的页面能画对，可实时换涂装） |
| `--bake-size` | **0** | 烘焙贴图最长边；`0` = 跟随 materialIdMap 原生尺寸（4096×2048） |
| `--slim` | **开** | 顶点属性量化（`KHR_mesh_quantization`）+ 丢掉 `_T4`/`TEXCOORD_1`/`TANGENT`，实测 GLB -53% |
| `--no-slim` | — | 保留 float32 全部属性（对照/调试用） |
| `--skins` / `--model-skins` | 关 | 皮肤不是默认产物：不加这两个开关时**不写** `skins/`，`manifest.skins` 为空数组 |
| `--texture-size` | **1024** | 贴图最大边长（PX），小图不放大 |
| `--lod` | **2** | `-1` = LOD0；`0–3` 指定级别，缺级部件回退 LOD0 |
| `--merge-shapes` | 关 | 每段合成一个网格（会丢面，见 known-issues） |
| `--split-shapes` | 关 | 按 `shape_spans` 拆分；**这就是默认行为**，只用于显式声明 |
| `--skins` | 关 | 导出 mskin/skin 材质皮肤 |
| `--permoflage` | 关 | 追加永久涂装（需同时给 `--skins`） |
| `--model-skins` | 关 | 导出换模型皮肤（各一套 GLB） |
| `--model-skin` | 空 | 把某套**模型皮肤当基准外观**导出（产物仍是 `<ship>.glb`）。
`auto` = 该船 permoflages 里第一套 model skin。用于 ARP 大和这类「默认模型是原版、
特殊外观在皮肤里」的船；manifest 会记 `base_skin` |

`--ship` 既接受 GameParams key（`PJSB018_Yamato_1944`），也接受模型目录名
（`JSB039_Yamato_1945`），取第一个匹配。

## 3. `list` / `export-keys`

```text
list        --game DIR [--server] [--filter TEXT] [--gp]
export-keys --game DIR [--server] [--filter TEXT] [--out FILE] [--gp]
```

`list` 输出四列（Tab 分隔）：`key  model_folder  T<tier>  nation`。
`--filter` 是**子串匹配**（大小写不敏感，匹配 key 与 model_folder）。

## 4. `import`

```text
import --game DIR [--server] [--gp] [--bin BUILD] [--version TEXT]
```

流程：

1. `_bootstrap()` 设 `wows_type / game_path / bin_folder`；
2. 把 GameParams 复制进 `<project>/data/`；
3. `bin_folder` = `--bin`，否则取 `bin/` **字典序最后一个**目录；
4. `run_process()` 同步跑（内部后台线程 + 信号，CLI 等 `data_processed`）；
5. 超时 1800 s；失败 `SystemExit(1)`。

## 5. `batch`

```text
batch --game DIR --filter TEXT --out DIR [--jobs N=1] [--limit N] [--force]
      + ship 的全部开关（含 --slim/--no-slim）
```

- 逐船判断 `geometry/<ship>/<ship>.glb` 是否已存在、且增量签名匹配（见 `.export_index.json`）：
  两者都满足才 `skip`；`--force` 强制全量重导；
- `--jobs > 1`：先 `export-keys` 写 `.keys.txt`，把 key 均分成 N 份，
  起 N 个 `batch-worker` 子进程并行；子进程参数由父进程按"偏离默认才追加"的规则拼；
- `--jobs 1`：父进程内串行调 `_export_ship()`（**不**起子进程，因此不了隔离崩溃）。

## 6. `batch-worker`

```text
batch-worker --game DIR --keys-file FILE --out DIR
             [--no-textures] [--zstd] [--armor] [--damage] [--decals]
             [--texture-size] [--lod] [--skins] [--permoflage]
             [--model-skins] [--model-skin ID|auto] [--merge-shapes] [--gp]
```

只为 `batch --jobs` 服务，一般不手跑。**历史坑**：`--skins / --permoflage /
--model-skins / --merge-shapes` 曾只在父进程解析、子命令没定义，
`batch --jobs 2` 会直接 `unrecognized arguments` 退出；现已补齐。

## 7. 参数语义与已知坑

`--merge-shapes` / `--split-shapes` 用 **argparse 互斥组**，同时给会直接报错。

- 默认：`split_shapes=True`（按 `shape_spans` 拆；段内接缝较多，但**不丢面**）；
- `--merge-shapes`：整段合成一个网格（接缝变少，但**会丢 shape**，见 known-issues）；
- `--split-shapes`：等于默认，只是为了脚本里写得明确。

已修正的历史问题（2026-10-26）：

| 问题 | 现状 |
|---|---|
| `--split-shapes` 是死参数（解析了但 `_export_ship` 不读） | 已接线，且与 `--merge-shapes` 互斥 |
| `--merge-shapes` 帮助写 "(default)"，与实现相反 | 帮助改为实际语义 |
| `batch` 没有 `--split-shapes` | 已补齐（互斥组） |
| `load_ship` docstring 写 `lod=-1` 默认 | 改为"默认 2"，并说明 `-1` = LOD0 |

仍存在：

| 现象 | 原因 | 影响 |
|---|---|---|
| `batch-worker` 不接受 `--force` / `--limit` | 由父进程处理 | 无 |
| `batch --jobs` 每次重开 DB | 子进程独立启动 | 每船固定开销（见 backlog P2-4） |

## 8. 典型用法

```bash
# 一次：建库（客户端更新后必须重跑）
python cli.py import --game D:\Games\Korabli --server lesta

# 列船确认 key
python cli.py list --game D:\Games\Korabli --filter Yamato

# 单船（默认 1024 / LOD2 / 剔 crack+decal）
python cli.py ship --game D:\Games\Korabli --ship PBSD511_Waterloo --out docs/sample/PBSD511_Waterloo

# 带装甲与 zstd
python cli.py ship --game D:\Games\World_of_Warships --server wg --ship PJSB018_Yamato_1944 --out out --armor --zstd

# 批量（4 进程）
python cli.py batch --game D:\Games\Korabli --filter 10 --out out --jobs 4 --armor --zstd
```

## 9. 退出码

| 情况 | 行为 |
|---|---|
| `ship` 找不到船 | `SystemExit("ship not found: …")` |
| `import` 解析/缓存失败 | `SystemExit(1)` |
| `batch` 单船失败 | 打印 `[fail]`，继续下一艘 |
| `batch-worker` 单船失败 | 打印 `[fail]`，继续 |
| 三处 `py_compile` 报错 | 不适用（运行期 import 失败直接抛） |
