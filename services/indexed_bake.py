"""INDEXED 材质烘焙：materialIdMap + 图集数组 + LUT + artMap → 标准 PBR 三图。

Lesta「视觉 2.0」的船体材质不走常规 PBS：它用一张 `materialIdMap` 把船体 UV 切成
最多 196 个「材质块」，每块再用 LUT（`tileIdxMatIdArr` / `offsetScaleMatIdArr` /
`rotationMatIdArr` / `albedoTintMatIdArr` / `artStrengthMatIdArr`）去采样
30 层的贴图集数组，最后叠一张 `artMap` 涂装图案。标准 glTF 查看器看不懂这套，
所以这里把它烘成 baseColor / normal / metallicRoughness 三张常规贴图。

关键点：shader 里每个输入都只是 `vUv` 的函数（materialIdMap、artMap 与图集采样都
按同一个 UV 取值），与几何无关 —— 因此**不需要光栅化**，直接在 UV 栅格上逐像素做
向量化采样即可；输出分辨率也可以自由选（默认跟随 materialIdMap 原生尺寸）。

与 shader 的刻意差异（写进 manifest.bake.notes）：
  - 法线：shader 的 `normalize(vN + vec3(ntex.xy*0.6, 0))` 是视图空间近似，
    无法烘成切线空间贴图；这里输出切线空间法线（保持 XY、补 Z，
    与 PBS 路径 `_to_gltf_normal_map` 同约定）。
  - 光照：shader 末尾的 `*1.65` / `mix(white,0.06)` / spec 是手搓光照，
    属于渲染而非材质，不烘。
"""
from __future__ import annotations

import numpy as np

#: LUT 行数（shader 里 `min(floor(m*255+0.5), 195)`）
_LUT_ROWS = 196
#: 采样行分块（控制峰值内存；4096×2048 全图一次 gather 会到 GB 级）
_STRIP_ROWS = 256


# ────────────────────────────────────────────────────────────────────────────
# sRGB 传递函数（GPU 对 sRGB 纹理用的是分段曲线，不是 2.2 幂）
# ────────────────────────────────────────────────────────────────────────────

def _build_srgb_lut() -> np.ndarray:
    x = np.arange(256, dtype=np.float32) / 255.0
    lin = np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)
    return lin.astype(np.float32)


_SRGB_TO_LINEAR = _build_srgb_lut()


def _linear_to_srgb(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.0031308, x * 12.92,
                    1.055 * np.power(x, 1.0 / 2.4) - 0.055).astype(np.float32)


def _u8_to_linear(rgb_u8: np.ndarray) -> np.ndarray:
    return _SRGB_TO_LINEAR[rgb_u8]


# ────────────────────────────────────────────────────────────────────────────
# DDS 纹理数组（惰性逐层解码）
# ────────────────────────────────────────────────────────────────────────────

_BLOCK8 = {71, 72, 77, 78, 80, 81}   # BC1 / BC4 系每块 8 字节
_DECODERS = {71: "decode_bc1", 72: "decode_bc1", 77: "decode_bc3", 78: "decode_bc3",
             80: "decode_bc4", 83: "decode_bc5", 98: "decode_bc7", 99: "decode_bc7"}


class DdsSlices:
    """DX10 纹理数组的惰性切片解码器（只解 mip0）。"""

    def __init__(self, data: bytes):
        import struct
        if len(data) < 148 or data[:4] != b"DDS " or data[84:88] != b"DX10":
            raise ValueError("not a DX10 dds")
        self.data = data
        self.width = int(struct.unpack_from("<I", data, 16)[0])
        self.height = int(struct.unpack_from("<I", data, 12)[0])
        self.dxgi = int(struct.unpack_from("<I", data, 128)[0])
        self.count = max(1, int(struct.unpack_from("<I", data, 128 + 12)[0]))
        if self.dxgi not in _DECODERS:
            raise ValueError(f"unsupported dxgi {self.dxgi}")
        block = 8 if self.dxgi in _BLOCK8 else 16
        self.slice_size = (max(1, (self.width + 3) // 4)
                           * max(1, (self.height + 3) // 4) * block)

    def slice(self, index: int) -> np.ndarray | None:
        """第 index 层 → (H, W, 4) uint8 RGBA。越界/解码失败返回 None。"""
        import texture2ddecoder as tdec
        if index < 0 or index >= self.count:
            return None
        off = 148 + index * self.slice_size
        raw = self.data[off:off + self.slice_size]
        if len(raw) < self.slice_size:
            return None
        dec = getattr(tdec, _DECODERS[self.dxgi])
        buf = dec(raw, self.width, self.height)
        arr = np.frombuffer(bytes(buf), dtype=np.uint8)
        need = self.width * self.height * 4
        if arr.size != need:
            return None
        bgra = arr.reshape(self.height, self.width, 4)
        return np.ascontiguousarray(bgra[:, :, [2, 1, 0, 3]])


# ────────────────────────────────────────────────────────────────────────────
# 采样
# ────────────────────────────────────────────────────────────────────────────

def _resize_tile(rgba_u8: np.ndarray, size: int, color: bool) -> np.ndarray:
    """把一层图集缩到 size×size 并转 float32（color=True 时做 sRGB→linear）。"""
    from PIL import Image
    im = Image.fromarray(rgba_u8, "RGBA")
    if im.width != size or im.height != size:
        im = im.resize((size, size), Image.LANCZOS if color else Image.BOX)
    arr = np.asarray(im, dtype=np.uint8)
    out = arr.astype(np.float32) / 255.0
    if color:
        out[:, :, :3] = _u8_to_linear(arr[:, :, :3])
    return out[:, :, :3]


def _sample(img: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """双线性 + wrap 采样。img (S,S,3)，x/y 为像素坐标。"""
    s = img.shape[0]
    x0 = np.floor(x).astype(np.int32)
    y0 = np.floor(y).astype(np.int32)
    fx = (x - x0).astype(np.float32)[:, None]
    fy = (y - y0).astype(np.float32)[:, None]
    x0 %= s
    y0 %= s
    x1 = (x0 + 1) % s
    y1 = (y0 + 1) % s
    top = img[y0, x0] * (1.0 - fx) + img[y0, x1] * fx
    bot = img[y1, x0] * (1.0 - fx) + img[y1, x1] * fx
    return top * (1.0 - fy) + bot * fy


def _nearest_resize(mat: np.ndarray, w: int, h: int) -> np.ndarray:
    """ID 图必须最近邻缩放（线性插值会把材质块边界混成不存在的 id）。"""
    from PIL import Image
    im = Image.fromarray(mat.astype(np.uint8), "L")
    if im.size != (w, h):
        im = im.resize((w, h), Image.NEAREST)
    return np.asarray(im, dtype=np.int16)


# ────────────────────────────────────────────────────────────────────────────
# 主流程
# ────────────────────────────────────────────────────────────────────────────

def _lut(params: dict, name: str) -> np.ndarray | None:
    arr = (params.get("arrays") or {}).get(name)
    if arr is None:
        return None
    a = np.asarray(arr, dtype=np.float32)
    if a.ndim != 2 or a.shape[0] < 2:
        return None
    return a


def bake_indexed(textures: dict, params: dict, max_size: int = 0) -> dict | None:
    """把一份 INDEXED 材质烘成三张贴图。

    textures: {贴图键: (vfs_path, dds_bytes)}（与 geometry_service 的 textures 同构）
    params:   index 参数 {arrays, offset, grid}
    max_size: 输出最长边上限（0/负 = 跟随 materialIdMap 原生尺寸）

    返回 {"albedo": (H,W,4)uint8, "normal": ..., "mr": ..., "size": (w,h),
          "notes": [...]}；数据不足时返回 None。
    """
    from services.export_service import dds_to_rgba

    mat_tx = textures.get("materialIdMap")
    alb_tx = textures.get("albedoArray") or textures.get("g_albedoMap")
    nrm_tx = textures.get("normalArray") or textures.get("g_normalMap")
    mg_tx = textures.get("MGArray") or textures.get("g_mgMap")
    if not (mat_tx and alb_tx):
        return None

    tile_arr = _lut(params, "tileIdxMatIdArr")
    off_arr = _lut(params, "offsetScaleMatIdArr")
    rot_arr = _lut(params, "rotationMatIdArr")
    tint_arr = _lut(params, "albedoTintMatIdArr")
    rem_arr = _lut(params, "albedoToRemoveTintMatIdArr")
    str_arr = _lut(params, "artStrengthMatIdArr")
    if tile_arr is None or off_arr is None:
        return None

    mat_rgba = dds_to_rgba(mat_tx[1])
    if mat_rgba is None:
        return None
    h0, w0 = mat_rgba.shape[:2]
    scale = 1.0
    if max_size and max(w0, h0) > max_size:
        scale = max_size / float(max(w0, h0))
    out_w = max(1, int(round(w0 * scale)))
    out_h = max(1, int(round(h0 * scale)))
    matid = _nearest_resize(mat_rgba[:, :, 0], out_w, out_h)

    alb = DdsSlices(alb_tx[1])
    nrm = DdsSlices(nrm_tx[1]) if nrm_tx else None
    mgn = DdsSlices(mg_tx[1]) if mg_tx else None

    # artMap：整图缩放到输出尺寸（颜色图用 LANCZOS），转 linear
    art_lin = art_a = None
    if textures.get("artMap"):
        art_rgba = dds_to_rgba(textures["artMap"][1])
        if art_rgba is not None:
            from PIL import Image
            im = Image.fromarray(art_rgba, "RGBA").resize((out_w, out_h), Image.LANCZOS)
            a8 = np.asarray(im, dtype=np.uint8)
            art_lin = _u8_to_linear(a8[:, :, :3])
            art_a = a8[:, :, 3].astype(np.float32) / 255.0

    rows = min(_LUT_ROWS, tile_arr.shape[0])
    tile_v = tile_arr[:rows]
    off_v = off_arr[:rows]
    rot_v = (rot_arr[:rows, 0] if rot_arr is not None else np.zeros(rows, np.float32))
    tint_v = tint_arr[:rows] if tint_arr is not None else np.zeros((rows, 4), np.float32)
    rem_v = rem_arr[:rows] if rem_arr is not None else np.ones((rows, 4), np.float32)
    str_v = (str_arr[:rows, 0] if str_arr is not None else np.ones(rows, np.float32))

    tint_lin = np.power(np.clip(tint_v[:, :3], 0.0, 1.0), 2.2).astype(np.float32)
    ref_lin = np.power(np.clip(rem_v[:, :3], 0.0, 1.0), 2.2).astype(np.float32)
    tint_thr = np.maximum(tint_v[:, 3], 1e-4).astype(np.float32)

    alb_out = np.zeros((out_h, out_w, 3), np.float32)
    nrm_out = np.zeros((out_h, out_w, 3), np.float32)
    mr_out = np.zeros((out_h, out_w, 3), np.float32)

    cache: dict = {}

    def _tile(slices: DdsSlices, layer: int, size: int, color: bool):
        key = (id(slices), layer, size, color)
        hit = cache.get(key)
        if hit is not None:
            return hit
        rgba = slices.slice(layer)
        if rgba is None:
            return None
        out = _resize_tile(rgba, size, color)
        cache[key] = out
        return out

    xs = (np.arange(out_w, dtype=np.float32) + 0.5) / out_w
    for y0 in range(0, out_h, _STRIP_ROWS):
        y1 = min(out_h, y0 + _STRIP_ROWS)
        ys = (np.arange(y0, y1, dtype=np.float32) + 0.5) / out_h
        mid = np.clip(matid[y0:y1], 0, rows - 1)
        u = np.broadcast_to(xs[None, :], (y1 - y0, out_w))
        v = np.broadcast_to(ys[:, None], (y1 - y0, out_w))
        art_lin_s = art_lin[y0:y1] if art_lin is not None else None
        art_a_s = art_a[y0:y1] if art_a is not None else None

        off = off_v[mid]
        tt = tile_v[mid]
        ang = rot_v[mid]
        su, sv = off[:, :, 2], off[:, :, 3]
        uu = u * su + off[:, :, 0]
        vv = v * sv + off[:, :, 1]
        ca, sa = np.cos(ang), np.sin(ang)
        ru = uu * ca + vv * sa
        rv = -uu * sa + vv * ca
        fu = ru - np.floor(ru)
        fv = rv - np.floor(rv)

        col = np.zeros((y1 - y0, out_w, 3), np.float32)
        nout = np.zeros_like(col)
        mout = np.zeros_like(col)
        for m in np.unique(mid):
            sel = mid == m
            if not sel.any():
                continue
            fu_s, fv_s = fu[sel], fv[sel]
            t = tt[sel][0]
            # 采样密度：每输出像素跨过多少源纹素 → 选对应的 mip 尺寸，避免摩尔纹
            foot = max(su[sel][0], sv[sel][0]) / max(out_w, out_h) * alb.width
            size = int(np.clip(round(alb.width / max(foot, 1.0)), 8, alb.width))
            x = fu_s * size
            y = fv_s * size

            a_img = _tile(alb, int(t[0]), size, True)
            if a_img is not None:
                rgb = _sample(a_img, x, y)
                ref = ref_lin[m]
                tint = tint_lin[m]
                diff = (np.abs(rgb - ref)).sum(axis=1, keepdims=True)
                k = np.clip((diff + 0.001) / tint_thr[m], 0.0, 1.0)
                rgb = tint + (rgb - tint) * k
                if art_lin_s is not None:
                    am = np.clip(art_a_s[sel] * str_v[m], 0.0, 1.0)[:, None]
                    rgb = rgb + (art_lin_s[sel] - rgb) * am
                col[sel] = rgb

            if nrm is not None:
                n_img = _tile(nrm, int(t[1]), size, False)
                if n_img is not None:
                    nv = _sample(n_img, x, y) * 2.0 - 1.0
                    nout[sel] = nv
            if mgn is not None:
                m_img = _tile(mgn, int(t[2]), size, False)
                if m_img is not None:
                    mout[sel] = _sample(m_img, x, y)

        alb_out[y0:y1] = col
        nrm_out[y0:y1] = nout
        mr_out[y0:y1] = mout

    def _u8(rgb: np.ndarray) -> np.ndarray:
        out = np.empty((out_h, out_w, 4), np.uint8)
        out[:, :, :3] = (_linear_to_srgb(rgb) * 255.0 + 0.5).astype(np.uint8)
        out[:, :, 3] = 255
        return out

    albedo = _u8(alb_out)

    # 法线：保持 XY、补 Z（与 PBS 路径同约定）；无数据时给平面法线
    nx, ny = nrm_out[:, :, 0], nrm_out[:, :, 1]
    nz = np.sqrt(np.clip(1.0 - nx * nx - ny * ny, 0.0, 1.0))
    normal = np.empty((out_h, out_w, 4), np.uint8)
    normal[:, :, 0] = ((nx * 0.5 + 0.5) * 255.0 + 0.5).astype(np.uint8)
    normal[:, :, 1] = ((ny * 0.5 + 0.5) * 255.0 + 0.5).astype(np.uint8)
    normal[:, :, 2] = ((nz * 0.5 + 0.5) * 255.0 + 0.5).astype(np.uint8)
    normal[:, :, 3] = 255

    # MR：glTF 约定 G=roughness(1-gloss)、B=metallic(F0 权重)
    mr = np.empty((out_h, out_w, 4), np.uint8)
    mr[:, :, 0] = 255
    mr[:, :, 1] = ((1.0 - mr_out[:, :, 1]) * 255.0 + 0.5).astype(np.uint8)
    mr[:, :, 2] = (mr_out[:, :, 0] * 255.0 + 0.5).astype(np.uint8)
    mr[:, :, 3] = 255

    return {"albedo": albedo, "normal": normal, "mr": mr,
            "size": (out_w, out_h),
            "notes": ["normal: tangent-space (shader uses a view-space blend)",
                      "lighting: not baked (renderer-side approximation)"]}
