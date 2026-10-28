"""Resolve the client build folder that matches the imported databases."""
from __future__ import annotations

import sqlite3


def imported_build(server: str) -> str:
    """读 game_data*.db 的 meta.build（导入时写入的客户端 build 号）。"""
    from utils.path_utils import get_data_dir
    name = "game_data_wg.db" if server == "wg" else "game_data.db"
    path = get_data_dir() / name
    if not path.is_file():
        return ""
    try:
        con = sqlite3.connect(str(path))
        try:
            row = con.execute("SELECT value FROM meta WHERE key='build'").fetchone()
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return ""
    return str(row[0]) if row and row[0] else ""
