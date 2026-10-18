"""06 步骤 6：向量检索 conformance（C1~C11，Python 侧）

后端覆盖：`memory` / `local` / `sqlite` 必跑；`mongodb` / `mysql` / `postgres`
实例不可达即 `pytest.skip`（约定对齐 `test_real_backends_e2e.py`）。

用例集以执行文档 06 §4.5 的 JSON 为唯一权威（``VECTOR-CONFORMANCE`` 标记夹取，
与 ``nodejs-store/tests/vector-conformance.test.js`` 由 ``scripts/parity-vector.js`` 对拍）。

运行：``$env:LOCAL_CORE='1'; python -m pytest py-store/tests/test_vector_conformance.py -q``
"""

import asyncio
import json
import os
from urllib.parse import unquote, urlparse

import pytest

from py_store import datasource, ddl, degraded, executors, init, permission, store
from py_store import schema as _sc
from py_store.local import connect as connect_local
from py_store.memory import connect as connect_memory

# VECTOR-CONFORMANCE-BEGIN
CONFORMANCE = json.loads(r'''{
  "schema": {
    "name": "VectorConformance",
    "collection": "vector_conformance",
    "fields": {
      "label": { "type": "string", "default": "" },
      "content": { "type": "string", "default": "" },
      "sourceHash": { "type": "string", "default": "" },
      "embedding": { "type": "vector", "dim": 3, "metric": "cosine" },
      "embedding_l2": { "type": "vector", "dim": 3, "metric": "l2" },
      "embedding_ip": { "type": "vector", "dim": 3, "metric": "ip" }
    },
    "indexes": [],
    "read": ["admin"],
    "write": ["admin"]
  },
  "docs": [
    { "_id": "c1", "label": "same",       "content": "a",
      "embedding": [1, 0, 0], "embedding_l2": [1, 0, 0], "embedding_ip": [1, 0, 0] },
    { "_id": "c2", "label": "orthogonal", "content": "b",
      "embedding": [0, 1, 0], "embedding_l2": [0, 1, 0], "embedding_ip": [0, 1, 0] },
    { "_id": "c3", "label": "near",       "content": "c",
      "embedding": [0.8, 0.6, 0], "embedding_l2": [0.8, 0.6, 0], "embedding_ip": [0.8, 0.6, 0] },
    { "_id": "c4", "label": "opposite",   "content": "d",
      "embedding": [-1, 0, 0], "embedding_l2": [-1, 0, 0], "embedding_ip": [-1, 0, 0] }
  ],
  "cases": [
    { "id": "C1", "kind": "near", "field": "embedding", "vector": [1, 0, 0], "limit": 3,
      "expectIds": ["c1", "c3", "c2"], "expectScores": [1.0, 0.8, 0.0], "tolerance": 1e-6 },
    { "id": "C2", "kind": "near", "field": "embedding_l2", "vector": [1, 0, 0], "limit": 3,
      "expectIds": ["c1", "c3", "c2"],
      "expectScores": [1.0, 0.612574113, 0.414213562], "tolerance": 1e-6 },
    { "id": "C3", "kind": "near", "field": "embedding_ip", "vector": [1, 0, 0], "limit": 3,
      "expectIds": ["c1", "c3", "c2"], "expectScores": [1.0, 0.8, 0.0], "tolerance": 1e-6 },
    { "id": "C4", "kind": "near", "field": "embedding", "vector": [1, 0], "limit": 1,
      "expectError": "dim" },
    { "id": "C5", "kind": "near", "field": "embedding", "vector": [0, 0, 0], "limit": 1,
      "expectError": "zero" },
    { "id": "C6", "kind": "near", "field": "embedding", "vector": [1, 0, 0],
      "expectError": "limit" },
    { "id": "C7", "kind": "near", "field": "embedding", "vector": [1, 0, 0], "limit": 10,
      "expectIds": ["c1", "c3", "c2", "c4"], "tolerance": 1e-6 },
    { "id": "C8", "kind": "near", "field": "embedding", "vector": [1, 0, 0], "limit": 3,
      "noIndex": true,
      "expectIds": ["c1", "c3", "c2"], "expectScores": [1.0, 0.8, 0.0], "tolerance": 1e-6 },
    { "id": "C9", "kind": "plain",
      "expectIds": ["c1", "c2", "c3", "c4"], "expectNoScore": true },
    { "id": "C10", "kind": "near", "field": "embedding", "vector": [1, 0, 0], "limit": 3,
      "filter": { "label": "near" },
      "expectIds": ["c3"], "expectDegraded": true },
    { "id": "C11", "kind": "reindex", "field": "embedding", "hashField": "sourceHash",
      "sourceFields": ["content"], "pre": "reindexForce",
      "mutateId": "c2", "mutateContent": "b2",
      "expectEmbedded": 1, "expectSkipped": 3 }
  ]
}''')
# VECTOR-CONFORMANCE-END

CASE = {c['id']: c for c in CONFORMANCE['cases']}

MYSQL_URI = os.environ.get(
    'MYSQL_URI', 'mysql://e2e:e2e123@127.0.0.1:3306/mongo_store_e2e_real?charset=utf8mb4')
PG_URI = os.environ.get('PG_URI', 'postgres://e2e:e2e123@127.0.0.1:5432/mongo_store_e2e_real')
MONGO_URI = os.environ.get('MONGO_URI', 'mongodb://127.0.0.1:27017/mongo_store_e2e_real')

BACKENDS = ('memory', 'local', 'sqlite', 'mongodb', 'mysql', 'postgres')


def _schema_name(tag):
    suffix = '' if tag == 'sqlite' else tag[0].upper() + tag[1:]
    return f'VectorConformance{suffix}'


def _register_test_schemas():
    """模块级注册（conftest 的 ``_register_test_schemas`` 约定：只定义、按需重放）"""
    for tag in BACKENDS:
        field_defs = {'_id': {'type': 'string'}, **CONFORMANCE['schema']['fields']}
        _sc.register({
            **CONFORMANCE['schema'],
            'name': _schema_name(tag),
            'fields': field_defs,
            'datasource': f'vc_{tag}',
        })


async def _connections(tag, tmp_path):
    """建立该后端连接；不可达 / 缺驱动返回 ``(None, reason, None)``

    第二返回值 conns 供 ``init``，第三返回值 driver 供用例结束关闭
    （``executors.create_connection`` 的描述符不含驱动句柄）。
    """
    if tag == 'memory':
        return {'vc_memory': connect_memory({'store': {}})}, None, None
    if tag == 'local':
        return {'vc_local': connect_local({'dir': str(tmp_path / 'local')})}, None, None
    if tag == 'sqlite':
        import aiosqlite

        db = await aiosqlite.connect(':memory:')
        await db.executescript(ddl.generate('sqlite', [_schema_name('sqlite')]))
        return {'vc_sqlite': executors.create_connection('sqlite', db)}, None, db
    if tag == 'mongodb':
        try:
            from pymongo import AsyncMongoClient
        except ImportError as e:  # pragma: no cover - 驱动缺失
            return None, f'缺少 PyMongo AsyncMongoClient: {e}', None
        client = AsyncMongoClient(MONGO_URI, serverSelectionTimeoutMS=1200)
        try:
            await client.admin.command({'ping': 1})
        except Exception as e:  # 实例不可达即 skip
            await client.close()
            return None, f'MongoDB 不可达（{MONGO_URI}）: {e}', None
        return {'vc_mongodb': client.get_default_database()}, None, client
    if tag == 'mysql':
        try:
            import asyncmy
        except ImportError as e:  # pragma: no cover
            return None, f'缺少 asyncmy 驱动: {e}', None
        u = urlparse(MYSQL_URI)
        cfg = {
            'host': u.hostname, 'port': u.port or 3306,
            'user': unquote(u.username or ''), 'password': unquote(u.password or ''),
            'db': unquote((u.path or '/').lstrip('/')),
            'autocommit': True, 'connect_timeout': 1,
        }
        try:
            pool = await asyncio.wait_for(asyncmy.create_pool(**cfg), timeout=3)
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute('SELECT 1')
                    await cur.fetchall()
        except Exception as e:
            return None, f'MySQL 不可达（{MYSQL_URI}）: {e}', None
        sql = ddl.generate('mysql', [_schema_name('mysql')])
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                for stmt in [s.strip() for s in sql.split(';') if s.strip()]:
                    await cur.execute(stmt)
        return {'vc_mysql': executors.create_connection('mysql', pool)}, None, pool
    if tag == 'postgres':
        try:
            import asyncpg
        except ImportError as e:  # pragma: no cover
            return None, f'缺少 asyncpg 驱动: {e}', None
        try:
            pool = await asyncio.wait_for(asyncpg.create_pool(dsn=PG_URI, timeout=2), timeout=4)
            await pool.execute('SELECT 1')
            sql = ddl.generate('postgres', [_schema_name('postgres')])
            for stmt in [s.strip() for s in sql.split(';') if s.strip()]:
                await pool.execute(stmt)
        except Exception as e:
            return None, f'PostgreSQL 不可达 / 无 pgvector（{PG_URI}）: {e}', None
        return {'vc_postgres': executors.create_connection('postgres', pool)}, None, pool
    raise AssertionError(tag)


async def _close(tag, driver):
    """关闭该后端的原始驱动（连接描述符不含句柄；memory/local 无驱动可关）"""
    try:
        if tag == 'sqlite':
            await driver.close()
        elif tag == 'mongodb':
            await driver.close()
        elif tag == 'mysql':
            # asyncmy 的 ``Pool.close()`` 为同步方法，``wait_closed()`` 才需 await
            driver.close()
            await driver.wait_closed()
        elif tag == 'postgres':
            await driver.close()
    finally:
        datasource.set_connections({})


def _near_gql(name):
    return f'{name}($condition:@c){{_id, _score}}'


def _near_params(c):
    cond = dict(c.get('filter') or {})
    near = {'$vector': c['vector']}
    if 'limit' in c:
        near['$limit'] = c['limit']
    cond[c['field']] = {'$near': near}
    return {'c': cond}


async def _query_near(name, c):
    return await store.query(_near_gql(name), _near_params(c))


async def _run(tag, tmp_path, body):
    """统一前置：权限上下文 → 连接 → 写入 4 条 → body(name) → 清理"""
    permission.set_context({'userId': 'vc_admin', 'roles': ['admin']})
    conns, reason, driver = await _connections(tag, tmp_path)
    if conns is None:
        pytest.skip(reason)
    name = _schema_name(tag)
    try:
        await init(conns)
        for doc in CONFORMANCE['docs']:
            await store.insert(name, dict(doc))
        await body(name)
    finally:
        # 持久后端（mongodb/mysql/postgres）跨用例共享库，seed 前不清理会 E11000；
        # 逐条按 _id 自清理，memory/local/sqlite 每用例独立实例删零行无副作用。
        # 清理失败静默（不得替换 body 的原始失败），漏删会由下一用例 E11000 直接暴露
        for doc in CONFORMANCE['docs']:
            try:
                await store.remove(name, {'_id': doc['_id']})
            except Exception:
                pass
        await _close(tag, driver)


def _assert_ranked(rows, c):
    assert [r['_id'] for r in rows] == c['expectIds'], f"{c['id']} ids"
    if c.get('expectScores'):
        for i, r in enumerate(rows):
            assert abs(r['_score'] - c['expectScores'][i]) <= c['tolerance'], \
                f"{c['id']}[{i}] _score={r['_score']} 期望 {c['expectScores'][i]}"


async def _assert_case_error(name, c, keywords):
    with pytest.raises(Exception) as ei:  # 契约错误类型由 core 决定
        await _query_near(name, c)
    msg = str(ei.value).lower()
    assert any(k.lower() in msg for k in keywords), f"{c['id']} 错误文案：{ei.value}"


@pytest.mark.parametrize('tag', BACKENDS)
def test_c1_cosine(tag, tmp_path):
    async def body(name):
        _assert_ranked(await _query_near(name, CASE['C1']), CASE['C1'])

    asyncio.run(_run(tag, tmp_path, body))


@pytest.mark.parametrize('tag', BACKENDS)
def test_c2_l2(tag, tmp_path):
    async def body(name):
        _assert_ranked(await _query_near(name, CASE['C2']), CASE['C2'])

    asyncio.run(_run(tag, tmp_path, body))


@pytest.mark.parametrize('tag', BACKENDS)
def test_c3_ip(tag, tmp_path):
    async def body(name):
        _assert_ranked(await _query_near(name, CASE['C3']), CASE['C3'])

    asyncio.run(_run(tag, tmp_path, body))


@pytest.mark.parametrize('tag', BACKENDS)
def test_c4_dim_mismatch(tag, tmp_path):
    async def body(name):
        await _assert_case_error(name, CASE['C4'], ('dim', '维度'))

    asyncio.run(_run(tag, tmp_path, body))


@pytest.mark.parametrize('tag', BACKENDS)
def test_c5_zero_vector(tag, tmp_path):
    async def body(name):
        await _assert_case_error(name, CASE['C5'], ('zero', '零向量'))

    asyncio.run(_run(tag, tmp_path, body))


@pytest.mark.parametrize('tag', BACKENDS)
def test_c6_missing_limit(tag, tmp_path):
    async def body(name):
        await _assert_case_error(name, CASE['C6'], ('limit',))

    asyncio.run(_run(tag, tmp_path, body))


@pytest.mark.parametrize('tag', BACKENDS)
def test_c7_limit_over_collection(tag, tmp_path):
    async def body(name):
        rows = await _query_near(name, CASE['C7'])
        assert [r['_id'] for r in rows] == CASE['C7']['expectIds']

    asyncio.run(_run(tag, tmp_path, body))


@pytest.mark.parametrize('tag', BACKENDS)
def test_c8_no_declared_index(tag, tmp_path):
    async def body(name):
        assert CONFORMANCE['schema']['indexes'] == [], '用例集应为无索引 schema'
        _assert_ranked(await _query_near(name, CASE['C8']), CASE['C8'])

    asyncio.run(_run(tag, tmp_path, body))


@pytest.mark.parametrize('tag', BACKENDS)
def test_c9_plain_query_has_no_score(tag, tmp_path):
    async def body(name):
        rows = await store.query(f'{name}{{_id, label}}', {})
        assert [r['_id'] for r in rows] == CASE['C9']['expectIds']
        for r in rows:
            assert '_score' not in r, '不得追加 _score'

    asyncio.run(_run(tag, tmp_path, body))


@pytest.mark.parametrize('tag', BACKENDS)
def test_c10_filter_and_degraded(tag, tmp_path):
    async def body(name):
        token = degraded.begin()
        try:
            rows = await _query_near(name, CASE['C10'])
            status = store.degraded_status()
        finally:
            degraded.end(token)
        assert [r['_id'] for r in rows] == CASE['C10']['expectIds']
        assert status['degraded'] is True, f'{tag} 应声明降级'
        events = [e for e in status.get('events') or [] if e.get('type') == 'vector_degraded']
        assert events, '缺 vector_degraded 事件'
        last = events[-1]
        assert last['backend'] == tag
        assert last['field'] == CASE['C10']['field']
        assert last['metric'] == 'cosine'
        if tag == 'postgres':
            assert '后过滤' in last['message']
            assert '精确检索' in last['message']
        else:
            assert '应用层精确检索' in last['message']

    asyncio.run(_run(tag, tmp_path, body))


@pytest.mark.parametrize('tag', BACKENDS)
def test_c11_reindex(tag, tmp_path):
    async def body(name):
        c = CASE['C11']

        async def embed(texts):
            return [[1, 0, 0] for _ in texts]

        base = {
            'field': c['field'], 'hashField': c['hashField'],
            'sourceFields': c['sourceFields'], 'embed': embed,
        }
        first = await store.reindex_vectors(name, **base, force=True)
        assert first['embedded'] == len(CONFORMANCE['docs']), 'force 全量回填'
        await store.update(name, {'_id': c['mutateId']}, {'content': c['mutateContent']})
        second = await store.reindex_vectors(name, **base)
        assert second['embedded'] == c['expectEmbedded']
        assert second['skipped'] == c['expectSkipped']

    asyncio.run(_run(tag, tmp_path, body))
