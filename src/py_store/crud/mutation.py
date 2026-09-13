"""Mutation / Upsert / 原生聚合 —— 规划步骤序列 → 依序执行 + 父子 _id 占位符回填"""

from .. import datasource as _datasource
from ..schema import core as _core
from ..schema import get as _get_schema
from .exec import _call, _ctx, _exec, _now_for, resolve_placeholders
from .id import _generate_id, _new_id_pool


async def _mutation_one(schema_name, data, route_override=None):
    """mutation 单条：规划步骤序列 → 依序执行 + 父子 _id 占位符回填"""
    plan = _call(lambda: _core.plan_mutation(
        schema_name, data, _now_for(schema_name), _new_id_pool(schema_name, data), _ctx(),
        route_override))

    async def _run_steps():
        resolved: list = []
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

    # 单一 SQL 源 → 步骤序列整体事务化（同连接同事务，任一步失败整体回滚）；
    # Mongo 源 / 跨源步骤按原样顺序执行（非原子边界见 README「事务边界」）
    sources = {(s['command'].get('source') or _datasource.DEFAULT_SOURCE) for s in plan['steps']}
    if len(sources) == 1:
        source = next(iter(sources))
        if _datasource.is_sql_source(source):
            return await _datasource.run_in_transaction(source, _run_steps)
    return await _run_steps()


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
    cmd = _call(lambda: _core.plan_aggregate(schema_name, pipeline or [], _ctx(), route_override))
    return await _exec(cmd)
