# 涂装数据探针（P1-3）

> 2026-10-27 实测，数据源：`data/assets_data.db`（Lesta build 8867689）+ `data/assets_data_wg.db`
> 只读查询，未启动客户端、未联网。
>
> 目的：回答 [todo-backlog.md](todo-backlog.md) P1-3 的 P-A1～P-C1，给 P1-2
> （`--skins` 是否要导 INDEXED 数组覆盖）与 P2-1（烘焙）定口径。

## 结论速览

| 编号 | 问题 | 实测答案 |
|---|---|---|
| P-A1 | 一船几个 camo / 几张 artMap | 看 `camo_ship_groups`：Waterloo 属 `UK_group_2` → **1** 条 camo；ARP 大和属 `IJN_group_5` → **2** 条；克里姆林**没有** camo group。一条 camo 给 **9 个部件**的贴图（hull/deckhouse/bulge/gun/director/misc/plane/float/wire） |
| P-A2 | camo 是否只换 artMap | **不是**。camo 是按部件换**贴图**（`camouflages.textures_json`），不是 artMap |
| P-A3 | `albedoTintMatIdArr` 随 camo 变？ | **不变**。DB 里根本没有 camo 专属的 INDEXED 材质：Waterloo 的 3 个 INDEXED mfm（本体 / wire / perforation）`artStrengthMatIdArr` 哈希全为 `a045a403eb`、`albedoTintMatIdArr` 全为 `a42b2bded3` |
| P-B1 | camo 有专属 geometry？ | 有，但只在特定船：ARP 大和 `--model-skins` 导出 **2** 套（`PJES340` / `PJES512`），是整船替换；Waterloo `--skins` 出 3 套材质皮肤、0 套模型皮肤 |
| P-B2 | 存在「camo 专属 mesh」？ | 无证据。皮肤只改材质/贴图；网格数在皮肤间不变（ARP 三份 GLB 都是 369 material / 同一网格集） |
| P-C1 | 可读涂装名从哪来 | `camouflages.display_name` 为空；名字走 GameParams 的 `IDS_*` + 皮肤 id（实测导出的 `display_name` 形如 `PCEM024_Steel_11lvl`、`PCEM028_Coal_11lvl`） |

## 1. 涂装清单怎么取（P-A1）

```
camo_ship_groups(group_name, ship)          ← 船 → 组
camouflages(ship_groups_json, textures_json, uv_json, color_scheme)
exteriors(ext_index, camouflage, species, color_scheme_id, parts_json)
```

实测：

```text
%Waterloo%     groups=UK_group_2   camos=1  ['camo_CosmoDay_tile_01']
%ARP_Yamato%   groups=IJN_group_5  camos=2  ['mat_Bogatyr_3', 'camo_Makoto_Kobayashi']
%Kremlin%      groups=-            camos=0
```

`textures_json` 的键就是部件名，与 `.visual` 的部件一致：

```json
{"hull": ".../camo_CosmoDay_tile_01.dds", "deckhouse": ".../Black_gun_camo_01.dds",
 "bulge": "...", "gun": "...", "director": "...", "misc": "...", "plane": "...",
 "float": "...", "wire": "..."}
```

## 2. INDEXED 的数组到底随不随涂装变（P-A2 / P-A3）

查 `material_full` 里所有 Waterloo 的 mfm：

| mfm | family | artMap | artStrength 哈希 | albedoTint 哈希 |
|---|---|---|---|---|
| `BSD100_Waterloo_1956.mfm` | indexed | `BSD100_Waterloo_1956_art.dds` | `a045a403eb` | `a42b2bded3` |
| `BSD100_Waterloo_1956_wire.mfm` | indexed | 同上 | `a045a403eb` | `a42b2bded3` |
| `BSD100_Waterloo_1956_perforation.mfm` | indexed | `..._perforation_art.dds` | `a045a403eb` | `a42b2bded3` |
| `BM1006_Crest_Waterloo.mfm` | pbs | — | — | — |

即：**同一艘船的所有 INDEXED 材质共用同一份 6 组数组**，artMap 也只有一个（除了
perforation 有自己的一张）。DB 里没有任何「camo 专用的 INDEXED 材质」，所以
「换涂装 → tint/artStrength 跟着变」这个假设在本批资产上**不成立**。

对照 WG 侧：ARP 大和同样只有 369 个材质、`--skins` 返回 0 —— 它的涂装差异全在
model skin（整船替换）里，而不是材质层。

## 3. 对 P1-2 的结论

P1-2 原本要做「皮肤 json 里加 `indexed_overrides: {artMapLayer, artStrength, tint}`」。
实测下来：

- **数组不需要导**：没有 camo 专属数组可导；
- 涂装对 INDEXED 船体真正可用的覆盖是 **artMap 贴图 + color scheme 颜色**，
  而不是 6 组数组；
- 而 `--skins` 现在导出的贴图是**按部件的 diffuse 替换**（`textures.hull` 等），
  对 INDEXED 船体没有对应槽位（INDEXED 的底色来自图集 + tint，不认 diffuseMap）。

所以 P1-2 建议改为**降级**：除非前端明确要「INDEXED 船体换涂装图案」，否则不做
数组覆盖；真要做，正确形状是给 skin json 增加：

```json
"indexed": { "artMap": "textures/<hash>.webp", "artStrength": 1.0,
             "tint": [r,g,b,a], "colorScheme": [...] }
```

由前端替换 `layers.artMap` 并调 `artStrength`。当前 `--bake-indexed` 默认开启时，
烘焙已把 artMap 混进底色，换涂装需要重烘 —— 这与「烘焙默认开」是互斥的两个产品方向，
需要单独决策。

## 4. 对 P2-1（烘焙）的影响

烘焙产物的底色 = 图集 × tint × artMap。既然 artMap 每船只有一张（默认涂装），
**烘焙结果对应该船的默认外观**；换涂装必须重烘或回退到 arrays 路径。
这与 `--no-bake-indexed` 的定位一致。

## 5. 复现命令

```bash
# 涂装清单
sqlite3 data/assets_data.db \
  "SELECT group_name FROM camo_ship_groups WHERE ship LIKE '%Waterloo%';"

# INDEXED 数组是否一致
sqlite3 data/assets_data.db \
  "SELECT mfm_path, indexed FROM material_full WHERE mfm_path LIKE '%Waterloo%';"
```

导皮肤（实测数量）：

```bash
python cli.py --server lesta --ship PBSD511_Waterloo --out out --skins
# → PCEM024 / PCEM028 / PCEM033（材质皮肤），0 套模型皮肤

python cli.py --server wg --ship PJSB700_ARP_Yamato --out out --skins --model-skins
# → [skins] 0 [] / [model-skins] 2 ['PJES340', 'PJES512']
```
