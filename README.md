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

产物：

    geometry/<key>.glb            渲染模型
    geometry/<key>.glb.zst
    armor/<key>.glb               装甲模型（可选预览）
    armor/<key>.glb.zst
    armor/<key>.json              装甲板块数据（zone/material/thickness/component/bounds）
    armor/<key>.bin               装甲三角形（positions/normals/plate_ids）
    manifests/<key>.json          materials / meshes / skins / hashes
    textures/<sha1>.webp          外置贴图（去重；geometry.glb 内不再嵌图）

## 说明

- import 每服只需跑一次（客户端更新后重跑）；
- 首次运行会自动从包里提取 GameParams 到 <game>/data/；
- geometry.glb 默认只含完好状态：`_crack_*` / `*_in` 断面几何被剔除（与 wows-model-exporter 的 `--damage` 口径一致）；
- 许可证：主体 Apache-2.0；data_extractor/kraken.py 为 GPLv3（见 LICENSE 文件）。

## 文档

- docs/web-render-design.md：网页端产物结构（geometry/manifest/armor/textures/skins）与 three.js 渲染/换肤设计。
- docs/known-issues.md：已知问题与剩余工作（船体接缝、Waterloo 深色块、颜色、KTX2、增量导出等）。
