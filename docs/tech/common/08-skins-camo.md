# 08 · 皮肤与涂装（default / mat / model）

> 入口：`cli.py` 的 `--skins` / `--permoflage` / `--model-skins` / `--model-skin`
> 服务：`services/skin_export.py` + `services/camo_service.py`

## 1. 三种产物

**皮肤不是默认产物**：不给 `--skins` / `--model-skins` 时不写 `skins/`，
`manifest.skins` 是空数组（页面把皮肤下拉置灰）。

| 类型 | 开关 | 产物 | 说明 |
|---|---|---|---|
| `default` | `--skins` 或 `--model-skins` | `skins/<ship>/default.json` | 空 overrides，主 GLB 用这套 |
| `mat` | `--skins` | `skins/<ship>/<id>.json` + 外置贴图 | 只换材质/贴图，不生成 GLB |
| `model` | `--model-skins` | `geometry/<ship>/<ship>__<id>.glb` + `manifests/<ship>/<ship>__<id>.json` + skin json | 换整船模型，各出一套 |

`--permoflage` 只是把 `permoflage` 这个 species 也纳入 `--skins`（默认不含）。

另有 `--model-skin <ID|auto>`（默认 `auto`）：把**某套模型皮肤当基准几何**，
产物仍是 `<ship>.glb`，不写 `skins/`，manifest 记 `base_skin`。见 §5.1。

## 2. 数据源

| 数据 | 来源 | 用途 |
|---|---|---|
| Exterior 实体 | `assets_data*.db` 的 `exteriors`（GameParams 直读，见 `03-import-assets.md`） | species / `peculiarityModels` / `hullConfig` / `nodesConfig` |
| `camouflages.xml` | 客户端包根（导入时入库到 `camouflages`） | 按部件分类的贴图、UV 变换、颜色方案、船组 |
| `permoflages` | GameParams 的 `Vehicle.permoflages()` → `ShipEntry.permoflages` | 归属权威来源 |

`CamoService._CAMO_SPECIES = {mskin, permoflage, skin, camouflage}`：
其它 species（`Ensign` 旗、`Decal`、`VisualCustomization` …）不当作涂装。

## 3. `list_camos()` 的匹配与分流

```python
list_camos(ship_indexes, ship_permoflages) -> list[CamoSchemeInfo]
```

- 第一项永远是 `{id: 0, display_name: "无涂装", origin: "none"}`；
- 归属判定优先用 `permoflages`（含 `nativePermoflage`），再用
  `camouflages.xml` 的 `shipGroups / targetShip` 匹配；
- 分两条 origin：
  - **`mat`**：`Exterior` 的 `camouflage`/`unpeculiarCamouflage` 命中 `camouflages` 条目，
    或 species 属涂装类 → 只换贴图；
  - **`model`**：`Exterior.data.peculiarityModels` 非空 → 换模型
    （`model_replace` = 原 `.model` 路径 → 替换路径，`model_folder` = 替换后的几何目录，
    `skin` = `hull_config/nodes_config/peculiarity_models` 完整数据）。

## 4. `--skins`（mat 型）实现

对每条 origin=`mat` 的条目：

1. 按 species 过滤：默认 `{mskin, skin}`；`--permoflage` 追加 `permoflage`；
   species 为空时只有在开启 `--permoflage` 且命中 `permoflages` 才收；
2. 逐贴图键下载（`.dd0 → .dd1 → .dd2 → .dds` 分层），按后缀判 kind
   （`_n` → normal，`_mg` → mr，其余 color）→ WebP 写入 `textures/`；
3. 生成 skin json：

```json
{
  "id": "PCEM210",
  "display_name": "…",
  "origin": "mat",
  "species": "MSkin",
  "use_color_scheme": true,
  "tiled": false,
  "color_schemes": ["…"],
  "colors": [[r,g,b,a] × 4],
  "uv_transforms": {"<贴图键>": {"scale": [u,v], "offset": [u,v]}},
  "textures": {"<贴图键>": "textures/<sha1>.webp"},
  "source_textures": {"<贴图键>": "content/.../xxx.dds"},
  "overrides": {}
}
```

> 现状：mat 皮肤**只覆盖贴图 + UV + 颜色**。INDEXED 材质的
> `artMap / artStrength / albedoTint` 数组覆盖**不做**（见 `../../todo-backlog.md` P1-2
> 与 [../../camo-probe.md](../../camo-probe.md)：同船所有 INDEXED mfm 的 6 组数组哈希一致，
> DB 里没有 camo 专属数组可覆盖）。`export_mat_skins()` 的注释里那句
> "INDEXED arrays … handled separately" 是历史遗留，实际别处没有实现。

实测 WG 大和：`--skins` → 8 个（PCEM004/017/027/188/210/215/221 + PJES360），
追加 `--permoflage` → 10 个（+ PCEP120、PCEP120_2）。

## 5. `--model-skins`（model 型）实现

对每条 origin=`model` 的条目：

1. `load_ship(info, skin=scheme.skin, model_replace=scheme.model_replace)`
   → 换掉船体与各挂载的 `.model`；
2. `export_render_glb()` 写 `geometry/<ship>/<ship>__<id>.glb`（默认参数，不受 CLI 的
   `--lod/--texture-size` 影响）；
3. `write_web_manifest()` 写 `manifests/<ship>/<ship>__<id>.json`（`skins: [<id>]`）；
4. skin json：

```json
{
  "id": "PJES300_KOF_Yamato",
  "display_name": "…",
  "origin": "model",
  "species": "Skin",
  "geometry": "geometry/<ship>/<ship>__<id>.glb",
  "manifest": "manifests/<ship>/<ship>__<id>.json",
  "overrides": {}
}
```

> 每套皮肤都完整跑一次 `load_ship`：模型皮肤会换 `hull_config.model` 与一批
> `peculiarityModels` 挂载，**几何无法复用**。能省的只有重复解码/编码
> （`keep_load_caches()` + 跨 builder 的贴图磁盘缓存）：WG 大和 3 套
> 84.9 s → **62.7 s**（每套 ~21 s，8 套约 167 s）。见 `../../todo-backlog.md` P2-3。

## 5.1 `--model-skin ID|auto`（基准外观）

与 `--model-skins` 的区别：**不生成额外一套产物**，而是把某套模型皮肤当作
`<ship>.glb` 的基准几何，manifest 记 `base_skin`。

判据（`cli._resolve_model_skin()`）：**只有 `permoflages[0]` 的 species 是模型皮肤时才套用
那一套**。数据依据：科技树船首个 permoflage 是 `Permoflage`（大和 `PCEP120`、北风
`PCEP119`），联动/PR 船首个就是 `Skin`。

| 船 | 首个 permoflage | `auto` 结果 |
|---|---|---|
| 大和 `PJSB018` | `PCEP120_Permo_Upgr_10_lvl`（Permoflage） | 不套，导原版 |
| ARP 大和 `PJSB700` | `PJES340_Yamato_ARP`（Skin） | 套 `PJES340` |
| 星之北风 `PJSD889` | `PJES509_Kitakaze_ranger`（Skin） | 套 `PJES509` |

`--no-model-skin` 等价于 `--model-skin none`；指定具体 ID 时按 `ext_index` /
`raw_name` / `display_name` 匹配，找不到会列出可选项后退出。

## 6. 贴图的 `kind` 判定

`texture_io.kind_for_path()`：

| 后缀 | kind | 变换 |
|---|---|---|
| `_n.dds/.dd0/.dd1/.dd2`、`_normal.dds` | `normal` | 补 Z |
| `_mg.*` | `mr` | G ← 1−gloss，B ← F0 权重 |
| 其余 | `color` | 原样 |

WebP 上限 2048（`dds_to_webp` 默认），与主模型的 `--texture-size` 独立。

## 7. 两服差异

| 项 | Lesta | WG |
|---|---|---|
| `exteriors` 行数 | 1,915 | 2,362 |
| `camouflages` 行数 | 384 | 515 |
| species 分布（前几） | Ensign 800 / Skin 603 / Permoflage 159 / Camouflage 155 / Decal 59 / MSkin 56 | Ensign 930 / Skin 667 / MSkin 246 / Permoflage 221 / Camouflage 131 / VisualCustomization 51 |

> `Ensign`（信号旗）数量最多但不属于涂装，`_CAMO_SPECIES` 会把它排除。
> Lesta 的 `VisualCustomization` 目前为 0 条，WG 有 51 条 —— 该 species
> 也**不**在涂装白名单里。
