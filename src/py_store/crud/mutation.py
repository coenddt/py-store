"""Mutation / Upsert / 原生聚合 —— 规划步骤序列 → 依序执行 + 父子 _id 占位符回填"""

from ..schema import core as _core, get as _get_schema
from .exec import _call, _ctx, _exec, _now_for, resolve_placeholders
from .id import _generate_id, _new_id_pool


async def _mutation_one(schema_name, data, route_override=None):
    """mutation 单条：规划步骤序列 → 依序执行 + 父子 _id 占位符回填"""
    plan = _call(lambda: _core.plan_mutation(
        schema_name, data, _now_for(schema_name), _new_id_pool(schema_name, data), _ctx(),
        route_override))

    resolved = []
    root_result = None
    for i, step in enumerate(plan['steps']):
        cmd = resolve_placeholders(step['command'], steps=resolved)
        result = await _exec(cmd)
        resolved.append(result.get('_id') if result else None)
        if i == 0:
            root_result = result  # 首步即根写入

    if not root_result:
        return None
    return _call(lambda: _core.apply_write_defaults(schema_name, root_result))


async def mutation(schema_name, data, route_override=None):
    """
    mutation — 智能持久化

    自动判断 upsert/insert，支持父子文档关联填充。
    ``route_override`` 可选：多租户路由 ``{'source', 'namespace'}``。
    """
    is_array = isinstance(data, list)
    items = data if is_array else [data]

    if not items:
        return [] if is_array else None

    results = []
    for item in items:
        results.append(await _mutation_one(schema_name, item, route_override))

    return results if is_array else results[0]


async def upsert(schema_name, condition, data, options=None, route_override=None):
    """
    upsert — 显式条件 upsert

    与 mutation 不同，upsert 需要调用方显式提供 match 条件，不处理父子关系。
    """
    s = _get_schema(schema_name)
    plan = _call(lambda: _core.plan_upsert(
        schema_name, condition, data, options, _now_for(schema_name),
        _generate_id(s) if s['idPrefix'] else '', _ctx(), route_override))
    result = await _exec(plan['command'])
    return _call(lambda: _core.apply_write_defaults(schema_name, result)) if result else None


async def aggregate(schema_name, pipeline, route_override=None):
    """对指定 schema 执行 MongoDB 原生聚合查询"""
    cmd = _call(lambda: _core.plan_aggregate(schema_name, pipeline or [], route_override))
    return await _exec(cmd)
