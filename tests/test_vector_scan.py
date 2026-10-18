"""向量检索降级扫描（exec 03 §4.7）— 对齐 ``nodejs-store/tests/vector-scan.test.js``

覆盖：
  - SQLite：真实引擎，``$vectorSearch.filter`` 预过滤 + TopK + 零向量跳过；
  - MySQL：驱动替身，``exec_sql`` 识别向量命令 → 候选只含 ``$match`` → core 精确打分；
  - Mongo：驱动替身，原生 ``$vectorSearch`` 报 40324 → 降级；``metric`` 只在物理命令上剥离。

``_score`` 的投影保留由 04 步骤 1 负责（本步只验降级链路：过滤、排序、条数、截断保护）。
本机无 MySQL 实例，MySQL 用替身验证编排（真实库为「待核实」项）。
"""

import asyncio
import re

import aiosqlite
import pytest

from py_store import (
    datasource, ddl, executors, naming, permission, schema, store, vector_scan,
)
from py_store.crud.exec import _exec_on

DIM = 4
Q = [1, 0, 0, 0]

# 逻辑行（cosine 分数：a=1、b=0.8、c=0、archived=1、zero 跳过）
ROWS = [
    {'_id': 'vs_a', 'title': 'Alpha', 'status': 'published', 'embedding': [1, 0, 0, 0]},
    {'_id': 'vs_b', 'title': 'Beta', 'status': 'published', 'embedding': [0.8, 0.6, 0, 0]},
    {'_id': 'vs_c', 'title': 'Gamma', 'status': 'published', 'embedding': [0, 1, 0, 0]},
    {'_id': 'vs_d', 'title': 'Archived', 'status': 'archived', 'embedding': [1, 0, 0, 0]},
    {'_id': 'vs_e', 'title': 'Zero', 'status': 'published', 'embedding': [0, 0, 0, 0]},
]


def _register_test_schemas():
    """供 conftest 在模块首个用例前重放（import 零副作用）"""
    for name, collection, source in (
            ('PvsSqlDoc', 'pvs_sql_docs', 'pvs_sql'),
            ('PvsMyDoc', 'pvs_my_docs', 'pvs_my'),
            ('PvsMongoDoc', 'pvs_mongo_docs', 'pvs_mongo')):
        schema.register({
            'name': name,
            'collection': collection,
            'idPrefix': 'vs_',
            'timestamps': False,
            'fields': {
                '_id': {'type': 'string'},
                'title': {'type': 'string'},
                'status': {'type': 'string'},
                'embedding': {'type': 'vector', 'dim': DIM, 'metric': 'cosine'},
            },
            'relations': {},
            'datasource': source,
        })


def _vector_command(collection, source, extra_stages=None):
    """手工 aggregate 命令（core 产出的中间形态；limit 3、cosine）"""
    return {
        'kind': 'aggregate',
        'collection': collection,
        'source': source,
        'pipeline': [
            {
                '$vectorSearch': {
                    'index': 'vector_index',
                    'path': 'embedding',
                    'queryVector': Q,
                    'numCandidates': 30,
                    'limit': 3,
                    'metric': 'cosine',
                    'filter': {'status': 'published'},
                },
            },
            {'$addFields': {'_score': {'$meta': 'vectorSearchScore'}}},
            *(extra_stages or []),
        ],
    }


@pytest.fixture(autouse=True)
def _no_permission_context():
    permission.set_context(None)
    yield


def test_sqlite_real_engine_filter_and_topk():
    async def run():
        db = await aiosqlite.connect(':memory:')
        try:
            await db.executescript(ddl.generate('sqlite', ['PvsSqlDoc']))
            datasource.set_connections(
                {'pvs_sql': executors.create_connection('sqlite', db)})
            for row in ROWS:
                await store.insert('PvsSqlDoc', row)
            return await store.query(
                'PvsSqlDoc($condition:@c0){_id, title}',
                {'c0': {
                    'status': 'published',
                    'embedding': {'$near': {'$vector': Q, '$limit': 3}},
                }})
        finally:
            await db.close()

    rows = asyncio.run(run())

    # 预过滤 archived 被排除；零向量行跳过；TopK=3
    assert [r['_id'] for r in rows] == ['vs_a', 'vs_b', 'vs_c']


def test_mysql_stub_candidate_only_match():
    plans = []

    async def exec_fn(plan):
        plans.append(plan)
        return {'docs': [r for r in ROWS if r['status'] == 'published']}

    conn = {'kind': 'mysql', 'exec': exec_fn}
    datasource.set_connections({'pvs_my': conn})

    out = asyncio.run(datasource.exec_sql(
        'pvs_my', conn, _vector_command('pvs_my_docs', 'pvs_my')))

    assert len(plans) == 1, '降级只应执行一次候选查询'
    # `SqlStmt.to_value()` 的 SQL 文本键为 `text`（`dialect/ir.rs:41-48`）
    sql = '\n'.join(s['text'] for s in plans[0]['stmts'])
    assert not re.search(r'vectorSearch|embedding\s*<=>', sql, re.I), \
        '候选 SQL 不得包含向量阶段'
    assert re.search(r'status', sql, re.I)

    assert [r['_id'] for r in out] == ['vs_a', 'vs_b', 'vs_c']

    # 原命令未被降级路径就地改写（metric 仍需供 local 求值）
    original = _vector_command('pvs_my_docs', 'pvs_my')
    assert original['pipeline'][0]['$vectorSearch']['metric'] == 'cosine'


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    async def to_list(self, length=None):
        return list(self._rows)


class _Coll:
    def __init__(self, seen):
        self._seen = seen

    async def aggregate(self, pipeline, **kwargs):
        self._seen.append(pipeline)
        if any(isinstance(s, dict) and s.get('$vectorSearch') for s in pipeline):
            err = RuntimeError("Unrecognized pipeline stage name: '$vectorSearch'")
            err.code = 40324
            raise err
        return _Cursor([r for r in ROWS if r['status'] == 'published'])


class _Db:
    def __init__(self, coll):
        self._coll = coll

    def __getitem__(self, name):
        return self._coll


def test_mongo_stub_40324_degrade_and_strip_metric():
    seen = []
    datasource.set_connections({'pvs_mongo': _Db(_Coll(seen))})

    out = asyncio.run(
        _exec_on('pvs_mongo', _vector_command('pvs_mongo_docs', 'pvs_mongo')))

    assert len(seen) == 2, '先原生一次，再降级候选一次'
    assert any(isinstance(s, dict) and s.get('$vectorSearch') for s in seen[0]), \
        '首次必须是原生 $vectorSearch'
    assert seen[0][0]['$vectorSearch'].get('metric') is None, \
        '驱动不得收到内部 metric 键'
    assert not any(isinstance(s, dict) and s.get('$vectorSearch') for s in seen[1]), \
        '候选阶段不得含 $vectorSearch'

    assert [r['_id'] for r in out] == ['vs_a', 'vs_b', 'vs_c']


def test_candidate_overflow_raises():
    rows = [{'embedding': Q} for _ in range(3)]

    async def exec_fn(cmd):
        return rows

    with pytest.raises(vector_scan.VectorScanOverflow) as ei:
        asyncio.run(vector_scan.scan(
            _vector_command('pvs_sql_docs', 'pvs_sql'), exec=exec_fn, max_rows=2))

    assert ei.value.code == 'vectorScanOverflow'
    assert '禁止静默截断' in str(ei.value)


def test_naming_translates_vector_field_path():
    """``$vectorSearch.path`` 是字段名（须按数据标识符译），``metric`` 等键原样保留"""
    schema.register({
        'name': 'PvsCamelDoc',
        'collection': 'pvs_camel_docs',
        'timestamps': False,
        'fields': {'_id': {'type': 'string'},
                   'cover_url': {'type': 'vector', 'dim': DIM}},
        'relations': {},
        'datasource': 'pvs_sql',
    })

    out = naming._to_mongo({
        'kind': 'aggregate',
        'collection': 'pvs_camel_docs',
        'pipeline': [{'$vectorSearch': {
            'index': 'vector_index',
            'path': 'cover_url',
            'queryVector': Q,
            'numCandidates': 10,
            'limit': 3,
            'metric': 'cosine',
        }}],
    })

    spec = out['pipeline'][0]['$vectorSearch']
    assert spec['path'] == 'coverUrl'
    assert spec['metric'] == 'cosine'
