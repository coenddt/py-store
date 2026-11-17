"""provider 注册表。"""

from typing import Any, cast

from . import local, s3

_REG = {"local": local, "s3": s3}


def register_provider(kind: str, mod: Any) -> None:
    """注册 provider 模块（须提供 ``create(options)`` 工厂，否则拒绝）。"""
    if mod is None or not hasattr(mod, "create"):
        raise ValueError(f'provider "{kind}" 须提供 create(options) 工厂')
    _REG[kind] = mod


def create_provider(kind: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
    """按 kind 实例化 provider（未知 kind 抛 ``ValueError``）。"""
    mod = _REG.get(kind)
    if mod is None:
        raise ValueError(f"未知资源 provider: {kind}（已注册: {', '.join(_REG)}）")
    return cast("dict[str, Any]", mod.create(options or {}))
