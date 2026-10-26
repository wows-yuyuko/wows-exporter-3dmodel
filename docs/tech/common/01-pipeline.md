# 01 · 端到端管线与产物

## 1. 六个命令对应的三条数据流

| 命令 | 读什么 | 写什么 | 是否用数据库 |
|---|---|---|---|
| `list` | `GameParams.data`（pickle） | stdout | ❌ |
| `export-keys` | 同上 | 文本（每行一个 ship key） | ❌ |
| `import` | GameParams + `content/assets.bin` | `data/game_data*.db`、`data/assets_data*.db` | 写 |
| `ship` | `assets_data*.db` + 客户端 pkg | 输出目录（见 §3） | 读 |
| `batch` | 同上 | 同上，逐船一个子进程 | 读 |
| `batch-worker` | 同上（内部） | 同上 | 读 |

`batch` 自己**不做导出**：它按 `--jobs` 把船 key 切成 N 份，起 N 个
`batch-worker` 子进程（`cli.py:225-241`）。所以 `batch` 与 `ship` 的行为差异
只体现在"跳过已存在产物"与分片，导出逻辑完全共用 `_export_ship()`。

## 2. 运行期依赖关系

```text
cli.py
 ├─ _bootstrap(game, server)            # 设 wows_type / game_path / bin_folder
 ├─ core.gameparams.ships(path, filter) # 列船（不进 DB）
 └─ _export_ship(ship, args, out)
     ├─ GeometryService.instance().load_ship(info, lod=…)
     │   ├─ data_extractor.GameExtractor  # 从 pkg 取 .geometry / .mfm / 贴图
     │   ├─ models.geometry_parser        # .geometry → 顶点/索引/装甲
     │   ├─ AssetsCacheService            # 读 assets_data*.db（render_sets / material_full / skeleton）
     │   └─ models.geometry_transform      # 解析空间 → 渲染空间
     ├─ export_service.export_render_glb   # 渲染 GLB
     ├─ export_service.export_armor_glb    # 装甲 GLB（--armor）
     ├─ armor_export.write_armor_files     # armor.json + armor.bin（--armor）
     ├─ skin_export.write_default_skin     # 必有
     ├─ skin_export.export_mat_skins       # --skins
     ├─ skin_export.export_model_skins     # --model-skins
     └─ web_manifest.write_web_manifest    # manifests/<ship>.json（必写）
```

`_bootstrap` 里的 build 解析顺序（`cli.py:58-73`）：

1. `core.build_info.imported_build(server)` 读 `game_data*.db` 的 `meta_import_state`；
2. 该目录不存在时，退到 `bin/` 下**字典序最后一个**目录。

> 第 2 步是坑：客户端更新后 `bin/` 会多出新目录，而库里的 build 是旧的
> （WG 现况：库 13488861、实际 13187581 / 13357625）。`assets_data*.db` 的
> `meta.bin_folder` 又是按精确值过滤的，于是 `shape_names` 查空
> （LOD/crack 的名字兜底失效）。**客户端更新后必须重跑 `import`。**

## 3. 输出目录结构

```text
<out>/
  geometry/<ship>.glb                 # 渲染模型（舰体 + 挂载）
  geometry/<ship>.glb.zst             # --zstd（level 10）
  geometry/<ship>__<skin>.glb         # --model-skins：换模型皮肤，各自一套
  armor/<ship>.glb                    # --armor
  armor/<ship>.json                   #   装甲索引（schema wows-armor/1）
  armor/<ship>.bin                    #   位置/法线/板块 id，三段拼接
  manifests/<ship>.json               # 网页清单（schema wows-web/1）
  manifests/<ship>__<skin>.json       #   换模型皮肤各自的清单
  textures/<sha1>.webp                # 外置贴图，文件名 = 内容 sha1
  textures/indexed_<sha1>.bin         # INDEXED 的 6 个 196×4 float 数组
  skins/<ship>/default.json           # 每次导出必写
  skins/<ship>/<skin>.json            # --skins / --model-skins 的皮肤描述
```

- 贴图文件名按内容哈希（`services/texture_export.write_webp`），同图不重复落盘；
- `textures/` 是**共享目录**：多次导出到同一个 `<out>` 会累积历史贴图，
  改过 `--texture-size` 后旧图不会被自动删（体积会虚高，需手工清）。
- `--no-textures` 时 `geometry/` 只有 GLB，不写 `textures/`；GLB 里也不嵌图。

## 4. 线程、取消与内存

- `GeometryService.load_ship()` 是**同步**的；GUI 版本把整个导出放后台线程，
  CLI 直接串行。两条路径共用的取消信号是 `threading.Event`，
  解析器在缓冲区/图元/装甲批次边界检查（`_check_cancel`），抛 `TaskCancelled` 正常退出。
- `partial_cb`：船体（+装甲）解析完、挂载尚未开始时回传一份**几何快照**
  （`_snapshot_without_mounts`），GUI 用它提前出图；CLI 不用。
- 内存红线 2 GB：`.geometry` 逐文件"读取 → 解析 → 合并 → 释放"，
  共享图集（20 MB+）按 base 路径缓存，避免同一材质反复 Kraken 解压。

## 5. 导出报告

`export_service.ExportReport`（`export_service.py:204`）：

| 字段 | 含义 |
|---|---|
| `nodes` / `meshes` / `triangles` / `vertices` | 产物统计 |
| `textures` / `skipped_textures` | 成功解码的贴图数 / 解码失败数 |
| `skipped_meshes` | 被规则跳过的网格（wire 等） |
| `warnings` | 人类可读告警（缺贴图、材质解析失败等） |
| `bounds_min` / `bounds_max` | 渲染空间包围盒 |
| `elapsed_seconds` | 耗时 |
| `content` | `render` 或 `armor` |

CLI 直接 `print(report)`，输出形如：

```text
✅ 导出渲染模型完成：429 网格 / 261,748 三角形 / 253 贴图，耗时 13.5s
```

## 6. 实测耗时（Windows，本机）

| 场景 | 耗时 |
|---|---|
| `ship` WG 大和（默认参数） | ~14 s |
| `ship` 大和（两服各一次） | 28 s / 2 m 08 s（取决于是否首次解包） |
| `ship` Waterloo（1,684→731 网格，INDEXED） | 2 m 45 s |
| `--model-skins` 8 套皮肤 | 3 m 01 s（瓶颈是每套重新 `load_ship`） |

Waterloo 慢在网格碎片多（LOD2 下 731 个网格），每个网格都要单独解材质/贴图；
大和虽然三角更多但网格少。
