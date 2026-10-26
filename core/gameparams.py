"""Minimal GameParams loader for the exporter CLI (no DB, no Qt)."""
from __future__ import annotations

import pickle
import sys
import zlib
from dataclasses import dataclass, field

from services import GameParams as _GP


@dataclass
class ShipEntry:
    key: str
    model: str
    model_folder: str
    nation: str
    tier: int
    ship_type: str
    index: str = ""
    permoflages: list = field(default_factory=list)


def _ensure_stub():
    sys.modules.setdefault("GameParams", _GP)


def load_gameparams(path: str):
    with open(path, "rb") as fh:
        raw = fh.read()
    # Lesta：反转 + zlib；WG：raw 是 deflate，解出 %bin 后整体反转再 zlib
    payload = None
    for blob in (raw[::-1], raw):
        try:
            payload = zlib.decompress(blob)
            break
        except zlib.error:
            continue
    if payload is None:
        try:
            inner = zlib.decompressobj(-15).decompress(raw)
            payload = zlib.decompress(inner[::-1])
        except zlib.error:
            payload = None
    if payload is None:
        # WG 正式包里的 GameParams 常是「原样存储」：直接反序列化（含反转）
        for blob in (raw[::-1], raw):
            try:
                return pickle.loads(blob, encoding="latin1")
            except Exception:
                continue
        raise RuntimeError("GameParams is not zlib-compressed or pickle (reversed or direct)")
    data = pickle.loads(payload, encoding="latin1")
    if isinstance(data, dict) and "" in data and isinstance(data[""], dict):
        return data[""]
    return data


def _as_dict(value):
    if isinstance(value, dict):
        return value
    if hasattr(value, "__dict__"):
        return vars(value)
    return {}


def _iter_ships(data):
    # Lesta: (dict[key -> GPData], set)；WG: {"": dict} 或 list[dict]
    if isinstance(data, tuple):
        data = data[0]
    if isinstance(data, dict):
        for key, value in data.items():
            if key == "":
                continue
            yield key, _as_dict(value)
        return
    for elem in data:
        if isinstance(elem, dict):
            for key, value in elem.items():
                if key == "":
                    continue
                yield key, _as_dict(value)


def exteriors(path: str) -> list[dict]:
    """Load Exterior entities (needed for permoflage/skin enumeration)."""
    _ensure_stub()
    data = load_gameparams(path)
    out = []
    for key, value in _iter_ships(data):
        info = _as_dict(value.get("typeinfo"))
        if str(info.get("type", "")) != "Exterior":
            continue
        raw = _as_dict(value)
        data = {k: raw.get(k) for k in (
            "camouflage", "unpeculiarCamouflage", "unpeculiarCamouflageColorSchemeId",
            "hullConfig", "nodesConfig", "peculiarityModels", "renderSetsStates",
            "miscFilter", "planesConfig", "decalsEnabled",
        ) if raw.get(k) is not None}
        out.append({
            "name": key,
            "index": str(raw.get("index") or ""),
            "species": str(info.get("species") or ""),
            "data": data,
        })
    return out


#: Exterior 实体里涂装/皮肤用到的字段（data_json 只存这些，不存整份实体）
EXTERIOR_DATA_KEYS = (
    "camouflage", "unpeculiarCamouflage", "unpeculiarCamouflageColorSchemeId",
    "hullConfig", "nodesConfig", "peculiarityModels", "renderSetsStates",
    "miscFilter", "planesConfig", "decalsEnabled",
)


def _plain(o):
    """GPData / 嵌套容器 → 纯 dict/list，便于 json 序列化。"""
    if isinstance(o, dict):
        return {k: _plain(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_plain(v) for v in o]
    d = getattr(o, "__dict__", None)
    if isinstance(d, dict):
        return {k: _plain(v) for k, v in d.items()}
    return o


def exterior_entities(path: str) -> list[tuple[str, dict]]:
    """全部 Exterior 实体 (name, 纯 dict)，等价于 split/Exterior/*.json 的内容。

    供 assets_data.db 直接入库，避免依赖导入时可能已被清理的 split 目录。
    """
    _ensure_stub()
    data = load_gameparams(path)
    out: list[tuple[str, dict]] = []
    for key, value in _iter_ships(data):
        info = _as_dict(value.get("typeinfo"))
        if str(info.get("type", "")) != "Exterior":
            continue
        out.append((key, _plain(value)))
    return out


def ships(path: str, flt: str = "") -> list[ShipEntry]:
    _ensure_stub()
    data = load_gameparams(path)
    out = []
    for key, value in _iter_ships(data):
        if not isinstance(value, dict):
            continue
        hull = _as_dict(value.get("A_Hull"))
        model = str(hull.get("model") or "")
        if not model:
            continue
        parts = [p for p in model.replace("\\", "/").split("/") if p]
        folder = parts[-2] if len(parts) >= 2 else ""
        info = _as_dict(value.get("typeinfo"))
        tier = value.get("level") or info.get("level") or info.get("tier") or 0
        try:
            tier = int(tier)
        except Exception:
            tier = 0
        entry = ShipEntry(
            key=key, model=model, model_folder=folder,
            nation=str(info.get("nation", "")), tier=tier,
            ship_type=str(info.get("type", "")),
            index=str(value.get("index") or ""),
            permoflages=list(value.get("permoflages") or []),
        )
        if flt:
            hay = (entry.key + " " + entry.model_folder).lower()
            if flt.lower() not in hay:
                continue
        out.append(entry)
    return out
