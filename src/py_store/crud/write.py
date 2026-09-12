"""写路径 —— 单条/批量插入、更新、删除归档、存在性与计数"""

from .. import datasource as _datasource
from ..schema import core as _core
from ..schema import get as _get_schema
from .exec import _call, _ctx, _exec, _now_for
from .id import _generate_id


async def _plan_with_probe(plan_fn):
    """creator 写权限探针：先规划，若 needsProbe 则执行探针命令后重入"""
    out = plan_fn(None, None)
    if out.get('needsProbe'):
        probe_doc = await _exec(out['needsProbe'])
        out = plan_fn(probe_doc is not None, probe_doc)
    return out


async def insert(schema_name, data, route_override=None):
    """插入一条（``route_override`` 可选：多租户路由 ``{'source', 'namespace'}``）"""
    s = _get_schema(schema_name)
    plan = _call(lambda: _core.plan_insert(
        schema_name, data, _now_for(schema_name), _generate_id(s) if s['idPrefix'] else '', _ctx(),
        route_override))
    await _exec(plan['command'])
    return plan['returns']


async def insert_many(schema_name, docs, route_override=None):
    """批量插入（带权限检查，自动生成 _id 和时间戳；空数组直接返回空）"""
    if not isinstance(docs, list) or not docs:
        return []

    s = _get_schema(schema_name)
    plan = _call(lambda: _core.plan_insert_many(
        schema_name,
        docs,
        _now_for(schema_name),
        # core 按需消费（仅无 _id 的文档取用），多备无害
        [_generate_id(s) if s['idPrefix'] else '' for _ in docs],
        _ctx(),
        route_override,
    ))
    if plan.get('command'):
        await _exec(plan['command'])
    return plan['returns']


async def update(schema_name, condition, data, options=None, route_override=None):
    """
    更新一条（支持原生操作符，不触发默认值）

    data 的 key 以 '$' 开头 → 原生 MongoDB 操作符（$set/$inc/$unset 等）直接透传。
    否则自动包装为 $set 模式。``route_override`` 可选（多租户路由）。
    """
    out = await _plan_with_probe(lambda found, doc: _call(lambda: _core.plan_update(
        schema_name, condition, data, options, _now_for(schema_name), _ctx(), found, doc,
        route_override)))
    result = await _exec(out['command'])
    return _call(lambda: _core.apply_write_defaults(schema_name, result)) if result else None


async def update_many(schema_name, condition, data, route_override=None):
    """批量更新（支持原生操作符）"""
    out = _call(lambda: _core.plan_update_many(
        schema_name, condition, data, _now_for(schema_name), _ctx(), route_override))
    result = await _exec(out['command'])
    return {'modifiedCount': result.modified_count}


async def remove(schema_name, condition, route_override=None):
    """删除 —— 原表数据先归档到对应 `_deleted` 附表（附 deletedAt），再物理删除原表数据。
    归档命令带 ``upsertById``（幂等），重试不再因 _id 冲突整批失败；单一 SQL 源时
    归档+删除整体事务化（Mongo / 跨源按顺序执行，非原子边界见 README「事务边界」）"""
    out = await _plan_with_probe(lambda found, doc: _call(lambda: _core.plan_remove(
        schema_name, condition, _ctx(), found, doc, route_override)))

    async def _do_remove():
        archived_count = 0
        if out.get('findCommand'):
            docs = await _exec(out['findCommand'])
            if docs:
                arch = _call(lambda: _core.plan_archive_docs(
                    schema_name, docs, _now_for(schema_name), route_override))
                await _exec(arch['command'])
                archived_count = len(docs)

        result = await _exec(out['deleteCommand'])
        return {'deletedCount': result.deleted_count, 'archivedCount': archived_count}

    sources = {out['deleteCommand'].get('source') or _datasource.DEFAULT_SOURCE}
    if out.get('findCommand'):
        sources.add(out['findCommand'].get('source') or _datasource.DEFAULT_SOURCE)
    if len(sources) == 1:
        source = next(iter(sources))
        if _datasource.is_sql_source(source):
            return await _datasource.run_in_transaction(source, _do_remove)
    return await _do_remove()


async def exists(schema_name, condition, route_override=None):
    """判断是否存在"""
    cmd = _call(lambda: _core.plan_exists(schema_name, condition, route_override))
    doc = await _exec(cmd)
    return doc is not None


async def count(schema_name, filter=None, route_override=None):
    """统计符合条件的文档数量"""
    cmd = _call(lambda: _core.plan_count(schema_name, filter, route_override))
    return await _exec(cmd)
