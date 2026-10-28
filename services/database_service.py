"""game_data.db 访问层 —— 单版本镜像 + 增量同步。

库结构见 `resources/database/*/database_new.sql`：主表 `ship`（数字 id）+
从表 `model`（模型目录），经 `ship_model` 关联。

「单版本镜像」是指库里只保留**最后一次同步**的那个 build 的数据，不做多版本共存：
多版本意味着 `version_code` 进主键，build 一变，未变动的上百万行也要 UPDATE 到
新 build，等于全量重写 —— 增量就无从谈起。

增量判据：
  · 船    —— `ship_data.sha1`（规范化快照 JSON 的哈希）变没变；
  · 模型  —— `model.sig`（该目录全部 `.geometry` 的 crc32+size 签名）变没变。
没变的行只刷 `last_checked`，不重写数据块 —— 快照 JSON 占整库 84%，这是主要收益。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from utils.path_utils import get_bundled_dir, get_data_dir

DB_SCHEMA_VERSION = 49


@dataclass
class ShipRow:
    """一艘船的元信息 + 规范化快照（导入时由 GameParams 实体构造）。"""

    id: int
    idx: str
    name: str
    nation: str = ""
    tier: int = 0
    ship_class: str = ""
    ship_group: str = ""
    model_folder: str = ""
    data_json: str = ""
    sha1: str = ""


@dataclass
class SyncStats:
    """一次同步的结果，供日志与上层增量决策使用。"""

    build: str = ""
    first_build: str = ""          # 库原本的 build（空 = 空库）
    mode: str = "incremental"      # full / incremental
    ships_new: int = 0
    ships_changed: int = 0
    ships_same: int = 0
    ships_removed: int = 0
    models_new: int = 0
    models_changed: int = 0
    models_same: int = 0
    models_removed: int = 0
    #: 本 build 内发生过内容变动的船（含被共享挂载带动的），累积口径
    changed_ships: int = 0
    #: **本次同步**真正改动/新增的船数（用增量导出时看这个）
    changed_now: int = 0
    #: 需要重建 3D 缓存（assets_data.db）的模型目录
    rebuild_models: set[str] = field(default_factory=set)
    #: 已从客户端消失的模型目录
    removed_models: set[str] = field(default_factory=set)

    @property
    def skipped_ships(self) -> int:
        return self.ships_same

    def summary(self) -> str:
        return ("战舰 新增 %d / 变动 %d / 未变 %d / 下架 %d；"
                "模型 新增 %d / 变动 %d / 未变 %d / 消失 %d；本次改动 %d 条"
                % (self.ships_new, self.ships_changed, self.ships_same, self.ships_removed,
                   self.models_new, self.models_changed, self.models_same,
                   self.models_removed, self.changed_now))


class DatabaseManager:
    """SQLite 数据库管理器（线程安全）。"""

    def __init__(self, db_path: str | Path | None = None, wows_type: str = ""):
        self._wows_type = wows_type or ""
        if db_path:
            self._db_path = Path(db_path)
        else:
            self._db_path = get_data_dir() / self._db_name(wows_type)
        self._local = threading.local()
        #: 本次 initialize 是否因 schema 版本落后而整库重建（旧数据被清空）。
        self._schema_rebuilt = False

    def _schema_subdir(self) -> str:
        """按服务器返回 SQL 架构子目录（lesta / wargaming）。"""
        wt = self._wows_type
        if not wt:
            try:
                from app.application import app as app_ctx
                wt = app_ctx.ctx.wows_type
            except Exception:  # noqa: BLE001
                wt = ""
        return "wargaming" if wt == "Wargaming" else "lesta"

    @staticmethod
    def _db_name(wows_type: str = "") -> str:
        """按服务器返回数据库文件名（两服各一个库，绝不合并）。"""
        if wows_type == "Wargaming":
            return "game_data_wg.db"
        return "game_data.db"

    _all_connections: set[sqlite3.Connection] = set()

    # ── 连接 ────────────────────────────────────────────────

    @property
    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = self._create_connection()
        else:
            try:
                self._local.conn.execute("SELECT 1")
            except (sqlite3.ProgrammingError, sqlite3.OperationalError):
                self._local.conn = self._create_connection()
        return self._local.conn

    def _create_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-8000")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA wal_autocheckpoint=4000")
        conn.row_factory = sqlite3.Row
        type(self)._all_connections.add(conn)
        return conn

    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            type(self)._all_connections.discard(self._local.conn)
            self._local.conn = None

    @property
    def exists(self) -> bool:
        return self._db_path.is_file()

    @property
    def db_size_mb(self) -> float:
        try:
            total = self._db_path.stat().st_size
            for suffix in ("-wal", "-shm"):
                p = Path(str(self._db_path) + suffix)
                if p.is_file():
                    total += p.stat().st_size
            return round(total / 1048576, 1)
        except OSError:
            return 0.0

    def checkpoint(self, mode: str = "TRUNCATE") -> None:
        """把 WAL 收回主库（大 WAL 会拖慢后续开库/写入）。"""
        try:
            self._conn.execute("PRAGMA wal_checkpoint(%s)" % mode)
        except sqlite3.OperationalError:
            pass

    # ── schema ─────────────────────────────────────────────

    def get_current_version(self) -> int:
        try:
            row = self._conn.execute(
                "SELECT version FROM meta_schema_version ORDER BY version DESC LIMIT 1").fetchone()
            return int(row["version"]) if row else 0
        except (sqlite3.OperationalError, TypeError, ValueError):
            return 0

    def _record_version(self, ver: int) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO meta_schema_version (version) VALUES (?)", (ver,))
        self._conn.commit()

    def _drop_all_tables(self) -> None:
        conn = self._conn
        conn.execute("PRAGMA foreign_keys=OFF")
        for (tname,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            try:
                conn.execute('DROP TABLE IF EXISTS "%s"' % tname)
            except sqlite3.OperationalError:
                pass
        conn.execute("PRAGMA foreign_keys=ON")
        conn.commit()

    def _vacuum_after_drop(self) -> None:
        """删表只把页标成空闲，文件不会缩小（可残留数百 MB），真空回收。"""
        try:
            self._conn.execute("VACUUM")
            self._conn.commit()
        except sqlite3.OperationalError:
            pass

    def _shrink_leftover_wal(self) -> None:
        """上次被强杀会遗留大 WAL，先并回主库再继续。"""
        try:
            wal = Path(str(self._db_path) + "-wal")
            if wal.is_file() and wal.stat().st_size > 8 * 1048576:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except (OSError, sqlite3.OperationalError):
            pass

    def initialize(self) -> None:
        """建表（幂等）。schema 版本落后 → 整库重建，之后一直走增量。"""
        current_ver = self.get_current_version()
        self._shrink_leftover_wal()
        self._schema_rebuilt = False
        if 0 < current_ver < DB_SCHEMA_VERSION:
            self._drop_all_tables()
            self._schema_rebuilt = True
            self._vacuum_after_drop()

        sub = self._schema_subdir()
        sql_path = get_bundled_dir() / "resources" / "database" / sub / "database_new.sql"
        if not sql_path.exists():
            sql_path = get_bundled_dir() / "resources" / "database" / "database_new.sql"
        if not sql_path.exists():
            raise RuntimeError("缺少 resources/database/%s/database_new.sql" % sub)
        self._conn.executescript(sql_path.read_text(encoding="utf-8"))
        self._conn.commit()
        if self.get_current_version() < DB_SCHEMA_VERSION:
            self._record_version(DB_SCHEMA_VERSION)

    # ── meta ───────────────────────────────────────────────

    def get_meta(self, key: str, default: str = "") -> str:
        try:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return str(row["value"]) if row else default
        except sqlite3.OperationalError:
            return default

    def set_meta(self, items: dict) -> None:
        rows = [(str(k), str(v)) for k, v in items.items()]
        if rows:
            self._conn.executemany(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", rows)
            self._conn.commit()

    @property
    def build(self) -> str:
        """库数据对应的客户端 build（空 = 空库/从未同步）。"""
        return self.get_meta("build")

    # ── 读 ─────────────────────────────────────────────────

    def load_ships(self, active_only: bool = True,
                   require_model: bool = True) -> list[sqlite3.Row]:
        """可载入舰船列表（3D 查看器 / 导出用）。

        require_model=True 只返回有船体模型的船 —— 库里也存了没模型的
        （测试船/dummy），它们进不了导出链路，不该出现在下拉框里。
        """
        sql = ("SELECT id, idx, name, nation, tier, ship_class, ship_group, model_folder "
               "FROM ship WHERE 1=1")
        if active_only:
            sql += " AND status='active'"
        if require_model:
            sql += " AND model_folder<>''"
        sql += " ORDER BY name"
        try:
            return self._conn.execute(sql).fetchall()
        except sqlite3.OperationalError:
            return []

    def count_ships(self) -> int:
        try:
            return int(self._conn.execute(
                "SELECT COUNT(*) FROM ship WHERE status='active'").fetchone()[0])
        except sqlite3.OperationalError:
            return 0

    def load_ship_snapshot(self, ship_id: int | str) -> dict:
        """按数字 id 或 GameParams 名取舰船快照（不存在返回 {}）。"""
        sid = ship_id
        try:
            if isinstance(ship_id, str):
                if ship_id.isdigit():
                    sid = int(ship_id)
                else:
                    row = self._conn.execute(
                        "SELECT id FROM ship WHERE name=?", (ship_id,)).fetchone()
                    if row is None:
                        return {}
                    sid = int(row["id"])
            row = self._conn.execute(
                "SELECT data_json FROM ship_data WHERE ship_id=?", (sid,)).fetchone()
            if row is None:
                return {}
            data = json.loads(row["data_json"])
            return data if isinstance(data, dict) else {}
        except (sqlite3.OperationalError, ValueError):
            return {}

    def load_models(self) -> dict[str, dict]:
        """{model_id: {sig, main_crc, main_size, file_count, status, ...}}。"""
        out: dict[str, dict] = {}
        try:
            rows = self._conn.execute(
                "SELECT model_id, model_path, kind, sig, main_crc, main_size, file_count, "
                "status, first_build, first_seen, last_build, last_changed, last_checked "
                "FROM model").fetchall()
        except sqlite3.OperationalError:
            return out
        for r in rows:
            out[r["model_id"]] = dict(r)
        return out

    def changed_ships(self, build: str = "") -> list[sqlite3.Row]:
        """本 build 内容有变化的船（含「引用了变动挂载」的船）。"""
        b = build or self.build
        try:
            return self._conn.execute(
                "SELECT id, idx, name FROM ship "
                "WHERE status='active' AND last_build=? ORDER BY name", (b,)).fetchall()
        except sqlite3.OperationalError:
            return []

    # ── 写（增量同步）──────────────────────────────────────

    def sync(self, ships: list[ShipRow], models: dict, links: dict[str, list[tuple[str, str]]],
             build: str, game_version: str = "", wows_type: str = "") -> SyncStats:
        """把一次导入的结果并入库：只重写内容变化的行，其余仅刷 last_checked。

        ships:  ShipRow 列表（快照 JSON 已在内存里构造好）
        models: {model_id: object with .sig/.main_crc/.main_size/.files}
        links:  {ship name: [(model_id, role), ...]}（船 → 模型，N:N）
        """
        stats = SyncStats(build=build, first_build=self.build)
        stats.mode = "full" if not stats.first_build else "incremental"
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        conn = self._conn

        old_ships = {int(r["id"]): dict(r) for r in conn.execute(
            "SELECT id, idx, name, nation, tier, ship_class, ship_group, model_folder, "
            "status, first_build, first_seen, last_build, last_changed FROM ship")}
        old_sha = {int(r["ship_id"]): r["sha1"] for r in conn.execute(
            "SELECT ship_id, sha1 FROM ship_data")}
        old_models = {r["model_id"]: dict(r) for r in conn.execute(
            "SELECT model_id, sig, status FROM model")}

        ship_up: list[tuple] = []
        ship_ins: list[tuple] = []
        data_up: list[tuple] = []
        data_ins: list[tuple] = []
        changed_ship_ids: set[int] = set()
        keep_ids: set[int] = set()

        for s in ships:
            keep_ids.add(s.id)
            prev = old_ships.get(s.id)
            if prev is None:
                ship_ins.append((s.id, s.idx, s.name, s.nation, s.tier, s.ship_class,
                                 s.ship_group, s.model_folder, "active",
                                 build, now, build, now, now))
                data_ins.append((s.id, s.data_json, len(s.data_json), s.sha1, now))
                stats.ships_new += 1
                changed_ship_ids.add(s.id)
                continue

            content_same = (
                old_sha.get(s.id) == s.sha1
                and prev["idx"] == s.idx and prev["name"] == s.name
                and prev["nation"] == s.nation and int(prev["tier"]) == int(s.tier)
                and prev["ship_class"] == s.ship_class
                and prev["ship_group"] == s.ship_group
                and prev["model_folder"] == s.model_folder
                and prev["status"] == "active"
            )
            if content_same:
                ship_up.append((s.idx, s.name, s.nation, s.tier, s.ship_class,
                                s.ship_group, s.model_folder, "active", prev["first_build"],
                                prev["first_seen"], prev["last_build"], prev["last_changed"],
                                now, s.id))
                stats.ships_same += 1
            else:
                ship_up.append((s.idx, s.name, s.nation, s.tier, s.ship_class,
                                s.ship_group, s.model_folder, "active", prev["first_build"],
                                prev["first_seen"], build, now, now, s.id))
                if old_sha.get(s.id) != s.sha1:
                    data_up.append((s.data_json, len(s.data_json), s.sha1, now, s.id))
                stats.ships_changed += 1
                changed_ship_ids.add(s.id)

        if ship_ins:
            conn.executemany(
                "INSERT INTO ship (id, idx, name, nation, tier, ship_class, ship_group, "
                "model_folder, status, first_build, first_seen, last_build, last_changed, "
                "last_checked) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ship_ins)
        if ship_up:
            conn.executemany(
                "UPDATE ship SET idx=?, name=?, nation=?, tier=?, ship_class=?, ship_group=?, "
                "model_folder=?, status=?, first_build=?, first_seen=?, last_build=?, "
                "last_changed=?, last_checked=? WHERE id=?", ship_up)
        if data_ins:
            conn.executemany(
                "INSERT INTO ship_data (ship_id, data_json, json_len, sha1, updated_at) "
                "VALUES (?,?,?,?,?)", data_ins)
        if data_up:
            conn.executemany(
                "UPDATE ship_data SET data_json=?, json_len=?, sha1=?, updated_at=? "
                "WHERE ship_id=?", data_up)

        # 下架的船：保留行（用 status 记住「曾经存在」），不删
        gone = set(old_ships) - keep_ids
        if gone:
            ids = list(gone)
            for i in range(0, len(ids), 400):
                chunk = ids[i:i + 400]
                conn.execute(
                    "UPDATE ship SET status='removed', last_checked=? "
                    "WHERE id IN (%s)" % ",".join("?" * len(chunk)), [now] + chunk)
            stats.ships_removed = len(gone)

        # ── 模型 ──────────────────────────────────────────
        m_ins: list[tuple] = []
        m_up: list[tuple] = []
        f_rows: list[tuple] = []
        rebuild: set[str] = set()
        for mid, entry in models.items():
            prev = old_models.get(mid)
            files = getattr(entry, "files", [])
            if prev is None:
                m_ins.append((mid, getattr(entry, "model_path", ""),
                              getattr(entry, "kind", "mount"), entry.sig,
                              entry.main_crc, entry.main_size, len(files), "active",
                              build, now, build, now, now))
                rebuild.add(mid)
                stats.models_new += 1
            elif prev["sig"] != entry.sig or prev["status"] != "active":
                m_up.append((getattr(entry, "model_path", ""), getattr(entry, "kind", "mount"),
                             entry.sig, entry.main_crc, entry.main_size, len(files), "active",
                             build, now, now, mid))
                rebuild.add(mid)
                stats.models_changed += 1
            else:
                conn.execute(
                    "UPDATE model SET last_checked=? WHERE model_id=?", (now, mid))
                stats.models_same += 1
                continue
            for f in files:
                f_rows.append((mid, f.path, f.role, f.crc32, f.size))

        if m_ins:
            conn.executemany(
                "INSERT INTO model (model_id, model_path, kind, sig, main_crc, main_size, "
                "file_count, status, first_build, first_seen, last_build, last_changed, "
                "last_checked) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", m_ins)
        if m_up:
            conn.executemany(
                "UPDATE model SET model_path=?, kind=?, sig=?, main_crc=?, main_size=?, "
                "file_count=?, status=?, last_build=?, last_changed=?, last_checked=? "
                "WHERE model_id=?", m_up)
        if f_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO model_file (model_id, path, role, crc32, size) "
                "VALUES (?,?,?,?,?)", f_rows)

        gone_models = set(old_models) - set(models)
        if gone_models:
            ids = list(gone_models)
            for i in range(0, len(ids), 400):
                chunk = ids[i:i + 400]
                conn.execute(
                    "UPDATE model SET status='removed', last_checked=? "
                    "WHERE model_id IN (%s)" % ",".join("?" * len(chunk)), [now] + chunk)
                conn.execute(
                    "DELETE FROM model_file WHERE model_id IN (%s)"
                    % ",".join("?" * len(chunk)), chunk)
            stats.models_removed = len(gone_models)

        stats.rebuild_models = rebuild
        stats.removed_models = gone_models

        # ── 船 ↔ 模型 ─────────────────────────────────────
        name_to_id = {s.name: s.id for s in ships}
        del_rows: list[tuple] = []
        link_rows: list[tuple] = []
        for ship_name, pairs in links.items():
            sid = name_to_id.get(ship_name)
            if sid is None:
                continue
            # 只重写「新增/变动」的船的关联；未变动的保持原样
            if sid not in changed_ship_ids:
                continue
            del_rows.append((sid,))
            for mid, role in pairs:
                link_rows.append((sid, mid, role))
        if del_rows:
            conn.executemany("DELETE FROM ship_model WHERE ship_id=?", del_rows)
            conn.executemany(
                "INSERT OR REPLACE INTO ship_model (ship_id, model_id, role) VALUES (?,?,?)",
                link_rows)

        # 引用了变动模型的船 → 也算「本 build 变动」（挂载是共享件）
        if rebuild:
            ids = sorted(rebuild)
            for i in range(0, len(ids), 400):
                chunk = ids[i:i + 400]
                ph = ",".join("?" * len(chunk))
                for r in conn.execute(
                        "SELECT DISTINCT ship_id FROM ship_model WHERE model_id IN (%s)" % ph,
                        chunk):
                    changed_ship_ids.add(int(r[0]))
                conn.execute(
                    "UPDATE ship SET last_build=?, last_changed=? WHERE id IN "
                    "(SELECT ship_id FROM ship_model WHERE model_id IN (%s))" % ph,
                    [build, now] + chunk)
        stats.changed_now = len(changed_ship_ids)

        self.set_meta({
            "schema_ver": DB_SCHEMA_VERSION,
            "build": build,
            "game_version": game_version,
            "wows_type": wows_type or self._wows_type,
            "synced_at": now,
            "sync_mode": stats.mode,
        })
        conn.commit()

        stats.changed_ships = len(self.changed_ships(build))
        return stats


# ── 模块级单例 ─────────────────────────────────────────────

_db_instances: dict[str, DatabaseManager] = {}
_db_lock = threading.Lock()


def get_db(wows_type: str = "") -> DatabaseManager:
    """按服务器取共享 DatabaseManager（同一进程内复用连接）。"""
    if not wows_type:
        try:
            from app.application import app as app_ctx
            wows_type = app_ctx.ctx.wows_type
        except Exception:  # noqa: BLE001
            wows_type = ""
    key = wows_type or "Lesta"
    with _db_lock:
        inst = _db_instances.get(key)
        if inst is None:
            inst = DatabaseManager(wows_type=wows_type)
            _db_instances[key] = inst
        return inst


def reset_db() -> None:
    """关闭并清空全部单例连接（导入收尾/切服时调用）。"""
    with _db_lock:
        for inst in _db_instances.values():
            try:
                inst.close()
            except Exception:  # noqa: BLE001
                pass
        _db_instances.clear()
