"""两个缓存库的共用底座：连接管理 / schema 版本 / meta / 全表维护。

`game_data*.db` 与 `assets_data*.db` 是独立文件（两服各一份，共 4 个），
但样板完全一致 —— WAL + 按线程复用的连接、`meta_schema_version` 版本判定、
key/value 的 `meta` 表、schema 落后时整库重建。这些放这里，子类只声明
四样：`DB_STEM` / `SQL_FILE` / `SCHEMA_VERSION` / 连接参数。

「单版本镜像」是共同的库级约定：库里只保留**最后一次同步**的那个 build，
不做多版本共存。若把 build 放进主键，客户端一更新，未变动的上百万行也要
UPDATE 到新 build，等于全量重写 —— 增量就无从谈起。build 号记在 `meta.build`。
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from utils.path_utils import get_bundled_dir, get_data_dir


class SqliteStore:
    """子类需覆盖：`DB_STEM`、`SQL_FILE`、`SCHEMA_VERSION`。"""

    #: 库文件名前缀：`<DB_STEM>.db` / `<DB_STEM>_wg.db`
    DB_STEM: str = ""
    #: `resources/database/<sub>/` 下的建表脚本文件名
    SQL_FILE: str = ""
    #: schema 版本；库内低于它就整库重建
    SCHEMA_VERSION: int = 0

    TIMEOUT: int = 10
    BUSY_TIMEOUT: int = 5000
    #: 子类追加的 PRAGMA（如 foreign_keys / wal_autocheckpoint）
    EXTRA_PRAGMAS: tuple[str, ...] = ()

    def __init__(self, db_path: str | Path | None = None, wows_type: str = ""):
        if not wows_type:
            try:
                from app.application import app as app_ctx
                wows_type = app_ctx.ctx.wows_type
            except Exception:  # noqa: BLE001
                wows_type = ""
        self._wows_type = wows_type or ""
        self._db_path = Path(db_path) if db_path else (
            get_data_dir() / self._db_name(self._wows_type))
        self._local = threading.local()
        #: initialize() 因 schema 版本落后而整库重建时置 True（上层据此提示重跑 import）
        self._schema_rebuilt = False

    # ── 标识 ────────────────────────────────────────────────

    @classmethod
    def _db_name(cls, wows_type: str = "") -> str:
        """按服务器返回库文件名（两服各一个库，绝不合并）。"""
        return "%s_wg.db" % cls.DB_STEM if wows_type == "Wargaming" else "%s.db" % cls.DB_STEM

    def _schema_subdir(self) -> str:
        """建表脚本的子目录（lesta / wargaming）。"""
        return "wargaming" if self._wows_type == "Wargaming" else "lesta"

    @property
    def exists(self) -> bool:
        return self._db_path.is_file()

    @property
    def build(self) -> str:
        """库数据对应的客户端 build（空 = 空库 / 从未同步）。"""
        return self.get_meta("build")

    # ── 连接 ────────────────────────────────────────────────

    @property
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.execute("SELECT 1")
                return conn
            except (sqlite3.ProgrammingError, sqlite3.OperationalError):
                pass
        self._local.conn = self._create_connection()
        return self._local.conn

    def _create_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False,
                               timeout=self.TIMEOUT)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-8000")
        conn.execute("PRAGMA busy_timeout=%d" % self.BUSY_TIMEOUT)
        for pragma in self.EXTRA_PRAGMAS:
            conn.execute(pragma)
        conn.row_factory = sqlite3.Row
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def checkpoint(self, mode: str = "TRUNCATE") -> None:
        """把 WAL 收回主库（大 WAL 会拖慢后续开库/写入）。"""
        try:
            self._conn.execute("PRAGMA wal_checkpoint(%s)" % mode)
        except sqlite3.OperationalError:
            pass

    # ── schema ─────────────────────────────────────────────

    def get_current_version(self) -> int:
        """当前已应用的 schema 版本（无表/异常返回 0）。"""
        try:
            row = self._conn.execute(
                "SELECT version FROM meta_schema_version "
                "ORDER BY version DESC LIMIT 1").fetchone()
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
        """删表只把页标成空闲，文件不会缩小，真空回收。"""
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

    def _schema_sql(self) -> str:
        sub = self._schema_subdir()
        path = get_bundled_dir() / "resources" / "database" / sub / self.SQL_FILE
        if not path.exists():
            path = get_bundled_dir() / "resources" / "database" / self.SQL_FILE
        if not path.exists():
            raise RuntimeError("缺少 resources/database/%s/%s" % (sub, self.SQL_FILE))
        return path.read_text(encoding="utf-8")

    def initialize(self) -> None:
        """建表（幂等）。schema 版本落后 → 整库重建，之后一直走增量。"""
        self._shrink_leftover_wal()
        current_ver = self.get_current_version()
        self._schema_rebuilt = False
        if 0 < current_ver < self.SCHEMA_VERSION:
            self._drop_all_tables()
            self._schema_rebuilt = True
            self._vacuum_after_drop()
        self._conn.executescript(self._schema_sql())
        self._conn.commit()
        if self.get_current_version() < self.SCHEMA_VERSION:
            self._record_version(self.SCHEMA_VERSION)

    # ── meta ───────────────────────────────────────────────

    def get_meta(self, key: str, default: str = "") -> str:
        try:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return str(row["value"]) if row else default
        except sqlite3.OperationalError:
            return default

    def set_meta(self, items: dict, commit: bool = True) -> None:
        """写 key/value。`commit=False` 用于「与大批写入共用事务」的场景。"""
        rows = [(str(k), str(v)) for k, v in items.items()]
        if not rows:
            return
        self._conn.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", rows)
        if commit:
            self._conn.commit()
