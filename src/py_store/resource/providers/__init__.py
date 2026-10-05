"""provider 注册表。"""
from . import local, s3

_REG = {"local": local, "s3": s3}


def register_provider(kind, mod):
    if mod is None or not hasattr(mod, "create"):
        raise ValueError(f'provider "{kind}" 须提供 create(options) 工厂')
    _REG[kind] = mod


def create_provider(kind, options=None):
    mod = _REG.get(kind)
    if mod is None:
        raise ValueError(f'未知资源 provider: {kind}（已注册: {", ".join(_REG)}）')
    return mod.create(options or {})
