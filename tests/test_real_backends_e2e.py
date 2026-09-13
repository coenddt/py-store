"""Phase 4 · A4：真实四库（MySQL / PostgreSQL / MongoDB / SQLite）CRUD 端到端（Python 侧）

对齐 nodejs-store/tests/real-backends-e2e.test.js（Node 侧真实三库；SQLite 回路另有
sql-executor.test.js）。本文件一次覆盖四库：MySQL(3306) / PostgreSQL(5432) / MongoDB(27017)
需本机已启动，库 `mongo_store_e2e`，账号 `e2e/e2e123`（可用 MYSQL_URI / PG_URI / MONGO_URI 覆盖）；
SQLite 走内存库（无需外部服务）。任一外部库不可达时，其相关用例自动 skip，不影响其余回归。
表 / 集合名统一带进程级 token 后缀（M-4）：同机多进程或跨仓（nodejs-store e2e）
并发跑同一共享库时，各实例只建并只清自己的表，互不干扰。

全程只走 store 统一入口：
  store.init(连接) → crud.* → datasource 路由 → core.dialect_translate
    → executors（绑定参数 + 执行 + restore_rows）→ 结果塑形
以及 syncSchema（introspect → schema_from_rows → merge_schema → register）。

每个后端的 collection 名互不相同 —— 路由按 `collection → datasource` 反查，
同名 collection 会串源（这也正是「多库并行」下 schema 命名需全局唯一的体现）。

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
PG_URI = os.environ.get('PG_URI', 'postgres://e2e:e2e123@127.0.0.1:5432/mongo_store_e2e')
MONGO_URI = os.environ.get('MONGO_URI', 'mongodb://127.0.0.1:27017/mongo_store_e2e')

# ─── 物理表结构（标量范式，与 core 关系模型一致；archive 表供 remove 归档） ───
# 归档表名 = collection + '_deleted'（core 在 register 时自动派生 `<collection>_deleted` 归档 schema）

MYSQL_DDL = [
    f'DROP TABLE IF EXISTS gadgets_{E2E_TOKEN}',
    f'DROP TABLE IF EXISTS widgets_{E2E_TOKEN}',
    f'DROP TABLE IF EXISTS my_posts_{E2E_TOKEN}_deleted',
    f'DROP TABLE IF EXISTS my_posts_{E2E_TOKEN}',
    f"""CREATE TABLE my_posts_{E2E_TOKEN} (
         _id VARCHAR(64) NOT NULL,
         title VARCHAR(255),
         status VARCHAR(64),
         views INT,
         PRIMARY KEY (_id)
       ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    f"""CREATE TABLE my_posts_{E2E_TOKEN}_deleted (
         _id VARCHAR(64) NOT NULL,
         title VARCHAR(255),
         status VARCHAR(64),
         views INT,
         deletedAt BIGINT,
         PRIMARY KEY (_id)
       ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    f"""CREATE TABLE widgets_{E2E_TOKEN} (
         _id VARCHAR(64) NOT NULL,
         sku VARCHAR(255) NOT NULL,
         price DOUBLE,
         PRIMARY KEY (_id)
       ) ENGINE=InnoDB""",
    f"""CREATE TABLE gadgets_{E2E_TOKEN} (
         _id VARCHAR(64) NOT NULL,
         widget_id VARCHAR(64),
         label VARCHAR(255),
         PRIMARY KEY (_id),
         FOREIGN KEY (widget_id) REFERENCES widgets_{E2E_TOKEN}(_id)
       ) ENGINE=InnoDB""",
]

PG_DDL = [
    f'DROP TABLE IF EXISTS gadgets_{E2E_TOKEN} CASCADE',
    f'DROP TABLE IF EXISTS widgets_{E2E_TOKEN} CASCADE',
    f'DROP TABLE IF EXISTS pg_posts_{E2E_TOKEN}_deleted CASCADE',
    f'DROP TABLE IF EXISTS pg_posts_{E2E_TOKEN} CASCADE',
    f'CREATE TABLE pg_posts_{E2E_TOKEN} '
    f'(_id TEXT PRIMARY KEY, title TEXT, status TEXT, views INTEGER)',
    f"""CREATE TABLE pg_posts_{E2E_TOKEN}_deleted (
         _id TEXT PRIMARY KEY, title TEXT, status TEXT, views INTEGER, "deletedAt" BIGINT
       )""",
    f'CREATE TABLE widgets_{E2E_TOKEN} '
    f'(_id TEXT PRIMARY KEY, sku TEXT NOT NULL, price DOUBLE PRECISION)',
    f"""CREATE TABLE gadgets_{E2E_TOKEN} (
         _id TEXT PRIMARY KEY, widget_id TEXT REFERENCES widgets_{E2E_TOKEN}(_id), label TEXT
       )""",
]

SQLITE_DDL = [
    f'DROP TABLE IF EXISTS gadgets_{E2E_TOKEN}',
    f'DROP TABLE IF EXISTS widgets_{E2E_TOKEN}',
    f'DROP TABLE IF EXISTS sq_posts_{E2E_TOKEN}_deleted',
    f'DROP TABLE IF EXISTS sq_posts_{E2E_TOKEN}',
    f"""CREATE TABLE sq_posts_{E2E_TOKEN} (
         _id TEXT PRIMARY KEY, title TEXT, status TEXT, views INTEGER
       )""",
    f"""CREATE TABLE sq_posts_{E2E_TOKEN}_deleted (
         _id TEXT PRIMARY KEY, title TEXT, status TEXT, views INTEGER, deletedAt INTEGER
       )""",
    f'CREATE TABLE widgets_{E2E_TOKEN} (_id TEXT PRIMARY KEY, sku TEXT NOT NULL, price REAL)',
    f"""CREATE TABLE gadgets_{E2E_TOKEN} (
         _id TEXT PRIMARY KEY, widget_id TEXT REFERENCES widgets_{E2E_TOKEN}(_id), label TEXT
       )""",
]


# ─── 后端上下文（物理名/注册名/连接句柄） ─────────────────────

class _Ctx:
    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.ready = False
        self.reason = '数据库不可达'
        self.driver = None
        self.conn = None
        self.client = None
        self.introspect_options = None
        self.reset_sql = []
        self.reset_collections = []


mysql_ctx = _Ctx(kind='mysql', ds='mysql_e2e', schema_name='MyPost',
                 collection=f'my_posts_{E2E_TOKEN}',
                 archive_schema='MyPostDeleted', archive=f'my_posts_{E2E_TOKEN}_deleted')
pg_ctx = _Ctx(kind='postgres', ds='pg_e2e', schema_name='PgPost',
              collection=f'pg_posts_{E2E_TOKEN}',
              archive_schema='PgPostDeleted', archive=f'pg_posts_{E2E_TOKEN}_deleted')
mongo_ctx = _Ctx(kind='mongodb', ds='mongo_e2e', schema_name='MgPost',
                 collection=f'mg_posts_{E2E_TOKEN}',
                 archive_schema='MgPostDeleted', archive=f'mg_posts_{E2E_TOKEN}_deleted')
# SQLite 内存库无需外部服务，天然作为 `default` 源（表名带 token 与其余后端保持一致）
sqlite_ctx = _Ctx(kind='sqlite', ds='default', schema_name='SqPost',
                  collection=f'sq_posts_{E2E_TOKEN}',
                  archive_schema='SqPostDeleted', archive=f'sq_posts_{E2E_TOKEN}_deleted')

CTXS = [mysql_ctx, pg_ctx, mongo_ctx, sqlite_ctx]
CRUD_CTXS = CTXS
SYNC_CTXS = [mysql_ctx, pg_ctx, sqlite_ctx]
_IDS = [c.kind for c in CTXS]
_SYNC_IDS = [c.kind for c in SYNC_CTXS]


def _register_post(ctx):
    """注册逻辑模型（自动派生 `<Name>Deleted` 归档表镜像）"""
    _sc.register({
        'name': ctx.schema_name,
        'collection': ctx.collection,
        'idPrefix': 'p_',
        'timestamps': False,
        'fields': {
            'title': {'type': 'string'},
            'status': {'type': 'string'},
            'views': {'type': 'number'},
        },
        'relations': {},
        'datasource': ctx.ds,
    })


def _parse_mysql_uri(uri):
    u = urlparse(uri)
    return {
        'host': u.hostname or '127.0.0.1',
        'port': u.port or 3306,
        'user': unquote(u.username or ''),
        'password': unquote(u.password or ''),
        'db': (u.path or '/').lstrip('/'),
    }


# ─── 各后端连接与建表 ────────────────────────────────────────

async def _setup_mysql(ctx):
    try:
        import asyncmy
    except ImportError as e:
        ctx.reason = f'缺少 asyncmy 驱动: {e}'
        return
    cfg = _parse_mysql_uri(MYSQL_URI)
    try:
        pool = await asyncio.wait_for(asyncmy.create_pool(autocommit=True, **cfg), timeout=5)
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute('SELECT 1')
                await cur.fetchall()
    except Exception as e:
        ctx.reason = f'MySQL 不可达（{MYSQL_URI}）: {e}'
        return

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            for stmt in MYSQL_DDL:
                await cur.execute(stmt)
    ctx.driver = pool
    ctx.conn = executors.create_connection('mysql', pool)
    ctx.reset_sql = [f'DELETE FROM my_posts_{E2E_TOKEN}',
                     f'DELETE FROM my_posts_{E2E_TOKEN}_deleted']
    _register_post(ctx)
    ctx.ready = True


async def _setup_postgres(ctx):
    try:
        import asyncpg
    except ImportError as e:
        ctx.reason = f'缺少 asyncpg 驱动: {e}'
        return
    try:
        pool = await asyncio.wait_for(asyncpg.create_pool(dsn=PG_URI, timeout=5), timeout=8)
        await pool.execute('SELECT 1')
    except Exception as e:
        ctx.reason = f'PostgreSQL 不可达（{PG_URI}）: {e}'
        return

    for stmt in PG_DDL:
        await pool.execute(stmt)
    ctx.driver = pool
    ctx.conn = executors.create_connection('postgres', pool)
    ctx.reset_sql = [f'DELETE FROM pg_posts_{E2E_TOKEN}',
                     f'DELETE FROM pg_posts_{E2E_TOKEN}_deleted']
    _register_post(ctx)
    ctx.ready = True


async def _setup_mongo(ctx):
    try:
        from pymongo import AsyncMongoClient
    except ImportError as e:
        ctx.reason = f'缺少 PyMongo AsyncMongoClient: {e}'
        return
    client = AsyncMongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
    try:
        await client.admin.command({'ping': 1})
    except Exception as e:
        ctx.reason = f'MongoDB 不可达（{MONGO_URI}）: {e}'
        await client.close()
        return
    db = client.get_default_database()
    ctx.client = client
    ctx.driver = db
    ctx.conn = db
    # M-4：只清理本 token 的集合（并发进程 / 跨仓实例互不干扰）
    ctx.reset_collections = [f'mg_posts_{E2E_TOKEN}', f'mg_posts_{E2E_TOKEN}_deleted']
    _register_post(ctx)
    ctx.ready = True


async def _setup_sqlite(ctx):
    try:
        import aiosqlite
    except ImportError as e:
        ctx.reason = f'缺少 aiosqlite 驱动: {e}'
        return
    db = await aiosqlite.connect(':memory:')
    for stmt in SQLITE_DDL:
        await db.execute(stmt)
    await db.commit()
    ctx.driver = db
    ctx.conn = executors.create_connection('sqlite', db)
    ctx.reset_sql = [f'DELETE FROM sq_posts_{E2E_TOKEN}',
                     f'DELETE FROM sq_posts_{E2E_TOKEN}_deleted']
    _register_post(ctx)
    ctx.ready = True


async def _reset(ctx):
    """清空该后端的主表与归档表（用例间隔离）"""
    if ctx.kind == 'mysql':
        async with ctx.driver.acquire() as conn:
            async with conn.cursor() as cur:
                for sql in ctx.reset_sql:
                    await cur.execute(sql)
    elif ctx.kind in ('postgres', 'sqlite'):
        for sql in ctx.reset_sql:
            await ctx.driver.execute(sql)
        if ctx.kind == 'sqlite':
            await ctx.driver.commit()
    elif ctx.kind == 'mongodb':
        for coll in ctx.reset_collections:
            await ctx.driver[coll].delete_many({})


async def _setup():
    permission.set_context(None)
    await _setup_sqlite(sqlite_ctx)
    await _setup_mysql(mysql_ctx)
    await _setup_postgres(pg_ctx)
    await _setup_mongo(mongo_ctx)

    connections = {c.ds: c.conn for c in CTXS if c.ready}
    # 兜底 default：本 pytest 进程内其他测试模块注册的 schema 未绑定数据源，
    # init 的索引巡检会按 default 反查连接；SQLite 源天然兜底（SQL 源不建索引，铁律 6）。
    connections.setdefault('default', sqlite_ctx.conn)
    await init(connections)


async def _teardown():
    if mysql_ctx.driver is not None:
        # asyncmy 的 ``Pool.close()`` 为同步方法，``wait_closed()`` 才需 await
        mysql_ctx.driver.close()
        await mysql_ctx.driver.wait_closed()
    if pg_ctx.driver is not None:
        await pg_ctx.driver.close()
    if sqlite_ctx.driver is not None:
        await sqlite_ctx.driver.close()
    if mongo_ctx.client is not None:
        await mongo_ctx.client.close()


# 四库连接须共用同一事件循环（asyncmy/asyncpg 连接池绑定创建时的 loop）
_LOOP = None


@pytest.fixture(scope='module', autouse=True)
def _boot():
    global _LOOP
    # m-11：惰性 / 安全建 loop —— asyncmy/asyncpg 连接池绑定创建时的 loop，连接须共用同一事件循环；
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


# ─── A4：CRUD 闭环（同一套断言跑四个后端） ────────────────────

async def _t_insert_and_query(ctx):
    S = ctx.schema_name
    await _reset(ctx)
    doc = await store.insert(S, {'title': '你好', 'status': 'draft', 'views': 3})
    assert doc and isinstance(doc.get('_id'), str) and doc['_id'].startswith('p_'), \
        '应生成 idPrefix 前缀 _id'

    items = await store.query(f'{S}{{_id, title, status, views}}')
    assert len(items) == 1
    assert items[0]['_id'] == doc['_id']
    assert items[0]['title'] == '你好'
    assert items[0]['views'] == 3


async def _t_count_exists(ctx):
    S = ctx.schema_name
    await _reset(ctx)
    await store.insert_many(S, [
        {'title': 'A', 'status': 'draft', 'views': 1},
        {'title': 'B', 'status': 'draft', 'views': 2},
    ])
    assert await store.count(S, {}) == 2
    assert await store.count(S, {'status': 'draft'}) == 2
    assert await store.count(S, {'views': {'$gte': 2}}) == 1
    assert await store.exists(S, {'title': 'A'}) is True
    assert await store.exists(S, {'title': 'nope'}) is False


async def _t_update(ctx):
    """写后回读：PG/SQLite RETURNING，MySQL 两段编排"""
    S = ctx.schema_name
    await _reset(ctx)
    doc = await store.insert(S, {'title': 'x', 'status': 'draft', 'views': 1})
    out = await store.update(S, {'_id': doc['_id']}, {'views': 42})
    assert out and out['_id'] == doc['_id'], '应回读到被更新文档'
    assert out['views'] == 42

    items = await store.query(f'{S}{{_id, views}}')
    assert items[0]['views'] == 42


async def _t_update_many(ctx):
    S = ctx.schema_name
    await _reset(ctx)
    await store.insert_many(S, [
        {'title': 'A', 'status': 'draft', 'views': 1},
        {'title': 'B', 'status': 'draft', 'views': 2},
    ])
    r = await store.update_many(S, {}, {'$inc': {'views': 10}})
    assert r['modifiedCount'] == 2
    items = await store.query(f'{S}{{_id, views}}')
    assert sorted(d['views'] for d in items) == [11, 12]


async def _t_remove(ctx):
    S, SD = ctx.schema_name, ctx.archive_schema
    await _reset(ctx)
    doc = await store.insert(S, {'title': 'gone', 'status': 'done', 'views': 7})
    r = await store.remove(S, {'_id': doc['_id']})
    assert r['deletedCount'] == 1
    assert r['archivedCount'] == 1

    assert await store.count(S, {}) == 0
    archived = await store.query(f'{SD}{{_id, title, deletedAt}}')
    assert len(archived) == 1
    assert archived[0]['_id'] == doc['_id']
    assert float(archived[0]['deletedAt']) > 0, '归档应写 deletedAt'


async def _t_mutation(ctx):
    S = ctx.schema_name
    await _reset(ctx)
    created = await store.mutation(S, {'title': 'm1', 'status': 'new', 'views': 5})
    assert created and isinstance(created.get('_id'), str), 'mutation 应写入并回读 _id'
    assert await store.count(S, {}) == 1


async def _t_upsert(ctx):
    """_id 冲突目标：未命中新建 / 命中更新"""
    S = ctx.schema_name
    await _reset(ctx)
    created = await store.upsert(S, {'_id': 'p_u1'}, {'status': 'on', 'views': 1})
    assert created and created['_id'] == 'p_u1', 'upsert 未命中应新建并回读'
    assert created['status'] == 'on'

    hit = await store.upsert(S, {'_id': 'p_u1'}, {'views': 9})
    assert hit['_id'] == 'p_u1'
    assert hit['views'] == 9
    assert await store.count(S, {}) == 1, '命中时不应产生新行'


async def _t_sync_schema(ctx):
    """introspect → schema_from_rows → register"""
    # 库里含已注册逻辑模型（my_posts 等），三元组唯一性下直接注册必冲突 ——
    # 故先 register_defs=False 拿 defs 验证映射，再显式断言冲突 fail fast，
    # 最后单独注册非冲突表验证注册链路。
    defs = await store.sync_schema(ctx.kind, ctx.driver, datasource=ctx.ds,
                                   register_defs=False)

    widgets = next((d for d in defs if d['collection'] == f'widgets_{E2E_TOKEN}'), None)
    assert widgets, '应产出 widgets 定义'
    assert widgets['collection'] == f'widgets_{E2E_TOKEN}'
    assert widgets['datasource'] == ctx.ds
    assert widgets['fields'].get('_id'), '主键应映射为 _id'
    assert widgets['fields']['sku']['required'] is True, 'NOT NULL 应映射 required'
    assert widgets['fields']['price']['type'] == 'number'
    # 关系键 = 物理表名（带 token 后缀），见探针确认
    assert widgets['relations'].get(f'gadgets_{E2E_TOKEN}'), '外键应生成反向 many 关系'
    assert widgets['relations'][f'gadgets_{E2E_TOKEN}']['type'] == 'many'

    gadgets = next((d for d in defs if d['collection'] == f'gadgets_{E2E_TOKEN}'), None)
    assert gadgets['relations'].get(f'widgets_{E2E_TOKEN}'), '外键侧应有 many-to-one 关系'
    assert gadgets['relations'][f'widgets_{E2E_TOKEN}']['type'] == 'one'
    assert gadgets['relations'][f'widgets_{E2E_TOKEN}']['localField'] == 'widget_id'

    # 三元组冲突 fail fast：已注册 <ctx.schema_name>（同 source、同 collection）再注册即抛错
    dupe = next((d for d in defs if d['collection'] == ctx.collection), None)
    assert dupe, 'introspect 应产出已注册表的 def'
    with pytest.raises(RuntimeError, match='冲突|占用'):  # noqa: RUF043  正则交替是本意（两类冲突文案二选一）
        _sc.register({**dupe, 'name': f"{dupe['name']}_dupe"})

    taken = {_sc.get(n)['collection'] for n in _sc.list()}
    for d in defs:
        # M-4：共享库中还有并发进程 / 跨仓（nodejs-store）的表，只注册本 token 的表
        if d['collection'] in taken or not d['collection'].endswith(f'_{E2E_TOKEN}'):
            continue
        _sc.register(d)
    assert _sc.has(f'widgets_{E2E_TOKEN}'), 'syncSchema 应完成非冲突表注册'


@pytest.mark.parametrize('ctx', CRUD_CTXS, ids=_IDS)
def test_insert_and_query(ctx):
    if not ctx.ready:
        pytest.skip(ctx.reason)
    _run(_t_insert_and_query(ctx))


@pytest.mark.parametrize('ctx', CRUD_CTXS, ids=_IDS)
def test_count_exists(ctx):
    if not ctx.ready:
        pytest.skip(ctx.reason)
    _run(_t_count_exists(ctx))


@pytest.mark.parametrize('ctx', CRUD_CTXS, ids=_IDS)
def test_update(ctx):
    if not ctx.ready:
        pytest.skip(ctx.reason)
    _run(_t_update(ctx))


@pytest.mark.parametrize('ctx', CRUD_CTXS, ids=_IDS)
def test_update_many(ctx):
    if not ctx.ready:
        pytest.skip(ctx.reason)
    _run(_t_update_many(ctx))


@pytest.mark.parametrize('ctx', CRUD_CTXS, ids=_IDS)
def test_remove(ctx):
    if not ctx.ready:
        pytest.skip(ctx.reason)
    _run(_t_remove(ctx))


@pytest.mark.parametrize('ctx', CRUD_CTXS, ids=_IDS)
def test_mutation(ctx):
    if not ctx.ready:
        pytest.skip(ctx.reason)
    _run(_t_mutation(ctx))


@pytest.mark.parametrize('ctx', CRUD_CTXS, ids=_IDS)
def test_upsert(ctx):
    if not ctx.ready:
        pytest.skip(ctx.reason)
    _run(_t_upsert(ctx))


@pytest.mark.parametrize('ctx', SYNC_CTXS, ids=_SYNC_IDS)
def test_sync_schema(ctx):
    if not ctx.ready:
        pytest.skip(ctx.reason)
    _run(_t_sync_schema(ctx))
