"""Resolve the client build folder that matches the imported databases."""
from __future__ import annotations

import sqlite3


def imported_build(server: str) -> str:
    """Read meta_import_state (e.g. '8867689_8867689') from data/game_data*.db."""
    from utils.path_utils import get_data_dir
    name = "game_data_wg.db" if server == "wg" else "game_data.db"
    path = get_data_dir() / name
    if not path.is_file():
        return ""
    try:
        con = sqlite3.connect(str(path))
        row = con.execute("SELECT * FROM meta_import_state ORDER BY rowid DESC LIMIT 1").fetchone()
        con.close()
    except Exception:
        return ""
    if not row or not row[0]:
        return ""
    return str(row[0]).split("_", 1)[0]
