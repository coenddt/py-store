"""向量相关纯逻辑分支单测（4.7.0 发版后 ci 覆盖率门禁回归补齐）

4.7.0 新增向量通道后 ci 覆盖率跌破 90%（88.10%）；本文件按
``test_coverage_margin.py`` 同一模式逐条覆盖新代码的分支盲区：
  - vector_scan：命令判定 / 剥离 / 候选管道 / scan 入参守卫 / 40324+6047401 降级判定；
  - capabilities：未配置源反馈 / postgres 探测（execute_raw 桩）/ Atlas 声明探测；
  - vector_index：_is_integer / 空 def / 无 keys 分支；
  - ddl：向量列类型、向量索引语句全分支（pgvector hnsw·ivfflat·flat 与不支持反馈）、
    迁移生成的向量边界。

全部零外部服务依赖：native core 走 PyPI rust-store-py，execute_raw 用桩。
运行：python -m pytest tests/test_vector_units.py -q
"""

import asyncio

import pytest

from py_store import (
    capabilities,
    datasource,
    ddl,
    feedback,
    schema,
    vector_index,
    vector_scan,
)
from py_store import (
    core as core_mod,
)

_EVENTS = []


@pytest.fixture(autouse=True)
def _isolate():
    """反馈事件收进 _EVENTS 供断言；连接映射随用例即清，不跨用例渗漏"""
    _EVENTS.clear()
    feedback.set_sink(_EVENTS.append)
    yield
    datasource.set_connections({})
    feedback.set_sink(None)


def _vcmd(**spec_over):
    """最小 $vectorSearch 命令（无 $addFields 尾随，够触发全部分判定）"""
    spec = {'index': 'vix', 'path': 'emb', 'queryVector': [1, 0],
            'limit': 2, 'metric': 'cosine'}
    spec.update(spec_over)
    return {'kind': 'aggregate', 'collection': 'c', 'pipeline': [{'$vectorSearch': spec}]}


# ─── vector_scan：命令判定与剥离 ─────────────────────────────

def test_is_vector_command_shapes():
    assert vector_scan.is_vector_command(_vcmd()) is True
    assert vector_scan.is_vector_command({'kind': 'find'}) is False
    assert vector_scan.is_vector_command({'kind': 'aggregate', 'pipeline': []}) is False
    assert vector_scan.is_vector_command({'kind': 'aggregate', 'pipeline': 'x'}) is False
    assert vector_scan.is_vector_command(
        {'kind': 'aggregate', 'pipeline': [{'$match': {}}]}) is False


def test_has_filter_shapes():
    assert vector_scan.has_filter(_vcmd(filter={'a': 1})) is True
    assert vector_scan.has_filter(_vcmd()) is False, '缺 filter 视为无过滤'
    assert vector_scan.has_filter(_vcmd(filter={})) is False
    assert vector_scan.has_filter(
        {'kind': 'aggregate', 'pipeline': [{'$vectorSearch': 'bad'}]}) is False
    assert vector_scan.has_filter({'kind': 'find'}) is False


def test_can_scan_backend_routing():
    assert vector_scan.can_scan(_vcmd(), 'mysql') is True
    assert vector_scan.can_scan(_vcmd(filter={'a': 1}), 'postgres') is True
    assert vector_scan.can_scan(_vcmd(), 'postgres') is False, \
        'postgres 无过滤走原生 pgvector SQL，不进降级扫描'
    assert vector_scan.can_scan({'kind': 'find'}, 'mysql') is False


def test_is_score_stage_shapes():
    good = {'$addFields': {'_score': {'$meta': 'vectorSearchScore'}}}
    assert vector_scan._is_score_stage(good) is True
    assert vector_scan._is_score_stage('x') is False
    assert vector_scan._is_score_stage({'$addFields': {'_score': 1, 'other': 2}}) is False
    assert vector_scan._is_score_stage({'$addFields': {'_score': {'$meta': 'other'}}}) is False
    assert vector_scan._is_score_stage({'$match': {}}) is False


def test_strip_score_recursion():
    assert vector_scan._strip_score([{'_score': 1}, {'a': 1}]) == [{'a': 1}]
    assert vector_scan._strip_score({'b': {'c': 1, '_score': 2}}) == {'b': {'c': 1}}
    assert vector_scan._strip_score({'_score': 5}) is None
    assert vector_scan._strip_score({'d': {'_score': 1}}) is None, '值被剥空后整键丢弃'


def test_candidate_pipeline_keeps_matches_only():
    out = vector_scan._candidate_pipeline(
        {'status': 'p', '_score': 1},
        [{'$limit': 3}, {'$match': {'x': 1}}, 'junk', {'$match': {'y': {'_score': 2}}}])
    assert out == [{'$match': {'status': 'p'}}, {'$match': {'x': 1}}], \
        '候选只保留可剥离出条件的 $match；$limit / 非映射阶段一律不进'


def test_registry_prefers_argument_with_local_eval():
    class _Core:
        def local_eval(self, collections, cmd):
            return {'result': 'from-arg'}

    stub = _Core()
    assert vector_scan._registry(stub) is stub
    assert vector_scan._registry(None) is core_mod.core, '缺省回落 core 单例'


def test_scan_input_guards():
    with pytest.raises(TypeError, match='exec'):
        asyncio.run(vector_scan.scan(_vcmd(), exec='nope'))
    with pytest.raises(ValueError, match='\\$vectorSearch'):
        asyncio.run(vector_scan.scan({'kind': 'find'}, exec=lambda c: []))
    no_path = {'kind': 'aggregate', 'collection': 'c',
               'pipeline': [{'$vectorSearch': {'limit': 1}}]}
    with pytest.raises(ValueError, match='path'):
        asyncio.run(vector_scan.scan(no_path, exec=lambda c: []))


def test_scan_candidate_must_be_list():
    async def exec_fn(_cmd):
        return 'nope'

    with pytest.raises(TypeError, match='非数组'):
        asyncio.run(vector_scan.scan(_vcmd(), exec=exec_fn))


def test_scan_full_flow_with_core_argument():
    class _Core:
        def local_eval(self, collections, cmd):
            rows = collections[cmd['collection']]
            return {'result': sorted(rows, key=lambda r: r['_id'])}

    async def exec_fn(cmd):
        assert cmd['pipeline'] == [{'$match': {'status': 'p'}}], '候选管道只保留 $match'
        return [{'_id': 'b', 'status': 'p'}, {'_id': 'a', 'status': 'p'}]

    out = asyncio.run(vector_scan.scan(
        _vcmd(filter={'status': 'p'}), exec=exec_fn, core=_Core()))
    assert out == [{'_id': 'a', 'status': 'p'}, {'_id': 'b', 'status': 'p'}], \
        '打分与 TopK 由 core 按原始完整管道重放'


def test_strip_vector_meta_skips_non_mapping_stage():
    cmd = {'kind': 'aggregate', 'pipeline': [
        'junk',
        {'$vectorSearch': {'path': 'emb', 'metric': 'cosine'}},
        {'$match': {'a': 1}},
    ]}
    out = vector_scan.strip_vector_meta(cmd)
    assert out['pipeline'][0] == 'junk'
    assert out['pipeline'][1] == {'$vectorSearch': {'path': 'emb'}}, '仅剥离内部 metric 键'


def test_is_unsupported_stage_error_two_forms():
    assert vector_scan.is_unsupported_stage_error(None) is False

    e40324 = RuntimeError('unrecognized')
    e40324.code = 40324
    e6047401 = RuntimeError('atlas only')
    e6047401.code = 6047401
    assert vector_scan.is_unsupported_stage_error(e40324) is True
    assert vector_scan.is_unsupported_stage_error(e6047401) is True, \
        'mongod 7+ 认识该阶段但仅 Atlas 可执行，与 40324 降级语义等价'

    msg_only = RuntimeError("Unrecognized pipeline stage name: '$vectorSearch'")
    assert vector_scan.is_unsupported_stage_error(msg_only) is True
    assert vector_scan.is_unsupported_stage_error(RuntimeError('topology closed')) is False


# ─── vector_index：分支盲区 ─────────────────────────────────

def test_is_integer_excludes_bool():
    assert vector_index._is_integer(True) is False
    assert vector_index._is_integer(4) is True
    assert vector_index._is_integer(4.0) is True
    assert vector_index._is_integer(4.5) is False


def test_ensure_vector_indexes_skips_missing_def_and_keyless_index():
    class _Store:
        def list(self):
            return ['ghost', 'real']

        def get(self, name):
            if name == 'ghost':
                return None
            return {'name': name, 'fields': {}, 'indexes': [{}]}

    out = asyncio.run(vector_index.ensure_vector_indexes(_Store()))
    assert out == {'created': [], 'skipped': []}


# ─── capabilities：探测分支（execute_raw 桩） ─────────────────

def test_backend_of_maps_mongo_and_none():
    assert capabilities._backend_of({'kind': 'mongo'}) == 'mongodb'
    assert capabilities._backend_of({}) == 'mongodb', '无 kind 视为 Mongo'
    assert capabilities._backend_of({'kind': 'mysql'}) == 'mysql'


def _register_atlas_schema(name, collection, source, with_index_name):
    idx = {'keys': {'emb': 'vector'}}
    if with_index_name:
        idx['options'] = {'indexName': 'vix'}
    schema.register({
        'name': name, 'collection': collection, 'timestamps': False,
        'fields': {'emb': {'type': 'vector', 'dim': 2}}, 'relations': {},
        'datasource': source, 'indexes': [idx],
    })


def test_atlas_declared_requires_index_name():
    _register_atlas_schema('VecCapAtlas', 'vec_cap_atlas', 'unit_mongo', True)
    _register_atlas_schema('VecCapNoName', 'vec_cap_no_name', 'unit_mongo2', False)
    assert capabilities._atlas_declared('unit_mongo') is True
    assert capabilities._atlas_declared('unit_mongo2') is False, '无名索引声明不算 Atlas'
    assert capabilities._atlas_declared('unit_mongo_none') is False


def test_pgvector_ready_rows_and_probe_failure(monkeypatch):
    async def ok(_source, _sql, _params=None, _is_write=None):
        return {'rows': [1], 'affectedRows': 0}

    monkeypatch.setattr(datasource, 'execute_raw', ok)
    assert asyncio.run(capabilities._pgvector_ready('unit_pg')) is True

    async def boom(_source, _sql, _params=None, _is_write=None):
        raise RuntimeError('连接拒绝')

    monkeypatch.setattr(datasource, 'execute_raw', boom)
    assert asyncio.run(capabilities._pgvector_ready('unit_pg')) is False
    assert any(e['code'] == 'capabilityProbeFailed' for e in _EVENTS), \
        '探测失败必须留痕（禁静默改判）'


def test_capabilities_unknown_source_reports_feedback():
    out = asyncio.run(capabilities.capabilities('unit_missing'))
    assert out['source'] == 'unit_missing'
    assert out['vector'] == {'supported': False, 'native': False,
                             'backend': 'unknown', 'metrics': [], 'maxDim': 0}
    assert any(e['code'] == 'capabilityUnknownSource' for e in _EVENTS)


def test_capabilities_postgres_native_probe(monkeypatch):
    datasource.set_connections({'unit_pg': {'kind': 'postgres'}})

    async def ok(_source, _sql, _params=None, _is_write=None):
        return {'rows': [1], 'affectedRows': 0}

    monkeypatch.setattr(datasource, 'execute_raw', ok)
    out = asyncio.run(capabilities.capabilities('unit_pg'))
    assert out['vector'] == {'backend': 'pgvector', 'maxDim': 2000,
                             'metrics': ['cosine', 'l2', 'ip'],
                             'native': True, 'supported': True}


def test_capabilities_mongo_atlas_declaration():
    datasource.set_connections({'unit_mongo': {'kind': 'mongo'}})
    _register_atlas_schema('VecCapAtlas2', 'vec_cap_atlas2', 'unit_mongo', True)
    out = asyncio.run(capabilities.capabilities('unit_mongo'))
    assert out['vector']['backend'] == 'atlas'
    assert out['vector']['native'] is True, '声明 options.indexName 即按 Atlas 能力上报'


# ─── ddl：向量列类型与向量索引语句 ───────────────────────────

def test_js_and_positive_int_helpers():
    assert ddl._js(None) == 'undefined'
    assert ddl._js(True) == 'true'
    assert ddl._js(2.0) == '2'
    assert ddl._js('x') == '"x"'
    assert ddl._js([1, 'a']) == '[1, "a"]'

    assert ddl._is_positive_int(True) is False
    assert ddl._is_positive_int(0) is False
    assert ddl._is_positive_int(3.0) is True
    assert ddl._is_positive_int('3') is False


def test_vector_type_per_backend():
    fdef = {'type': 'vector', 'dim': 4}
    assert ddl._vector_type('postgres', fdef) == 'vector(4)'
    assert ddl._vector_type('mysql', fdef) == 'JSON'
    assert ddl._vector_type('sqlite', fdef) == 'TEXT'
    with pytest.raises(ValueError, match='dim'):
        ddl._vector_type('postgres', {'type': 'vector'})


def test_pname_passthrough_empty():
    assert ddl._pname('postgres', '') == ''
    assert ddl._pname('postgres', None) is None


def test_vector_index_stmt_mysql_unsupported_feedback():
    out = ddl._vector_index_stmt(
        {'name': 'VecIx', 'collection': 'vec_ix_docs'}, 'mysql', 'emb', {})
    assert out == []
    assert any(e['code'] == 'vectorIndexUnsupported' for e in _EVENTS)


def test_vector_index_stmt_guards():
    no_dim = {'name': 'VecIx', 'collection': 'vec_ix_docs',
              'fields': {'emb': {'type': 'vector'}}}
    with pytest.raises(ValueError, match='dim'):
        ddl._vector_index_stmt(no_dim, 'postgres', 'emb', {})

    dim4 = {'name': 'VecIx', 'collection': 'vec_ix_docs',
            'fields': {'emb': {'type': 'vector', 'dim': 4}}}
    with pytest.raises(ValueError, match='method'):
        ddl._vector_index_stmt(dim4, 'postgres', 'emb', {'method': 'goss'})
    with pytest.raises(ValueError, match='上限'):
        ddl._vector_index_stmt(
            {'name': 'VecIx', 'collection': 'vec_ix_docs',
             'fields': {'emb': {'type': 'vector', 'dim': 2001}}},
            'postgres', 'emb', {})
    with pytest.raises(ValueError, match='metric'):
        ddl._vector_index_stmt(dim4, 'postgres', 'emb', {'metric': 'dot'})


def test_vector_index_stmt_flat_no_index():
    out = ddl._vector_index_stmt(
        {'name': 'VecIx', 'collection': 'vec_ix_docs',
         'fields': {'emb': {'type': 'vector', 'dim': 4}}},
        'postgres', 'emb', {'method': 'flat'})
    assert out == []
    assert any(e['code'] == 'vectorIndexFlatNoIndex' for e in _EVENTS)


def test_vector_index_stmt_hnsw_and_ivfflat():
    defn = {'name': 'VecIx', 'collection': 'vec_ix_docs',
            'fields': {'emb': {'type': 'vector', 'dim': 4}}}
    hnsw = ddl._vector_index_stmt(defn, 'postgres', 'emb', {'m': 24, 'efConstruction': 96})
    assert len(hnsw) == 1
    assert 'USING hnsw' in hnsw[0] and 'vector_cosine_ops' in hnsw[0]
    assert 'm = 24' in hnsw[0] and 'ef_construction = 96' in hnsw[0]

    ivf = ddl._vector_index_stmt(
        defn, 'postgres', 'emb', {'method': 'ivfflat', 'lists': 50, 'metric': 'ip'})
    assert 'USING ivfflat' in ivf[0] and 'vector_ip_ops' in ivf[0]
    assert 'lists = 50' in ivf[0]

    default_ivf = ddl._vector_index_stmt(defn, 'postgres', 'emb', {'method': 'ivfflat'})
    assert 'lists = 100' in default_ivf[0], 'lists 缺省 100'


def test_index_stmts_skips_malformed_entries():
    defn = {'name': 'VecIx', 'collection': 'vec_ix_docs',
            'fields': {'emb': {'type': 'vector', 'dim': 4}},
            'indexes': ['junk', {'keys': {}}]}
    assert ddl._index_stmts(defn, 'postgres') == []


def test_index_stmts_vector_single_field_dispatch():
    defn = {'name': 'VecIx', 'collection': 'vec_ix_docs',
            'fields': {'emb': {'type': 'vector', 'dim': 4}},
            'indexes': [{'keys': {'emb': 'vector'}, 'options': {'metric': 'l2'}}]}
    stmts = ddl._index_stmts(defn, 'postgres')
    assert len(stmts) == 1 and 'vector_l2_ops' in stmts[0]

    with pytest.raises(ValueError, match='单字段'):
        ddl._index_stmts(
            {'name': 'VecIx', 'collection': 'vec_ix_docs',
             'fields': {'emb': {'type': 'vector', 'dim': 4}},
             'indexes': [{'keys': {'emb': 'vector', 'title': 1}}]}, 'postgres')


# ─── ddl：迁移生成的向量边界 ─────────────────────────────────

def test_field_sql_type_and_sql_literal():
    assert ddl._field_sql_type('postgres', {'type': 'vector', 'dim': 4}) == 'vector(4)'
    assert ddl._field_sql_type('mysql', {'type': 'object'}) == 'JSON'
    with pytest.raises(ValueError, match='未知'):
        ddl._field_sql_type('sqlite', {'type': 'blob'})

    assert ddl._sql_literal(True) == 'TRUE'
    assert ddl._sql_literal(False) == 'FALSE'
    assert ddl._sql_literal(3) == '3'
    assert ddl._sql_literal("o'brien") == "'o''brien'"
    with pytest.raises(ValueError, match='标量'):
        ddl._sql_literal({'a': 1})


def test_diff_defs_rejects_vector_meta_change_and_unknown_new_type():
    old = {'name': 'DiffVec', 'fields': {'emb': {'type': 'vector', 'dim': 4, 'metric': 'cosine'}}}
    new = {'name': 'DiffVec', 'fields': {'emb': {'type': 'vector', 'dim': 8, 'metric': 'cosine'}}}
    plan = ddl.diff_defs(old, new)
    assert any('vector 元数据变更不支持' in e for e in plan['errors'])

    old2 = {'name': 'DiffNew', 'fields': {'a': 'int'}}
    new2 = {'name': 'DiffNew', 'fields': {'a': 'int', 'b': 'blob'}}
    plan2 = ddl.diff_defs(old2, new2)
    assert any("类型 'blob' 未知" in e for e in plan2['errors'])


def test_generate_migration_unsupported_and_vector_default():
    with pytest.raises(ValueError, match='MIGRATION_UNSUPPORTED'):
        ddl.generate_migration('postgres', {'name': 'MigVec', 'fields': {'a': 'int'}},
                               {'name': 'MigVec', 'fields': {'a': 'string'}})
    old = {'name': 'MigVec2', 'fields': {'a': 'int'}}
    new = {'name': 'MigVec2',
           'fields': {'a': 'int',
                      'emb': {'type': 'vector', 'dim': 4, 'default': [0, 0, 0, 0]}}}
    with pytest.raises(ValueError, match='不支持 DEFAULT'):
        ddl.generate_migration('postgres', old, new)
