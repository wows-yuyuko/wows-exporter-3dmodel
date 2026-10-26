# 已知问题与剩余工作

更新时间：2026-10-26

本文档记录当前导出器仍未解决的问题、已经排除的方向，以及下一步建议。
设计说明见 [web-render-design.md](web-render-design.md)。

## 状态总览

| 模块 | 状态 |
|---|---|
| 双服 GameParams / 建库 / 列船 | 完成 |
| PBS 几何 + 外置 WebP + manifest | 完成 |
| 装甲 JSON / bin / GLB | 完成 |
| mat 皮肤 | 完成 |
| model 皮肤导出 | 完成 |
| INDEXED 数组 + atlas + materialIdMap + artMap | 完成 |
| three.js 示例 | 完成 |
| skipped_textures | 0 |
| 白色主题 / 水下光照黑块 | 已修 |
| 船体接缝 / 裂缝 | 未解决 |
| 舰体深色/透视块 | 未解决 |
| 颜色还原 | 偏灰，未对齐游戏 |
| KTX2 真编码 | 未实现（WebP 回退） |
| 增量导出 | 未实现 |
| batch 启动性能 | 每船重新加载 DB |
| 自动化测试 / CI / NOTICE | 未做 |

## P0：船体接缝 / 裂缝

### 现象

- 所有船都有，不只是 ARP 大和；
- 近看舰体侧面出现黑/白细缝、锯齿三角；
- `debug=normal` 下几何完整，没有大洞；
- `debug=basic` 下贴图正常，不是贴图缺失；
- wireframe 下网格连续，没有大面积缺三角。

### 已排除

- 不是 AO（关闭 AO 无效）；
- 不是法线贴图 / roughness（关闭无效）；
- 不是 UV wrap（RepeatWrapping 无效）；
- 不是 alphaMode（全部 OPAQUE）；
- 不是 MESHDECAL（隐藏无效）；
- 不是 log depth / polygonOffset（无效）；
- 不是 ARP 皮肤加载错模型（hull_config 字符串解析已修，问题仍在）。

### 已尝试

1. `--merge-shapes`：每个 hull 分段整段导出，不按 shape_spans 拆。
   - 接缝明显减少；
   - 但部分 hull 面片丢失，Waterloo 侧面出现透明洞；
   - 已回退，默认仍为分段导出。
2. `polygonOffset` / `logarithmicDepthBuffer`：无效。

### 当前结论

接缝来自相邻 hull 分段在边界处的重叠/共面三角形，属于深度冲突，不是缺面。
简单合并会丢 shape，不能直接用。

### 下一步

- 对相邻 hull 分段做顶点焊接，去掉内部重复面；
- 或按分段给不同的 polygonOffset / renderOrder，只做视觉规避；
- 先确认 `.geometry` 的 `shape_spans` 与真实材质分组是否一致。

## P0：Waterloo 舰体深色 / 透视块

### 现象

- `PBSD511_Waterloo` 舰体侧面有一块深色/像透明洞的区域；
- 近看仍能看到纹理，不是完全空白；
- 其他船水下区域也曾出现类似黑块。

### 已排除

- 不是背景透出（背景白色，洞应该是白色）；
- 不是 alphaMode（manifest 全部 OPAQUE）；
- 不是 AO / 法线 / UV；
- 不是合并 hull 后的缺面（已回退到分段导出，问题仍在）。

### 当前判断

- 更可能是 Hull albedo 中黑色占位区，或 camo/colorScheme mask；
- 也可能是分段边界重叠面在同一位置反复遮挡，视觉上像透明。

### 下一步

- 按材质名逐个隐藏，定位覆盖该区域的 mesh/material；
- 检查该 material 的 albedo 在对应 UV 区域是否本来就是黑色；
- 如果是 colorScheme mask，需要导出默认涂装或烘焙；
- 如果两层几何重叠，按 P0 接缝方案处理。

## P1：颜色还原

- 当前 INDEXED shader 使用近似光照：
  `col = c * (0.75 + 0.35*ndl) + spec * mg.r * 0.35;`
  `col *= 1.65;`
  `col = pow(col, 1/2.2);`
- PBS 使用 MeshStandardMaterial，金属度/粗糙度为估算值；
- 水下光照黑块已通过提高 HemisphereLight groundColor + AmbientLight 修复；
- 整体仍偏灰，未与游戏内截图逐像素对齐。

### 下一步

- 用游戏内同角度截图做颜色对比；
- PBS 的 composite/artMap 叠加公式需要完整实现；
- INDEXED 的 tint/remove/art 强度需要校准。

## P1：KTX2 真编码

- `--ktx2` 只有在系统存在 `toktx` 时才生效；
- 当前实际产物是 WebP；
- 需要安装 Basis Universal / KTX-Software，或接编码库；
- 同时更新 manifest 的 texture_format 和 three.js 的 KTX2Loader 路径。

## P1：增量导出

尚未实现：

- 没有 `data/export_manifest.db`；
- 没有单船 geom_fp / tex_fp / skin_fp；
- `batch` 目前只按“文件是否存在”跳过；
- 没有把 assets.bin / GameParams / build 变化作为总闸。

## P1：batch 性能

- 每艘船都是独立 Python 进程；
- 每次都重新加载 GameParams、DB、VFS 索引；
- 四艘串行导出需要数分钟到十几分钟；
- 可在同一进程内批量导出，省掉重复初始化。

## P2：工程化

- 没有自动化测试 / CI；
- 没有 NOTICE（kraken.py 为 GPLv3）；
- `docs/sample` 约 700 MB，未纳入 git（.gitignore 已忽略）；
- 文档中的样本清单需要随导出更新。

## 已修复（备查）

- 双服 GameParams 解析；
- Lesta INDEXED 数组 / atlas / materialIdMap；
- skipped_textures 658 → 0（DX10 texture array 判断）；
- ARP 皮肤 hull_config 字符串解析；
- 水下光照黑块（HemisphereLight groundColor + AmbientLight）；
- three.js 白底主题；
- model 皮肤独立 geometry/manifest 与 switchModel()；
- `--merge-shapes` 保留为可选，但不推荐（会丢 shape）。

## 测试样本

```text
docs/sample/
  PBSD511_Waterloo/              80.3 MB
  PJSB018_Yamato_1944/          164.6 MB   Lesta
  PJSB018_Yamato_1944_WG/       373.0 MB   WG
  PJSB520_Raiju/                 34.8 MB
  PJSB700_ARP_Yamato/            70.9 MB
```

本地预览：

```powershell
cd docs
python -m http.server 8767
```

```text
http://127.0.0.1:8767/threejs-example.html?ship=PBSD511_Waterloo&root=sample/PBSD511_Waterloo/
```

## 复现命令

Waterloo：

```bash
python cli.py ship --game D:\Games\Korabli --ship PBSD511_Waterloo \
  --out out --armor --skins
```

WG 大和：

```bash
python cli.py ship --server wg --game D:\Games\World_of_Warships \
  --ship PJSB018_Yamato_1944 --out out --armor --skins --model-skins
```

Lesta 大和：

```bash
python cli.py ship --game D:\Games\Korabli --ship PJSB018_Yamato_1944 \
  --out out --armor --skins --model-skins
```
