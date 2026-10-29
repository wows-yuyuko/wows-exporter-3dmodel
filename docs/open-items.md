# 未解决清单（Open Items）

更新时间：2026-10-30

**这是唯一入口**：只列「现在还没做完 / 还是错的」的项，按当前代码逐条核实过。
已修复的历史档案在 [known-issues.md](known-issues.md)，参考项目 todo 的逐条判定在
[todo-backlog.md](todo-backlog.md)，两者保留原文不再维护「当前状态」。

图例：🔴 产物是错的 · 🟡 能力缺失 / 观感 · 🔵 体积性能 · ⚪ 工程化

---

## 一、🔴 缺陷

### O1. WG `camouflage_color_schemes` 只有 1 条（Lesta 868）

| | |
|---|---|
| 实测 | Lesta `camouflages 385 / 配色 868 / 船组 1,024`；WG `505 / **1** / 1,076` |
| 位置 | `services/camo_service.py:154-170`（`parse_camouflages` 的 colorschemes 段） |
| 影响 | WG 侧涂装切换拿不到配色，`camouflages.color_scheme` 指向的 id 在配色表里查不到 |

**根因（已定位，2026-10-30）**：两服 `<colorschemes.xml>` 结构不同。

```xml
<!-- Lesta：标签名本身就是配色名 -->
<colorSchemeEUR01>
  <color0>0.0100 0.0100 0.0100 1.0000</color0>
  ...
</colorSchemeEUR01>

<!-- WG：外层统一叫 colorScheme，名字在 <name> 子元素里 -->
<colorScheme>
  <name>	ColorScheme_Hel	</name>
  <color0>0.1350 0.1280 0.1300 0.9000</color0>
  ...
</colorScheme>
```

解析器用 `name = grp.tag`，WG 下所有节点的 tag 都是 `colorScheme`，字典不断被覆盖，
**只剩最后一条**（`colorSchemecolorJohnson_alt`，名字里还带原始 tab 空白）。

**修法（约 5 行）**：`grp.tag == "colorScheme"` 时改取 `_child_text(grp, "name")` 并
`.strip()`；Lesta 分支保持不变。修完用上表三行数字验收（WG 配色应落到数百条量级）。

**顺带**：`services/assets_cache_service.py:814` 涂装入库整段是
`except Exception: pass` —— 上一轮 `Path` 被误删导致三表恒为 0 就是被这里吞掉的。
应改成 `_p(f"⚠️ 涂装入库失败: {exc}")`。

### O2. `--no-textures` 时 GLB 只剩 1 个材质（连带 `extras` 丢失）

| | |
|---|---|
| 位置 | `services/export_service.py:838`（`_GlbBuilder.add_material` 的去重键） |
| 影响 | 无贴图导出时整船一个材质，DCC 里无法按部位分材质；`extras` 在缓存命中时静默丢弃 |

去重键是
`(texture_idx, base_color, alpha_mode, double_sided, normal_tex, mr_tex, occlusion_tex, emissive_tex, metallic, roughness)`
——**不含 `name` 与 `extras`**。`--no-textures` 下贴图索引全为 `None`、大量材质
`base_color=(1,1,1,1)` 且 metallic/roughness 相同 → 全部命中同一个 key，只 `append`
第一个，其余复用它的 index。

**修法**：key 里加 `name` 和 `tuple(sorted((extras or {}).items()))`。注意 `extras` 可能
含 list，需先转成可哈希形式。

### O3. 涂装/外观入库的异常不可见

同 O1 末尾：`assets_cache_service` 里外观与涂装两段仍是裸 `except ... pass`。这条单独列是
因为它的代价已经付过一次（三表恒为 0 整整一个版本没人发现）。

---

## 二、🟡 保真度（要对着游戏截图验）

| # | 项 | 现状 |
|---|---|---|
| B1 | 颜色还原 | 整体偏灰。INDEXED 用近似光照 `c*(0.75+0.35*ndl)+spec*mg.r*0.35` 再 `*1.65`；PBS 的金属度/粗糙度是估算值。未与游戏内同角度截图逐像素对齐 |
| B2 | decal | 默认整片丢弃（Waterloo 少 960 片 / 383,536 三角 = 62%）。候选方案 B：导出为独立节点 + `BLEND` + `polygonOffset`，节点名带 `decal` 便于单独开关 |
| B3 | `_alpha_n` 法线叠加层 | INDEXED 的 `normalMap` 是 alpha 叠加层，烘焙时未参与（P2-5 遗留） |
| B4 | 自发光 | `--emissive` 实现但默认关：实测 `_mg.B` 不是掩码（船体均值 0.47、信号灯 0），开着整船半自发光。要做得先搞清 B 通道的真实语义 |
| B5 | INDEXED 换涂装图案 | 探针证明**没有** camo 专属数组，所以 `--skins` 不能靠数组覆盖。若将来要做，正确形状是 skin json 加 `indexed: {artMap, artStrength, tint}` 由前端替换 `layers.artMap`，且与默认开启的烘焙互斥 |

---

## 三、🔵 体积

| # | 项 | 现状（2026-10-30 实测） | 目标 |
|---|---|---|---|
| A1 | `assets_data` 按路径类型裁剪 | Lesta **554 MiB** / WG **622 MiB**（`ls` 为 581 / 653 MB）。`skeleton_bones` 812k/876k 行、`skeleton_mounts` 484k/630k、`render_set` 402k/388k | 估 350 / 400 MiB |
| A2 | `ship_data` 快照瘦身 | `game_data` 118 / 154 MiB（`ls` 124 / 162 MB），`ship_data` 占大头（1,165 / 1,230 条 × 均 ~105 KB） | 只存导出用到的字段 → 估 40 / 55 MiB |

**A1 约束**：必须按**路径类型**裁（`/ship/`、`/gun/` 保留，`location` / 建筑 / `aircraft` 评估后再定），
不能按船名 —— 否则以后导冷门船会缺数据。
**A2 风险**：漏一个 `A_*` 组件就少一批挂载，必须逐船 A/B 对比网格数。

---

## 四、🔵 性能

| # | 项 | 现状 |
|---|---|---|
| C1 | 模型皮肤每套重新 `load_ship` | 已优化 26%（3 套 84.9 → 62.7 s）。几何无法复用（皮肤换 `hull_config.model`），只剩解码/编码可省 |
| C2 | 跨船几何池 | 同船 10 套皮肤按图元哈希去重：214.5 → 69.9 MiB（省 67%）。**跨船命中率未测**，先写「只统计不导出」的探针，在同 build 的 20 条船上量一遍再决定 |
| C3 | 增量导出是**输入级** | `<out>/.export_index.json` 的签名含 `build`，客户端一更新整批失效 → 全量重跑（未变的船仍是本地读盘 + 写盘）。要感知「资产变了但 build 没变」需把 geom/tex 指纹写进 manifest 再比对 |

---

## 五、⚪ 工程化

| # | 项 | 现状 |
|---|---|---|
| D1 | 自动化测试 | 没有 `tests/`。回归手段只有 `tools/ab_render_compare.py`（像素 A/B）和 `.workbuddy/tmp/probe_*.py` 一次性脚本 |
| D2 | CI | 没有 `.github/workflows` |
| D3 | NOTICE | 没有。`data_extractor/kraken.py` 是 **GPLv3**，其余 Apache-2.0，分发必须带 NOTICE |
| D4 | 文档行号漂移 | `文件:行号` 会漂（上一轮核对 18 处里 8 处失准，最大偏 290 行）。约定见 `tech/README.md`：**按符号 grep**，改符号锚点暂不做 |
| D5 | 剩余大函数 | `geometry_service._load_ship_impl`(274 行)、`_load_mounts`(174 行)。`_GlbBuilder`(709 行) 评估后结构合理，不动 |

---

## 六、明确不做（避免反复讨论）

| 项 | 结论 |
|---|---|
| wire 网格（栏杆/索具/天线） | 实测不成立，本项目的 render_set 里没有独立 wire 层 |
| `wg_compat.WG_TYPE_CATEGORY_MAP` 为 `None` | WG 导入实测正常（命名与 Lesta 一致），无调用方需要它 |
| `--skins` 导出 INDEXED 数组覆盖 | 探针证明同船所有 INDEXED mfm 的 6 组数组哈希完全一致，没有可覆盖的东西（B5 是另一条路） |
| 贴图 KTX2 / 压缩纹理 | 定稿 WebP |
| `.po/.mo` 本地化读取 | 产物不需要中文名 |
| `tools/blender_wows_vertex_groups.py` | 不做 |

---

## 附：验收用的基线数字

改完任一项，用这些数字对拍（默认参数 `--texture-size 1024 --lod 2`，烘焙开、`--slim` 开）：

| 船 | 网格 | 三角 | 顶点 |
|---|---|---|---|
| Lesta 星之北风 | 265 | 89,771 | 118,050 |
| Lesta Waterloo | 731 | 297,425 | — |
| Lesta 大和 | 422 | — | — |
| WG Tre Kronor | 172 | 124,862 | — |
| WG ARP 大和 | 429 | — | — |

库内行数（Lesta / WG）：`render_set` 402,395 / 388,129、`skeleton_mounts` 483,925 / 630,354、
`skeleton_bones` 812,395 / 876,588、`material_full` 29,688 / 31,487、
`camouflages` 385 / 505、`camo_color_schemes` 868 / **1（见 O1）**。
