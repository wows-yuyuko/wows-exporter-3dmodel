"""Wargaming（WG）服的 GameParams 类型差异。

WG 的 GameParams 与 Lesta 的实体 type 集合可能不同，`processor_service` 用它决定
哪些实体算 Ship。没按 WG 实测校准前留空 —— 调用方回退 Lesta 默认映射
（见 `docs/todo-backlog.md` E2）。
"""
from __future__ import annotations

#: WG 实体 type → 类别；None = 未校准 → 调用方回退 Lesta 默认。
#: 校准示例（替换 None 即生效）::
#:
#:     WG_TYPE_CATEGORY_MAP = {"Ship": "Ship", "Gun": "Gun", "Projectile": "Projectile",
#:                             "Aircraft": "Aircraft", "Ability": "Ability",
#:                             "Modernization": "Modernization", "Crew": "Crew",
#:                             "Exterior": "Exterior", "Other": "Other"}
WG_TYPE_CATEGORY_MAP: dict[str, str] | None = None


def get_type_category_map(wows_type: str) -> dict[str, str] | None:
    """WG 已校准则返回 WG 版，否则 None（调用方回退 Lesta 默认）。"""
    return WG_TYPE_CATEGORY_MAP if wows_type == "Wargaming" else None


def is_wg(wows_type: str = "") -> bool:
    """当前/指定服务器是否为 Wargaming。"""
    if not wows_type:
        from app.application import app as app_ctx
        wows_type = app_ctx.ctx.wows_type
    return wows_type == "Wargaming"
