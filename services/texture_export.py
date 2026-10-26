"""DDS -> WebP helper for external texture export."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path

from services.export_service import _TEX_COLOR, _TEX_MR, _TEX_NORMAL, dds_to_png


def dds_to_webp(dds: bytes, kind: str = "color", max_size: int = 2048) -> bytes | None:
    k = {"color": _TEX_COLOR, "normal": _TEX_NORMAL, "mr": _TEX_MR}.get(kind, _TEX_COLOR)
    png = dds_to_png(dds, max_size=max_size, kind=k)
    if not png:
        return None
    try:
        from PIL import Image as PILImage
        im = PILImage.open(io.BytesIO(png)).convert("RGBA")
        if max(im.size) > max_size:
            im.thumbnail((max_size, max_size), PILImage.LANCZOS)
        out = io.BytesIO()
        im.save(out, "WEBP", quality=85, method=2)
        return out.getvalue()
    except Exception:
        return None


def write_webp(out_dir: Path, data: bytes) -> str:
    out_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(data).hexdigest()
    path = out_dir / (digest + ".webp")
    if not path.exists():
        path.write_bytes(data)
    return "textures/" + digest + ".webp"


def kind_for_path(path: str) -> str:
    lower = path.lower()
    if lower.endswith(("_n.dds", "_n.dd0", "_n.dd1", "_n.dd2", "_normal.dds")):
        return "normal"
    if lower.endswith(("_mg.dds", "_mg.dd0", "_mg.dd1", "_mg.dd2")):
        return "mr"
    return "color"
