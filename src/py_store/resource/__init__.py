"""资源能力（宿主旁路，B 档）—— provider 注册表 + put/open/remove/url。

铁律（与 node 端同构）：
  1. 元数据走普通 CRUD（Resource/ResourceLocation/ResourceBinding 三个 schema）；
  2. 字节 IO 只经 provider（唯一 IO 边界）；
  3. 表名（schema）与字段名（fields）均可由业务自定义：表名经 ``configure({"schema": ...})``，
     字段名经 ``configure({"fields": ...})`` 的「逻辑角色 → 物理字段」映射。缺省 = canonical。
"""
from __future__ import annotations

import copy
import hashlib
import inspect
from typing import Any

from ..core import native
from ..feedback import emit as _emit_feedback
from . import providers

_DEFAULT_SCHEMA = {"resource": "Resource", "location": "ResourceLocation", "binding": "ResourceBinding"}

# 逻辑角色 → 物理字段名（canonical 缺省）。业务可经 configure_resource({"fields": ...}) 覆盖，
# 使资源三表的字段结构由业务 schema 自定义；缺省 = canonical（向后逐字节兼容）。
_DEFAULT_FIELDS = {
    "resource": {"sha1": "sha1", "fileName": "fileName", "mime": "mime", "size": "size", "kind": "kind"},
    "location": {"resourceId": "resourceId", "backend": "backend", "key": "key",
                 "status": "status", "priority": "priority"},
    "binding": {"resourceId": "resourceId", "businessTable": "businessTable",
                "businessId": "businessId", "userId": "userId"},
}

# 必填角色：值为 null / 非空字符串之外的任何值 → configure 抛错（该列缺失则引擎无法定位）
_REQUIRED_ROLES = {
    "resource": ("sha1",),
    "location": ("resourceId", "backend", "key"),
    "binding": ("resourceId", "businessTable", "businessId"),
}


def _parse_fields(raw=None):
    """解析并校验 ``cfg["fields"]``：未知表 / 未知角色 → 抛错；
    必填角色须为非空字符串；可选角色为「非空字符串」或 ``None``（``None`` = 跳过该列）。
    """
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        raise ValueError("configure_resource: fields 须为对象")
    for table in raw:
        if table not in _DEFAULT_FIELDS:
            raise ValueError(f"configure_resource: fields 未知表: {table}")
    out = {}
    for table, defaults in _DEFAULT_FIELDS.items():
        given = raw.get(table) or {}
        if not isinstance(given, dict):
            raise ValueError(f"configure_resource: fields.{table} 须为对象")
        for role in given:
            if role not in defaults:
                raise ValueError(f"configure_resource: fields.{table} 未知角色: {role}")
        mapped = {**defaults, **given}
        for role, v in mapped.items():
            is_str = isinstance(v, str) and v != ""
            if role in _REQUIRED_ROLES[table]:
                if not is_str:
                    raise ValueError(f"configure_resource: fields.{table}.{role} 为必填角色，须为非空字符串")
            elif v is not None and not is_str:
                raise ValueError(f"configure_resource: fields.{table}.{role} 须为非空字符串或 null")
        out[table] = mapped
    return out


_cfg: dict[str, Any] = {
    "schema": dict(_DEFAULT_SCHEMA),
    "fields": _parse_fields({}),
    "store": None,
    "providers": [],
    "url": {},
    "sign": None,
}
_pool: dict = {}

_BYTES_TYPES = (bytes, bytearray)


def configure(cfg=None):
    global _cfg, _pool
    cfg = cfg or {}
    _cfg = {
        "schema": {**_DEFAULT_SCHEMA, **(cfg.get("schema") or {})},
        "fields": _parse_fields(cfg.get("fields") or {}),
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


def fields():
    """当前生效字段映射（深拷贝；只读快照）"""
    return copy.deepcopy(_cfg["fields"])


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


def _loc_row(resource_id, backend, key, status, priority):
    """location 落库行：仅写映射到的列（``None`` 角色跳过），``_id`` 由 store 生成"""
    f = _cfg["fields"]["location"]
    row = {}
    if f["resourceId"]:
        row[f["resourceId"]] = resource_id
    if f["backend"]:
        row[f["backend"]] = backend
    if f["key"]:
        row[f["key"]] = key
    if f["status"]:
        row[f["status"]] = status
    if f["priority"]:
        row[f["priority"]] = priority
    return row


def _loc_gql(schema):
    """location 读投影 + 条件键：按映射生成（``_id`` 恒含）"""
    f = _cfg["fields"]["location"]
    cols = ["_id"] + [v for v in f.values() if isinstance(v, str) and v != ""]
    return f"{schema}($condition: @c0) {{ {', '.join(dict.fromkeys(cols))} }}"


def _meta_gql(schema):
    """Resource 元数据读投影：``_id`` + 映射到的 fileName/mime"""
    f = _cfg["fields"]["resource"]
    cols = ["_id"]
    if f["fileName"]:
        cols.append(f["fileName"])
    if f["mime"]:
        cols.append(f["mime"])
    return f"{schema}($condition: @c0) {{ {', '.join(dict.fromkeys(cols))} }}"


async def put(*, bytes=None, file_name=None, mime=None, kind=None, bind=None):
    if bytes is None:
        raise ValueError("resource.put 需要 bytes")
    data = bytes if isinstance(bytes, _BYTES_TYPES) else str(bytes).encode("utf-8")
    crud = _crud()
    f = _cfg["fields"]
    sha1 = hashlib.sha1(data).hexdigest()
    resource_id = sha1
    key = native.resource_content_path(sha1)

    locations = []          # 返回用（canonical 键，API 契约不变）
    location_rows = []      # 落库用（按映射列名）
    for backend, p in _pool.items():
        try:
            await _call(p["put"], key, data, {"mime": mime})
            locations.append({"resourceId": resource_id, "backend": backend, "key": key,
                              "status": "ok", "priority": p["priority"]})
            location_rows.append(_loc_row(resource_id, backend, key, "ok", p["priority"]))
        except Exception as e:  # 允许部分失败，但必须反馈（禁静默）
            locations.append({"resourceId": resource_id, "backend": backend, "key": key,
                              "status": "failed", "priority": p["priority"]})
            location_rows.append(_loc_row(resource_id, backend, key, "failed", p["priority"]))
            _emit_feedback({
                "type": "resource_location_write_failed", "code": "resourceLocationWriteFailed",
                "layer": "resource", "backend": backend, "resourceId": resource_id,
                "message": f"资源副本写入失败（backend={backend}, id={resource_id}）：{e}",
                "hint": "检查该 provider 配置与连通性；其余副本不受影响",
            })
    if not await _call(crud.exists, _cfg["schema"]["resource"], {"_id": resource_id}):
        row: dict[str, Any] = {"_id": resource_id}
        if f["resource"]["sha1"]:
            row[f["resource"]["sha1"]] = sha1
        if f["resource"]["fileName"]:
            row[f["resource"]["fileName"]] = file_name or "unnamed"
        if f["resource"]["mime"]:
            row[f["resource"]["mime"]] = mime or "application/octet-stream"
        if f["resource"]["size"]:
            row[f["resource"]["size"]] = len(data)
        if f["resource"]["kind"]:
            row[f["resource"]["kind"]] = kind or "file"
        await _call(crud.insert, _cfg["schema"]["resource"], row)
    if location_rows:
        existing = await _call(
            crud.query, _loc_gql(_cfg["schema"]["location"]), {"c0": {f["location"]["resourceId"]: resource_id}})
        have = {r[f["location"]["backend"]] for r in existing}
        fresh = [row for row in location_rows if row[f["location"]["backend"]] not in have]
        if fresh:
            await _call(crud.insert_many, _cfg["schema"]["location"], fresh)
    if bind:
        b: dict[str, Any] = {}
        if f["binding"]["resourceId"]:
            b[f["binding"]["resourceId"]] = resource_id
        if f["binding"]["businessTable"]:
            b[f["binding"]["businessTable"]] = bind["businessTable"]
        if f["binding"]["businessId"]:
            b[f["binding"]["businessId"]] = str(bind["businessId"])
        if f["binding"]["userId"]:
            b[f["binding"]["userId"]] = str(bind["userId"]) if bind.get("userId") is not None else None
        await _call(crud.insert, _cfg["schema"]["binding"], b)
    return {"resourceId": resource_id, "sha1": sha1, "locations": locations}


async def open(resource_id, *, order=None):
    crud = _crud()
    f = _cfg["fields"]
    rows = await _call(crud.query, _loc_gql(_cfg["schema"]["location"]),
                       {"c0": {f["location"]["resourceId"]: resource_id}})
    seq = list(order) if order else list(_pool.keys())

    def rank(b):
        return seq.index(b) if b in seq else 2 ** 31

    def backend_of(r):
        return r[f["location"]["backend"]]

    def prio_of(r):
        return (r.get(f["location"]["priority"]) or 0) if f["location"]["priority"] else 0

    rows = sorted(rows, key=lambda r: (rank(backend_of(r)), prio_of(r)))
    last_err = None
    for loc in rows:
        backend = backend_of(loc)
        if f["location"]["status"] and loc.get(f["location"]["status"]) == "failed":
            continue
        p = _pool.get(backend)
        if p is None:
            _emit_feedback({"type": "resource_location_provider_missing", "code": "resourceLocationProviderMissing",
                            "layer": "resource", "backend": backend, "resourceId": resource_id,
                            "message": f'副本 backend "{backend}" 未配置 provider，跳过（id={resource_id}）',
                            "hint": "补齐 configure(providers=[...]) 或清理该副本"})
            continue
        key = loc[f["location"]["key"]]
        try:
            data = await _call(p["get"], key)
        except Exception as e:
            last_err = e
            _emit_feedback({"type": "resource_location_degraded", "code": "resourceLocationDegraded",
                            "layer": "resource", "backend": backend, "resourceId": resource_id,
                            "message": f"资源副本读取失败，降级到下一副本（backend={backend}, id={resource_id}）：{e}",
                            "hint": "检查该 provider 可用性；该副本可能需要重建"})
            continue
        # 字节成功 → 读元数据（读失败不降级：Resource 未注册/无权限时响亮抛出，语义清晰）
        meta = await _call(crud.query_one, _meta_gql(_cfg["schema"]["resource"]), {"c0": {"_id": resource_id}})
        file_name = None
        mime = None
        if meta:
            if f["resource"]["fileName"]:
                file_name = meta.get(f["resource"]["fileName"])
            if f["resource"]["mime"]:
                mime = meta.get(f["resource"]["mime"])
        else:
            _emit_feedback({"type": "resource_meta_missing", "code": "resourceMetaMissing",
                            "layer": "resource", "resourceId": resource_id,
                            "message": f"资源元数据缺失（{_cfg['schema']['resource']} 无 _id={resource_id} 行）：open 仍返回字节",
                            "hint": "检查资源表是否被外部清理；fileName/mime 将回落调用方兜底值"})
        return {"bytes": data, "resourceId": resource_id, "backend": backend, "key": key,
                "fileName": file_name, "mime": mime}
    if last_err is not None:
        raise last_err  # 有副本行但读取失败：原样重抛（500 透传），禁改语义
    # core 稳定前缀（与 ERR_PERMISSION: / ERR_GQL_PARSE: 同构）：适配层按前缀判定 → 404，
    # 禁按中文文案匹配。仅「按 resource_id 查到零 ResourceLocation 行」时抛出。
    raise FileNotFoundError(f"ERR_RESOURCE_NOT_FOUND:资源不存在或无可读副本: {resource_id}")


async def remove(resource_id):
    crud = _crud()
    f = _cfg["fields"]
    rows = await _call(crud.query, _loc_gql(_cfg["schema"]["location"]),
                       {"c0": {f["location"]["resourceId"]: resource_id}})
    removed = []
    for loc in rows:
        backend = loc[f["location"]["backend"]]
        p = _pool.get(backend)
        if p is None:
            continue
        try:
            await _call(p["remove"], loc[f["location"]["key"]])
            removed.append(backend)
        except Exception as e:
            _emit_feedback({"type": "resource_location_remove_failed", "code": "resourceLocationRemoveFailed",
                            "layer": "resource", "backend": backend, "resourceId": resource_id,
                            "message": f"资源副本删除失败（backend={backend}, id={resource_id}）：{e}",
                            "hint": "检查该 provider 可用性；可能需要手工清理孤儿文件"})
    await _call(crud.remove, _cfg["schema"]["location"], {f["location"]["resourceId"]: resource_id})
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
