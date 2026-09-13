"""Phase 5 · A5/A6：跨库联邦查询端到端（Python 侧）

对齐 nodejs-store/tests/federation-e2e.test.js。

A5：一条 GQL 跨源取数（根 `mongo_e2e` → 子 `mysql_e2e`）出嵌套结果；
A6：跨库计算列（依赖子源关系字段）在同一 GQL 内生效。

需本机 127.0.0.1 已启动 MySQL(3306) / MongoDB(27017)，库 `mongo_store_e2e`，
账号 `e2e/e2e123`（可用 MYSQL_URI / MONGO_URI 覆盖）；任一不可达则整体 skip。
表 / 集合名统一带进程级 token 后缀（M-4）：同机多进程或跨仓（nodejs-store e2e）
并发跑同一共享库时，各实例只建并只清自己的表，互不干扰。

全程只走 store 统一入口：`store.query_federated` → core `plan_federated` 拆源
  → 逐源执行（Mongo 原生 / SQL translate→exec）→ core `merge_federated` 内存 join
  → 统一后处理（asyncFn 计算列）。

运行：$env:LOCAL_CORE='1'; $env:PYTHONPATH='py-store/src'; python -m pytest py-store/tests/ -q
"""

import asyncio
import os
import uuid
from urllib.parse import unquote, urlparse

import pytest

from py_store import executors, init, permission, store
from py_store import schema as _sc

# 进程级唯一 token（M-4）：表 / 集合名统一加此后缀，`_reset()` 只清理自己的表。
# 每次运行随机生成；可用 PYSTORE_E2E_TOKEN 环境变量覆盖（复现并发冲突时固定值）。
E2E_TOKEN = os.environ.get('PYSTORE_E2E_TOKEN') or uuid.uuid4().hex[:8]

MYSQL_URI = os.environ.get(
    'MYSQL_URI', 'mysql://e2e:e2e123@127.0.0.1:3306/mongo_store_e2e?charset=utf8mb4')
MONGO_URI = os.environ.get('MONGO_URI', 'mongodb://127.0.0.1:27017/mongo_store_e2e')

MYSQL_DDL = [
    f'DROP TABLE IF EXISTS fed_orders_{E2E_TOKEN}',
    f"""CREATE TABLE fed_orders_{E2E_TOKEN} (
         _id VARCHAR(64) NOT NULL,
         userId VARCHAR(64),
         code VARCHAR(255),
         amount DOUBLE,
         __present VARCHAR(255),
         PRIMARY KEY (_id)
       ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]


class _State:
    def __init__(self):
        self.mongo_ready = False
        self.mysql_ready = False
        self.reason = '数据库不可达'
        self.mongo_client = None
        self.mongo_db = None
        self.mysql_pool = None
        self.mysql_conn = None


state = _State()


def _parse_mysql_uri(uri):
    u = urlparse(uri)
    return {
        'host': u.hostname or '127.0.0.1',
        'port': u.port or 3306,
        'user': unquote(u.username or ''),
        'password': unquote(u.password or ''),
        'db': (u.path or '/').lstrip('/'),
    }


async def _setup_mongo():
    try:
        from pymongo import AsyncMongoClient
    except ImportError as e:
        state.reason = f'缺少 PyMongo AsyncMongoClient: {e}'
        return
    client = AsyncMongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
    try:
        await client.admin.command({'ping': 1})
    except Exception as e:
        state.reason = f'MongoDB 不可达（{MONGO_URI}）: {e}'
        await client.close()
        return
    state.mongo_client = client
    state.mongo_db = client.get_default_database()
    await state.mongo_db[f'fed_users_{E2E_TOKEN}'].delete_many({})
    state.mongo_ready = True


async def _setup_mysql():
    try:
        import asyncmy
    except ImportError as e:
        state.reason = f'缺少 asyncmy 驱动: {e}'
        return
    try:
        pool = await asyncio.wait_for(
            asyncmy.create_pool(autocommit=True, **_parse_mysql_uri(MYSQL_URI)), timeout=5)
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute('SELECT 1')
                await cur.fetchall()
    except Exception as e:
        state.reason = f'MySQL 不可达（{MYSQL_URI}）: {e}'
        return
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            for stmt in MYSQL_DDL:
                await cur.execute(stmt)
    state.mysql_pool = pool
    state.mysql_conn = executors.create_connection('mysql', pool)
    state.mysql_ready = True


def _order_codes(items, _ctx=None):
    """A6：跨库计算列（依赖子源关系字段 `orders{code}`）——由 Host 在 merge 后执行"""
    for it in items:
        it['orderCodes'] = ','.join(o.get('code') or '' for o in (it.get('orders') or []))


async def _setup():
    permission.set_context(None)
    await _setup_mongo()
    await _setup_mysql()
    if not (state.mongo_ready and state.mysql_ready):
        return

    _sc.register({
        'name': 'FedUser',
        'collection': f'fed_users_{E2E_TOKEN}',
        'idPrefix': 'u_',
        'datasource': 'mongo_e2e',
        'timestamps': False,
        'fields': {'name': {'type': 'string'}},
        'relations': {
            'orders': {'model': 'FedOrder', 'type': 'many',
                       'localField': '_id', 'foreignField': 'userId'},
        },
        'computes': {
            'orderCodes': {
                'type': 'string',
                'asyncFn': _order_codes,
                'fnRef': 'fed_order_codes',
                'depends': ['orders{code}'],
            },
        },
    })
    _sc.register({
        'name': 'FedOrder',
        'collection': f'fed_orders_{E2E_TOKEN}',
        'idPrefix': 'o_',
        'datasource': 'mysql_e2e',
        'timestamps': False,
        'fields': {
            'userId': {'type': 'string'},
            'code': {'type': 'string'},
            'amount': {'type': 'number'},
        },
        'relations': {},
    })

    connections = {'mongo_e2e': state.mongo_db, 'mysql_e2e': state.mysql_conn}
    # 兜底 default：同进程内其他测试模块在 import 期注册的 schema 未绑定数据源，
    # init 的索引巡检会按 default 反查连接（Mongo 源不建索引时仅查询元数据）。
    connections.setdefault('default', state.mongo_db)
    await init(connections)


async def _teardown():
    if state.mysql_pool is not None:
        state.mysql_pool.close()
        await state.mysql_pool.wait_closed()
    if state.mongo_client is not None:
        await state.mongo_client.close()


async def _reset():
    # M-4：只清理本 token 的表（并发进程 / 跨仓实例互不干扰）
    async with state.mysql_pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(f'DELETE FROM fed_orders_{E2E_TOKEN}')
    await state.mongo_db[f'fed_users_{E2E_TOKEN}'].delete_many({})


_LOOP = None


@pytest.fixture(scope='module', autouse=True)
def _boot():
    global _LOOP
    # m-11：惰性 / 安全建 loop —— asyncmy 连接池绑定创建时的 loop，连接须共用同一事件循环；
    # loop 在 fixture（首次使用）时创建，模块导入期零副作用；仅当当前线程无运行中 loop
    # 时才创建并 set（不覆盖宿主 / 异步框架已有 loop），teardown 关闭并清理本 fixture 的 set。
    # 取舍说明：3.14 起获取「线程 current loop」的标准 API 均已弃用，故 teardown 统一
    # set_event_loop(None)（测试自管理的既有取舍，进程结束即回收）；真实宿主应自行管理 loop。
    # （schema._schemas / datasource._connections 全局单例为既定设计，维持现状，不动。）
    try:
        asyncio.get_running_loop()  # 同步 fixture 上下文不应有运行中 loop，此处恒走 except
        _LOOP = asyncio.get_event_loop()
    except RuntimeError:
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    try:
        _LOOP.run_until_complete(_setup())
        yield _LOOP
    finally:
        try:
            _LOOP.run_until_complete(_teardown())
        finally:
            _LOOP.close()
            asyncio.set_event_loop(None)


def _run(coro):
    return _LOOP.run_until_complete(coro)


# ─── A5/A6：跨库联邦 ─────────────────────────────────────────

async def _t_cross_source():
    await _reset()
    u1 = await store.insert('FedUser', {'name': 'A'})
    u2 = await store.insert('FedUser', {'name': 'B'})
    await store.insert('FedOrder', {'userId': u1['_id'], 'code': 'c1', 'amount': 10})
    await store.insert('FedOrder', {'userId': u1['_id'], 'code': 'c2', 'amount': 20})
    await store.insert('FedOrder', {'userId': u2['_id'], 'code': 'c3', 'amount': 30})

    items = await store.query_federated(
        'FedUser($condition:@c0){name, orders{code, amount}, orderCodes}', {'c0': {}})

    assert len(items) == 2, '应返回两个用户'
    a = next((d for d in items if d['name'] == 'A'), None)
    b = next((d for d in items if d['name'] == 'B'), None)
    assert a and b, '应包含 A / B 两个用户'

    assert sorted(o['code'] for o in a['orders']) == ['c1', 'c2'], 'A5 跨源关联应挂载子源订单'
    assert sorted(o['amount'] for o in a['orders']) == [10, 20]
    assert a['orderCodes'] == 'c1,c2', 'A6 跨库计算列应基于子源关系字段'
    assert [o['code'] for o in b['orders']] == ['c3']
    assert b['orderCodes'] == 'c3'


async def _t_single_source():
    await _reset()
    doc = await store.insert('FedUser', {'name': 'Solo'})
    items = await store.query_federated('FedUser($condition:@c0){_id, name}', {'c0': {}})
    solo = next((d for d in items if d['name'] == 'Solo'), None)
    assert solo, '单源联邦应走与单库一致的执行路径'
    assert solo['_id'] == doc['_id']


def test_cross_source_nested_and_computed():
    """A5 + A6：单 GQL 跨 Mongo→MySQL，嵌套关联 + 跨库计算列"""
    if not (state.mongo_ready and state.mysql_ready):
        pytest.skip(state.reason)
    _run(_t_cross_source())


def test_single_source_same_shape_as_query():
    """单源（未跨源）联邦查询与单库 query 同形"""
    if not (state.mongo_ready and state.mysql_ready):
        pytest.skip(state.reason)
    _run(_t_single_source())
