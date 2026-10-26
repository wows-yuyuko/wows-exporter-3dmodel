"""wows-exporter-3dmodel CLI.

Subcommands:
    list  --game DIR [--server lesta|wg] [--filter TEXT]
    ship  --game DIR --ship KEY --out DIR [--server ...] [--no-textures]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core.gameparams import ships as gp_ships  # noqa: E402


def _auto_extract(game: Path, server: str) -> Path:
    from data_extractor import GameExtractor
    bins = sorted(p.name for p in (game / "bin").iterdir() if p.is_dir())
    if not bins:
        raise SystemExit("no game build under %s/bin" % game)
    ex = GameExtractor(str(game), bin_folder=bins[-1])
    try:
        entries = [e for e in ex.list_files(["content/GameParams*.data"]) if not e.is_directory]
        if not entries:
            raise SystemExit("GameParams not found in package")
        target = game / "data"
        target.mkdir(parents=True, exist_ok=True)
        for e in entries:
            name = e.path.rsplit("/", 1)[-1]
            ex.extract_single(e.path, target / name)
        print("[extract]", [e.path for e in entries])
    finally:
        ex.close()
    return _gameparams_path(game, server)


def _gameparams_path(game: Path, server: str, gp: str | None = None) -> Path:
    if gp:
        return Path(gp)
    data = game / "data"
    names = ["GameParams_py3.data", "GameParams_py2.data", "GameParams.data"] if server == "lesta" \
        else ["GameParams.data", "GameParams_py3.data"]
    for name in names:
        if (data / name).is_file():
            return data / name
    return _auto_extract(game, server)


def cmd_list(args):
    for ship in gp_ships(str(_gameparams_path(Path(args.game), args.server, args.gp)), args.filter):
        print("%s\t%s\tT%d\t%s" % (ship.key, ship.model_folder, ship.tier, ship.nation))


def _bootstrap(game: Path, server: str):
    from app.application import app
    if server == "lesta":
        app.config.wows_type = "Lesta"
        app.config.game_path_lesta = str(game)
    else:
        app.config.wows_type = "Wargaming"
        app.config.game_path_wargaming = str(game)
    from core.build_info import imported_build
    build = imported_build(server)
    if not build or not (game / "bin" / build).is_dir():
        bins = sorted(p.name for p in (game / "bin").iterdir() if p.is_dir())
        build = bins[-1] if bins else ""
    app.config.bin_folder = build
    return app


def _export_ship(ship, args, out: Path):
    from app.application import app
    from services.geometry_service import GeometryService, ShipInfo
    from services.export_service import export_render_glb, GlbRenderOptions

    info = ShipInfo(game_key=ship.key, display_name=ship.key,
                    model_folder=ship.model_folder, model_path=ship.model,
                    nation=ship.nation, ship_type=ship.ship_type, tier=ship.tier)
    snap = {}
    geom = GeometryService.instance().load_ship(
        info, progress_cb=lambda p, m: None,
        partial_cb=lambda g: snap.setdefault("g", g),
        lod=getattr(args, "lod", 2))
    geom_dir = out / "geometry"
    armor_dir = out / "armor"
    manifest_dir = out / "manifests"
    for d in (geom_dir, armor_dir, manifest_dir):
        d.mkdir(parents=True, exist_ok=True)
    textures_dir = out / "textures"
    glb = geom_dir / (ship.key + ".glb")
    options = GlbRenderOptions(
        embed_textures=not args.no_textures,
        textures_dir=str(textures_dir) if not args.no_textures else "",
        texture_uri_prefix="../textures",
        texture_format="webp",
        texture_max_size=getattr(args, "texture_size", 1024),
        split_shapes=(getattr(args, "split_shapes", False)
                      or not getattr(args, "merge_shapes", False)),
        include_damage=getattr(args, "damage", False),
        include_decals=getattr(args, "decals", False),
    )
    report = export_render_glb(geom, str(glb), options)
    print(report)
    if args.zstd:
        import zstandard
        zst = geom_dir / (ship.key + ".glb.zst")
        with open(glb, "rb") as src, open(zst, "wb") as dst:
            zstandard.ZstdCompressor(level=10).copy_stream(src, dst)
        print("wrote", zst)
    print("wrote", glb)
    armor_ref = None
    if args.armor:
        from models.armor_scene import ArmorScene
        from services.export_service import export_armor_glb
        from services.armor_export import write_armor_files
        scene = ArmorScene.build(geom.armor_meshes)
        if scene.tri_count:
            armor_glb = armor_dir / (ship.key + ".glb")
            report = export_armor_glb(geom, scene, str(armor_glb))
            print(report)
            if args.zstd:
                import zstandard
                zst = armor_dir / (ship.key + ".glb.zst")
                with open(armor_glb, "rb") as src, open(zst, "wb") as dst:
                    zstandard.ZstdCompressor(level=10).copy_stream(src, dst)
                print("wrote", zst)
            doc = write_armor_files(armor_dir, ship.key, app.config.bin_folder, scene, geom.armor_meshes)
            if doc:
                armor_ref = "armor/" + ship.key + ".json"
                print("wrote", armor_dir / (ship.key + ".json"))
            print("wrote", armor_glb)
        else:
            print("[warn] no armor triangles")
    from services.skin_export import write_default_skin, export_mat_skins, export_model_skins
    skin_path = write_default_skin(out, ship.key)
    print("wrote", skin_path)
    skins = ["default"]
    if getattr(args, "skins", False):
        extra_skins = export_mat_skins(out, ship, app.config.bin_folder,
                                        gameparams_path=str(_gameparams_path(Path(args.game), args.server, args.gp)),
                                        include_permoflage=getattr(args, "permoflage", False))
        skins += extra_skins
        print("[skins]", len(extra_skins), extra_skins[:8])
    if getattr(args, "model_skins", False):
        model_skins = export_model_skins(out, ship, app.config.bin_folder,
                                          gameparams_path=str(_gameparams_path(Path(args.game), args.server, args.gp)))
        skins += model_skins
        print("[model-skins]", len(model_skins), model_skins[:8])
    from services.web_manifest import write_web_manifest
    write_web_manifest(glb, manifest_dir / (ship.key + ".json"), ship.key,
                       app.config.bin_folder, armor_ref=armor_ref, skins=skins)
    print("wrote", manifest_dir / (ship.key + ".json"))
    return glb


def cmd_ship(args):
    _bootstrap(Path(args.game), args.server)
    ship = next((s for s in gp_ships(str(_gameparams_path(Path(args.game), args.server, args.gp)), args.ship)
                 if s.key == args.ship or s.model_folder == args.ship), None)
    if ship is None:
        raise SystemExit("ship not found: %s" % args.ship)
    _export_ship(ship, args, Path(args.out))


def cmd_batch(args):
    ships = gp_ships(str(_gameparams_path(Path(args.game), args.server, args.gp)), args.filter)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if args.limit:
        ships = ships[:args.limit]

    extra = []
    if args.no_textures:
        extra.append("--no-textures")
    if args.zstd:
        extra.append("--zstd")
    if args.armor:
        extra.append("--armor")
    if getattr(args, "merge_shapes", False):
        extra.append("--merge-shapes")
    if getattr(args, "damage", False):
        extra.append("--damage")
    if getattr(args, "decals", False):
        extra.append("--decals")
    if getattr(args, "texture_size", 1024) != 1024:
        extra += ["--texture-size", str(args.texture_size)]
    if getattr(args, "lod", 2) != 2:
        extra += ["--lod", str(args.lod)]
    if getattr(args, "skins", False):
        extra.append("--skins")
    if getattr(args, "permoflage", False):
        extra.append("--permoflage")
    if getattr(args, "model_skins", False):
        extra.append("--model-skins")
    if args.gp:
        extra += ["--gp", str(args.gp)]

    def run_one(ship):
        target = out / "geometry" / (ship.key + ".glb")
        if target.is_file() and not args.force:
            return ("skip", ship.key)
        cmd = [sys.executable, str(Path(__file__).resolve()), "ship",
               "--game", str(args.game), "--server", args.server,
               "--ship", ship.key, "--out", str(out)] + extra
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            return ("fail", ship.key + ": " + (proc.stderr or proc.stdout)[-300:])
        return ("ok", ship.key)

    if args.jobs and args.jobs > 1:
        import tempfile
        keys_file = out / ".keys.txt"
        subprocess.run([sys.executable, str(Path(__file__).resolve()), "export-keys",
                        "--game", str(args.game), "--server", args.server,
                        "--filter", args.filter, "--out", str(keys_file)]
                       + (["--gp", args.gp] if args.gp else []), check=True)
        keys = keys_file.read_text(encoding="utf-8").splitlines()
        chunks = [keys[i::args.jobs] for i in range(args.jobs)]
        child_extra = [a for a in extra if a != "--armor"]
        procs = []
        for i, chunk in enumerate(chunks):
            if not chunk:
                continue
            cf = out / (".chunk%d.txt" % i)
            cf.write_text("\n".join(chunk), encoding="utf-8")
            cmd = [sys.executable, str(Path(__file__).resolve()), "batch-worker",
                   "--game", str(args.game), "--server", args.server,
                   "--keys-file", str(cf), "--out", str(out)] + child_extra
            if args.armor:
                cmd.append("--armor")
            if args.gp:
                cmd += ["--gp", args.gp]
            procs.append(subprocess.Popen(cmd))
        for p in procs:
            p.wait()
        keys_file.unlink(missing_ok=True)
        for i in range(len(chunks)):
            (out / (".chunk%d.txt" % i)).unlink(missing_ok=True)
        ok = sum(1 for k in keys if (out / "geometry" / (k + ".glb")).is_file())
        print("[done]", ok, "exported to", out)
        return

    results = [run_one(s) for s in ships]
    for status, info in results:
        print("[%s]" % status, info)
    ok = sum(1 for s, _ in results if s == "ok")
    print("[done]", ok, "exported to", out)


def cmd_import(args):
    app = _bootstrap(Path(args.game), args.server)
    import shutil
    from utils.path_utils import get_data_dir
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    gp_src = _gameparams_path(Path(args.game), args.server, args.gp)
    shutil.copy2(gp_src, data_dir / gp_src.name)
    bins = sorted((Path(args.game) / "bin").iterdir()) if (Path(args.game) / "bin").is_dir() else []
    bin_folder = args.bin or (bins[-1].name if bins else "")
    app.config.bin_folder = bin_folder
    app.config.game_version = args.version or bin_folder
    print("importing", gp_src.name, "bin", bin_folder)

    import threading
    from app.signals import bus
    from services.processor_service import run_process
    done = threading.Event()
    result = {}
    bus.data_processed.connect(lambda ok: (result.setdefault("ok", ok), done.set()))
    run_process()
    if not done.wait(timeout=1800):
        raise SystemExit("import timed out")
    print("import result:", result)
    if not result.get("ok"):
        raise SystemExit(1)


def cmd_export_keys(args):
    keys = [s.key for s in gp_ships(str(_gameparams_path(Path(args.game), args.server, args.gp)), args.filter)]
    Path(args.out).write_text("\n".join(keys), encoding="utf-8")
    print(len(keys), "keys ->", args.out)


def cmd_batch_worker(args):
    keys = set(Path(args.keys_file).read_text(encoding="utf-8").splitlines())
    _bootstrap(Path(args.game), args.server)
    ships = [s for s in gp_ships(str(_gameparams_path(Path(args.game), args.server, args.gp)), "")
             if s.key in keys]
    out = Path(args.out)
    done = 0
    for ship in ships:
        try:
            _export_ship(ship, args, out)
            done += 1
        except Exception as exc:
            print("[fail]", ship.key, exc)
    print("[worker done]", done)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="wows-export")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list")
    p.add_argument("--game", required=True)
    p.add_argument("--server", choices=("lesta", "wg"), default="lesta")
    p.add_argument("--filter", default="")
    p.add_argument("--gp", default=None, help="explicit GameParams file")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("ship")
    p.add_argument("--game", required=True)
    p.add_argument("--server", choices=("lesta", "wg"), default="lesta")
    p.add_argument("--ship", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--no-textures", action="store_true")
    p.add_argument("--zstd", action="store_true", help="also write .glb.zst")
    p.add_argument("--armor", action="store_true", help="also export armor GLB")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--merge-shapes", action="store_true",
                   help="merge each hull segment into one mesh (slower to load, can drop faces)")
    g.add_argument("--split-shapes", action="store_true",
                   help="split hull by shape_spans (default; flag kept for explicitness)")
    p.add_argument("--damage", action="store_true",
                   help="include damage/crack geometry (default: excluded)")
    p.add_argument("--decals", action="store_true",
                   help="include SHIP_MESHDECAL flat decals (default: excluded)")
    p.add_argument("--texture-size", type=int, default=1024,
                   help="max texture dimension in pixels (default: 1024)")
    p.add_argument("--lod", type=int, default=2,
                   help="LOD level to export (-1=LOD0/auto, 0-3; default: 2)")
    p.add_argument("--skins", action="store_true", help="export mskin/skin material skins")
    p.add_argument("--permoflage", action="store_true",
                   help="also export permoflage (needs --skins)")
    p.add_argument("--model-skins", action="store_true", help="export model-replacement skins")
    p.add_argument("--gp", default=None, help="explicit GameParams file")
    p.set_defaults(func=cmd_ship)

    p = sub.add_parser("import")
    p.add_argument("--game", required=True)
    p.add_argument("--server", choices=("lesta", "wg"), default="lesta")
    p.add_argument("--gp", default=None)
    p.add_argument("--bin", default=None)
    p.add_argument("--version", default=None)
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("batch")
    p.add_argument("--game", required=True)
    p.add_argument("--server", choices=("lesta", "wg"), default="lesta")
    p.add_argument("--filter", default="")
    p.add_argument("--out", required=True)
    p.add_argument("--no-textures", action="store_true")
    p.add_argument("--zstd", action="store_true")
    p.add_argument("--armor", action="store_true")
    p.add_argument("--skins", action="store_true")
    p.add_argument("--permoflage", action="store_true",
                   help="also export permoflage (needs --skins)")
    p.add_argument("--model-skins", action="store_true")
    gb = p.add_mutually_exclusive_group()
    gb.add_argument("--merge-shapes", action="store_true")
    gb.add_argument("--split-shapes", action="store_true",
                    help="split hull by shape_spans (default; flag kept for explicitness)")
    p.add_argument("--damage", action="store_true",
                   help="include damage/crack geometry (default: excluded)")
    p.add_argument("--decals", action="store_true",
                   help="include SHIP_MESHDECAL flat decals (default: excluded)")
    p.add_argument("--texture-size", type=int, default=1024,
                   help="max texture dimension in pixels (default: 1024)")
    p.add_argument("--lod", type=int, default=2,
                   help="LOD level to export (-1=LOD0/auto, 0-3; default: 2)")
    p.add_argument("--force", action="store_true")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--jobs", type=int, default=1)
    p.add_argument("--gp", default=None, help="explicit GameParams file")
    p.set_defaults(func=cmd_batch)

    p = sub.add_parser("export-keys")
    p.add_argument("--game", required=True)
    p.add_argument("--server", choices=("lesta", "wg"), default="lesta")
    p.add_argument("--filter", default="")
    p.add_argument("--out", required=True)
    p.add_argument("--gp", default=None)
    p.set_defaults(func=cmd_export_keys)

    p = sub.add_parser("batch-worker")
    p.add_argument("--game", required=True)
    p.add_argument("--server", choices=("lesta", "wg"), default="lesta")
    p.add_argument("--keys-file", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--no-textures", action="store_true")
    p.add_argument("--zstd", action="store_true")
    p.add_argument("--armor", action="store_true")
    p.add_argument("--damage", action="store_true",
                   help="include damage/crack geometry (default: excluded)")
    p.add_argument("--decals", action="store_true",
                   help="include SHIP_MESHDECAL flat decals (default: excluded)")
    p.add_argument("--texture-size", type=int, default=1024,
                   help="max texture dimension in pixels (default: 1024)")
    p.add_argument("--lod", type=int, default=2,
                   help="LOD level to export (-1=LOD0/auto, 0-3; default: 2)")
    p.add_argument("--skins", action="store_true")
    p.add_argument("--permoflage", action="store_true")
    p.add_argument("--model-skins", action="store_true")
    p.add_argument("--merge-shapes", action="store_true")
    p.add_argument("--gp", default=None)
    p.set_defaults(func=cmd_batch_worker)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
