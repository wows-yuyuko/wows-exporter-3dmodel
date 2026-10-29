# wows-exporter-3dmodel

从《战舰世界》/ Korabli 客户端导出舰船 3D 模型（GLB + zstd），支持 Wargaming 与 Lesta 双服。

这是从 Wows_Paragrams_Unpack 裁剪出的纯 CLI 导出器：没有 GUI，没有数据浏览/对比/本地化功能。

## 安装

    pip install -r requirements.txt

## 会读游戏目录里的哪些文件

本工具**只读客户端、不改它**。唯一例外：`<game>/data/` 里没有 GameParams 时，会从
`bin/<build>/` 的 pkg 现场解包一份写回去（下次直接复用）。双服目录结构一致，下面以
`D:\Games\Korabli` 为例。

### 建库（`import`）时读

| 路径 | 用途 | 必需 |
|---|---|---|
| `<game>/data/GameParams_py3.data`、`GameParams_py2.data`（Lesta）<br>`<game>/data/GameParams.data`（WG） | 舰船 / 涂装 / 模块等全部实体数据 | ✅ |
| `<game>/content/assets.bin` | PrototypeDatabase：材质、视觉渲染集、骨架、模型路径 | ✅ |
| `<game>/bin/<build>/idx/*.idx` + 同级 `*.pkg` | 解包索引与数据块（所有 `.geometry` / `.mfm` / 贴图都在这里） | ✅ |

### 导出（`ship` / `batch`）时读

| 路径 | 用途 |
|---|---|
| `data/game_data*.db` | 舰船快照（装甲厚度、各挂载 `model` 路径、`permoflages`）+ 模型清单 + build 号 |
| `data/assets_data*.db` | 材质 / 渲染集 / 骨骼 / 涂装索引（表清单见 `docs/tech/common/03-import-assets.md`） |
| `<game>/data/GameParams*.data` | 取 `ShipInfo` 与 `permoflages`（决定 `--model-skin auto` 套不套皮肤）；**不存在时从 pkg 解包并写回该目录** |
| `<game>/bin/<build>/**.pkg` | `.geometry`、`.mfm`、贴图（按 `.dd0 → .dd1 → .dd2 → .dds` 分层取） |

`diff` 只读 idx，不进数据库。`data/split/` 不再产出也不会被读。

## 建库 / 更新

    python cli.py import --game D:\Games\Korabli --server lesta
    python cli.py import --game D:\Games\World_of_Warships --server wg

会生成 data/game_data*.db（舰船主数据）和 data/assets_data*.db（骨架/渲染集/材质）。

**增量**：同 build 重跑只花 18 s（全量约 4 min）。只有内容变了的船与模型会被重写 ——
判据是舰船快照的 sha1 与 `.geometry` 的 crc32 签名，后者在 `bin/<build>/idx` 里现成可读。
解析逻辑改过、或怀疑缓存脏了，用 `import ... --rebuild` 强制全量重解析 3D 缓存。

客户端更新后不用手动建库 —— 直接跑导出，它会自己判断（首次建库，之后增量同步）：

    python cli.py --server lesta

`config.json` 只存两个游戏路径；build 号（`bin/` 下的版本目录）不落盘，每次自动取
**数字最大**的那个目录，所以客户端更新后不需要改配置。`--server` 必须显式给，没有默认值；
`--game` 不给就用配置里对应服的路径。独立的 `python -m data_extractor.cli` 也读同一份配置。

## 使用

    # 全部导出（首次会先建库，约 4 min；之后同 build 重跑同步只要 ~15 s）
    python cli.py --server lesta --out out/lesta

    # 只导某个等级：10 / 3,4,7 / 5-11
    python cli.py --server lesta --ship 10 --out out/lesta

    # 指定几条（完整 key、idx、模型目录都能混用，可重复给）
    python cli.py --server lesta --ship PJSB018 --ship PJSD889 --out out/lesta

    # 渲染 + 装甲 + zst
    python cli.py --server lesta --ship PJSB520_Raiju --out out/lesta --armor --zstd

    # 材质皮肤（默认只导 mskin / skin；加 --permoflage 连同永久涂装）
    python cli.py --server lesta --ship PJSB520_Raiju --out out/lesta --skins --permoflage

    # 多进程
    python cli.py --server lesta --ship 10 --jobs 4 --out out/lesta

    # 需要沉没状态的断面几何时加 --damage（默认剔除）
    python cli.py --server lesta --ship PJSB520_Raiju --out out/lesta --damage

    # 贴图分辨率 / LOD 可调（默认 1024 像素 / LOD2；--lod -1 回到 LOD0 高模）
    python cli.py --server lesta --ship PJSB520_Raiju --out out/lesta --texture-size 2048 --lod -1

    # Lesta INDEXED 材质（视觉 2.0）默认烘成 baseColor/normal/MR，标准查看器直接可看；
    # 需要实时换涂装（原始图集 + 6 数组）时加 --no-bake-indexed
    python cli.py --server lesta --ship PBSD511_Waterloo --out out/lesta --no-bake-indexed

    # 联动/PR 船（ARP 大和、星之北风）的本来样子在模型皮肤里 —— 默认 auto 已自动套上；
    # 想强制原版模型、或指定某一套皮肤：
    python cli.py --server wg --ship PJSB700_ARP_Yamato --out out/wg
    python cli.py --server wg --ship PJSB700_ARP_Yamato --out out/wg --no-model-skin
    python cli.py --server wg --ship PJSB700_ARP_Yamato --out out/wg --model-skin PJES512

    # 只同步库、不导出；只想知道两版之间动了哪些船（后者不碰数据库）
    python cli.py import --server lesta
    python cli.py diff   --server wg

产物（除 `textures/` 外都按船名分一层目录）：

    geometry/<ship>/<ship>.glb            渲染模型（顶点属性已量化，见下）
    geometry/<ship>/<ship>.glb.zst
    geometry/<ship>/<ship>__<skin>.glb    模型皮肤（仅 --model-skins）
    armor/<ship>/<ship>.glb               装甲模型（可选预览）
    armor/<ship>/<ship>.glb.zst
    armor/<ship>/<ship>.json              装甲板块数据（zone/material/thickness/component/bounds）
    armor/<ship>/<ship>.bin               装甲三角形（positions/normals/plate_ids）
    manifests/<ship>/<ship>.json          materials / meshes / skins / hashes
    manifests/<ship>/<ship>__<skin>.json
    skins/<ship>/*.json                   皮肤元数据（仅 --skins / --model-skins）
    textures/<sha1>.webp                  外置贴图（扁平、按内容哈希去重、跨船共享）

`--slim`（默认开）把顶点属性量化成 glTF 扩展 `KHR_mesh_quantization`：索引 u16、
法线 i8、UV u16、位置 u16（按图元 AABB 量化 + 节点变换还原），并丢掉没人读的
`_T4` / `TEXCOORD_1` / `TANGENT`。实测 Waterloo 的 GLB 18.7 → 8.8 MiB（-53%），
画面 A/B 对照 MAE 0.11/255。需要原始 float 顶点时加 `--no-slim`。

## 说明

- import 每服只需跑一次（客户端更新后重跑）；
- 首次运行会自动从包里提取 GameParams 到 <game>/data/；
- geometry.glb 默认只含完好状态：`_crack_*` / `*_in` 断面几何被剔除（与 wows-model-exporter 的 `--damage` 口径一致）；
- 许可证：主体 Apache-2.0；data_extractor/kraken.py 为 GPLv3（见 LICENSE 文件）。

## 文档

- docs/tech/README.md：**已实现模块的技术细节**（按模块分文件，Lesta / WG 差异单独成篇）。
- docs/web-render-design.md：网页端产物结构（geometry/manifest/armor/textures/skins）与 three.js 渲染/换肤设计。
- docs/known-issues.md：已知问题与剩余工作。
- docs/todo-backlog.md：待办（对照 Wows_Paragrams_Unpack/todo_list 逐条判定）。
