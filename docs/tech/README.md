# 技术文档（已实现的模块细节）

> 面向维护者：**已经落地**的实现是怎么做的。不改设计、不提需求，
> 遇到"为什么这么写"先看这里；未解决的问题看 [../known-issues.md](../known-issues.md)，
> 待办看 [../todo-backlog.md](../todo-backlog.md)。
>
> 代码位置用「模块.符号」指代（如 `export_service.GlbRenderOptions`）。少数地方还留着
> `文件:行号`，那是写文档时的快照 —— 加过函数就会漂，**找不到时按符号名 grep**。

## 目录

```text
docs/tech/
  README.md                     ← 本文件：索引 + 服务器隔离总表
  common/                       ← 两服共用的实现（99% 代码在这）
    01-pipeline.md              端到端管线与产物目录
    02-import-game-data.md      GameParams → game_data*.db
    03-import-assets.md         assets.bin → assets_data*.db
    04-geometry.md              .geometry → HullMesh / MountMesh
    05-materials-textures.md    .mfm / INDEXED / DDS → WebP
    06-glb-render.md            渲染模型 GLB（舰体 + 挂载）
    07-glb-armor.md             装甲 GLB + armor.json/bin
    08-skins-camo.md            皮肤（default / mat / model）与涂装服务
    09-manifest-web.md          manifests/<ship>.json（wows-web/1）
    10-cli.md                   CLI 六命令、参数、默认值、已知坑
  servers/
    lesta.md                    Lesta（Korabli）专属事实与差异
    wg.md                       Wargaming 专属事实与差异
```

## 端到端一句话

```text
GameParams.data ─┬─ import ─→ game_data*.db ─┐
                 │                          ├─ ship/batch ─→ geometry/*.glb
content/assets.bin ┴─ import ─→ assets_data*.db ┘                armor/*.glb+json+bin
                                                                 manifests/*.json
                                                                 textures/*.webp
                                                                 skins/<ship>/*.json
```

- `list` / `export-keys` **不进数据库**，直接解析 GameParams；
- `ship` / `batch` **只读数据库**（`assets_data*.db`），运行期才从客户端 pkg 取
  `.geometry` 与贴图字节；
- `import` 只在客户端更新后跑一次。

## 服务器隔离总表

所有"两服不一样"的地方都收在这张表里，具体展开见 `servers/*.md`。

| 维度 | Lesta（Korabli） | Wargaming | 代码位置 |
|---|---|---|---|
| 客户端目录 | `D:\Games\Korabli` | `D:\Games\World_of_Warships` | `app/config.py:25-26` |
| GameParams 文件（优先序） | `GameParams_py3.data` → `py2` → `.data` | `GameParams.data` → `py3` | `cli.py:_gameparams_path()` |
| GameParams 编码 | 反转 + zlib | 反转 + zlib（另有 deflate 变体回退） | `core/gameparams.py:30-56` |
| 主库 | `data/game_data.db` | `data/game_data_wg.db` | `database_service._db_name()` |
| 资源库 | `data/assets_data.db` | `data/assets_data_wg.db` | `assets_cache_service._db_name()` |
| SQL schema 目录 | `resources/database/lesta/` | `resources/database/wargaming/` | `database_service._schema_subdir()` |
| assets.bin 类型表 | **12 类**（含 ModelFbx） | **10 类** | `uncode_assets/types.py:1-20` |
| Material 条目尺寸 | `0x88` | `0x78` | 同上 |
| Visual 条目尺寸 | `0x40` | `0x70` | 同上 |
| Model 条目尺寸 | `0x20` | `0x28` | 同上 |
| INDEXED 材质（视觉 2.0） | **有**（当前库 192 个） | **无**（0 个） | `material_full.family` |
| 当前导入 build | `8867689` | `13488861`（与 bin 目录不一致，见 wg.md） | `meta` / `meta_import_state` |
| 舰船数 | 1,165 | 1,246 | `ship_basic_info` |
| render_sets 行数 | 399,008 | 392,305 | `assets_data*.db` |
| exteriors 行数 | 1,915 | 2,362 | 同上 |
| camouflages 行数 | 384 | 515 | 同上 |
| 独有表 | — | `ship_module_atba_config`、`crew_skill_groups` | `resources/database/*/database_new.sql` |

> 结论性区别：**INDEXED 分块涂装只有 Lesta 有**，WG 的舰体材质全是 PBS。
> 所以 `05-materials-textures.md` 的 INDEXED 章节、`../known-issues.md` 的
> 空腔问题对 WG 均不适用。

## 三条共享铁律（改代码前务必知道）

1. **解析空间 ≠ 渲染空间**：`.geometry` 是左手系，渲染/glTF 是右手系，
   只差一个 Z 镜像。所有变换集中在 `models/geometry_transform.prepare_render_mesh()`，
   渲染与导出共用，禁止各自实现。→ `04-geometry.md`
2. **稀疏材质原则**：只认 `.mfm` **声明**的贴图键，绝不按文件名补全缺失通道。
   → `05-materials-textures.md`
3. **导入期只存路径，不存字节**：`assets_data*.db` 里全是路径与参数，
   贴图字节在导出时按 `.dd0 → .dd1 → .dd2 → .dds` 分层实时读取。
   → `03-import-assets.md` / `05-materials-textures.md`

## 样本产物（2026-10-27，默认参数：texture 1024 / LOD2 / `--slim` / INDEXED 烘焙开 / 无皮肤）

一个服务器一个输出根（船 key 同名会互相覆盖，见 `README.md`）：

| 目录（相对 `docs/sample/`） | 平台 | GLB | 网格 | 三角形 |
|---|---|---|---|---|
| `lesta/geometry/PBSD511_Waterloo/` | Lesta | 8.9 MiB | 731 | 297,425 |
| `lesta/geometry/PJSB018_Yamato_1944/` | Lesta | 8.6 MiB | 422 | 245,767 |
| `lesta/geometry/PJSD889_Kitakaze_PR/` | Lesta | 4.0 MiB | 265 | 89,771 |
| `wg/geometry/PJSB018_Yamato_1944/` | WG | 9.1 MiB | 429 | 261,748 |
| `wg/geometry/PJSB700_ARP_Yamato/` | WG | 9.1 MiB | 429 | 264,772 |

逐文件体积、贴图张数与预览命令以
[`../known-issues.md`](../known-issues.md) 的「测试样本」为准（本表只列量级）。

Waterloo 是**唯一**带 INDEXED 舰体的样本（`TL2_SHIPMAT_INDEXED_PBS_Hull`），
其余全 PBS —— 对比这两类船就能看出 INDEXED 差异。星之北风与 ARP 大和还各带一个
`base_skin`（默认 `--model-skin auto` 套上的模型皮肤，扫描 `permoflages[0]` 得出）。
