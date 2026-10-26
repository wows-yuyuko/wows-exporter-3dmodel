"""Build manifests/<ship>.json from an exported geometry GLB (external textures)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pygltflib import GLTF2


def _sha1_file(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _asset_uris(materials: dict) -> list[str]:
    """材质引用的**全部外部文件**（相对输出目录）。

    只统计 `slots` 会漏掉 INDEXED 的图集/索引图与数组 bin ——
    换涂装（artMap）或换图集时 `hashes.textures` 不变，前端缓存无法失效。
    """
    uris: set[str] = set()
    for m in materials.values():
        uris.update(u for u in (m.get("slots") or {}).values() if u)
        uris.update(u for u in (m.get("layers") or {}).values() if u)
        idx = m.get("indexed") or {}
        for meta in (idx.get("textures") or {}).values():
            if isinstance(meta, dict) and meta.get("uri"):
                uris.add(meta["uri"])
        for meta in (idx.get("arrays") or {}).values():
            if isinstance(meta, dict) and meta.get("bin"):
                uris.add(meta["bin"])
    return sorted(uris)


def _slot_uri(gltf: GLTF2, info):
    if info is None:
        return None
    tex = gltf.textures[info.index]
    img = gltf.images[tex.source]
    uri = img.uri or ""
    return uri.replace("../textures/", "textures/")


def write_web_manifest(glb_path: Path, manifest_path: Path, ship: str, build: str,
                       armor_ref: str | None = None, skins: list[str] | None = None) -> dict:
    gltf = GLTF2().load(str(glb_path))
    mat_keys: dict[int, str] = {}
    materials: dict[str, dict] = {}
    for idx, mat in enumerate(gltf.materials or []):
        extras = mat.extras or {}
        source = str(extras.get("texture_path") or "")
        key = "m_" + hashlib.sha1((str(mat.name or "") + "|" + source).encode("utf-8")).hexdigest()[:10]
        while key in materials:
            key += "x"
        mat_keys[idx] = key
        slots = {}
        pbr = mat.pbrMetallicRoughness
        if pbr is not None:
            base = _slot_uri(gltf, pbr.baseColorTexture)
            if base:
                slots["albedo"] = base
            mr = _slot_uri(gltf, pbr.metallicRoughnessTexture)
            if mr:
                slots["mg"] = mr
        normal = _slot_uri(gltf, mat.normalTexture)
        if normal:
            slots["normal"] = normal
        occlusion = _slot_uri(gltf, mat.occlusionTexture)
        if occlusion:
            slots["ao"] = occlusion
        emissive = _slot_uri(gltf, mat.emissiveTexture)
        if emissive:
            slots["emissive"] = emissive
        indexed = extras.get("wows_indexed") or {}
        idx_tex = {}
        for slot, val in (extras.get("wows_indexed_textures") or {}).items():
            if isinstance(val, dict):
                meta = dict(val)
                if meta.get("uri"):
                    meta["uri"] = meta["uri"].replace("../textures/", "textures/")
                idx_tex[slot] = meta
            else:
                tex = gltf.textures[val]
                img = gltf.images[tex.source]
                if img.uri:
                    idx_tex[slot] = img.uri.replace("../textures/", "textures/")
        if idx_tex:
            indexed["textures"] = idx_tex
        if indexed.get("arrays"):
            for meta in indexed["arrays"].values():
                if meta.get("bin"):
                    meta["bin"] = meta["bin"].replace("../textures/", "textures/")
        wows_tex = extras.get("wows_textures") or {}
        mode = "indexed" if any("Array" in str(k) for k in wows_tex) else "pbs"
        layers = {}
        for slot, tex_idx in (extras.get("wows_layer_textures") or {}).items():
            tex = gltf.textures[tex_idx]
            img = gltf.images[tex.source]
            if img.uri:
                layers[slot] = img.uri.replace("../textures/", "textures/")
        materials[key] = {
            "id": key,
            "name": str(mat.name or ""),
            "source": source,
            "mode": mode,
            "slots": slots,
            "layers": layers,
            "indexed": indexed,
            "wows_textures": wows_tex,
            "double_sided": bool(mat.doubleSided),
            "alpha_mode": str(mat.alphaMode or "OPAQUE"),
        }

    meshes = []
    for node in gltf.nodes or []:
        if node.mesh is None:
            continue
        mesh = gltf.meshes[node.mesh]
        mesh_extras = mesh.extras or {}
        for prim in mesh.primitives or []:
            key = mat_keys.get(prim.material)
            if not key:
                continue
            meshes.append({
                "node": node.name or "",
                "mesh": mesh.name or "",
                "material": key,
                "component": mesh_extras.get("component", (node.extras or {}).get("component", "")),
            })

    #: 覆盖 slots + layers + INDEXED 图集 + 数组 bin（换涂装/换图集都能让指纹变化）
    assets = _asset_uris(materials)
    tex_hash = hashlib.sha1()
    for uri in assets:
        tex_hash.update(uri.encode("utf-8"))
        p = manifest_path.parent.parent / uri
        if p.is_file():
            tex_hash.update(_sha1_file(p).encode("ascii"))
    doc = {
        "schema": "wows-web/1",
        "ship": ship,
        "build": build,
        "geometry": "geometry/" + ship + ".glb",
        "materials": materials,
        "meshes": meshes,
        "skins": skins or ["default"],
        "hashes": {
            "geometry": _sha1_file(glb_path),
            "materials": hashlib.sha1(json.dumps(materials, sort_keys=True).encode("utf-8")).hexdigest(),
            "textures": tex_hash.hexdigest(),
        },
    }
    if armor_ref:
        doc["armor"] = armor_ref
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return doc
