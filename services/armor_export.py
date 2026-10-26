"""Armor JSON/bin export (web-friendly, independent of the armor GLB)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np


def _component_by_triangle(scene, armor_meshes):
    meshes = [m for m in (armor_meshes or []) if m is not None and m.positions.size]
    comps = []
    for (start, count), mesh in zip(scene.mesh_tri_range, meshes):
        comps.extend([getattr(mesh, "component", "")] * count)
    if len(comps) < scene.tri_count:
        comps.extend([""] * (scene.tri_count - len(comps)))
    return comps


def write_armor_files(out_dir: Path, ship_key: str, build: str, scene, armor_meshes) -> dict | None:
    """Write armor/<ship>.json + armor/<ship>.bin. Returns the JSON dict."""
    if scene is None or not scene.tri_count:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    comps = _component_by_triangle(scene, armor_meshes)

    order: list[int] = []
    plates: list[dict] = []
    for zone in sorted(scene.zones):
        for part in sorted(scene.zones[zone]):
            for tenths in sorted(scene.zones[zone][part]):
                tris = np.asarray(scene.zones[zone][part][tenths], dtype=np.int64)
                if tris.size == 0:
                    continue
                info = scene.tri_info[int(tris[0])]
                mm = tenths / 10.0
                plate = {
                    "id": "%s|%s %gmm" % (zone, part, mm),
                    "zone": zone,
                    "material": part,
                    "thickness_mm": float(mm),
                    "component": comps[int(tris[0])] if comps else "",
                    "hidden": bool(getattr(info, "hidden", False)),
                    "tri_start": len(order),
                    "tri_count": int(tris.size),
                }
                order.extend(int(t) for t in tris)
                plates.append(plate)

    tri = np.asarray(order, dtype=np.int64)
    pos = scene.world_positions[tri[:, None] * 3 + np.arange(3)].reshape(-1, 3).astype(np.float32)
    nrm = scene.world_normals[tri[:, None] * 3 + np.arange(3)].reshape(-1, 3).astype(np.float32)
    pid = np.empty(len(plates), dtype=np.uint32)
    for i, plate in enumerate(plates):
        pid[i] = i
        sl = pos[plate["tri_start"] * 3:(plate["tri_start"] + plate["tri_count"]) * 3]
        plate["bounds"] = {
            "min": [float(v) for v in sl.min(axis=0)],
            "max": [float(v) for v in sl.max(axis=0)],
        }
    plate_ids = np.repeat(pid, [p["tri_count"] for p in plates])

    bin_path = out_dir / (ship_key + ".bin")
    pos_bytes = pos.tobytes()
    nrm_bytes = nrm.tobytes()
    pid_bytes = plate_ids.astype(np.uint32).tobytes()
    bin_path.write_bytes(pos_bytes + nrm_bytes + pid_bytes)

    doc = {
        "schema": "wows-armor/1",
        "ship": ship_key,
        "build": build,
        "space": "ship",
        "bounds": {
            "min": [float(v) for v in scene.bounds_min],
            "max": [float(v) for v in scene.bounds_max],
        },
        "triangle_count": int(scene.tri_count),
        "plates": plates,
        "zones": {
            zone: {part: sorted(scene.zones[zone][part]) for part in scene.zones[zone]}
            for zone in scene.zones
        },
        "buffers": {
            "bin": "armor/" + ship_key + ".bin",
            "positions": {"offset": 0, "count": int(pos.size)},
            "normals": {"offset": len(pos_bytes), "count": int(nrm.size)},
            "plate_ids": {"offset": len(pos_bytes) + len(nrm_bytes), "count": int(plate_ids.size)},
        },
    }
    (out_dir / (ship_key + ".json")).write_text(
        json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return doc
