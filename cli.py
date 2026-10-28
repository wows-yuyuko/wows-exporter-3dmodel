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
    build = _latest_bin(game)
    if not build:
        raise SystemExit("no game build under %s/bin" % game)
    ex = GameExtractor(str(game), bin_folder=build)
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


def _latest_bin(game: Path) -> str:
    """`<game>/bin` 下**数字最大**的目录名（客户端版本号）。

    不能用字符串排序：`8867689` 与 `999999` 这类会按字典序排错，且旧版本里
    可能存在非数字目录（如 `tmp`）。没有数字目录时返回空串。
    """
    bin_root = game / "bin"
    if not bin_root.is_dir():
        return ""
    nums = [p.name for p in bin_root.iterdir() if p.is_dir() and p.name.isdigit()]
    return max(nums, key=int) if nums else ""


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
        build = _latest_bin(game)
    app.config.bin_folder = build
    return app


#: `--model-skin` 的默认值：智能套用（仅当首个 permoflage 就是模型皮肤）
AUTO_MODEL_SKIN = "auto"


def _resolve_model_skin(ship, skin_arg: str, build: str):
    """解析 `--model-skin`：返回 CamoInfo 或 None。

    - ""            → 不套（--no-model-skin）
    - "auto"        → **仅当 `permoflages[0]` 是模型皮肤**时套用那一套。
      科技树船（大和/北风）首个 permoflage 是 Permoflage（换贴图涂装），
      那些 model skin 只是可选外观，不能当默认；联动/PR 船（ARP 大和、
      星之北风）首个 permoflage 就是 Skin，那才是这艘船本来的样子。
    - 具体 ID       → 按 ext_index / raw_name / display_name 精确匹配。
    """
    from services.camo_service import CamoService
    from services.geometry_service import GeometryService
    gs = GeometryService.instance()
    svc = CamoService(extractor=gs.get_extractor(), cache=gs._get_assets_cache(),
                      bin_folder=build)
    svc._ensure_loaded()
    candidates = [c for c in svc.list_camos({ship.key}, set(ship.permoflages or []))
                  if c.origin == "model" and c.skin]
    if skin_arg.lower() == AUTO_MODEL_SKIN:
        if not candidates:
            return None
        first_idx = str((ship.permoflages or [""])[0]).split("_")[0]
        return next((c for c in candidates if str(c.ext_index or "") == first_idx), None)
    if not candidates:
        raise SystemExit("%s 没有模型皮肤（model skin），--model-skin 不适用" % ship.key)
    for c in candidates:
        ids = {str(c.ext_index or ""), str(getattr(c, "raw_name", "") or ""),
               str(getattr(c, "display_name", "") or "")}
        if skin_arg in ids:
            return c
    raise SystemExit("找不到模型皮肤 %s；可用：%s" % (
        skin_arg, ", ".join("%s(%s)" % (c.ext_index, c.display_name or "") for c in candidates)))


def _export_ship(ship, args, out: Path):
    from app.application import app
    from services.geometry_service import GeometryService, ShipInfo
    from services.export_service import export_render_glb, GlbRenderOptions

    info = ShipInfo(game_key=ship.key, display_name=ship.key,
                    model_folder=ship.model_folder, model_path=ship.model,
                    nation=ship.nation, ship_type=ship.ship_type, tier=ship.tier)
    # ── --model-skin：把某套模型皮肤当作**基准外观**导出 ──
    # 默认 auto：只有「首个 permoflage 就是模型皮肤」时才套用（见 _resolve_model_skin）。
    skin_arg = str(getattr(args, "model_skin", AUTO_MODEL_SKIN) or "").strip()
    skin_sel = _resolve_model_skin(ship, skin_arg, app.config.bin_folder) if skin_arg else None
    if skin_sel is not None:
        print("[model-skin] 基准外观 = %s %s" % (skin_sel.ext_index, skin_sel.display_name or ""))
    snap = {}
    geom = GeometryService.instance().load_ship(
        info, progress_cb=lambda p, m: None,
        partial_cb=lambda g: snap.setdefault("g", g),
        skin=skin_sel.skin if skin_sel else None,
        model_replace=(skin_sel.model_replace or None) if skin_sel else None,
        lod=getattr(args, "lod", 2))
    # 布局：geometry/<ship>/、manifests/<ship>/、armor/<ship>/ 各一层船名目录，
    # textures/ 保持扁平（按内容哈希命名，跨船共享）
    geom_dir = out / "geometry" / ship.key
    armor_dir = out / "armor" / ship.key
    manifest_dir = out / "manifests" / ship.key
    for d in (geom_dir, armor_dir, manifest_dir):
        d.mkdir(parents=True, exist_ok=True)
    textures_dir = out / "textures"
    glb = geom_dir / (ship.key + ".glb")
    options = GlbRenderOptions(
        embed_textures=not args.no_textures,
        textures_dir=str(textures_dir) if not args.no_textures else "",
        texture_uri_prefix="../../textures",
        texture_format="webp",
        texture_max_size=getattr(args, "texture_size", 1024),
        split_shapes=(getattr(args, "split_shapes", False)
                      or not getattr(args, "merge_shapes", False)),
        include_damage=getattr(args, "damage", False),
        include_decals=getattr(args, "decals", False),
        bake_indexed=getattr(args, "bake_indexed", False),
        bake_size=getattr(args, "bake_size", 0),
        emissive=getattr(args, "emissive", False),
        slim_attributes=getattr(args, "slim", True),
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
                armor_ref = "armor/%s/%s.json" % (ship.key, ship.key)
                print("wrote", armor_dir / (ship.key + ".json"))
            print("wrote", armor_glb)
        else:
            print("[warn] no armor triangles")
    from services.skin_export import write_default_skin, export_mat_skins, export_model_skins
    # 皮肤不是默认产物：只有显式要皮肤时才写 skins/ 与 manifest.skins
    want_skins = bool(getattr(args, "skins", False) or getattr(args, "model_skins", False))
    skins: list[str] = []
    if want_skins:
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
    # 同一输出根只应放一个服务器/版本的产物：船 key 相同会互相覆盖（Lesta 与 WG
    # 的 PJSB018_Yamato_1944 就是同名不同内容），这里显式提醒
    man_path = manifest_dir / (ship.key + ".json")
    if man_path.is_file():
        try:
            import json as _json
            old_build = str(_json.loads(man_path.read_text(encoding="utf-8")).get("build") or "")
        except Exception:
            old_build = ""
        if old_build and old_build != str(app.config.bin_folder):
            print("[warn] %s 已存在于该输出目录（build %s → %s），将被覆盖；"
                  "不同服务器请用不同的 --out 根目录"
                  % (ship.key, old_build, app.config.bin_folder))
    write_web_manifest(glb, man_path, ship.key,
                       app.config.bin_folder, armor_ref=armor_ref, skins=skins,
                       base_skin=(skin_sel.ext_index if skin_sel else None))
    print("wrote", manifest_dir / (ship.key + ".json"))
    return glb


def cmd_ship(args):
    _bootstrap(Path(args.game), args.server)
    ship = next((s for s in gp_ships(str(_gameparams_path(Path(args.game), args.server, args.gp)), args.ship)
                 if s.key == args.ship or s.model_folder == args.ship), None)
    if ship is None:
        raise SystemExit("ship not found: %s" % args.ship)
    _export_ship(ship, args, Path(args.out))


def cmd_diff(args):
    """对比两个 build 的 `.geometry` crc32，列出变动的船 / 挂载模型。

    只读 idx 与 game_data*.db，不加载 assets.bin、不解包几何（秒级）。
    """
    from services.version_diff import run

    text, n = run(args.game, args.server, args.old, args.new, args.format)
    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text + ("" if not text or text.endswith("\n") else "\n"), encoding="utf-8")
        if args.format == "keys":
            print("%d 条船 -> %s（可直接给 batch --keys-file）" % (n, p))
        else:
            print("写出 %s（受影响 %d 条船）" % (p, n))
    else:
        print(text)


def cmd_batch(args):
    ships = gp_ships(str(_gameparams_path(Path(args.game), args.server, args.gp)), args.filter)
    # --keys-file：只导指定船（可直接用 `diff --format keys` 的输出）
    if getattr(args, "keys_file", None):
        raw = Path(args.keys_file).read_text(encoding="utf-8").splitlines()
        wanted = {k.strip() for k in raw if k.strip() and not k.strip().startswith("#")}
        ships = [s for s in ships if s.key in wanted]
        missing = wanted - {s.key for s in ships}
        print("[keys] %s：命中 %d 条" % (Path(args.keys_file).name, len(ships)))
        if missing:
            print("[warn] %d 条不在当前 GameParams 里（跳过）：%s"
                  % (len(missing), ", ".join(sorted(missing)[:5])))
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
    if not getattr(args, "bake_indexed", True):
        extra.append("--no-bake-indexed")
    if getattr(args, "emissive", False):
        extra.append("--emissive")
    if not getattr(args, "slim", True):
        extra.append("--no-slim")
    if getattr(args, "bake_size", 0):
        extra += ["--bake-size", str(args.bake_size)]
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
    _mskin = str(getattr(args, "model_skin", AUTO_MODEL_SKIN) or "")
    if not _mskin:
        extra.append("--no-model-skin")
    elif _mskin != AUTO_MODEL_SKIN:
        extra += ["--model-skin", _mskin]
    if args.gp:
        extra += ["--gp", str(args.gp)]

    # 增量指纹：batch 以前只看 glb 是否存在，改参数后仍跳过（只能 --force 全量）。
    # 现在按「build + 这组参数 + 船模型路径」算签名，签名变了就重导。
    from services.export_index import ExportIndex, export_signature
    from core.build_info import imported_build
    flags = {
        "no_textures": bool(args.no_textures), "zstd": bool(args.zstd),
        "armor": bool(args.armor), "damage": bool(getattr(args, "damage", False)),
        "decals": bool(getattr(args, "decals", False)),
        "bake_indexed": bool(getattr(args, "bake_indexed", True)),
        "emissive": bool(getattr(args, "emissive", False)),
        "bake_size": int(getattr(args, "bake_size", 0) or 0),
        "merge_shapes": bool(getattr(args, "merge_shapes", False)),
        "skins": bool(getattr(args, "skins", False)),
        "permoflage": bool(getattr(args, "permoflage", False)),
        "model_skins": bool(getattr(args, "model_skins", False)),
        "model_skin": str(getattr(args, "model_skin", AUTO_MODEL_SKIN) or ""),
    }
    build = imported_build(args.server) or ""
    index = ExportIndex(out)

    def run_one(ship):
        target = out / "geometry" / ship.key / (ship.key + ".glb")
        # model 不进签名：同一 build 内模型不会变，而并行分支拿不到 ship 对象
        sig = export_signature(args.server, build, ship.key, "",
                               int(getattr(args, "lod", 2)),
                               int(getattr(args, "texture_size", 1024)), flags)
        if not args.force and target.is_file() and index.matches(ship.key, sig):
            return ("skip", ship.key)
        cmd = [sys.executable, str(Path(__file__).resolve()), "ship",
               "--game", str(args.game), "--server", args.server,
               "--ship", ship.key, "--out", str(out)] + extra
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            index.forget(ship.key)
            return ("fail", ship.key + ": " + (proc.stderr or proc.stdout)[-300:])
        index.record(ship.key, sig)
        return ("ok", ship.key)

    # 并行/串行都走 batch-worker：GameParams 与 DB 只初始化一次（以前每船一个
    # `ship` 子进程，每条船都要重新加载 GameParams/数据库）。--jobs 0 保留旧行为。
    if args.jobs and args.jobs >= 1:
        import tempfile
        keys_file = out / ".keys.txt"
        if getattr(args, "keys_file", None):
            # 已按 --keys-file 过滤过，别再让 export-keys 覆盖成全量
            keys = [s.key for s in ships]
        else:
            subprocess.run([sys.executable, str(Path(__file__).resolve()), "export-keys",
                            "--game", str(args.game), "--server", args.server,
                            "--filter", args.filter, "--out", str(keys_file)]
                           + (["--limit", str(args.limit)] if args.limit else [])
                           + (["--gp", args.gp] if args.gp else []), check=True)
            keys = keys_file.read_text(encoding="utf-8").splitlines()
        # 并行分支同样吃增量指纹：先剔掉签名一致的，跑完再按产物登记
        sig_by_key = {k: export_signature(args.server, build, k, "", int(getattr(args, "lod", 2)),
                                          int(getattr(args, "texture_size", 1024)), flags)
                      for k in keys}
        if not args.force:
            pending = [k for k in keys
                       if not ((out / "geometry" / k / (k + ".glb")).is_file()
                               and index.matches(k, sig_by_key[k]))]
            skipped = len(keys) - len(pending)
            if skipped:
                print("[skip]", skipped, "unchanged ships")
            keys = pending
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
        ok = sum(1 for k in keys if (out / "geometry" / k / (k + ".glb")).is_file())
        for k in keys:
            if (out / "geometry" / k / (k + ".glb")).is_file():
                index.record(k, sig_by_key[k])
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
    bin_folder = args.bin or _latest_bin(Path(args.game))
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
    if getattr(args, "limit", 0):
        keys = keys[:args.limit]
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
    p.add_argument("--server", choices=("lesta", "wg"), required=True,
                   help="which client the --game path points at (no default)")
    p.add_argument("--filter", default="")
    p.add_argument("--gp", default=None, help="explicit GameParams file")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("diff", help="对比两个 build 的 geometry crc32，列出变动的船")
    p.add_argument("--game", required=True)
    p.add_argument("--server", choices=("lesta", "wg"), required=True,
                   help="which client the --game path points at (no default)")
    p.add_argument("--old", default=None, help="旧 build 目录名（默认 bin/ 下第二新的）")
    p.add_argument("--new", default=None, help="新 build 目录名（默认 bin/ 下最新的）")
    p.add_argument("--format", choices=("text", "keys", "json"), default="text",
                   help="text=报告 / keys=一行一个船 key（喂 batch --keys-file）/ json")
    p.add_argument("--out", default=None, help="写到文件（不给则打印到 stdout）")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("ship")
    p.add_argument("--game", required=True)
    p.add_argument("--server", choices=("lesta", "wg"), required=True,
                   help="which client the --game path points at (no default)")
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
    p.add_argument("--bake-indexed", dest="bake_indexed", action="store_true",
                   default=True,
                   help="bake INDEXED materials (materialIdMap + atlas arrays + artMap) "
                        "into standard baseColor/normal/MR textures (default)")
    p.add_argument("--no-bake-indexed", dest="bake_indexed", action="store_false",
                   help="keep the raw INDEXED arrays instead of baking "
                        "(needed for live camo switching; bigger, shader-only)")
    p.add_argument("--bake-size", type=int, default=0, metavar="PX",
                   help="max edge of baked textures (default: 0 = materialIdMap native size)")
    p.add_argument("--slim", dest="slim", action="store_true", default=True,
                   help="quantize vertex attributes (KHR_mesh_quantization) and drop "
                        "unused _T4/TEXCOORD_1/TANGENT (default)")
    p.add_argument("--no-slim", dest="slim", action="store_false",
                   help="keep float32 attributes and every extra attribute (bigger files)")
    p.add_argument("--emissive", dest="emissive", action="store_true", default=False,
                   help="export emissive = baseColor x _mg.B (default off: B is a "
                        "continuous channel on live assets, not a clean mask)")
    p.add_argument("--no-emissive", dest="emissive", action="store_false",
                   help="skip emissive (default)")
    p.add_argument("--texture-size", type=int, default=1024,
                   help="max texture dimension in pixels (default: 1024)")
    p.add_argument("--lod", type=int, default=2,
                   help="LOD level to export (-1=LOD0/auto, 0-3; default: 2)")
    p.add_argument("--skins", action="store_true", help="export mskin/skin material skins")
    p.add_argument("--permoflage", action="store_true",
                   help="also export permoflage (needs --skins)")
    p.add_argument("--model-skins", action="store_true", help="export model-replacement skins")
    p.add_argument("--model-skin", default=AUTO_MODEL_SKIN, metavar="ID|auto|none",
                   help="base appearance from a model skin (default: auto = use it only "
                        "when the ship's first permoflage is a model skin, e.g. ARP "
                        "Yamato / Kitakaze PR); output stays <ship>.glb")
    p.add_argument("--no-model-skin", dest="model_skin", action="store_const", const="",
                   help="never apply a model skin (export the plain ship.model)")
    p.add_argument("--gp", default=None, help="explicit GameParams file")
    p.set_defaults(func=cmd_ship)

    p = sub.add_parser("import")
    p.add_argument("--game", required=True)
    p.add_argument("--server", choices=("lesta", "wg"), required=True,
                   help="which client the --game path points at (no default)")
    p.add_argument("--gp", default=None)
    p.add_argument("--bin", default=None)
    p.add_argument("--version", default=None)
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("batch")
    p.add_argument("--game", required=True)
    p.add_argument("--server", choices=("lesta", "wg"), required=True,
                   help="which client the --game path points at (no default)")
    p.add_argument("--filter", default="")
    p.add_argument("--keys-file", default=None,
                   help="只导这些船（一行一个 key，可直接用 "
                        "`diff --format keys --out FILE` 的输出）")
    p.add_argument("--out", required=True)
    p.add_argument("--no-textures", action="store_true")
    p.add_argument("--zstd", action="store_true")
    p.add_argument("--armor", action="store_true")
    p.add_argument("--skins", action="store_true")
    p.add_argument("--permoflage", action="store_true",
                   help="also export permoflage (needs --skins)")
    p.add_argument("--model-skins", action="store_true")
    p.add_argument("--model-skin", default=AUTO_MODEL_SKIN, metavar="ID|auto|none")
    p.add_argument("--no-model-skin", dest="model_skin", action="store_const", const="")
    gb = p.add_mutually_exclusive_group()
    gb.add_argument("--merge-shapes", action="store_true")
    gb.add_argument("--split-shapes", action="store_true",
                    help="split hull by shape_spans (default; flag kept for explicitness)")
    p.add_argument("--damage", action="store_true",
                   help="include damage/crack geometry (default: excluded)")
    p.add_argument("--decals", action="store_true",
                   help="include SHIP_MESHDECAL flat decals (default: excluded)")
    p.add_argument("--bake-indexed", dest="bake_indexed", action="store_true",
                   default=True,
                   help="bake INDEXED materials (materialIdMap + atlas arrays + artMap) "
                        "into standard baseColor/normal/MR textures (default)")
    p.add_argument("--no-bake-indexed", dest="bake_indexed", action="store_false",
                   help="keep the raw INDEXED arrays instead of baking "
                        "(needed for live camo switching; bigger, shader-only)")
    p.add_argument("--bake-size", type=int, default=0, metavar="PX",
                   help="max edge of baked textures (default: 0 = materialIdMap native size)")
    p.add_argument("--slim", dest="slim", action="store_true", default=True,
                   help="quantize vertex attributes (KHR_mesh_quantization) and drop "
                        "unused _T4/TEXCOORD_1/TANGENT (default)")
    p.add_argument("--no-slim", dest="slim", action="store_false",
                   help="keep float32 attributes and every extra attribute (bigger files)")
    p.add_argument("--emissive", dest="emissive", action="store_true", default=False,
                   help="export emissive = baseColor x _mg.B (default off: B is a "
                        "continuous channel on live assets, not a clean mask)")
    p.add_argument("--no-emissive", dest="emissive", action="store_false",
                   help="skip emissive (default)")
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
    p.add_argument("--server", choices=("lesta", "wg"), required=True,
                   help="which client the --game path points at (no default)")
    p.add_argument("--filter", default="")
    p.add_argument("--out", required=True)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--gp", default=None)
    p.set_defaults(func=cmd_export_keys)

    p = sub.add_parser("batch-worker")
    p.add_argument("--game", required=True)
    p.add_argument("--server", choices=("lesta", "wg"), required=True,
                   help="which client the --game path points at (no default)")
    p.add_argument("--keys-file", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--no-textures", action="store_true")
    p.add_argument("--zstd", action="store_true")
    p.add_argument("--armor", action="store_true")
    p.add_argument("--damage", action="store_true",
                   help="include damage/crack geometry (default: excluded)")
    p.add_argument("--decals", action="store_true",
                   help="include SHIP_MESHDECAL flat decals (default: excluded)")
    p.add_argument("--bake-indexed", dest="bake_indexed", action="store_true",
                   default=True,
                   help="bake INDEXED materials (materialIdMap + atlas arrays + artMap) "
                        "into standard baseColor/normal/MR textures (default)")
    p.add_argument("--no-bake-indexed", dest="bake_indexed", action="store_false",
                   help="keep the raw INDEXED arrays instead of baking "
                        "(needed for live camo switching; bigger, shader-only)")
    p.add_argument("--bake-size", type=int, default=0, metavar="PX",
                   help="max edge of baked textures (default: 0 = materialIdMap native size)")
    p.add_argument("--slim", dest="slim", action="store_true", default=True,
                   help="quantize vertex attributes (KHR_mesh_quantization) and drop "
                        "unused _T4/TEXCOORD_1/TANGENT (default)")
    p.add_argument("--no-slim", dest="slim", action="store_false",
                   help="keep float32 attributes and every extra attribute (bigger files)")
    p.add_argument("--emissive", dest="emissive", action="store_true", default=False,
                   help="export emissive = baseColor x _mg.B (default off: B is a "
                        "continuous channel on live assets, not a clean mask)")
    p.add_argument("--no-emissive", dest="emissive", action="store_false",
                   help="skip emissive (default)")
    p.add_argument("--texture-size", type=int, default=1024,
                   help="max texture dimension in pixels (default: 1024)")
    p.add_argument("--lod", type=int, default=2,
                   help="LOD level to export (-1=LOD0/auto, 0-3; default: 2)")
    p.add_argument("--skins", action="store_true")
    p.add_argument("--permoflage", action="store_true")
    p.add_argument("--model-skins", action="store_true")
    p.add_argument("--model-skin", default=AUTO_MODEL_SKIN, metavar="ID|auto|none")
    p.add_argument("--no-model-skin", dest="model_skin", action="store_const", const="")
    p.add_argument("--merge-shapes", action="store_true")
    p.add_argument("--gp", default=None)
    p.set_defaults(func=cmd_batch_worker)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
