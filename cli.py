"""wows-exporter-3dmodel CLI.

默认动作（不带子命令）= 增量同步数据库 + 导出选中的船：

    python cli.py --server lesta                      # 同步 + 导出全部
    python cli.py --server lesta --ship 10            # 只导 10 级
    python cli.py --server lesta --ship 3,4,7 --ship PJSB018

子命令只有两个（`--jobs>1` 的子进程入口是隐藏的 `--worker`，不在命令列表里）：
    import  只建库 / 同步，不导出
    diff    对比两个 build 的 geometry crc32（只读 idx，不用建库）
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core.gameparams import locate as gp_locate, ships as gp_ships  # noqa: E402
from utils.path_utils import latest_build  # noqa: E402


def _gameparams_path(game: Path, server: str, gp: str | None = None) -> Path:
    """`--gp` 优先，否则按服找；都没有就从 pkg 解包（唯一会写游戏目录的地方）。"""
    return Path(gp) if gp else Path(gp_locate(game, server, extract=True))


def _bootstrap(game: Path, server: str):
    from app.application import app
    if server == "lesta":
        app.config.wows_type = "Lesta"
        app.config.game_path_lesta = str(game)
    else:
        app.config.wows_type = "Wargaming"
        app.config.game_path_wargaming = str(game)
    from services.database_service import imported_build
    build = imported_build(server)
    if not build or not (game / "bin" / build).is_dir():
        build = latest_build(game)
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


def cmd_diff(args):
    """对比两个 build 的 `.geometry` crc32，列出变动的船 / 挂载模型。

    只读 idx 与 game_data*.db，不加载 assets.bin、不解包几何（约 15 s）。
    """
    from services.version_diff import run

    args.game = str(_resolve_game(args))
    text, n = run(args.game, args.server, args.old, args.new, args.format)
    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text + ("" if not text or text.endswith("\n") else "\n"), encoding="utf-8")
        print("写出 %s（受影响 %d 条船）" % (p, n))
    else:
        print(text)


def cmd_import(args):
    """只建库 / 同步，不导出。"""
    game = _resolve_game(args)
    _bootstrap(game, args.server)
    _sync_db(game, args)


# ── 默认动作：同步 + 导出 ──────────────────────────────────

#: 等级表达式：10 / 3,4,7 / 5-11
_TIER_SPEC = re.compile(r"^\d+(?:[-,]\d+)*$")


def _parse_tiers(spec: str) -> set[int]:
    out: set[int] = set()
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            a, b = token.split("-", 1)
            lo, hi = int(a), int(b)
            if lo > hi:
                lo, hi = hi, lo
            out.update(range(lo, hi + 1))
        else:
            out.add(int(token))
    return out


def _select_ships(ships: list, specs: list[str] | None) -> list:
    """按 `--ship` 筛选。支持四种写法（可重复给，取并集）：

    - 完整 key：``PJSB018_Yamato_1944``
    - idx：``PJSB018``（key 的第一段）
    - 模型目录：``JSB039_Yamato_1945``
    - 等级：``10`` / ``3,4,7`` / ``5-11``

    精确都没命中的 spec 再退化成子串匹配（兼容旧的 `--filter Yamato`）。
    """
    if not specs:
        return list(ships)

    tiers: set[int] = set()
    names: set[str] = set()
    for raw in specs:
        s = str(raw).strip()
        if not s:
            continue
        if _TIER_SPEC.match(s):
            tiers |= _parse_tiers(s)
        else:
            names.add(s.lower())

    picked: dict[str, object] = {}
    for ship in ships:
        key = (ship.key or "").lower()
        idx = key.split("_", 1)[0]
        folder = (ship.model_folder or "").lower()
        if ship.tier in tiers or key in names or idx in names or folder in names:
            picked[ship.key] = ship

    exact: set[str] = set()
    for ship in picked.values():
        key = (ship.key or "").lower()
        exact.update((key, key.split("_", 1)[0], (ship.model_folder or "").lower()))

    for name in sorted(names - exact):
        hits = [s for s in ships if name in (s.key or "").lower()]
        if not hits:
            print("[warn] --ship %r 没匹配到任何船" % name)
        for ship in hits:
            picked[ship.key] = ship
    return [picked[k] for k in sorted(picked)]


def _resolve_game(args) -> Path:
    """`--game` 优先；没给就取 config.json 里 `--server` 对应的路径。"""
    g = getattr(args, "game", None)
    if g:
        return Path(g)
    from app.config import ConfigManager
    cm = ConfigManager()
    raw = (cm.game_path_lesta if args.server == "lesta" else cm.game_path_wargaming) or ""
    if not raw or raw == "未设置":
        raise SystemExit(
            "没有 --game，config.json 里也没配 game_path_%s。\n"
            "  要么加 --game <游戏根目录>，要么把路径写进项目 config.json。"
            % ("lesta" if args.server == "lesta" else "wargaming"))
    return Path(raw)


def _require_server(args) -> None:
    if not getattr(args, "server", None):
        raise SystemExit("--server 必填：lesta 或 wg")


def _sync_db(game: Path, args) -> None:
    """建库 / 增量同步（`import` 与默认动作共用）。"""
    import shutil
    import threading

    from utils.path_utils import get_data_dir

    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    gp_src = _gameparams_path(game, args.server, getattr(args, "gp", None))
    shutil.copy2(gp_src, data_dir / gp_src.name)

    from app.application import app
    bin_folder = getattr(args, "bin", None) or latest_build(game)
    app.config.bin_folder = bin_folder
    app.config.game_version = getattr(args, "version", None) or bin_folder
    print("[sync] build %s ← %s" % (bin_folder, gp_src.name))

    from app.signals import bus
    from services.processor_service import run_process

    # bus 的进度消息默认没有消费者（GUI 才接），CLI 里直接打到 stdout
    bus.log_message.connect(lambda msg: print("  " + str(msg), flush=True))

    done = threading.Event()
    result: dict = {}
    bus.data_processed.connect(lambda ok: (result.setdefault("ok", ok), done.set()))
    run_process(rebuild=bool(getattr(args, "rebuild", False)))
    if not done.wait(timeout=1800):
        raise SystemExit("同步超时（1800 s）")
    if not result.get("ok"):
        raise SystemExit(1)
    print("[sync] done")


def _export_flags(args) -> list[str]:
    """args → 子进程用的导出开关（与 argparse 定义一一对应）。"""
    extra: list[str] = []
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
    mskin = str(getattr(args, "model_skin", AUTO_MODEL_SKIN) or "")
    if not mskin:
        extra.append("--no-model-skin")
    elif mskin != AUTO_MODEL_SKIN:
        extra += ["--model-skin", mskin]
    if getattr(args, "gp", None):
        extra += ["--gp", str(args.gp)]
    return extra


def _export_ships(ships: list, args, out: Path) -> None:
    """导出给定的一批船（进程内串行；--jobs>1 时切分给 --worker 子进程）。"""
    from services.database_service import imported_build
    from services.export_index import ExportIndex, export_signature

    extra = _export_flags(args)
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

    def sig_of(key: str) -> str:
        # model 不进签名：同一 build 内模型不会变
        return export_signature(args.server, build, key, "",
                                int(getattr(args, "lod", 2)),
                                int(getattr(args, "texture_size", 1024)), flags)

    keys = [s.key for s in ships]
    if not args.force:
        pending = [k for k in keys
                   if not ((out / "geometry" / k / (k + ".glb")).is_file()
                           and index.matches(k, sig_of(k)))]
        skipped = len(keys) - len(pending)
        if skipped:
            print("[skip] %d 艘未变（签名命中），直接跳过" % skipped)
        keys = pending
    if not keys:
        print("[done] 0 艘需要导出（都已是最新）→", out)
        return

    if int(getattr(args, "jobs", 1) or 1) > 1:
        jobs = int(args.jobs)
        chunks = [keys[i::jobs] for i in range(jobs)]
        procs = []
        chunk_files: list[Path] = []
        for i, chunk in enumerate(chunks):
            if not chunk:
                continue
            cf = out / (".chunk%d.txt" % i)
            cf.write_text("\n".join(chunk), encoding="utf-8")
            chunk_files.append(cf)
            cmd = [sys.executable, str(Path(__file__).resolve()), "--worker",
                   "--game", str(args.game), "--server", args.server,
                   "--keys-file", str(cf), "--out", str(out)] + extra
            procs.append(subprocess.Popen(cmd))
        for proc in procs:
            proc.wait()
        for cf in chunk_files:
            cf.unlink(missing_ok=True)
        ok = 0
        for k in keys:
            if (out / "geometry" / k / (k + ".glb")).is_file():
                index.record(k, sig_of(k))
                ok += 1
        print("[done]", ok, "exported to", out)
        return

    by_key = {s.key: s for s in ships}
    ok = 0
    for k in keys:
        try:
            _export_ship(by_key[k], args, out)
            index.record(k, sig_of(k))
            ok += 1
        except Exception as exc:  # noqa: BLE001
            index.forget(k)
            print("[fail]", k, exc)
    print("[done]", ok, "/", len(keys), "exported to", out)


def cmd_run(args):
    """默认动作：增量同步库（必要时建库）→ 导出选中的船（默认全部）。"""
    _require_server(args)
    game = _resolve_game(args)
    args.game = str(game)
    _bootstrap(game, args.server)

    if not getattr(args, "no_sync", False):
        _sync_db(game, args)

    ships = gp_ships(str(_gameparams_path(game, args.server, args.gp)), "")
    picked = _select_ships(ships, getattr(args, "ship", None))
    if not picked:
        raise SystemExit("没有匹配的船：%s" % ", ".join(args.ship or []))
    if getattr(args, "limit", 0):
        picked = picked[:args.limit]

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    print("[plan] %d / %d 艘 → %s" % (len(picked), len(ships), out))
    _export_ships(picked, args, out)


def cmd_worker(args):
    """内部：`--jobs>1` 时父进程切分 key 后调用（`--worker`，不出现在命令列表）。"""
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
        except Exception as exc:  # noqa: BLE001
            print("[fail]", ship.key, exc)
    print("[worker done]", done)


def _add_export_flags(p: argparse.ArgumentParser) -> None:
    """导出开关（默认动作与隐藏的 --worker 共用，保证两边参数一一对应）。"""
    p.add_argument("--no-textures", action="store_true", help="不写贴图（GLB 里也不嵌图）")
    p.add_argument("--zstd", action="store_true", help="额外写 .glb.zst")
    p.add_argument("--armor", action="store_true", help="额外导出装甲 GLB + JSON + bin")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--merge-shapes", action="store_true",
                   help="把舰体各分段合并成单网格（加载慢、可能丢面）")
    g.add_argument("--split-shapes", action="store_true",
                   help="按 shape_spans 拆（默认行为，显式写出来只是为了清楚）")
    p.add_argument("--damage", action="store_true", help="导出破损/断面几何（默认剔除）")
    p.add_argument("--decals", action="store_true", help="导出 SHIP_MESHDECAL 平面贴花（默认剔除）")
    p.add_argument("--bake-indexed", dest="bake_indexed", action="store_true", default=True,
                   help="INDEXED 材质烘焙成标准 PBR（默认开）")
    p.add_argument("--no-bake-indexed", dest="bake_indexed", action="store_false",
                   help="保留原始 INDEXED 数组（涂装实时切换需要，体积大）")
    p.add_argument("--bake-size", type=int, default=0, metavar="PX",
                   help="烘焙贴图最大边长（默认 0 = materialIdMap 原始尺寸）")
    p.add_argument("--slim", dest="slim", action="store_true", default=True,
                   help="顶点量化 + 丢弃未用属性（默认）")
    p.add_argument("--no-slim", dest="slim", action="store_false",
                   help="保留 float32 与全部属性（体积更大）")
    p.add_argument("--emissive", dest="emissive", action="store_true", default=False,
                   help="自发光 = baseColor × _mg.B（默认关：B 在实机是连续通道，不是干净掩码）")
    p.add_argument("--no-emissive", dest="emissive", action="store_false", help="不导自发光（默认）")
    p.add_argument("--texture-size", type=int, default=1024, help="贴图最大边长（默认 1024）")
    p.add_argument("--lod", type=int, default=2, help="LOD 级别（-1=LOD0/auto，0-3；默认 2）")
    p.add_argument("--skins", action="store_true", help="导出材质皮肤")
    p.add_argument("--permoflage", action="store_true", help="连 permoflage 一起导（需 --skins）")
    p.add_argument("--model-skins", action="store_true", help="导出换模型的皮肤")
    p.add_argument("--model-skin", default=AUTO_MODEL_SKIN, metavar="ID|auto|none",
                   help="用某套模型皮肤当基准外观（默认 auto：首个 permoflage 是模型皮肤才用）")
    p.add_argument("--no-model-skin", dest="model_skin", action="store_const", const="",
                   help="绝不套模型皮肤（导原始 ship.model）")


def _add_conn_flags(p: argparse.ArgumentParser) -> None:
    """连接类参数（主命令与子命令共用；都给才不冲突）。"""
    p.add_argument("--server", choices=("lesta", "wg"), help="哪一服（默认路径取 config.json）")
    p.add_argument("--game", default=None, help="游戏根目录（默认取 config.json 里对应服路径）")
    p.add_argument("--gp", default=None, help="显式指定 GameParams 文件")


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="wows-export",
        description="WoWS 模型导出。不带子命令 = 增量同步数据库 + 导出选中的船（默认全部）。",
        epilog="例：python cli.py --server lesta --ship 10 --ship PJSB018 --out out")
    _add_conn_flags(ap)
    ap.add_argument("--out", default="out", help="输出目录（默认 out）")
    ap.add_argument("--ship", action="append", metavar="SPEC",
                    help="筛选船，可重复：完整 key / idx（PJSB018）/ 模型目录 / "
                         "等级（10、3,4,7、5-11）；不给 = 全部")
    ap.add_argument("--jobs", type=int, default=1, help="并行进程数（默认 1 = 进程内串行）")
    ap.add_argument("--limit", type=int, default=0, help="只导前 N 条（调试用）")
    ap.add_argument("--force", action="store_true", help="忽略增量签名，强制重导")
    ap.add_argument("--no-sync", dest="no_sync", action="store_true",
                    help="跳过建库/同步（默认每次都做增量同步）")
    ap.add_argument("--rebuild", action="store_true", help="强制全量重建 3D 缓存")
    ap.add_argument("--bin", default=None, help="指定 build 目录（默认 bin/ 下数字最大的）")
    ap.add_argument("--version", default=None, help="写进 meta 的 game_version（默认同 build）")
    # 内部：--jobs>1 的子进程入口（不出现在 --help）
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--keys-file", default=None, help=argparse.SUPPRESS)
    _add_export_flags(ap)

    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("import", help="只建库 / 同步，不导出")
    _add_conn_flags(p)
    p.add_argument("--bin", default=None)
    p.add_argument("--version", default=None)
    p.add_argument("--rebuild", action="store_true",
                   help="重建 3D 缓存（assets_data.db）：忽略增量判据，全量重解析")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("diff", help="对比两个 build 的 geometry crc32（只读 idx，不用建库）")
    _add_conn_flags(p)
    p.add_argument("--old", default=None, help="旧 build 目录名（默认 bin/ 下第二新的）")
    p.add_argument("--new", default=None, help="新 build 目录名（默认 bin/ 下最新的）")
    p.add_argument("--format", choices=("text", "json"), default="text",
                   help="text=报告 / json=结构化")
    p.add_argument("--out", default=None, help="写到文件（不给则打印到 stdout）")
    p.set_defaults(func=cmd_diff)

    args = ap.parse_args(argv)
    if args.worker:
        if not args.keys_file:
            raise SystemExit("--worker 需要 --keys-file")
        return cmd_worker(args)
    if args.cmd is None:
        return cmd_run(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
