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

    # 批量（分片并行）
    python cli.py batch --game D:\Games\Korabli --filter 10 --jobs 2 --out out --armor --zstd

产物：<key>.glb、<key>.glb.zst、<key>_armor.glb、<key>_armor.glb.zst。

## 说明

- import 每服只需跑一次（客户端更新后重跑）；
- 首次运行会自动从包里提取 GameParams 到 <game>/data/；
- 许可证：主体 Apache-2.0；data_extractor/kraken.py 为 GPLv3（见 LICENSE 文件）。
