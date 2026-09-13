"""Mongo 执行器（驱动：PyMongo async）

只做「Command JSON → 原生驱动调用」（铁律 1/3）：全部纯逻辑（GQL 解析、权限、
命令规划、结果后处理）都在 Rust core。对齐 ``nodejs-store/src/crud/exec.js#_execMongo``。

唯一原生边界改写：`_explicit_null` —— 把「字段 = null」的**等值条件**编译为
「字段存在且为 null」（`{$eq: null, $exists: true}`）。理由：Mongo 原生把 `{f: null}`
同时命中「值为 null」与「字段缺失」两类文档，而本 store 的三态契约（F-07/H-09）
要求 `null` 只命中显式 null、`$exists:false` 才命中缺失 —— 这与 SQL 侧
`col IS NULL`（显式 null）语义对齐。运算对象（`$ne:null`/`$exists`/`$gt`…）不改写。
"""

from pymongo import ReturnDocument


def _explicit_null(v):
    """递归改写 filter：`field: null`（标量等值）→ `field: {$eq: null, $exists: true}`。"""
    if isinstance(v, dict):
        out = {}
        for k, val in v.items():
            if k.startswith('$') and isinstance(val, (dict, list)):
                out[k] = _explicit_null(val)
            elif k.startswith('$'):
                out[k] = val
            elif isinstance(val, dict):
                out[k] = _explicit_null(val)
            elif val is None:
                out[k] = {'$eq': None, '$exists': True}
            else:
                out[k] = val
        return out
    if isinstance(v, list):
        return [_explicit_null(x) for x in v]
    return v


def _norm_filter(cmd):
    filter_ = cmd.get('filter')
    if isinstance(filter_, dict):
        cmd['filter'] = _explicit_null(filter_)


def _norm_pipeline(cmd):
    pipe = cmd.get('pipeline')
    if isinstance(pipe, list):
        for stage in pipe:
            if isinstance(stage, dict) and isinstance(stage.get('$match'), dict):
                stage['$match'] = _explicit_null(stage['$match'])


async def exec_mongo(db, cmd):
    """Command JSON → PyMongo 驱动调用"""
    coll = db[cmd['collection']]
    kind = cmd['kind']

    if kind == 'find':
        _norm_filter(cmd)
        cursor = coll.find(cmd['filter'], cmd.get('projection'))
        return await cursor.to_list(length=None)
    if kind == 'aggregate':
        # PyMongo async 下 ``aggregate`` 是协程（与 ``find`` 直接返回游标不同）
        _norm_pipeline(cmd)
        cursor = await coll.aggregate(cmd['pipeline'])
        return await cursor.to_list(length=None)
    if kind == 'countDocuments':
        _norm_filter(cmd)
        return await coll.count_documents(cmd['filter'])
    if kind == 'findOne':
        _norm_filter(cmd)
        return await coll.find_one(cmd['filter'], cmd.get('projection'))
    if kind == 'insertOne':
        await coll.insert_one(cmd['doc'])
        return cmd['doc']
    if kind == 'insertMany':
        if cmd.get('upsertById'):
            # 归档幂等（core plan_archive_docs）：按 _id 逐条覆盖 —— 「归档成功但删除失败」
            # 的重试不再因 _id 冲突整批失败。SQL 侧由 dialect 的 ON CONFLICT/REPLACE 承接。
            for doc in cmd['docs']:
                await coll.replace_one({'_id': doc['_id']}, doc, upsert=True)
            return {'insertedCount': len(cmd['docs'])}
        await coll.insert_many(cmd['docs'])
        return {'insertedCount': len(cmd['docs'])}
    if kind == 'findOneAndUpdate':
        _norm_filter(cmd)
        options = dict(cmd.get('options') or {})
        rd = options.pop('returnDocument', 'after')
        options['return_document'] = (
            ReturnDocument.AFTER if rd == 'after' else ReturnDocument.BEFORE)
        return await coll.find_one_and_update(cmd['filter'], cmd['update'], **options)
    if kind == 'updateMany':
        _norm_filter(cmd)
        return await coll.update_many(cmd['filter'], cmd['update'])
    if kind == 'deleteMany':
        _norm_filter(cmd)
        return await coll.delete_many(cmd['filter'])

    raise RuntimeError(f'未支持的命令: {kind}')
