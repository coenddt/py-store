"""最小复现（Python 宿主，真实 MongoDB）：`$pipeline` 直通已移除后的验收。

与 nodejs-store/tmp/repro-pipeline.cjs 一一对应，用于确认两个宿主共用同一 Rust core、
行为零漂移：

① 含 `$pipeline` 的 GQL 必须显式报错
② 不含 `$pipeline`：投影零回归（只请求 `_id` 就只返回 `_id`）
③ 不含 `$pipeline`：计算列零回归
④ `$group` / `$having` 零回归
⑤ 关系聚合谓词（semi-join）零回归
"""

import asyncio

from pymongo import AsyncMongoClient

from py_store import init, store

MONGO_URI = 'mongodb://127.0.0.1:27017'
MONGO_DB = 'repro_pipeline'


def _register():
    store.register({
        'name': 'AuditLog',
        'collection': 'audit_logs',
        'idPrefix': 'AL',
        'timestamps': False,
        'fields': {
            '_id': {'type': 'string'},
            'action': {'type': 'string'},
            'module': {'type': 'string'},
            'userId': {'type': 'string'},
        },
        'relations': {},
        'computes': {
            'actionLabel': {
                'type': 'string',
                'depends': ['action'],
                'fn': lambda item: '登录' if item.get('action') == 'login' else '其他',
            },
        },
    })
    store.register({
        'name': 'User',
        'collection': 'users',
        'idPrefix': 'U',
        'timestamps': False,
        'fields': {
            '_id': {'type': 'string'},
            'name': {'type': 'string'},
        },
        'relations': {
            'logs': {
                'model': 'AuditLog',
                'type': 'many',
                'localField': '_id',
                'foreignField': 'userId',
            },
        },
    })


async def main():
    client = AsyncMongoClient(MONGO_URI)
    db = client[MONGO_DB]
    # py 宿主会把 collection 名归一到 camelCase（audit_logs → auditLogs），
    # 两种命名都清一次，保证重复运行幂等
    for name in ('audit_logs', 'auditLogs'):
        await db[name].delete_many({})
    await db['users'].delete_many({})

    await init({'default': db})
    _register()

    await store.insert('User', {'_id': 'u1', 'name': 'A'})
    await store.insert('AuditLog', {'action': 'login', 'module': 'auth', 'userId': 'u1'})
    await store.insert('AuditLog', {'action': 'logout', 'module': 'auth', 'userId': 'u1'})

    # ① 含 $pipeline → 必须显式报错
    err = None
    try:
        await store.query('AuditLog($pipeline:@p){ _id }', {'p': [{'$match': {'module': '__none__'}}]})
    except Exception as exc:  # noqa: BLE001 - 断言需要捕获任意错误
        err = exc
    assert err is not None, '① 含 $pipeline 的 GQL 必须显式报错'
    msg = str(err)
    assert 'ERR_GQL_PARSE' in msg, f'① 应带 ERR_GQL_PARSE 前缀: {msg}'
    assert '$pipeline' in msg, f'① 应点明 $pipeline: {msg}'

    # ①b 带根参数的变体同样显式报错
    err = None
    try:
        await store.query(
            'AuditLog($condition:@c,$pipeline:@p){ _id }',
            {'c': {}, 'p': [{'$match': {'module': 'auth'}}]},
        )
    except Exception as exc:  # noqa: BLE001
        err = exc
    assert err is not None, '①b 含 $pipeline + $condition 的 GQL 必须显式报错'
    assert 'ERR_GQL_PARSE' in str(err), f'①b 应带 ERR_GQL_PARSE 前缀: {err}'

    # ② 投影零回归
    only_id = await store.query('AuditLog{ _id }', {})
    assert len(only_id) == 2, f'② 期望 2 行: {only_id}'
    assert sorted(only_id[0].keys()) == ['_id'], f'② 只请求 _id 应只返回 _id: {only_id[0]}'

    # ③ 计算列零回归
    rows = await store.query('AuditLog{ _id, action, actionLabel }', {})
    login = next((r for r in rows if r.get('action') == 'login'), None)
    assert login is not None, f'③ 应能取到 login 行: {rows}'
    assert login.get('actionLabel') == '登录', f'③ 计算列应求值: {login}'

    # ④ $group / $having 零回归
    grouped = await store.query(
        'AuditLog($group:@g,$having:@h){ module, n }',
        {
            'g': {'by': ['module'], 'agg': {'n': {'$count': '*'}}},
            'h': {'n': {'$gt': 1}},
        },
    )
    assert len(grouped) == 1, f'④ 期望 1 组: {grouped}'
    assert int(grouped[0]['n']) == 2, f'④ 计数应为 2: {grouped[0]}'

    # ⑤ 关系聚合谓词零回归
    users = await store.query('User($condition:@c){ _id }', {'c': {'logs': {'$count': {'$gt': 0}}}})
    assert [u['_id'] for u in users] == ['u1'], f'⑤ 关系谓词结果不符: {users}'

    print('repro OK: ① 显式报错 ② 投影 ③ 计算列 ④ $group/$having ⑤ 关系谓词 全部通过')
    await client.close()


if __name__ == '__main__':
    asyncio.run(main())
