"""Atlas 向量索引幂等创建（mock 驱动）— 执行文档 02 §5 步骤 8。"""

import asyncio

from py_store import datasource, schema, vector_index


def _run(coro):
    return asyncio.run(coro)


def _one_store(name):
    defn = schema.get(name)
    return _StoreView(name, defn)


class _StoreView:
    """单 schema 视图：避免共享注册表让其他用例的 schema 混入同一次调用"""

    def __init__(self, name, defn):
        self._name = name
        self._defn = defn

    def list(self):
        return [self._name]

    def get(self, name):
        return self._defn if name == self._name else None


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    async def to_list(self, length=None):
        return list(self._rows)


class _Coll:
    def __init__(self, existing):
        self.existing = existing
        self.created = []

    def list_search_indexes(self):
        return _Cursor(self.existing)

    async def create_search_index(self, spec):
        self.created.append(spec)


class _Db:
    def __init__(self, coll):
        self.coll = coll
        self.collection = None

    def __getitem__(self, name):
        self.collection = name
        return self.coll


def test_creates_with_default_cosine_and_field_dim():
    schema.register({
        'name': 'PVecIdxOne',
        'collection': 'pvec_idx_one',
        'fields': {'_id': {'type': 'string'}, 'embedding': {'type': 'vector', 'dim': 8}},
        'indexes': [{'keys': {'embedding': 'vector'}, 'options': {'indexName': 'pvidx_one'}}],
    })
    coll = _Coll([])
    db = _Db(coll)
    datasource.set_connections({'default': db})

    out = _run(vector_index.ensure_vector_indexes(_one_store('PVecIdxOne')))

    assert out == {'created': ['pvidx_one'], 'skipped': []}
    assert db.collection == 'pvec_idx_one'
    assert coll.created == [{
        'name': 'pvidx_one',
        'definition': {'fields': [{
            'type': 'vector', 'path': 'embedding', 'numDimensions': 8, 'similarity': 'cosine',
        }]},
    }]


def test_option_metric_precedes_field_metric():
    schema.register({
        'name': 'PVecIdxTwo',
        'collection': 'pvec_idx_two',
        'fields': {'_id': {'type': 'string'},
                   'embedding': {'type': 'vector', 'dim': 4, 'metric': 'l2'}},
        'indexes': [{'keys': {'embedding': 'vector'},
                     'options': {'indexName': 'pvidx_two', 'metric': 'ip'}}],
    })
    coll = _Coll([])
    datasource.set_connections({'default': _Db(coll)})

    _run(vector_index.ensure_vector_indexes(_one_store('PVecIdxTwo')))

    assert coll.created[0]['definition']['fields'][0]['similarity'] == 'dotProduct'


def test_existing_index_skipped():
    schema.register({
        'name': 'PVecIdxThree',
        'collection': 'pvec_idx_three',
        'fields': {'_id': {'type': 'string'},
                   'embedding': {'type': 'vector', 'dim': 8, 'metric': 'cosine'}},
        'indexes': [{'keys': {'embedding': 'vector'}, 'options': {'indexName': 'pvidx_three'}}],
    })
    coll = _Coll([{'name': 'pvidx_three'}])
    datasource.set_connections({'default': _Db(coll)})

    out = _run(vector_index.ensure_vector_indexes(_one_store('PVecIdxThree')))

    assert out == {'created': [], 'skipped': ['pvidx_three']}
    assert coll.created == []


def test_without_index_name_does_not_connect():
    schema.register({
        'name': 'PVecIdxFour',
        'collection': 'pvec_idx_four',
        'fields': {'_id': {'type': 'string'}, 'embedding': {'type': 'vector', 'dim': 8}},
        'indexes': [{'keys': {'embedding': 'vector'}, 'options': {'method': 'hnsw'}}],
    })
    datasource.set_connections({})

    out = _run(vector_index.ensure_vector_indexes(_one_store('PVecIdxFour')))

    assert out == {'created': [], 'skipped': []}


def test_invalid_dim_raises():
    # 注册期由 core 校验（A1，rust 侧 vector_fields 用例覆盖）；
    # 这里直接用伪造视图绕过注册期，验证 ensure_vector_indexes 自身的防御分支。
    defn = {
        'name': 'PVecIdxFiveFake',
        'collection': 'pvec_idx_five_fake',
        'fields': {'embedding': {'type': 'vector'}},
        'indexes': [{'keys': {'embedding': 'vector'}, 'options': {'indexName': 'pvidx_five_fake'}}],
    }
    datasource.set_connections({'default': _Db(_Coll([]))})

    try:
        _run(vector_index.ensure_vector_indexes(_StoreView(defn['name'], defn)))
    except ValueError as e:
        assert 'dim/metric 非法' in str(e)
    else:  # pragma: no cover - 防御：不抛即失败
        raise AssertionError('dim 非法必须显式抛错')


def test_non_mongo_source_raises():
    schema.register({
        'name': 'PVecIdxSix',
        'collection': 'pvec_idx_six',
        'fields': {'_id': {'type': 'string'}, 'embedding': {'type': 'vector', 'dim': 8}},
        'indexes': [{'keys': {'embedding': 'vector'}, 'options': {'indexName': 'pvidx_six'}}],
    })
    datasource.set_connections({'default': {'kind': 'sqlite'}})

    try:
        _run(vector_index.ensure_vector_indexes(_one_store('PVecIdxSix')))
    except ValueError as e:
        assert '不是 Mongo 源' in str(e)
    else:  # pragma: no cover - 防御：不抛即失败
        raise AssertionError('非 Mongo 源声明 indexName 必须显式抛错')
