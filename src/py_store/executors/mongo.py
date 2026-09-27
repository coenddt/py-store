"""Mongo 执行器（驱动：PyMongo async）

只做「Command JSON → 原生驱动调用」（铁律 1/3）：全部纯逻辑（GQL 解析、权限、
命令规划、结果后处理）都在 Rust core。对齐 ``nodejs-store/src/crud/exec.js#_execMongo``。

唯一原生边界改写：`_explicit_null` —— 把「字段 = null」的**等值条件**编译为
「字段存在且为 null」（`{$eq: null, $exists: true}`）。理由：Mongo 原生把 `{f: null}`
同时命中「值为 null」与「字段缺失」两类文档，而本 store 的三态契约（F-07/H-09）
要求 `null` 只命中显式 null、`$exists:false` 才命中缺失 —— 这与 SQL 侧
`col IS NULL`（显式 null）语义对齐。运算对象（`$ne:null`/`$exists`/`$gt`…）不改写。

Mongo session 事务：本模块只提供原语 `open_transaction`（`start_session` +
`start_transaction`）与 `exec_mongo(..., session=...)` 透传；事务的编排（提交/回滚/
降级声明）由 datasource 层负责。Mongo **无** `SAVEPOINT` 原语，故不提供保存点系列。
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
            elif isinstance(val, (dict, list)):
                # 与 nodejs-store/src/executors/mongo.js#_explicitNull 对齐：
                # 非 `$` 键的对象/数组值一律递归下钻（否则数组内嵌套对象的显式 null 不会被改写 → 三端 parity 破）
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


def _client_of(connection):
    """Mongo 连接的 client：db 实例取 ``.client``；MongoClient 返回自身

    本模块不 import ``datasource``（会被 ``datasource`` 反向 import，形成环），
    故在此自带最小判定。对齐 ``nodejs-store/src/executors/mongo.js#_clientOf``。
    """
    if hasattr(connection, 'get_database') and not hasattr(connection, 'get_collection'):
        return connection          # MongoClient
    return connection.client       # Database.client


def _opts(session, **kw):
    """session 非 None 时注入 ``session`` 关键字；None 时保持原调用形态（零回归）"""
    if session is not None:
        kw['session'] = session
    return kw


async def open_transaction(connection):
    """Mongo 事务句柄：start_session + start_transaction

      - ``commit`` / ``rollback`` 幂等（closed 标志）；``release`` 结束 session；
      - **无保存点原语** —— Mongo 不支持 ``SAVEPOINT``；嵌套作用域由 datasource
        层走降级声明（``nested_savepoint_unsupported``），不在此伪造。
    """
    client = _client_of(connection)
    # PyMongo async：``start_session`` 为同步方法，返回 AsyncClientSession；
    # ``start_transaction`` / ``commit_transaction`` / ``abort_transaction`` / ``end_session`` 为协程。
    session = client.start_session()
    await session.start_transaction()
    closed = False

    async def commit():
        nonlocal closed
        if closed:
            return
        closed = True
        await session.commit_transaction()

    async def rollback():
        nonlocal closed
        if closed:
            return
        closed = True
        await session.abort_transaction()

    async def release():
        await session.end_session()

    return {'session': session, 'commit': commit, 'rollback': rollback, 'release': release}


async def exec_mongo(db, cmd, session=None):
    """Command JSON → PyMongo 驱动调用（session 非 None 时全部操作携带该 session）"""
    coll = db[cmd['collection']]
    kind = cmd['kind']

    if kind == 'find':
        _norm_filter(cmd)
        cursor = coll.find(cmd['filter'], cmd.get('projection'), **_opts(session))
        return await cursor.to_list(length=None)
    if kind == 'aggregate':
        # PyMongo async 下 ``aggregate`` 是协程（与 ``find`` 直接返回游标不同）
        _norm_pipeline(cmd)
        cursor = await coll.aggregate(cmd['pipeline'], **_opts(session))
        return await cursor.to_list(length=None)
    if kind == 'countDocuments':
        _norm_filter(cmd)
        return await coll.count_documents(cmd['filter'], **_opts(session))
    if kind == 'findOne':
        _norm_filter(cmd)
        return await coll.find_one(cmd['filter'], cmd.get('projection'), **_opts(session))
    if kind == 'insertOne':
        await coll.insert_one(cmd['doc'], **_opts(session))
        return cmd['doc']
    if kind == 'insertMany':
        if cmd.get('upsertById'):
            # 归档幂等（core plan_archive_docs）：按 _id 逐条覆盖 —— 「归档成功但删除失败」
            # 的重试不再因 _id 冲突整批失败。SQL 侧由 dialect 的 ON CONFLICT/REPLACE 承接。
            for doc in cmd['docs']:
                await coll.replace_one({'_id': doc['_id']}, doc, **_opts(session, upsert=True))
            return {'insertedCount': len(cmd['docs'])}
        await coll.insert_many(cmd['docs'], **_opts(session))
        return {'insertedCount': len(cmd['docs'])}
    if kind == 'findOneAndUpdate':
        _norm_filter(cmd)
        options = dict(cmd.get('options') or {})
        rd = options.pop('returnDocument', 'after')
        options['return_document'] = (
            ReturnDocument.AFTER if rd == 'after' else ReturnDocument.BEFORE)
        options.update(_opts(session))
        return await coll.find_one_and_update(cmd['filter'], cmd['update'], **options)
    if kind == 'updateMany':
        _norm_filter(cmd)
        return await coll.update_many(cmd['filter'], cmd['update'], **_opts(session))
    if kind == 'deleteMany':
        _norm_filter(cmd)
        return await coll.delete_many(cmd['filter'], **_opts(session))

    raise RuntimeError(f'未支持的命令: {kind}')
