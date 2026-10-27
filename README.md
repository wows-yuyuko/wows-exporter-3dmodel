# wows-exporter-3dmodel

从《战舰世界》/ Korabli 客户端导出舰船 3D 模型（GLB + zstd），支持 Wargaming 与 Lesta 双服。

这是从 Wows_Paragrams_Unpack 裁剪出的纯 CLI 导出器：没有 GUI，没有数据浏览/对比/本地化功能。

## 安装

    pip install -r requirements.txt

## 首次：建库

    python cli.py import --game D:\Games\Korabli --server lesta
    python cli.py import --game D:\Games\World_of_Warships_PT --server wg

会生成 data/game_data*.db 和 data/assets_data*.db（挂载/材质用）。

## 使用

    # 列船
    python cli.py list --game D:\Games\Korabli --filter Yamato

    # 单船：渲染 + 装甲 + zst
    python cli.py ship --game D:\Games\Korabli --ship PJSB520_Raiju --out out --armor --zstd

    # 材质皮肤（默认只导 mskin / skin；加 --permoflage 连同永久涂装）
    python cli.py ship --game D:\Games\Korabli --ship PJSB520_Raiju --out out --skins --permoflage

    # 批量（分片并行）
    python cli.py batch --game D:\Games\Korabli --filter 10 --jobs 2 --out out --armor --zstd --skins

    # 需要沉没状态的断面几何时加 --damage（默认剔除）
    python cli.py ship --game D:\Games\Korabli --ship PJSB520_Raiju --out out --damage

    # 贴图分辨率 / LOD 可调（默认 1024 像素 / LOD2；--lod -1 回到 LOD0 高模）
    python cli.py ship --game D:\Games\Korabli --ship PJSB520_Raiju --out out --texture-size 2048 --lod -1

    # Lesta INDEXED 材质（视觉 2.0）默认烘成 baseColor/normal/MR，标准查看器直接可看；
    # 需要实时换涂装（原始图集 + 6 数组）时加 --no-bake-indexed
    python cli.py ship --game D:\Games\Korabli --ship PBSD511_Waterloo --out out --no-bake-indexed

    # 联动/PR 船（ARP 大和、星之北风）的本来样子在模型皮肤里 —— 默认 auto 已自动套上；
    # 想强制原版模型、或指定某一套皮肤：
    python cli.py ship --server wg --game D:\Games\World_of_Warships       --ship PJSB700_ARP_Yamato --out out\wg            # auto → PJES340（ARP 外观）
    python cli.py ship --server wg --game D:\Games\World_of_Warships       --ship PJSB700_ARP_Yamato --out out\wg --no-model-skin      # 原版大和
    python cli.py ship --server wg --game D:\Games\World_of_Warships       --ship PJSB700_ARP_Yamato --out out\wg --model-skin PJES512 # 指定另一套

产物（除 `textures/` 外都按船名分一层目录）：

    geometry/<ship>/<ship>.glb            渲染模型（顶点属性已量化，见下）
    geometry/<ship>/<ship>.glb.zst
    geometry/<ship>/<ship>__<skin>.glb    模型皮肤（仅 --skins / --model-skins）
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
