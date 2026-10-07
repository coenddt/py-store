"""资源能力（宿主旁路，B 档）—— provider 注册表 + put/open/remove/url。"""
from __future__ import annotations

import hashlib
import inspect
from typing import Any

from ..core import native
from ..feedback import emit as _emit_feedback
from . import providers

_DEFAULT_SCHEMA = {"resource": "Resource", "location": "ResourceLocation", "binding": "ResourceBinding"}
_cfg: dict[str, Any] = {"schema": dict(_DEFAULT_SCHEMA), "store": None, "providers": [], "url": {}, "sign": None}
_pool: dict = {}

_BYTES_TYPES = (bytes, bytearray)


def configure(cfg=None):
    global _cfg, _pool
    cfg = cfg or {}
    _cfg = {
        "schema": {**_DEFAULT_SCHEMA, **(cfg.get("schema") or {})},
        "store": cfg.get("store"),
        "providers": list(cfg.get("providers") or []),
        "url": cfg.get("url") or {},
        "sign": cfg.get("sign") if callable(cfg.get("sign")) else None,
    }
    _pool = {}
    for i, spec in enumerate(_cfg["providers"]):
        p = providers.create_provider(spec["kind"], spec.get("options") or {})
        p["priority"] = spec.get("priority", i)
        _pool[spec["kind"]] = p
    return _pool


def register_provider(kind, mod):
    providers.register_provider(kind, mod)


def _crud():
    if _cfg["store"] is not None:
        return _cfg["store"]
    from .. import crud
    return crud


async def _call(fn, *a, **kw):
    out = fn(*a, **kw)
    if inspect.isawaitable(out):
        out = await out
    return out


async def put(*, bytes=None, file_name=None, mime=None, kind=None, bind=None):
    if bytes is None:
        raise ValueError("resource.put 需要 bytes")
    data = bytes if isinstance(bytes, _BYTES_TYPES) else str(bytes).encode("utf-8")
    crud = _crud()
    sha1 = hashlib.sha1(data).hexdigest()
    resource_id = sha1
    key = native.resource_content_path(sha1)

    locations = []
    for backend, p in _pool.items():
        try:
            await _call(p["put"], key, data, {"mime": mime})
            locations.append({"resourceId": resource_id, "backend": backend, "key": key,
                              "status": "ok", "priority": p["priority"]})
        except Exception as e:  # 允许部分失败，但必须反馈（禁静默）
            locations.append({"resourceId": resource_id, "backend": backend, "key": key,
                              "status": "failed", "priority": p["priority"]})
            _emit_feedback({
                "type": "resource_location_write_failed", "code": "resourceLocationWriteFailed",
                "layer": "resource", "backend": backend, "resourceId": resource_id,
                "message": f"资源副本写入失败（backend={backend}, id={resource_id}）：{e}",
                "hint": "检查该 provider 配置与连通性；其余副本不受影响",
            })
    if not await _call(crud.exists, _cfg["schema"]["resource"], {"_id": resource_id}):
        await _call(crud.insert, _cfg["schema"]["resource"], {
            "_id": resource_id, "sha1": sha1, "fileName": file_name or "unnamed",
            "mime": mime or "application/octet-stream", "size": len(data), "kind": kind or "file",
        })
    if locations:
        existing = await _call(
            crud.query, _loc_gql(_cfg["schema"]["location"]), {"c0": {"resourceId": resource_id}})
        have = {r["backend"] for r in existing}
        fresh = [loc for loc in locations if loc["backend"] not in have]
        if fresh:
            await _call(crud.insert_many, _cfg["schema"]["location"], fresh)
    if bind:
        await _call(crud.insert, _cfg["schema"]["binding"], {
            "resourceId": resource_id, "businessTable": bind["businessTable"],
            "businessId": str(bind["businessId"]), "userId": str(bind["userId"]) if bind.get("userId") is not None else None,
        })
    return {"resourceId": resource_id, "sha1": sha1, "locations": locations}


def _loc_gql(schema):
    return f"{schema}($condition: @c0) {{ _id, resourceId, backend, key, status, priority }}"


async def open(resource_id, *, order=None):
    crud = _crud()
    rows = await _call(crud.query, _loc_gql(_cfg["schema"]["location"]), {"c0": {"resourceId": resource_id}})
    seq = list(order) if order else list(_pool.keys())

    def rank(b):
        return seq.index(b) if b in seq else 2 ** 31

    rows = sorted(rows, key=lambda r: (rank(r["backend"]), r.get("priority") or 0))
    last_err = None
    for loc in rows:
        if loc.get("status") == "failed":
            continue
        p = _pool.get(loc["backend"])
        if p is None:
            _emit_feedback({"type": "resource_location_provider_missing", "code": "resourceLocationProviderMissing",
                            "layer": "resource", "backend": loc["backend"], "resourceId": resource_id,
                            "message": f'副本 backend "{loc["backend"]}" 未配置 provider，跳过（id={resource_id}）',
                            "hint": "补齐 configure(providers=[...]) 或清理该副本"})
            continue
        try:
            data = await _call(p["get"], loc["key"])
            return {"bytes": data, "resourceId": resource_id, "backend": loc["backend"], "key": loc["key"]}
        except Exception as e:
            last_err = e
            _emit_feedback({"type": "resource_location_degraded", "code": "resourceLocationDegraded",
                            "layer": "resource", "backend": loc["backend"], "resourceId": resource_id,
                            "message": f'资源副本读取失败，降级到下一副本（backend={loc["backend"]}, id={resource_id}）：{e}',
                            "hint": "检查该 provider 可用性；该副本可能需要重建"})
    if last_err is not None:
        raise last_err  # 有副本行但读取失败：原样重抛（500 透传），禁改语义
    # core 稳定前缀（与 ERR_PERMISSION: / ERR_GQL_PARSE: 同构）：适配层按前缀判定 → 404，
    # 禁按中文文案匹配。仅「按 resource_id 查到零 ResourceLocation 行」时抛出。
    raise FileNotFoundError(f"ERR_RESOURCE_NOT_FOUND:资源不存在或无可读副本: {resource_id}")


async def remove(resource_id):
    crud = _crud()
    rows = await _call(crud.query, _loc_gql(_cfg["schema"]["location"]), {"c0": {"resourceId": resource_id}})
    removed = []
    for loc in rows:
        p = _pool.get(loc["backend"])
        if p is None:
            continue
        try:
            await _call(p["remove"], loc["key"])
            removed.append(loc["backend"])
        except Exception as e:
            _emit_feedback({"type": "resource_location_remove_failed", "code": "resourceLocationRemoveFailed",
                            "layer": "resource", "backend": loc["backend"], "resourceId": resource_id,
                            "message": f'资源副本删除失败（backend={loc["backend"]}, id={resource_id}）：{e}',
                            "hint": "检查该 provider 可用性；可能需要手工清理孤儿文件"})
    await _call(crud.remove, _cfg["schema"]["location"], {"resourceId": resource_id})
    await _call(crud.remove, _cfg["schema"]["resource"], {"_id": resource_id})
    return {"removed": removed}


async def url(reference, *, vars=None):
    cfg = dict(_cfg["url"])
    if vars:
        cfg["vars"] = {**(cfg.get("vars") or {}), **vars}
    if _cfg["sign"]:
        signed = await _call(_cfg["sign"], reference, vars or {})
        if isinstance(signed, dict):
            cfg["query"] = {**(cfg.get("query") or {}), **signed}
    return native.resource_compose_url(reference, cfg)
