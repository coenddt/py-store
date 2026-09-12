"""多数据源定位用例（B 系列，Host 侧 · Python 同构）

对齐 ``nodejs-store/tests/multi-datasource.test.js``（共享断言语义）：

  - B1: 两个 Mongo db 实例 source，同名集合 users，各查各库无串源
  - B2: 单 MongoClient source，两个 schema 声明不同 namespace（db 名），各查各库
  - B3: (source, namespace, collection) 冲突注册 → 抛错（fail fast，非静默串源）
  - B4: 同 SQL 连接双 namespace（SQLite attached db 代演 PG schema）各自命中
  - B9: 旧用法 init(db) + schema 无 datasource/namespace → source='default'、
        namespace=None，行为零变更
  - B10: namespace 非空但 source 为 db 实例 / client 缺 namespace → 显式报错
  - B11: sync_schema({ namespace }) 回写 def 的 namespace，与手动声明等价

B5-B8（联邦下推 / routeOverride）在 core 侧：``rust-store/core/tests/pushdown_usecases.rs``。
运行：``PYTHONPATH=src python -m pytest py-store/tests/test_multi_datasource.py -q``
"""

import asyncio

import pytest

from py_store import datasource, executors, init, permission, store
from py_store import schema as _sc

# ─── Mongo 桩驱动 ────────────────────────────────────────────

class _MemCursor:
    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, length=None):
        return list(self._docs)


class _MemColl:
    def __init__(self, docs):
        self._docs = docs or []

    def find(self, _filter=None, _projection=None):
        return _MemCursor(self._docs)

    async def aggregate(self, _pipeline=None):
        return _MemCursor(self._docs)

    async def count_documents(self, _filter=None):
        return len(self._docs)

    async def find_one(self, _filter=None, _projection=None):
        return dict(self._docs[0]) if self._docs else None

    async def insert_one(self, doc):
        self._docs.append(doc)

    async def list_indexes(self):
        return _MemCursor([])

    async def create_index(self, *_args, **_kwargs):
        return None


class FakeDb:
    """Mongo db 实例形态（``__getitem__`` 取集合；无 ``get_database``）"""

    def __init__(self, colls=None):
        self.colls = colls or {}
        self.accessed = []

    def __getitem__(self, name):
        self.accessed.append(name)
        return _MemColl(self.colls.get(name))


class FakeClient:
    """MongoClient 形态（``get_database`` 动态取库；无 ``get_collection``）"""

    def __init__(self):
        self.dbs = {}
        self.db_names = []

    def get_database(self, name):
        self.db_names.append(name)
        if name not in self.dbs:
            self.dbs[name] = FakeDb()
        return self.dbs[name]


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_context():
    permission.set_context(None)
    yield


# ─── B1：双 db 实例，同名集合，各查各库 ──────────────────────

def test_b1_two_mongo_db_instances_same_collection_no_cross_read():
    db_a = FakeDb({'users': [{'_id': 'a1', 'side': 'A'}]})
    db_b = FakeDb({'users': [{'_id': 'b1', 'side': 'B'}]})

    _sc.register({
        'name': 'B1UserA', 'collection': 'users', 'timestamps': False,
        'fields': {'side': {'type': 'string'}}, 'relations': {},
        'datasource': 'mongo_a',
    })
    _sc.register({
        'name': 'B1UserB', 'collection': 'users', 'timestamps': False,
        'fields': {'side': {'type': 'string'}}, 'relations': {},
        'datasource': 'mongo_b',
    })

    _run(init({'mongo_a': db_a, 'mongo_b': db_b}))

    got_a = _run(store.query('B1UserA{_id, side}'))
    got_b = _run(store.query('B1UserB{_id, side}'))

    assert got_a == [{'_id': 'a1', 'side': 'A'}], 'A 源应命中 A 库数据'
    assert got_b == [{'_id': 'b1', 'side': 'B'}], 'B 源应命中 B 库数据'


# ─── B2：单 MongoClient，双 namespace（db 名） ───────────────

def test_b2_single_mongo_client_two_namespaces():
    client = FakeClient()
    client.dbs['tenant_a'] = FakeDb({'b2_docs': [{'_id': 't1', 'tag': 'T-A'}]})
    client.dbs['tenant_b'] = FakeDb({'b2_docs': [{'_id': 't2', 'tag': 'T-B'}]})

    # 同 collection 名，仅靠 namespace 区分（三元组唯一性由 namespace 维度保证）
    _sc.register({
        'name': 'B2DocA', 'collection': 'b2_docs', 'timestamps': False,
        'fields': {'tag': {'type': 'string'}}, 'relations': {},
        'datasource': 'mongo_cluster', 'namespace': 'tenant_a',
    })
    _sc.register({
        'name': 'B2DocB', 'collection': 'b2_docs', 'timestamps': False,
        'fields': {'tag': {'type': 'string'}}, 'relations': {},
        'datasource': 'mongo_cluster', 'namespace': 'tenant_b',
    })

    _run(init({'mongo_cluster': client}))

    got_a = _run(store.query('B2DocA{_id, tag}'))
    got_b = _run(store.query('B2DocB{_id, tag}'))

    assert got_a == [{'_id': 't1', 'tag': 'T-A'}]
    assert got_b == [{'_id': 't2', 'tag': 'T-B'}]
    assert 'tenant_a' in client.db_names, '应按 namespace 取 client.get_database(tenant_a)'
    assert 'tenant_b' in client.db_names, '应按 namespace 取 client.get_database(tenant_b)'


# ─── B3：三元组冲突注册 → 抛错 ───────────────────────────────

def test_b3_conflicting_triple_registration_raises():
    _sc.register({
        'name': 'B3First', 'collection': 'b3_same', 'timestamps': False,
        'fields': {'v': {'type': 'string'}}, 'relations': {},
        'datasource': 'b3_src', 'namespace': 'b3_ns',
    })
    import re
    with pytest.raises(Exception, match=re.compile('三元组|已注册|唯一|conflict', re.I)):
        _sc.register({
            'name': 'B3Second', 'collection': 'b3_same', 'timestamps': False,
            'fields': {'v': {'type': 'string'}}, 'relations': {},
            'datasource': 'b3_src', 'namespace': 'b3_ns',
        })


# ─── B4：同 SQL 连接双 namespace（SQLite attached 代演 PG schema） ──

def _aiosqlite_or_skip():
    try:
        import aiosqlite
    except ImportError as e:
        pytest.skip(f'缺少 aiosqlite 驱动: {e}')
    return aiosqlite


def test_b4_same_sql_connection_two_namespaces():
    aiosqlite = _aiosqlite_or_skip()

    async def scenario():
        db = await aiosqlite.connect(':memory:')
        await db.execute("ATTACH ':memory:' AS app_a")
        await db.execute("ATTACH ':memory:' AS app_b")
        await db.execute('CREATE TABLE app_a.b4_rows (_id TEXT PRIMARY KEY, tag TEXT)')
        await db.execute('CREATE TABLE app_b.b4_rows (_id TEXT PRIMARY KEY, tag TEXT)')
        await db.commit()

        _sc.register({
            'name': 'B4RowA', 'collection': 'b4_rows', 'idPrefix': 'b4a_',
            'timestamps': False, 'fields': {'tag': {'type': 'string'}},
            'relations': {}, 'datasource': 'b4_sqlite', 'namespace': 'app_a',
        })
        _sc.register({
            'name': 'B4RowB', 'collection': 'b4_rows', 'idPrefix': 'b4b_',
            'timestamps': False, 'fields': {'tag': {'type': 'string'}},
            'relations': {}, 'datasource': 'b4_sqlite', 'namespace': 'app_b',
        })

        await init({'b4_sqlite': executors.create_connection('sqlite', db)})

        a = await store.insert('B4RowA', {'tag': 'NS-A'})
        b = await store.insert('B4RowB', {'tag': 'NS-B'})

        got_a = await store.query('B4RowA{_id, tag}')
        got_b = await store.query('B4RowB{_id, tag}')
        assert [d['tag'] for d in got_a] == ['NS-A']
        assert [d['tag'] for d in got_b] == ['NS-B']

        # 物理落库位置核对：namespace 即 attached db
        async def count(schema_name, doc_id):
            cur = await db.execute(
                f'SELECT COUNT(*) FROM {schema_name}.b4_rows WHERE _id = ?', (doc_id,))
            row = await cur.fetchone()
            await cur.close()
            return row[0]

        assert await count('app_a', a['_id']) == 1, 'A 应物理落在 app_a'
        assert await count('app_b', b['_id']) == 1, 'B 应物理落在 app_b'
        await db.close()

    asyncio.run(scenario())


# ─── B9：旧用法零变更（default source + None namespace） ─────

def test_b9_legacy_single_db_defaults():
    db = FakeDb({'b9_legacy': [{'_id': 'l1', 'name': 'legacy'}]})
    _sc.register({
        'name': 'B9Legacy', 'collection': 'b9_legacy', 'timestamps': False,
        'fields': {'name': {'type': 'string'}}, 'relations': {},
    })

    _run(init(db))  # 单实例旧用法

    plan = _sc.core.plan_query('B9Legacy{_id, name}', {})
    for c in plan['commands']:
        assert c['source'] == 'default'
        assert c['namespace'] is None

    got = _run(store.query('B9Legacy{_id, name}'))
    assert got == [{'_id': 'l1', 'name': 'legacy'}]


# ─── B10：Mongo 双形态严格校验（不猜） ───────────────────────

def test_b10_namespace_on_db_instance_raises():
    import re
    with pytest.raises(RuntimeError, match=re.compile('db 实例|namespace', re.I)):
        datasource.mongo_db(FakeDb(), 's', 'tenant_x')


def test_b10_client_without_namespace_raises():
    import re
    with pytest.raises(RuntimeError, match=re.compile('namespace', re.I)):
        datasource.mongo_db(FakeClient(), 's', None)


# ─── B8：route_override 同一 schema 落不同租户 namespace ─────

def test_b8_route_override_multi_tenant():
    aiosqlite = _aiosqlite_or_skip()

    async def scenario():
        db = await aiosqlite.connect(':memory:')
        await db.execute("ATTACH ':memory:' AS tenant_42")
        await db.execute('CREATE TABLE b8_rows (_id TEXT PRIMARY KEY, tag TEXT)')
        await db.execute('CREATE TABLE tenant_42.b8_rows (_id TEXT PRIMARY KEY, tag TEXT)')
        await db.commit()

        _sc.register({
            'name': 'B8Row', 'collection': 'b8_rows', 'idPrefix': 'b8_',
            'timestamps': False, 'fields': {'tag': {'type': 'string'}},
            'relations': {}, 'datasource': 'b8_sqlite',
        })

        await init({'b8_sqlite': executors.create_connection('sqlite', db)})

        # 带 override 写入租户库
        doc = await store.insert('B8Row', {'tag': 'T42'}, {'namespace': 'tenant_42'})

        # 带 override 读：命中租户库；不带 override 读：默认库为空
        got_tenant = await store.query('B8Row{_id, tag}', None, {'namespace': 'tenant_42'})
        assert [d['_id'] for d in got_tenant] == [doc['_id']]
        assert await store.query('B8Row{_id, tag}') == []
        assert await store.count('B8Row') == 0
        assert await store.count('B8Row', {}, {'namespace': 'tenant_42'}) == 1
        await db.close()

    asyncio.run(scenario())


# ─── B11：sync_schema({ namespace }) 回写 def ────────────────

def test_b11_sync_schema_writes_namespace():
    aiosqlite = _aiosqlite_or_skip()

    async def scenario():
        db = await aiosqlite.connect(':memory:')
        await db.execute("ATTACH ':memory:' AS aux")
        await db.execute('CREATE TABLE aux.b11_widgets (_id TEXT PRIMARY KEY, sku TEXT)')
        await db.commit()

        defs = await store.sync_schema(
            'sqlite', db,
            introspect_options={'database': 'aux'},
            datasource='b11_sqlite',
            namespace='aux',
            register_defs=False,
        )

        defn = next((d for d in defs if d['collection'] == 'b11_widgets'), None)
        assert defn is not None, '应产出 b11_widgets 定义'
        assert defn['namespace'] == 'aux', 'namespace 应回写到 def'
        assert defn['datasource'] == 'b11_sqlite'

        # 注册后路由与手动声明 namespace 的 schema 等价（同一 attached db 可查）
        manual = _sc.register({
            'name': 'B11Manual', 'collection': 'b11_widgets', 'idPrefix': 'b11_',
            'timestamps': False, 'fields': {'sku': {'type': 'string'}},
            'relations': {}, 'datasource': 'b11_sqlite', 'namespace': 'aux',
        })
        assert manual['namespace'] == 'aux'
        # 等价性：sync_schema 产出的 def 与手动声明的定位三元组一致
        assert {'source': defn['datasource'], 'namespace': defn['namespace'],
                'collection': defn['collection']} == \
               {'source': manual['datasource'], 'namespace': manual['namespace'],
                'collection': manual['collection']}

        await init({'b11_sqlite': executors.create_connection('sqlite', db)})
        doc = await store.insert('B11Manual', {'sku': 'w1'})
        got = await store.query('B11Manual{_id, sku}')
        assert len(got) == 1
        assert got[0]['_id'] == doc['_id']
        await db.close()

    asyncio.run(scenario())
