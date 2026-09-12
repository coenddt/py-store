"""写路径 —— 单条/批量插入、更新、删除归档、存在性与计数"""

from ..schema import core as _core, get as _get_schema
from .exec import _call, _ctx, _exec, _now_for
from .id import _generate_id


async def _plan_with_probe(plan_fn):
    """creator 写权限探针：先规划，若 needsProbe 则执行探针命令后重入"""
    out = plan_fn(None, None)
    if out.get('needsProbe'):
        probe_doc = await _exec(out['needsProbe'])
        out = plan_fn(probe_doc is not None, probe_doc)
    return out


async def insert(schema_name, data):
    """插入一条"""
    s = _get_schema(schema_name)
    plan = _call(lambda: _core.plan_insert(
        schema_name, data, _now_for(schema_name), _generate_id(s) if s['idPrefix'] else '', _ctx()))
    await _exec(plan['command'])
    return plan['returns']


async def insert_many(schema_name, docs):
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
    ))
    if plan.get('command'):
        await _exec(plan['command'])
    return plan['returns']


async def update(schema_name, condition, data, options=None):
    """
    更新一条（支持原生操作符，不触发默认值）

    data 的 key 以 '$' 开头 → 原生 MongoDB 操作符（$set/$inc/$unset 等）直接透传。
    否则自动包装为 $set 模式。
    """
    out = await _plan_with_probe(lambda found, doc: _call(lambda: _core.plan_update(
        schema_name, condition, data, options, _now_for(schema_name), _ctx(), found, doc)))
    result = await _exec(out['command'])
    return _call(lambda: _core.apply_write_defaults(schema_name, result)) if result else None


async def update_many(schema_name, condition, data):
    """批量更新（支持原生操作符）"""
    out = _call(lambda: _core.plan_update_many(
        schema_name, condition, data, _now_for(schema_name), _ctx()))
    result = await _exec(out['command'])
    return {'modifiedCount': result.modified_count}


async def remove(schema_name, condition):
    """删除 —— 原表数据先归档到对应 `_deleted` 附表（附 deletedAt），再物理删除原表数据"""
    out = await _plan_with_probe(lambda found, doc: _call(lambda: _core.plan_remove(
        schema_name, condition, _ctx(), found, doc)))

    archived_count = 0
    if out.get('findCommand'):
        docs = await _exec(out['findCommand'])
        if docs:
            arch = _call(lambda: _core.plan_archive_docs(schema_name, docs, _now_for(schema_name)))
            await _exec(arch['command'])
            archived_count = len(docs)

    result = await _exec(out['deleteCommand'])
    return {'deletedCount': result.deleted_count, 'archivedCount': archived_count}


async def exists(schema_name, condition):
    """判断是否存在"""
    cmd = _call(lambda: _core.plan_exists(schema_name, condition))
    doc = await _exec(cmd)
    return doc is not None


async def count(schema_name, filter=None):
    """统计符合条件的文档数量"""
    cmd = _call(lambda: _core.plan_count(schema_name, filter))
    return await _exec(cmd)
