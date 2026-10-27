# 网页端渲染设计参考（模型 / 材质 / 涂装 / 装甲）

> 状态：设计稿（2026-10-26）
> 适用：wows-exporter-3dmodel 的网页端产物
> 决策：three.js、贴图统一 WebP（KTX2 已弃用）、贴图外置、涂装按游戏内叠加/图集还原

## 1. 目录布局

geometry.glb 和 manifest.json **不放进单船目录**，按类型平铺，按 ship_key 查找：

~~~
web/<server>/<build>/
  geometry/
    PJSB520_Raiju/
      PJSB520_Raiju.glb
      PJSB520_Raiju__camo_01.glb   # 模型皮肤（可选）
  manifests/
    PJSB520_Raiju/
      PJSB520_Raiju.json
      PJSB520_Raiju__camo_01.json
  armor/
    PJSB520_Raiju/
      PJSB520_Raiju.json
      PJSB520_Raiju.bin            # 可选：装甲三角形二进制
      PJSB520_Raiju.glb
  textures/
    <sha1>.webp                    # 扁平，跨船共享
  skins/                           # 只在 --skins / --model-skins 时存在
    PJSB520_Raiju/
      default.json
      camo_01.json
~~~

除 `textures/` 外，每类产物都按船名分一层目录；manifest 里的路径统一相对
**输出根**（`geometry/<ship>/<ship>.glb`、`textures/<sha1>.webp`），
GLB 内部的贴图 URI 则是相对 GLB 的 `../../textures/<sha1>.webp`。

- 每艘船的文件名统一用 GameParams 的 ship key（例如 PJSB520_Raiju），不用显示名；
- <build> 用 bin 目录名（例如 8867689），换版本后可整体切换/回滚；
- 贴图按内容 hash 命名，跨船共享、可永久缓存。

## 2. geometry.glb 规范

只包含几何和材质引用，不包含任何 image/texture：

- 节点层级：Scene Root → 部件（船体分段 / 挂载）→ visual 节点 → mesh；
- 每个 mesh 的 extras 带 material_id（对应 manifest.materials 的 key）；
- 每个 material 的 extras 带 wows_material（原始 mfm 路径）和 mode（pbs / indexed）；
- 坐标：Y-up、右手系，当前导出已做 Z 镜像；单位与游戏一致（米）；
- 顶点烘焙到世界坐标，挂载节点保留自己的矩阵（与现有导出行为一致）；
- 不写 skin 时仍保留 _JOINTS_0 / _WEIGHTS_0 自定义属性（可选）。

## 3. manifest.json

~~~json
{
  "schema": "wows-web/1",
  "ship": "PJSB520_Raiju",
  "build": "8867689",
  "bounds": { "min": [-1.47, -0.81, -8.48], "max": [1.50, 2.52, 8.17] },
  "materials": {
    "m_0": {
      "mfm": "content/.../Hull.mfm",
      "mode": "pbs",
      "double_sided": true,
      "slots": {
        "albedo": "textures/ab12.webp",
        "normal": "textures/cd34.webp",
        "mg": "textures/ef56.webp"
      },
      "params": { "alpha": 1.0, "tint": [1, 1, 1] }
    },
    "m_1": {
      "mfm": "content/.../Deck.mfm",
      "mode": "indexed",
      "array": "textures/aa00.webp",
      "tile_index": 12,
      "uv": { "scale": [1, 1], "offset": [0, 0] }
    }
  },
  "meshes": [
    { "node": "Hull_Bow_wireShape", "material": "m_0" }
  ],
  "skins": ["default", "camo_01"],
  "hashes": {
    "geometry": "sha1:...",
    "materials": "sha1:...",
    "textures": "sha1:..."
  }
}
~~~

约定：

- materials 的 key 是稳定 id（不要用数组下标，重导出后不能变）；
- slots 只写实际存在的通道；
- indexed 材质的 array 指向拼好的图集 WebP，tile_index 是图集内格号；着色器按
  `atlasUV(slice, uv)` 换算（`uGrid` 给出列/行数），不需要 sampler2DArray；
- manifest 里的路径全部相对 manifests/ 或站点根目录，二选一并在加载器里统一。

## 4. 皮肤 / 涂装

skins/<ship>/<skin_id>.json：

~~~json
{
  "id": "camo_01",
  "display_name": "Camo 01",
  "overrides": {
    "m_0": {
      "albedo": "textures/98ab.webp",
      "composite": "textures/77cd.webp",
      "artMap": "textures/66ef.webp",
      "colors": { "base": "#3a4a5a", "camo": "#6b7c8d" },
      "blend": "lerp"
    }
  }
}
~~~

### 标准 PBS 涂装

游戏内涂装不是简单换 albedo，而是：

- 基础 albedo；
- composite（RGBA 遮罩/通道）；
- artMap（图案/色块）；
- colorScheme（颜色）；
- 按遮罩混合。

网页 shader 需要实现同样的合成，参数放在 skins 的 overrides 里。如果精度要求不高，可以离线烘焙一张最终 albedo，但这会丢失动态换色能力。

### INDEXED（视觉 2.0）

- 材质使用 albedoArray / normalArray / MGArray；
- 每个材质有 tile_index 和 UV 变换；
- JSON 里写 array + tile_index + uv；
- 网页用 sampler2DArray（three.js DataArrayTexture）按层采样，不能用普通 MeshStandardMaterial。

## 5. 装甲数据

### 现状

- 渲染 GLB 里**不包含装甲**；
- 装甲目前导出为单独的 <ship>_armor.glb；
- 板块元数据（zone / material / thickness / hidden）写在装甲 GLB 的 material/node extras 里，**没有独立 JSON**。

### 目标

新增 armor/<ship>.json，任何需要装甲数据的模块都读它，不必解析 GLB：

~~~json
{
  "schema": "wows-armor/1",
  "ship": "PJSB520_Raiju",
  "build": "8867689",
  "space": "ship",
  "bounds": { "min": [-1.46, -0.70, -8.42], "max": [1.46, 1.68, 8.13] },
  "triangle_count": 8601,
  "plates": [
    {
      "id": "Hull|Belt 100mm",
      "zone": "Hull",
      "material": "Belt",
      "thickness_mm": 100.0,
      "component": "船体",
      "hidden": false,
      "tri_start": 0,
      "tri_count": 120,
      "bounds": { "min": [-1.2, 0.1, -3.0], "max": [1.2, 0.9, 1.4] }
    }
  ]
}
~~~

- plates 按 zone → material → thickness 分组，与游戏内装甲查看器一致；
- tri_start / tri_count 指向装甲三角形数组（见下）；
- component 用于按部件筛选（船体/主炮塔/副炮/防空/…）；
- hidden 保留游戏内隐藏板的语义。

### 装甲几何

两种落盘方式，二选一或并存：

1. armor/<ship>.glb：每个板块一个节点，extras 带 id；适合直接渲染/描边；
2. armor/<ship>.bin + JSON 索引：紧凑、便于拾取和高亮。

bin 布局（小端）：

~~~
offset 0:  positions  float32[t*3*3]   (每三角形 3 个顶点)
然后:      normals    float32[t*3*3]
然后:      plate_ids  uint32[t]        (每个三角形对应 plates 下标)
~~~

JSON 里增加：

~~~json
"buffers": {
  "bin": "armor/PJSB520_Raiju.bin",
  "positions": { "offset": 0, "count": 25803 },
  "normals": { "offset": 309636, "count": 25803 },
  "plate_ids": { "offset": 619272, "count": 8601 }
}
~~~

网页可按 plate_id 建 group、做射线拾取、按厚度着色、显示 tooltip；装甲数据同时可供分析/导出工具使用。

## 6. three.js 集成

### 加载

- GLTFLoader 加载 geometry.glb（不期望 image）；
- 贴图按 manifest 的 URI 用 TextureLoader 加载（全部 WebP，无 KTX2 分支）；
- 所有贴图设置正确 colorSpace：albedo 用 SRGBColorSpace，normal/MG 用 NoColorSpace。

### 材质工厂

- PBS：MeshStandardMaterial + onBeforeCompile 注入 composite/artMap 采样与混合；
- INDEXED：ShaderMaterial + sampler2DArray，按 tile_index 采样；
- 每个 material 保存 uniforms 引用，换肤时只改 uniform/texture，不重建 geometry。

### 换肤

1. 预加载目标 skin 的贴图；
2. 更新对应 material 的 uniform（albedo/composite/artMap/colors）；
3. 不需要重新加载 GLB，也不需要重建 mesh。

### 装甲叠加

- 用 armor.json + bin 构建独立 BufferGeometry；
- 每个三角形写 plate_id 属性，颜色按厚度映射；
- 射线拾取命中三角形 → plate_id → plates 元数据 → tooltip；
- 装甲层可开关、半透明、只显示某 zone/component。

## 7. 缓存与增量

- geometry.glb 用 ship + build + geometry hash 做缓存键；
- textures 用内容 hash，永久缓存（immutable）；
- manifest 带 materials/textures hash，变更时网页刷新材质；
- 增量导出清单可复用同一套 hash：geometry_fp / tex_fp / skin_fp，分别判断是否需要重导。

## 8. 验收清单

- [ ] geometry.glb 内没有 image/texture；
- [ ] manifest 的 material key 在重导出后保持稳定；
- [ ] WebP 贴图在 three.js 中正确显示（含 colorSpace 与 wrap）；
- [ ] 切换皮肤不重新加载几何；
- [ ] PBS 涂装与游戏内截图对比（颜色/遮罩方向）；
- [ ] INDEXED 材质按 tile_index 采样正确；
- [ ] 装甲 JSON 的 plate 数量/三角形数与 GLB 一致；
- [ ] 装甲拾取能返回 zone/material/thickness。

## 9. 待定

- 涂装 shader 的精确公式（需要从游戏 shader / 现有 camo_service 对齐）；
- 坐标缩放是否在导出时统一（当前 bounds 数值偏小，网页端需确认单位）。
