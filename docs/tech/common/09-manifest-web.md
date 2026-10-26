# 09 · `manifests/<ship>.json`（schema `wows-web/1`）

> 生成：`services/web_manifest.write_web_manifest()`，每次 `ship` 导出**必写**
> 用途：网页（`docs/threejs-example.html`）不解析 GLB 也能知道材质/贴图/节点结构

## 1. 顶层结构

```json
{
  "schema": "wows-web/1",
  "ship": "PBSD511_Waterloo",
  "build": "8867689",
  "geometry": "geometry/PBSD511_Waterloo.glb",
  "materials": { "m_8866c7c5c2": { … } },
  "meshes": [ { "node": "BowShape", "mesh": "BowShape",
                "material": "m_8866c7c5c2", "component": "hull" } ],
  "skins": ["default"],
  "hashes": { "geometry": "…", "materials": "…", "textures": "…" },
  "armor": "armor/PBSD511_Waterloo.json"          // 仅 --armor 时存在
}
```

| 字段 | 说明 |
|---|---|
| `schema` | 固定 `wows-web/1` |
| `ship` / `build` | 舰船 key、客户端 build 号 |
| `geometry` | 相对 manifest 目录的 GLB 路径 |
| `materials` | 以 `m_<sha1(name|source)[:10]>` 为键的材质表（见 §2） |
| `meshes` | 扁平列表，每条 = GLB 里一个节点（名/材质键/归属） |
| `skins` | 该套模型可用的皮肤 id；换模型皮肤的 manifest 只有自己那一个 |
| `hashes` | 三个缓存指纹，见 §4 |
| `armor` | `--armor` 时指向 `armor/<ship>.json` |

## 2. 材质条目

```json
"m_8866c7c5c2": {
  "id": "m_8866c7c5c2",
  "name": "TL2_SHIPMAT_INDEXED_PBS_Hull",
  "source": "content/gameplay/common/textures/CIT000_1k_ship_tiles_a.dds",
  "mode": "indexed",
  "slots": { "albedo": "textures/…webp", "normal": "…", "mr": "…", "ao": "…" },
  "layers": { "materialIdMap": "textures/…webp", "artMap": "textures/…webp" },
  "indexed": {
    "arrays": { "offsetScaleMatIdArr": {"bin": "textures/indexed_…bin",
                                        "dtype": "float32", "shape": [196, 4]}, … },
    "offset": [-0.005, 0.0], "grid": [37.7, 39.4],
    "textures": { "albedoArray": {"uri": "…", "tile": 256, "cols": 6,
                                  "rows": 5, "count": 30}, … }
  },
  "wows_textures": { "albedoArray": "content/…/CIT000_1k_ship_tiles_a.dds", … },
  "double_sided": true,
  "alpha_mode": "OPAQUE"
}
```

要点：

- **`mode` 不是从材质名猜的**，而是"`wows_textures` 里有没有 `*Array` 键"：
  有 → `indexed`，否则 `pbs`；
- `slots` 只放 glTF 标准槽（albedo/normal/mr/ao/emissive），
  INDEXED 舰体**没有 albedo**（颜色来自图集 + matId，标准查看器里就是空）
  —— 这是当前设计，不是缺数据；
- `layers` 是"图集/索引图"（`materialIdMap`、`artMap`），与 `wows_textures`
  （原始 DDS 路径清单）不是一回事；
- `indexed.textures` 里的 `tile/cols/rows/count` 由图集生成时写入，
  网页用 `atlasUV(slice, uv) = (cell + fract(uv)) / grid` 取层。

> 已知：`layers.artMap` 现在**恒缺失**（`_is_dds_array()` 8 MB 阈值误判），
> 见 [../../todo-backlog.md](../../todo-backlog.md) P0-1。

## 3. `meshes` 条目

```json
{"node": "BowShape", "mesh": "BowShape", "material": "m_8866c7c5c2", "component": "hull"}
```

- 顺序 = GLB 节点顺序（渲染顺序不依赖它）；
- `component` 取自 mesh extras，缺失时回退 node extras；
- 同名节点（`#2`）会各自出现一条。

## 4. 三个哈希（缓存失效用）

| 键 | 算法 | 覆盖范围 |
|---|---|---|
| `geometry` | GLB 文件 sha1 | 整个几何文件 |
| `materials` | `sha1(json.dumps(materials, sort_keys=True))` | 材质表（路径/参数/模式/indexed 参数） |
| `textures` | 对材质引用的**全部外部文件**排序，逐个拼 `路径 + 文件 sha1`，再整体 sha1 | `slots` + `layers` + INDEXED 图集 + 数组 `.bin` |

`textures` 的收集函数是 `_asset_uris()`（`web_manifest.py:19`）：

- `slots.*`（albedo/normal/mr/ao/emissive）；
- `layers.*`（`materialIdMap`、`artMap`）；
- `indexed.textures[*].uri`（albedo/normal/MG 图集）；
- `indexed.arrays[*].bin`（6 个 196×4 数组）。

所以换涂装（artMap）、换图集、换数组都会让指纹变化，前端缓存能正确失效。
2026-10-26 之前只统计 `slots`，INDEXED 船换涂装时指纹不变（已修）。

## 5. 与 GLB 的对应关系

| manifest | GLB |
|---|---|
| `materials[key].slots.*` | `material.pbrMetallicRoughness.*Texture` |
| `materials[key].layers.*` | 只存在于 `material.extras.wows_layer_textures` |
| `materials[key].indexed.*` | 只存在于 `material.extras.wows_indexed*` |
| `meshes[].node` | `node.name` |
| `meshes[].mesh` | `mesh.name` |

manifest 是 GLB extras 的**扁平化视图**，不引入额外信息（除了 `build` 与 `hashes`）。
