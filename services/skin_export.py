"""Skin descriptors: default + mat-type permoflage/mskin/skin variants."""
from __future__ import annotations

import json
import re
from pathlib import Path


def write_default_skin(out_root: Path, ship_key: str) -> Path:
    d = out_root / "skins" / ship_key
    d.mkdir(parents=True, exist_ok=True)
    path = d / "default.json"
    doc = {
        "id": "default",
        "display_name": "Default",
        "overrides": {},
    }
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def _safe_id(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", name).strip("_") or "skin"


def export_model_skins(out_root: Path, ship, build: str, gameparams_path: str,
                       limit: int = 0) -> list[str]:
    """Export origin=model skins as separate geometry.glb + manifest + skin json."""
    from services.geometry_service import GeometryService, ShipInfo
    from services.camo_service import CamoService
    from services.export_service import export_render_glb, GlbRenderOptions
    from services.web_manifest import write_web_manifest

    gs = GeometryService.instance()
    svc = CamoService(extractor=gs.get_extractor(), cache=gs._get_assets_cache(), bin_folder=build)
    svc._ensure_loaded()
    try:
        from core.gameparams import exteriors
        svc._exteriors = exteriors(gameparams_path)
    except Exception:
        pass
    infos = svc.list_camos({ship.key}, set(ship.permoflages or []))
    geom_dir = out_root / "geometry"
    manifest_dir = out_root / "manifests"
    skin_dir = out_root / "skins" / ship.key
    textures_dir = out_root / "textures"
    for d in (geom_dir, manifest_dir, skin_dir):
        d.mkdir(parents=True, exist_ok=True)
    exported: list[str] = []
    for scheme in infos:
        if scheme.origin != "model" or not scheme.skin:
            continue
        sid = _safe_id(scheme.raw_name or scheme.ext_index or ("model_%d" % scheme.id))
        while sid in exported:
            sid += "_2"
        info = ShipInfo(game_key=ship.key, display_name=ship.key,
                        model_folder=ship.model_folder, model_path=ship.model,
                        nation=ship.nation, ship_type=ship.ship_type, tier=ship.tier)
        geom = gs.load_ship(info, skin=scheme.skin, model_replace=scheme.model_replace or None)
        glb = geom_dir / (ship.key + "__" + sid + ".glb")
        options = GlbRenderOptions(embed_textures=True, textures_dir=str(textures_dir),
                                   texture_uri_prefix="../textures", texture_format="webp")
        export_render_glb(geom, str(glb), options)
        man = manifest_dir / (ship.key + "__" + sid + ".json")
        write_web_manifest(glb, man, ship.key + "__" + sid, build, skins=[sid])
        doc = {
            "id": sid,
            "display_name": scheme.display_name or sid,
            "origin": "model",
            "species": scheme.species or "skin",
            "geometry": "geometry/" + ship.key + "__" + sid + ".glb",
            "manifest": "manifests/" + ship.key + "__" + sid + ".json",
            "overrides": {},
        }
        (skin_dir / (sid + ".json")).write_text(
            json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        exported.append(sid)
        if limit and len(exported) >= limit:
            break
    return exported


def export_mat_skins(out_root: Path, ship, build: str, limit: int = 0,
                     gameparams_path: str = "") -> list[str]:
    """Export material-type skins referenced by the ship permoflages.

    Only PBS-style camo textures are exported here (webp); INDEXED arrays and
    model-replacement skins are handled separately.
    """
    from services.geometry_service import GeometryService
    from services.camo_service import CamoService
    from services.texture_export import dds_to_webp, write_webp, kind_for_path

    gs = GeometryService.instance()
    extractor = gs.get_extractor()
    svc = CamoService(extractor=extractor, cache=gs._get_assets_cache(), bin_folder=build)
    svc._ensure_loaded()
    if gameparams_path:
        try:
            from core.gameparams import exteriors
            svc._exteriors = exteriors(gameparams_path)
        except Exception:
            pass
    permo = set(ship.permoflages or [])
    infos = svc.list_camos({ship.key}, permo)

    species_wanted = {"permoflage", "mskin", "skin"}
    textures_dir = out_root / "textures"
    skin_dir = out_root / "skins" / ship.key
    skin_dir.mkdir(parents=True, exist_ok=True)
    exported: list[str] = []
    for scheme in infos:
        entry = scheme.entry
        if scheme.origin != "mat" or entry is None:
            continue
        sp = (scheme.species or "").lower()
        if sp not in species_wanted and scheme.raw_name not in permo and scheme.ext_index not in permo:
            continue
        tex_uris = {}
        for cat, vpath in (entry.textures or {}).items():
            base = vpath
            for ext in (".dds", ".dd0", ".dd1", ".dd2"):
                if base.lower().endswith(ext):
                    base = base[:-len(ext)]
                    break
            try:
                vp, dds = gs._load_texture_tier(base, extractor)
            except Exception:
                continue
            if not dds:
                continue
            webp = dds_to_webp(dds, kind_for_path(vpath))
            if webp:
                tex_uris[cat] = write_webp(textures_dir, webp)
        if not tex_uris:
            continue
        sid = _safe_id(scheme.raw_name or scheme.ext_index or ("skin_%d" % scheme.id))
        while sid in exported:
            sid += "_2"
        uv = {}
        for cat, t in (entry.uv_transforms or {}).items():
            uv[cat] = {"scale": list(t.scale), "offset": list(t.offset)}
        doc = {
            "id": sid,
            "display_name": scheme.display_name or entry.display_name or sid,
            "origin": "mat",
            "species": scheme.species or "permoflage",
            "use_color_scheme": bool(entry.use_color_scheme),
            "tiled": bool(entry.tiled),
            "color_schemes": list(entry.color_schemes or []),
            "colors": entry.colors,
            "uv_transforms": uv,
            "textures": tex_uris,
            "source_textures": dict(entry.textures or {}),
            "overrides": {},
        }
        (skin_dir / (sid + ".json")).write_text(
            json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        exported.append(sid)
        if limit and len(exported) >= limit:
            break
    return exported
