"""数据解析服务 —— 解密 GameParams、建库、同步 3D 缓存。

两条链路：
  game_data.db  —— 战舰主数据（ship / ship_data / model / ship_model），**增量**写入；
  assets_data.db —— 骨架/渲染集/材质缓存，按模型目录**增量**重建。

增量判据见 `services/model_index.py`（geometry crc32 签名）与
`services/database_service.py`（快照 sha1）。首次运行是全量，之后只重写变化的部分。
"""

from __future__ import annotations

import hashlib
import json
import pickle
import shutil
import sys
import threading
import zlib

from app.signals import bus
from app.application import app as app_ctx
from utils.threading_utils import run_async
from utils.path_utils import get_data_dir, get_split_dir, model_id_of
from services.database_service import DatabaseManager, ShipRow, reset_db

from services import GameParams as _GameParamsModule
from services import wg_compat
sys.modules['GameParams'] = _GameParamsModule


class _GPEncode(json.JSONEncoder):
    def default(self, o):
        try:
            for e in ['Cameras', 'DockCamera', 'damageDistribution', 'salvoParams']:
                if hasattr(o, '__dict__'):
                    o.__dict__.pop(e, None)
            return o.__dict__
        except AttributeError:
            return {}


#: 实体类型 → 类别（WG 若在 wg_compat 已填充则用 WG 版）
TYPE_CATEGORY_MAP = {
    "Ship": "Ship", "Gun": "Gun", "Projectile": "Projectile",
    "Aircraft": "Aircraft", "Ability": "Ability",
    "Modernization": "Modernization", "Crew": "Crew",
    "Other": "Other", "Exterior": "Exterior",
}


def _entity_type(v) -> str:
    """取实体的 typeinfo.type —— 兼容 dict 与 GameParams 的自定义对象。"""
    ti = v.get('typeinfo') if isinstance(v, dict) else getattr(v, 'typeinfo', None)
    if ti is None:
        return ""
    if isinstance(ti, dict):
        return str(ti.get('type') or '')
    return str(getattr(ti, 'type', '') or '')


def _iter_mounts(entity: dict):
    """遍历舰船实体的 A_* 组，产出 (role, model_path)。

    role 形如 ``hull`` / ``A_Artillery.HP_JGM_1``（挂载共享，靠 role 区分位置）。

    ⚠️ `A_Hull` 的 `model` 值是**字符串**，其它组是 `{hp_key: {"model": ...}}`，
       所以不能只认 dict —— 否则全船 model_folder 为空（2026-10-28 踩过）。
    """
    for grp, hp in entity.items():
        if not (isinstance(grp, str) and grp.startswith("A_") and isinstance(hp, dict)):
            continue
        for key, node in hp.items():
            if isinstance(node, dict):
                model = node.get("model") or ""
            elif isinstance(node, str) and key == "model":
                model = node
            else:
                continue
            if not model:
                continue
            role = "hull" if (grp == "A_Hull" and key == "model") else "%s.%s" % (grp, key)
            yield role, str(model)


def _build_ship_rows(ships: dict[str, dict]) -> tuple[list[ShipRow], dict[str, list[tuple[str, str]]],
                                                     dict[str, dict]]:
    """实体 dict → (ShipRow 列表, 船→模型关联, 模型附加信息)。

    模型附加信息（model_path / kind）来自 GameParams：idx 里只有 geometry，
    模型目录与 `.model` 路径的对应关系只有实体里才有。
    """
    rows: list[ShipRow] = []
    links: dict[str, list[tuple[str, str]]] = {}
    model_info: dict[str, dict] = {}

    for key, v in ships.items():
        ti = v.get("typeinfo") if isinstance(v.get("typeinfo"), dict) else {}
        hull_model = ""
        pairs: list[tuple[str, str]] = []
        for role, path in _iter_mounts(v):
            folder = model_id_of(path)
            if not folder:
                continue
            info = model_info.setdefault(folder, {"model_path": path, "kind": "mount"})
            if role == "hull":
                hull_model = folder
                info["kind"] = "hull"
                info["model_path"] = path
            pairs.append((folder, role))

        data_json = json.dumps(v, sort_keys=True, ensure_ascii=False)
        rows.append(ShipRow(
            id=int(v.get("id") or 0),
            idx=str(v.get("index") or key.split("_", 1)[0]),
            name=str(v.get("name") or key),
            nation=str(ti.get("nation") or ""),
            tier=int(v.get("level") or 0),
            ship_class=str(ti.get("species") or ""),
            ship_group=str(v.get("group") or ""),
            model_folder=hull_model,
            data_json=data_json,
            sha1=hashlib.sha1(data_json.encode("utf-8")).hexdigest(),
        ))
        links[str(v.get("name") or key)] = pairs
    return rows, links, model_info


def run_process(rebuild: bool = False) -> "_AppTask":
    data_dir = get_data_dir()
    split_dir = get_split_dir()

    db: DatabaseManager | None = None
    assets_done = threading.Event()
    assets_ok = [False]
    assets_started = [False]

    _cat_map = wg_compat.get_type_category_map(app_ctx.ctx.wows_type) or TYPE_CATEGORY_MAP

    def _start_assets_job(models: set[str] | None, removed: set[str] | None,
                          force_full: bool = False) -> None:
        """后台启动 3D 缓存同步（与主线程写 game_data.db 并行）。"""
        assets_started[0] = True
        try:
            # ★ 启动前固定客户端信息：导入过程中用户切服会拿新服务器的类型表
            #   去解旧客户端的 assets.bin（实测 WG 12 万 visual 全部解析失败）
            _job_bin = app_ctx.ctx.bin_folder
            _job_ver = app_ctx.ctx.game_version
            _job_type = app_ctx.ctx.wows_type
            _job_game = app_ctx.ctx.game_path

            def _assets_cache_job():
                try:
                    assets_ok[0] = _populate_assets_cache(
                        bin_folder=_job_bin, game_version=_job_ver,
                        wows_type=_job_type, game_path=_job_game,
                        models=models, removed=removed, force_full=force_full)
                except Exception as e:  # noqa: BLE001
                    bus.log_message.emit(f"❌ assets_data.db 写入失败: {e}")
                    assets_ok[0] = False
                finally:
                    _cleanup_assets_temp()
                    assets_done.set()

            run_async(_assets_cache_job)
        except Exception:  # noqa: BLE001
            assets_done.set()

    def _process():
        nonlocal db
        # split 已不再产出；清掉旧版本残留，避免后续步骤误读
        if split_dir.exists():
            shutil.rmtree(str(split_dir))

        for n in ["GameParams_py3.data", "GameParams_py2.data", "GameParams.data"]:
            p = data_dir / n
            if p.exists():
                found = str(p)
                break
        else:
            return False, f"未找到数据文件: {data_dir}"

        with open(found, 'rb') as f:
            gpd = f.read()
        gpd = gpd[::-1]
        gpd = zlib.decompress(gpd)
        data = pickle.loads(gpd, encoding='latin1')

        source_dict = None
        if isinstance(data, (list, tuple)):
            for elem in data:
                if isinstance(elem, dict) and '' in elem and isinstance(elem[''], dict):
                    source_dict = elem['']
                    break
        elif isinstance(data, dict) and '' in data and isinstance(data[''], dict):
            source_dict = data['']

        # 实体 → 舰船（只保留 Ship：导出链路只读舰船）
        ships: dict[str, dict] = {}

        def _take(ej: dict) -> None:
            for k, v in ej.items():
                # ★ 先按 typeinfo.type 过滤再序列化：2.3 万实体全做一次
                #   json 往返要几十秒，而只有舰船会被保留。
                if _cat_map.get(_entity_type(v)) != "Ship":
                    continue
                ships[k] = json.loads(json.dumps(v, cls=_GPEncode, ensure_ascii=False))

        if source_dict:
            _take(source_dict)
            msg = "Wargaming 拆分完成"
        else:
            for idx, elem in enumerate(data):
                if isinstance(elem, dict):
                    _take(elem)
            msg = "Lesta 拆分完成"

        if not ships:
            return False, "GameParams 里没有解析出舰船实体"

        ship_rows, links, model_info = _build_ship_rows(ships)

        # 模型清单（idx 视角）：只读 idx，约 5 s，不解包任何几何
        from services.model_index import scan as scan_models
        game_path = app_ctx.ctx.game_path
        bin_folder = app_ctx.ctx.bin_folder or ""
        bus.log_message.emit(f"🔍 扫描 idx 建立模型清单（build {bin_folder}）...")
        models = scan_models(game_path, bin_folder) if game_path else {}
        for mid, info in model_info.items():
            entry = models.get(mid)
            if entry is not None:
                entry.model_path = info["model_path"]
                entry.kind = info["kind"]
        bus.log_message.emit(f"📦 模型清单: {len(models)} 个目录 / "
                             f"{sum(e.file_count for e in models.values())} 个 geometry")

        db_path = str(get_data_dir() / DatabaseManager._db_name(app_ctx.ctx.wows_type))
        db = DatabaseManager(db_path=db_path, wows_type=app_ctx.ctx.wows_type)
        db.initialize()
        bus.task_progress.emit(30, "步骤 2/3: 写入战舰主数据")

        stats = db.sync(
            ship_rows, models, links,
            build=bin_folder,
            game_version=app_ctx.ctx.game_version or "",
            wows_type=app_ctx.ctx.wows_type or "")
        bus.log_message.emit("📦 %s（%s）" % (stats.summary(), stats.mode))
        bus.task_progress.emit(60, "步骤 3/3: 同步 3D 缓存")

        # 是否需要重建 3D 缓存：本次有模型变动，或上一轮没成功同步到本 build
        assets_ready = db.get_meta("assets_build") == bin_folder
        if rebuild or stats.rebuild_models or not assets_ready:
            _start_assets_job(stats.rebuild_models or None,
                              stats.removed_models or None,
                              force_full=rebuild)
            # 在**工作线程**里等：run_async 的回调跑在主线程，阻塞它会卡住界面
            if not assets_done.wait(timeout=900):
                bus.log_message.emit("⚠️ 3D 缓存（assets_data.db）等待超时")
            if assets_ok[0]:
                db.set_meta({"assets_build": bin_folder})
        else:
            bus.log_message.emit("✅ 3D 缓存已是最新（本 build 无模型变动），跳过重建")
        return True, msg

    def _ok(ret):
        ok, msg = ret
        if ok:
            try:
                for n in ["GameParams_py3.data", "GameParams_py2.data", "GameParams.data"]:
                    p = data_dir / n
                    if p.exists():
                        p.unlink()
                        bus.log_message.emit(f"🧹 已删除原始数据文件: {n}")
                if assets_started[0]:
                    if assets_ok[0]:
                        bus.log_message.emit("✅ 3D 缓存（assets_data.db）已就绪")
                    else:
                        bus.log_message.emit("❌ 3D 缓存写入失败——下次 import 会自动重试")
                if db is not None:
                    db.checkpoint("TRUNCATE")
                bus.log_message.emit(f"✅ 数据解析完成: {msg}")
                bus.task_progress.emit(100, "全部完成")
                app_ctx.set_game_data_state(True)
                bus.data_processed.emit(True)
                bus.folder_selected.emit("__REFRESH__")
            finally:
                if db is not None:
                    try:
                        db.close()
                    except Exception:  # noqa: BLE001
                        pass
                reset_db()
        else:
            bus.log_message.emit(f"❌ {msg}")
            bus.data_processed.emit(False)
            if assets_started[0]:
                assets_done.set()
            try:
                if db is not None:
                    db.close()
            except Exception:  # noqa: BLE001
                pass
            reset_db()

    def _err(msg: str):
        bus.log_message.emit(f"❌ 解析失败: {msg}")
        bus.data_processed.emit(False)

    return run_async(_process, on_finished=_ok, on_error=_err)


def _populate_assets_cache(bin_folder: str, game_version: str, wows_type: str,
                           game_path: str = "", models: set[str] | None = None,
                           removed: set[str] | None = None,
                           force_full: bool = False) -> bool:
    """把当前客户端 assets.bin 的 3D 数据同步到 assets_data.db。

    models 为 None 表示全量；给了集合则只重建这些模型目录。
    """
    try:
        from services.assets_cache_service import AssetsCacheService
        cache = AssetsCacheService(wows_type=wows_type)
        cache.initialize()
        # 库不是本 build 建的（或刚被 schema 重建）→ 只能全量
        if force_full:
            models, removed = None, None
        elif cache.build != (bin_folder or "") or cache._schema_rebuilt:
            if models is not None:
                bus.log_message.emit("⏳ 3D 缓存库与当前 build 不符，改走全量重建")
            models, removed = None, None

        if game_path:
            from uncode_assets.service import read_assets_bin_bytes
            data = read_assets_bin_bytes(game_path, bin_folder=bin_folder or None)
        else:
            from services.geometry_service import GeometryService
            data = GeometryService.instance().load_assets_bytes()
        if not data:
            bus.log_message.emit("⚠️ assets.bin 不可用，跳过 3D 数据缓存")
            return False

        bus.log_message.emit(
            "⏳ 同步 3D 缓存（%s）..." % ("全量" if models is None else "%d 个模型" % len(models)))
        counts = cache.populate(
            assets_bytes=data, bin_folder=bin_folder or "",
            game_version=game_version or "", wows_type=wows_type or "",
            game_dir=(game_path or None), models=models, removed=removed,
            progress_cb=lambda msg: bus.log_message.emit(f"⏳ 3D 缓存: {msg}"))
        del data
        bus.log_message.emit(
            f"📦 assets.bin 数据已缓存（模式 {counts.get('mode')} / "
            f"骨架挂点 {counts['skeleton']} / 骨骼 {counts['skeleton_bones']} / "
            f"渲染集 {counts['render_sets']} / 材质 {counts['mfm_textures']}）")
        return True
    except Exception as e:  # noqa: BLE001
        bus.log_message.emit(f"❌ assets.bin 缓存写入失败: {e}")
        return False


def _cleanup_assets_temp() -> None:
    """清理**历史版本遗留**的 assets.bin 落盘产物（现流程全程内存流转）。"""
    try:
        dd = get_data_dir()
        targets = [dd / "assets.bin"] + list(dd.glob("assets_*.bin")) + [dd / ".uncode_cache"]
        for p in targets:
            try:
                if p.is_dir():
                    shutil.rmtree(str(p))
                elif p.exists():
                    p.unlink()
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        pass
