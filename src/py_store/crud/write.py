"""写路径 —— 单条/批量插入、更新、删除归档、存在性与计数"""

from ..schema import core as _core
from ..schema import get as _get_schema
from .exec import _call, _ctx, _exec, _now_for, _sources_of, run_atomic
from .id import _generate_id


async def _plan_with_probe(plan_fn):
    """creator 写权限探针：先规划，若 needsProbe 则执行探针命令后重入"""
    out = plan_fn(None, None)
    if out.get('needsProbe'):
        probe_doc = await _exec(out['needsProbe'])
        out = plan_fn(probe_doc is not None, probe_doc)
    return out


async def insert(schema_name, data, route_override=None):
    """插入一条（``route_override`` 可选：多租户路由 ``{'source', 'database', 'schema'}``）"""
    s = _get_schema(schema_name)
    plan = _call(lambda: _core.plan_insert(
        schema_name, data, _now_for(schema_name), _generate_id(s) if s['idPrefix'] else '', _ctx(),
        route_override))
    result = await _exec(plan['command'])
    returns = plan['returns']
    # 阶段2：autoincrement 主键 —— 执行器已回读自增值，returns 补 `_id`
    if isinstance(returns, dict) and not returns.get('_id')             and isinstance(result, dict) and result.get('_id') is not None:
        returns = {**returns, '_id': result['_id']}
    return returns


async def insert_many(schema_name, docs, route_override=None):
    """批量插入（带权限检查，自动生成 _id 和时间戳；空数组直接返回空）"""
    if not isinstance(docs, list) or not docs:
        return []

    s = _get_schema(schema_name)
    # 阶段2（no-error-masking）：autoincrement 的批量自增值回读不可靠（MySQL 批量
    # lastrowid 仅首行、且并发插入会留间隙）→ 显式报错，不静默产出错误 _id
    id_fdef = (s.get('fields') or {}).get('_id') or {}
    if id_fdef.get('strategy') == 'autoincrement' and any(not (d or {}).get('_id') for d in docs):
        raise ValueError(
            'AUTOINCREMENT_NOT_SUPPORTED: insert_many 不支持 autoincrement schema'
            '（批量自增值回读不可靠）；请逐条 insert 或显式提供 _id')
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

    「权限探针 + 写」整体纳入同一原子作用域（``run_atomic``）：单一 SQL 源时探针与写
    同连接同事务，消除二者之间的并发窗口；``now`` 只取一次，两次规划共用（调用级确定性）。
    """
    now = _now_for(schema_name)
    ctx = _ctx()
    first = _call(lambda: _core.plan_update(
        schema_name, condition, data, options, now, ctx, None, None, route_override))
    sources = _sources_of(first)

    async def _do():
        out = first
        if out.get('needsProbe'):
            probe_doc = await _exec(out['needsProbe'])
            out = _call(lambda: _core.plan_update(
                schema_name, condition, data, options, now, ctx,
                probe_doc is not None, probe_doc, route_override))
        result = await _exec(out['command'])
        return _call(lambda: _core.apply_write_defaults(schema_name, result)) if result else None

    return await run_atomic(sources, _do)


async def _exec_with_pre(command):
    """执行带 ``preCommand`` 的命令（阶段1：mutation 关系谓词归一）。

    preCommand（aggregate 取命中 `_id`）先行执行，把结果 `_id` 列表回填进主命令
    filter 的 `_id.$in` 占位（core 规划注入 ``"__REL_PRED_IDS__"``）。空集 → `$in: []`，
    各后端语义一致 = 不命中任何行。同批可传 ``extra_targets``：其他携带同一占位的
    命令（如 remove 的归档 findCommand）一并回填，避免二次执行 preCommand。"""
    pre = command.get('preCommand')
    if not pre:
        return await _exec(command)
    ids = []
    for doc in await _exec(pre):
        if isinstance(doc, dict) and '_id' in doc:
            ids.append(doc['_id'])
    return await _fill_pre_ids(command, ids)


def _fill_pre_ids(command, ids):
    """把 preCommand 取得的 `_id` 列表回填进命令 filter 的 `$in` 占位（递归查找后执行）。

    core 注入的占位可能位于 `$and` 数组内（改写条件已有其他键时），故递归遍历。"""
    main = {k: v for k, v in command.items() if k != 'preCommand'}

    def _walk(node):
        if isinstance(node, dict):
            for k, v in list(node.items()):
                if isinstance(v, dict) and v.get('$in') == '__REL_PRED_IDS__':
                    node[k] = {'$in': list(ids)}
                else:
                    _walk(v)
        elif isinstance(node, list):
            for it in node:
                _walk(it)

    if 'filter' in main:
        _walk(main['filter'])
    return _exec(main)


async def update_many(schema_name, condition, data, route_override=None):
    """批量更新（支持原生操作符）"""
    out = _call(lambda: _core.plan_update_many(
        schema_name, condition, data, _now_for(schema_name), _ctx(), route_override))
    result = await _exec_with_pre(out['command'])
    return {'modifiedCount': result.modified_count}


async def remove(schema_name, condition, route_override=None):
    """删除 —— 原表数据先归档到对应 `_deleted` 附表（附 deletedAt），再物理删除原表数据。
    归档命令带 ``upsertById``（幂等），重试不再因 _id 冲突整批失败；单一 SQL 源时
    归档+删除整体事务化（Mongo / 跨源按顺序执行，非原子边界见 README「事务边界」）"""
    out = await _plan_with_probe(lambda found, doc: _call(lambda: _core.plan_remove(
        schema_name, condition, _ctx(), found, doc, route_override)))

    async def _do_remove():
        archived_count = 0
        # 关系谓词：先执行 deleteCommand.preCommand 取命中 _id（归档 find 与删除共用同一列表）
        pre = (out.get('deleteCommand') or {}).get('preCommand')
        ids = None
        if pre:
            ids = [d['_id'] for d in await _exec(pre) if isinstance(d, dict) and '_id' in d]
        if out.get('findCommand'):
            find_cmd = out['findCommand']
            docs = await (_fill_pre_ids(find_cmd, ids) if ids is not None else _exec(find_cmd))
            if docs:
                arch = _call(lambda: _core.plan_archive_docs(
                    schema_name, docs, _now_for(schema_name), route_override))
                await _exec(arch['command'])
                archived_count = len(docs)

        result = await (_fill_pre_ids(out['deleteCommand'], ids)
                        if ids is not None else _exec(out['deleteCommand']))
        return {'deletedCount': result.deleted_count, 'archivedCount': archived_count}

    sources = _sources_of(out)
    return await run_atomic(sources, _do_remove)


async def exists(schema_name, condition, route_override=None):
    """判断是否存在"""
    cmd = _call(lambda: _core.plan_exists(schema_name, condition, route_override))
    doc = await _exec(cmd)
    return doc is not None


async def count(schema_name, filter=None, route_override=None):
    """统计符合条件的文档数量"""
    cmd = _call(lambda: _core.plan_count(schema_name, filter, _ctx(), route_override))
    return await _exec(cmd)
