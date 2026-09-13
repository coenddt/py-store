"""py-store 行为测试（纯逻辑，无真实 DB）

schema / pipeline / permission / computes 的纯逻辑已全部下沉 Rust core（独立仓库 rust-store），
对拍由其 core/tests/parity*.rs + core-py/test/parity_test.py 覆盖；
本文件只测薄 Host 适配层的行为（读路径分支 + 写路径全流程，mock 驱动）。

对标 nodejs-store/tests/test-nodejs-store.js。
"""

import asyncio

from py_store import crud as _crud_mod
from py_store import permission as perm
from py_store import schema as _sc

# ─────────────────────────────────────────────────────────────
# 内置最小 schema（与业务工程 CommercialLedger 同构）
# ─────────────────────────────────────────────────────────────

_sc.register({
    'name': 'CommercialLedger', 'collection': 'commercial_ledger', 'idPrefix': 'CL', 'timestamps': True,
    'fields': {'unit': 'string', 'income': 'float'}, 'relations': {}, 'read': None, 'write': None,
})
_sc.register({
    'name': 'GoalLedger', 'collection': 'goal_ledger', 'idPrefix': 'GL', 'timestamps': True,
    'fields': {'income': 'float'}, 'relations': {}, 'read': None, 'write': None,
})
# 权限相关
_sc.register({
    'name': 'PermModel', 'collection': 'perm_model', 'timestamps': False,
    'fields': {
        'a': {'type': 'string'},
        'b': {'type': 'string', 'read': ['admin'], 'write': ['admin']},
    },
    'relations': {}, 'computes': {}, 'read': ['seller'], 'write': ['seller'],
})
_sc.register({
    'name': 'OwnerModel', 'collection': 'owner_model', 'timestamps': False,
    'fields': {'a': 'string'}, 'relations': {}, 'computes': {}, 'read': ['creator'],
})


def _run(coro):
    return asyncio.run(coro)


# ─────────────────────────────────────────────────────────────
# schema 镜像（薄适配层：core.register + Host 元数据 + 归档表派生）
# ─────────────────────────────────────────────────────────────

def test_schema_mirror_and_archive():
    s = _sc.get('CommercialLedger')
    assert s['idPrefix'] == 'CL'
    assert s['collection'] == 'commercial_ledger'
    # 自动派生归档表镜像
    assert _sc.has('CommercialLedgerDeleted') is True
    assert 'CommercialLedgerDeleted' in _sc.list()
    assert _sc.get('CommercialLedgerDeleted')['collection'] == 'commercial_ledger_deleted'


def test_schema_get_unregistered_raises():
    try:
        _sc.get('NotRegistered')
    except KeyError as e:
        assert '未注册' in str(e)
    else:
        raise AssertionError('应抛出 KeyError')


def test_store_camel_and_snake_aliases():
    from py_store import store
    assert callable(store.query) and callable(store.query_one)          # 蛇形：显式方法
    # 驼峰与蛇形为**同一实现**的别名（全显式绑定，非 __getattr__ 动态查找）
    assert store.queryOne.__func__ is store.query_one.__func__
    assert store.queryWithCount.__func__ is store.query_with_count.__func__
    assert store.buildPipeline.__func__ is store.build_pipeline.__func__
    assert store.register is _sc.register                               # 其余 API：显式绑定
    assert store.PermissionError is perm.PermissionError
    assert not hasattr(store, 'quer')                                   # 拼错属性即 AttributeError


# ─────────────────────────────────────────────────────────────
# 权限上下文 + core 权限方法包装
# ─────────────────────────────────────────────────────────────

def test_perm_scoped_roles_and_run_as_internal():
    perm.set_context(None)
    with perm.scoped_roles(['seller']):
        assert perm.get_context()['roles'] == ['seller']
    assert perm.get_context() is None  # 退出恢复原上下文

    async def _inner():
        return perm.get_context().get('internal')

    assert _run(perm.run_as_internal(_inner)) is True


def test_perm_schema_read_write():
    s = _sc.get('PermModel')
    assert perm.can_read_schema(s, {'roles': ['seller']}) is True
    assert perm.can_read_schema(s, {'roles': ['admin']}) is True
    assert perm.can_read_schema(s, {'roles': ['guest']}) is False
    assert perm.can_write_schema(s, {'roles': ['seller']}) is True
    assert perm.can_write_schema(s, {'roles': ['guest']}) is False


def test_perm_owner_condition():
    s = _sc.get('OwnerModel')
    # admin / internal 不注入，原样返回
    assert perm.merge_owner_condition(s, {'roles': ['admin']}, {}) == {}
    assert perm.merge_owner_condition(s, {'internal': True}, {'a': 1}) == {'a': 1}
    # 非 admin 且 read 含 creator → 注入 createdBy
    out = perm.merge_owner_condition(s, {'roles': ['seller'], 'userId': 'u1'}, None)
    assert out == {'createdBy': 'u1'}
    out2 = perm.merge_owner_condition(s, {'roles': ['seller'], 'userId': 'u1'}, {'year': 1})
    assert out2 == {'$and': [{'year': 1}, {'createdBy': 'u1'}]}


def test_perm_readable_and_writable_fields():
    s = _sc.get('PermModel')
    assert perm.get_readable_fields(s, None) is None      # 无上下文 → 不裁剪
    assert perm.get_readable_fields(s, {'roles': ['seller']}) == ['a']
    assert perm.get_writable_fields(s, {'roles': ['seller']}) == ['a']
    # 无上下文 → 不过滤写入数据
    assert perm.filter_writable_data(s, None, {'a': 1, 'b': 1}) == {'a': 1, 'b': 1}


# ─────────────────────────────────────────────────────────────
# crud 行为测试（mock 驱动）
# ─────────────────────────────────────────────────────────────

class _Result:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _MemCursor:
    def __init__(self, docs):
        self.docs = docs

    async def to_list(self, length=None):
        return list(self.docs)


class _MemColl:
    """内存版 collection：覆盖 crud 写路径所需全部方法"""

    def __init__(self, docs=None):
        self.docs = docs if docs is not None else []

    def find(self, query=None, projection=None):
        return _MemCursor(self.docs)

    async def aggregate(self, pipeline):
        # 对齐 PyMongo async：``aggregate`` 为协程（``find`` 则直接返回游标）
        return _MemCursor(self.docs)

    async def find_one(self, query=None, projection=None):
        return dict(self.docs[0]) if self.docs else None

    async def count_documents(self, filter=None):
        return len(self.docs)

    async def insert_one(self, doc):
        self.docs.append(doc)
        return _Result(inserted_id=doc.get('_id'))

    async def insert_many(self, docs):
        self.docs.extend(docs)
        return _Result(inserted_count=len(docs))

    async def replace_one(self, condition, doc, upsert=False):
        """归档幂等（insertMany + upsertById）走 replaceOne(upsert)"""
        for i, d in enumerate(self.docs):
            if d.get('_id') == condition.get('_id'):
                self.docs[i] = dict(doc)
                return _Result(modified_count=1)
        if upsert:
            self.docs.append(dict(doc))
        return _Result(modified_count=0)

    async def find_one_and_update(self, condition, update, **options):
        base = dict(self.docs[0]) if self.docs else {}
        for st in update.values():
            if isinstance(st, dict):
                base.update(st)
        if options.get('upsert') and not self.docs:
            self.docs.append(base)
        return base

    async def update_many(self, condition, data):
        return _Result(modified_count=len(self.docs))

    async def delete_many(self, condition):
        n = len(self.docs)
        self.docs = []
        return _Result(deleted_count=n)

    def list_indexes(self):
        return _MemCursor([])


class _FakeDb:
    """按 collection 名分配独立 coll；未知名自动建空 _MemColl"""

    def __init__(self, coll=None):
        self._colls = {}
        if coll is not None:
            self._colls['commercial_ledger'] = coll

    def __getitem__(self, name):
        if name not in self._colls:
            self._colls[name] = _MemColl()
        return self._colls[name]


def _crud_mock():
    """读路径 mock：内存 coll + 空权限 ctx"""
    docs = [{'unit': 'a', 'income': 100.0, '_id': '1'}]
    coll = _MemColl(docs)
    perm.set_context(None)
    _crud_mod.set_db(_FakeDb(coll))
    return coll, docs


def _crud_w_mock(docs=None):
    """写路径 mock：ctx=空 + 内存 coll"""
    coll = _MemColl(docs)
    perm.set_context(None)
    _crud_mod.set_db(_FakeDb(coll))
    return coll


def test_crud_query_plain_match():
    _crud_mock()
    items = _run(_crud_mod.query('CommercialLedger{unit, income}'))
    assert isinstance(items, list) and items[0]['unit'] == 'a'


def test_crud_query_one():
    _crud_mock()
    one = _run(_crud_mod.query_one('CommercialLedger{unit, income}'))
    assert one and one['unit'] == 'a'
    # GoalLedger 无数据 → null
    assert _run(_crud_mod.query_one('GoalLedger{income}')) is None


def test_crud_query_with_count_page():
    coll, _ = _crud_mock()

    async def _count200(*args, **kwargs):
        return 200

    coll.count_documents = _count200
    r = _run(_crud_mod.query_with_count(
        'CommercialLedger($skip:@s,$limit:@l){unit, income}', {'s': 0, 'l': 50}))
    assert r['total'] == 200
    assert r['pageSize'] == 50
    assert r['page'] == 0


def test_crud_query_with_count_pagesize_cap():
    _crud_mock()
    r = _run(_crud_mod.query_with_count('CommercialLedger{unit}', {'pageSize': 99999, 'page': 0}))
    assert r['pageSize'] == 5000  # 上限 5000 防拖库


def test_crud_insert_autoid_timestamp():
    coll = _crud_w_mock()
    doc = _run(_crud_mod.insert('CommercialLedger', {'unit': 'x', 'income': 9.0}))
    assert doc['_id'].startswith('CL')
    assert doc.get('createdAt') and doc.get('updatedAt')
    assert coll.docs[0]['unit'] == 'x'


def test_crud_insert_many_empty():
    _crud_w_mock()
    assert _run(_crud_mod.insert_many('CommercialLedger', [])) == []


def test_crud_insert_many_fills_ids():
    coll = _crud_w_mock()
    out = _run(_crud_mod.insert_many('CommercialLedger', [{'unit': 'a'}, {'unit': 'b'}]))
    assert len(out) == 2
    assert all(d['_id'].startswith('CL') for d in out)
    assert len(coll.docs) == 2


def test_crud_update_set_mode():
    _crud_w_mock([{'_id': '1', 'unit': 'a', 'income': 100.0, 'createdAt': 1, 'updatedAt': 1}])
    out = _run(_crud_mod.update('CommercialLedger', {'_id': '1'}, {'income': 200.0}))
    assert out and out['income'] == 200.0
    assert out.get('updatedAt')


def test_crud_update_raw_operators():
    _crud_w_mock([{'_id': '1', 'income': 100.0, 'createdAt': 1, 'updatedAt': 1}])
    out = _run(_crud_mod.update('CommercialLedger', {'_id': '1'}, {'$inc': {'income': 5}}))
    assert out is not None


def test_crud_update_empty_set_raises():
    _crud_w_mock([{'_id': '1', 'unit': 'a'}])
    try:
        _run(_crud_mod.update('CommercialLedger', {'_id': '1'}, {'_id': '1'}))
    except RuntimeError as e:
        assert '没有提供要更新的字段' in str(e)
    else:
        raise AssertionError('应抛出 RuntimeError')


def test_crud_update_many_raw_and_set():
    _crud_w_mock([{'income': 1.0}])
    r1 = _run(_crud_mod.update_many('CommercialLedger', {'income': 1.0}, {'$inc': {'income': 1}}))
    assert r1['modifiedCount'] == 1
    r2 = _run(_crud_mod.update_many('CommercialLedger', {'income': 1.0}, {'income': 2.0}))
    assert r2['modifiedCount'] == 1


def test_crud_remove_archives():
    # CommercialLedger 注册时已自动注册 CommercialLedgerDeleted，归档分支命中
    coll = _crud_w_mock([{'_id': '1', 'unit': 'a', 'income': 1.0}])
    r = _run(_crud_mod.remove('CommercialLedger', {'_id': '1'}))
    assert r['deletedCount'] == 1
    assert r['archivedCount'] == 1
    coll.docs = [{'_id': '2', 'unit': 'b'}]
    r2 = _run(_crud_mod.remove('CommercialLedger', {'_id': '2'}))
    assert r2['archivedCount'] == 1


def test_crud_exists_and_count():
    _crud_w_mock([{'_id': '1'}])
    assert _run(_crud_mod.exists('CommercialLedger', {'_id': '1'})) is True
    assert _run(_crud_mod.count('CommercialLedger', {'_id': '1'})) == 1


def test_crud_upsert_by_condition():
    _crud_w_mock()
    out = _run(_crud_mod.upsert('CommercialLedger', {'_id': 'u1'}, {'unit': 'upserted'}))
    assert out and out['unit'] == 'upserted'


def test_crud_upsert_generates_id():
    _crud_w_mock()
    out = _run(_crud_mod.upsert('CommercialLedger', {'year': 2026}, {'unit': 'x'}))
    assert out and out['_id'].startswith('CL')


def test_crud_mutation_single_and_array():
    _crud_w_mock()
    single = _run(_crud_mod.mutation('CommercialLedger', {'year': 2026, 'unit': 'solo'}))
    assert single and single['_id'].startswith('CL')
    arr = _run(_crud_mod.mutation('CommercialLedger', [{'unit': 'a'}, {'unit': 'b'}]))
    assert isinstance(arr, list) and len(arr) == 2


def test_crud_mutation_empty_array():
    _crud_w_mock()
    assert _run(_crud_mod.mutation('CommercialLedger', [])) == []


# ─────────────────────────────────────────────────────────────
# 扩展守卫：timestamps 单位 / feedback 事件
# ─────────────────────────────────────────────────────────────

def test_schema_timestamp_unit_seconds_insert():
    # timestamps: 's' → Host 时钟注入秒级时间戳（< 10 位量级）
    _sc.register({
        'name': 'SecLedger', 'collection': 'sec_ledger', 'idPrefix': 'SEC', 'timestamps': 's',
        'fields': {'unit': 'string'}, 'relations': {}, 'read': None, 'write': None,
    })
    assert _sc.get('SecLedger')['timestampUnit'] == 's'
    _crud_w_mock()
    doc = _run(_crud_mod.insert('SecLedger', {'unit': 'x'}))
    assert 0 < doc['createdAt'] < 10 ** 10
    assert 0 < doc['updatedAt'] < 10 ** 10


def test_schema_timestamp_unit_ms_default():
    # 缺省 timestamps: true → 毫秒级（13 位量级）
    _crud_w_mock()
    doc = _run(_crud_mod.insert('CommercialLedger', {'unit': 'x', 'income': 1.0}))
    assert doc['createdAt'] >= 10 ** 12


def test_schema_register_rejects_invalid_timestamps():
    # 行为收紧：原先任意非 false 值放行，现非法值注册即报错（core 校验）
    try:
        _sc.register({
            'name': 'BadLedger', 'collection': 'bad_ledger', 'timestamps': 'years',
            'fields': {}, 'relations': {},
        })
    except Exception as e:
        assert 'timestamps 仅支持' in str(e)
    else:
        raise AssertionError('非法 timestamps 应报错')


def test_feedback_sink_and_default_stderr():
    from py_store import feedback
    events = []
    feedback.set_sink(events.append)
    feedback.emit({'type': 'federation_degraded', 'code': 'crossSourceSort',
                   'layer': 'federation', 'message': 'm', 'hint': 'h'})
    assert events and events[0]['code'] == 'crossSourceSort'
    feedback.set_sink(None)  # 恢复默认 stderr（验证不抛错即可）
    feedback.emit({'code': 'x'})


def test_pushdown_unsupported_error_feedback():
    import py_store.datasource as ds
    err = ds.PushdownUnsupportedError('mysql_a', 'mysql', ['lookupTopN'], ['每父 top-N 无法下推'])
    assert isinstance(err, RuntimeError)  # 旧调用方兼容
    ev = err.feedback()
    assert ev['type'] == 'sql_pushdown_unsupported'
    assert ev['code'] == 'pushdownUnsupported'
    assert ev['layer'] == 'dialect'
    assert ev['source'] == 'mysql_a'


def test_exec_sql_unsupported_raises_and_emits():
    # 拦截即告警：exec_sql 抛结构化异常的同时自动产出反馈事件
    import py_store.datasource as ds
    from py_store import feedback

    class _FakeCore:
        def dialect_translate(self, kind, cmd):
            return {'unsupported': [{'code': 'lookupTopN'}], 'warnings': ['w1']}

    old_core, old_sink = ds._core, feedback._sink
    events = []
    ds._core = _FakeCore()
    feedback.set_sink(events.append)
    try:
        _run(ds.exec_sql('mysql_a', {'kind': 'mysql', 'exec': None},
                         {'collection': 'commercial_ledger'}))
    except ds.PushdownUnsupportedError as e:
        assert 'lookupTopN' in str(e)
    else:
        raise AssertionError('应抛出 PushdownUnsupportedError')
    finally:
        ds._core = old_core
        feedback.set_sink(old_sink)
    assert events and events[0]['type'] == 'sql_pushdown_unsupported'
