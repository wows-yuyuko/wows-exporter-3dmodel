"""版本对比：按 `.geometry` 的 crc32 找出两版之间变动的船与挂载。

只读 `bin/<build>/idx/*.idx` 与 `data/game_data*.db`，**不加载 assets.bin、不解包几何**。
判据是 idx 里本来就存的 `crc32`（解压后内容的 CRC32，实测与 `zlib.crc32(内容)` 一致）。

口径（"宽松"，2026-10-28 与用户确认）：
    crc32 覆盖整个 `.geometry`，含顶点/索引 —— 所以「只改了模型细节」也会判为变动。
    这是刻意取舍：宁可多导一次，不要漏导。

实测（WG 13187581 → 13357625）：31,843 个同名 geometry 里 99.9% 的 crc32 不变，
只有 25 个变了 —— 所以这一层足以把「没动的船」几乎全部滤掉。
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from utils.path_utils import list_builds

#: 船体分段后缀：主文件（不带这些）才可能含装甲
_SEG_SUFFIX = re.compile(r"_(Bow|MidFront|MidBack|Stern|Superstructure|Hull|Mid)$", re.I)


@dataclass(frozen=True)
class GeomFile:
    """一个 `.geometry` 在 idx 里的元数据。"""

    path: str
    crc32: int
    size: int

    @property
    def folder(self) -> str:
        """模型目录名（如 BSD100_Waterloo_1956）。"""
        return self.path.rsplit("/", 2)[-2]

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def is_main(self) -> bool:
        """主文件：文件名（去 `.geometry`）与目录名相同。

        装甲只在这里（分段实测恒 0），所以「是否主文件」决定要不要重导装甲。
        """
        return self.name[: -len(".geometry")] == self.folder


@dataclass
class ChangedModel:
    """按模型目录聚合的变动。"""

    folder: str
    main: bool = False            # 主文件变了（可能含装甲）
    files: list[str] = field(default_factory=list)
    ships: set[str] = field(default_factory=set)
    is_hull: bool = False         # 能在 ship_models 里找到 → 是某条船的船体

    @property
    def seg_count(self) -> int:
        return len(self.files) - (1 if self.main else 0)


def snapshot_build(game: str | Path, build: str) -> dict[str, GeomFile]:
    """读一个 build 的 idx，返回 {vfs 路径: GeomFile}（不解包、不读 pkg）。"""
    from data_extractor import GameExtractor

    ex = GameExtractor(str(game), bin_folder=str(build))
    out: dict[str, GeomFile] = {}
    try:
        for e in ex.list_files(["content/**/*.geometry"]):
            if getattr(e, "is_directory", False):
                continue
            fi = e.file_info
            out[e.path] = GeomFile(e.path, fi.crc32, fi.unpacked_size)
    finally:
        ex.close()
    return out


def ship_model_index(db_path: str | Path) -> tuple[dict[str, set[str]], dict[str, str]]:
    """构建两份映射：模型目录 → 船名集合；船体目录 → 船名。

    返回 ``(by_folder, hull_folder)``：
      - ``by_folder``：所有被引用的模型目录 → 引用它的船（含挂载共享件）
      - ``hull_folder``：``ship.model_folder`` → 船（只有这些是船体）
    """
    db_path = Path(db_path)
    if not db_path.is_file():
        return {}, {}

    by_folder: dict[str, set[str]] = {}
    hull_folder: dict[str, str] = {}
    conn = sqlite3.connect(str(db_path))
    try:
        id_to_name: dict[int, str] = {}
        for sid, name, folder in conn.execute(
                "SELECT id, name, model_folder FROM ship WHERE status='active'"):
            id_to_name[int(sid)] = str(name)
            if folder:
                hull_folder[str(folder)] = str(name)
                by_folder.setdefault(str(folder), set()).add(str(name))
        for sid, mid in conn.execute("SELECT ship_id, model_id FROM ship_model"):
            name = id_to_name.get(int(sid))
            if name and mid:
                by_folder.setdefault(str(mid), set()).add(name)
    except (sqlite3.OperationalError, ValueError):
        return by_folder, hull_folder
    finally:
        conn.close()
    return by_folder, hull_folder


def diff_snapshots(old: dict[str, GeomFile],
                   new: dict[str, GeomFile]) -> tuple[list[ChangedModel], list[GeomFile], list[GeomFile]]:
    """对比两份快照，按模型目录聚合变动。

    返回 ``(changed, added, removed)``：changed 已聚合；added/removed 保留文件粒度
    （新船/删船的判定交给调用方）。
    """
    common = old.keys() & new.keys()
    changed_models: dict[str, ChangedModel] = {}

    for path in common:
        a, b = old[path], new[path]
        if (a.crc32, a.size) == (b.crc32, b.size):
            continue
        m = changed_models.setdefault(a.folder, ChangedModel(folder=a.folder))
        m.files.append(a.name)
        if a.is_main:
            m.main = True

    added = [new[p] for p in sorted(new.keys() - old.keys())]
    removed = [old[p] for p in sorted(old.keys() - new.keys())]
    changed = sorted(changed_models.values(), key=lambda x: x.folder)
    return changed, added, removed


def annotate(changed: list[ChangedModel],
             by_folder: dict[str, set[str]],
             hull_folder: dict[str, str]) -> None:
    """就地补上 `ships` / `is_hull`。"""
    for m in changed:
        m.is_hull = m.folder in hull_folder
        if m.is_hull:
            m.ships = {hull_folder[m.folder]}
        else:
            m.ships = set(by_folder.get(m.folder) or ())


def affected_ships(changed: list[ChangedModel]) -> list[str]:
    out: set[str] = set()
    for m in changed:
        out |= m.ships
    return sorted(out)


def format_text(old_build: str, new_build: str,
                changed: list[ChangedModel],
                added: list[GeomFile], removed: list[GeomFile],
                total_old: int, total_new: int) -> str:
    hull = [m for m in changed if m.is_hull]
    mounts = [m for m in changed if not m.is_hull]
    hull_main = [m for m in hull if m.main]
    lines = [
        "对比 %s → %s" % (old_build, new_build),
        "  .geometry：旧 %d / 新 %d，同名 %d"
        % (total_old, total_new, total_old + total_new - len(added) - len(removed)),
        "  变动 %d 个模型目录（%d 个文件）"
        % (len(changed), sum(len(m.files) for m in changed)),
    ]
    if not changed:
        lines.append("  ✅ 没有变动")
        return "\n".join(lines)

    lines.append("")
    lines.append("船体变动 %d 条%s："
                 % (len(hull), "（其中 %d 条动了主文件，可能含装甲）" % len(hull_main) if hull_main else ""))
    for m in hull:
        kind = "主文件" if m.main else "仅分段"
        seg = " + %d 分段" % m.seg_count if m.seg_count else ""
        keys = ", ".join(sorted(m.ships)) or "（未入库）"
        lines.append("  %-38s %-34s %s%s" % (keys, m.folder, kind, seg))

    if mounts:
        total = len(affected_ships(mounts))
        lines.append("")
        lines.append("挂载模型变动 %d 个，波及 %d 条船：" % (len(mounts), total))
        for m in mounts:
            lines.append("  %-38s %s"
                         % (m.folder, "、".join(sorted(m.ships)) or "（当前库里没有船引用它）"))

    if added or removed:
        lines.append("")
        lines.append("新增 %d 个 geometry / 删除 %d 个" % (len(added), len(removed)))
        for f in added[:6]:
            lines.append("  + %s" % f.path)
        if len(added) > 6:
            lines.append("  …（其余 %d 个用 --format json 看）" % (len(added) - 6))
        for f in removed[:6]:
            lines.append("  - %s" % f.path)
        if len(removed) > 6:
            lines.append("  …（其余 %d 个）" % (len(removed) - 6))
    return "\n".join(lines)


def format_json(old_build: str, new_build: str,
                changed: list[ChangedModel],
                added: list[GeomFile], removed: list[GeomFile]) -> str:
    payload = {
        "old": old_build,
        "new": new_build,
        "changed": [
            {
                "folder": m.folder,
                "hull": m.is_hull,
                "main": m.main,
                "files": m.files,
                "ships": sorted(m.ships),
            }
            for m in changed
        ],
        "added": [{"path": f.path, "crc32": "%08x" % f.crc32, "size": f.size} for f in added],
        "removed": [{"path": f.path, "crc32": "%08x" % f.crc32, "size": f.size} for f in removed],
        "affected_ships": affected_ships(changed),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def run(game: str | Path, server: str, old_build: str | None = None,
        new_build: str | None = None, fmt: str = "text") -> tuple[str, int]:
    """执行对比，返回 (输出文本, 受影响船数)。

    `old_build` / `new_build` 缺省时取 `bin/` 下数值最大的两个（新 = 最大）。
    """
    if not old_build or not new_build:
        builds = list_builds(game, 2)
        if len(builds) < 2:
            raise SystemExit("bin/ 下不足两个版本目录，无法对比；请显式给 --old / --new")
        new_build = new_build or builds[0]
        old_build = old_build or builds[1]

    old = snapshot_build(game, old_build)
    new = snapshot_build(game, new_build)
    changed, added, removed = diff_snapshots(old, new)

    from services.database_service import DatabaseManager
    from utils.path_utils import get_data_dir

    db = get_data_dir() / DatabaseManager._db_name(
        "Wargaming" if server == "wg" else "Lesta")
    by_folder, hull_folder = ship_model_index(db)
    annotate(changed, by_folder, hull_folder)

    if fmt == "json":
        text = format_json(old_build, new_build, changed, added, removed)
        return text, len(affected_ships(changed))
    text = format_text(old_build, new_build, changed, added, removed, len(old), len(new))
    if not db.is_file():
        text += "\n\n⚠️ 没有 %s，无法把模型映射到船 key（先跑 import 或用 --format json 看目录名）" % db.name
    return text, len(affected_ships(changed))
