"""Mongo 执行器原生边界改写 + 写路径降级反馈（code-review m-10 回归）

覆盖 Host 侧两处新增逻辑（此前无对应测试）：

  1. ``_explicit_null`` 三态改写 —— 「字段 = null」编译为「字段存在且为 null」
     （``{$eq: null, $exists: true}``，对齐 SQL ``IS NULL``）；嵌套对象 / 数组下钻；
     ``$`` 算子对象不改写（``$ne: null`` 维持）。与 ``nodejs-store`` 同构。
  2. ``mutation_degraded`` —— 规划期降级（``plan.degraded``）经统一 ``feedback``
     通道发射，禁止静默失守（§11.4）。

运行：python -m pytest py-store/tests/test_mongo_executor.py
"""

import asyncio
from importlib import import_module

from py_store import feedback
from py_store import schema as _sc
from py_store.executors.mongo import _explicit_null, exec_mongo

# 注意：`py_store.crud` 把 `mutation` 函数重导出为同名属性，`from ... import mutation`
# 拿到的是函数而非模块 —— 需用 importlib 取真实子模块才能 monkeypatch 内部 `_core`。
_mut = import_module('py_store.crud.mutation')


def _run(coro):
    return asyncio.run(coro)


# ─── 1. _explicit_null 三态改写 ─────────────────────────────

def test_explicit_null_scalar():
    assert _explicit_null({'f': None}) == {'f': {'$eq': None, '$exists': True}}


def test_explicit_null_nested_object_and_array():
    # 非 `$` 键的对象 / 数组值一律下钻（与 nodejs-store/src/executors/mongo.js 对齐）
    src = {'a': {'b': None}, 'arr': [{'c': None}, None]}
    assert _explicit_null(src) == {
        'a': {'b': {'$eq': None, '$exists': True}},
        'arr': [{'c': {'$eq': None, '$exists': True}}, None],
    }


def test_explicit_null_operator_object_not_rewritten():
    # `$` 算子对象：下钻但谓词不改写（`$ne: null` 维持；`$or` 内嵌条件仍改写）
    assert _explicit_null({'f': {'$ne': None}}) == {'f': {'$ne': None}}
    assert _explicit_null({'$or': [{'f': None}]}) == {
        '$or': [{'f': {'$eq': None, '$exists': True}}],
    }


# ─── 2. exec_mongo 接线（filter / $match 改写落地） ──────────

class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, length=None):
        return list(self._docs)


class _Coll:
    def __init__(self):
        self.last_filter = None
        self.last_pipeline = None

    def find(self, query=None, projection=None):
        self.last_filter = query
        return _Cursor([])

    async def aggregate(self, pipeline):
        self.last_pipeline = pipeline
        return _Cursor([])


class _Db:
    def __init__(self):
        self.coll = _Coll()

    def __getitem__(self, name):
        return self.coll


def test_exec_mongo_find_rewrites_explicit_null():
    db = _Db()
    _run(exec_mongo(db, {'collection': 'c', 'kind': 'find', 'filter': {'f': None}}))
    assert db.coll.last_filter == {'f': {'$eq': None, '$exists': True}}


def test_exec_mongo_aggregate_rewrites_match():
    db = _Db()
    _run(exec_mongo(db, {
        'collection': 'c', 'kind': 'aggregate',
        'pipeline': [{'$match': {'f': None}}],
    }))
    assert db.coll.last_pipeline == [{'$match': {'f': {'$eq': None, '$exists': True}}}]


# ─── 3. mutation_degraded 反馈发射 ──────────────────────────

def test_mutation_emits_mutation_degraded(monkeypatch):
    _sc.register({
        'name': 'NbDegradedDoc', 'collection': 'nb_degraded_docs', 'timestamps': False,
        'fields': {'a': 'string'}, 'relations': {}, 'computes': {}, 'read': None, 'write': None,
    })

    class _FakeCore:
        @staticmethod
        def plan_mutation(*a, **k):
            return {
                'steps': [],
                'degraded': [{
                    'code': 'relationSkipped', 'layer': 'mutation',
                    'message': '关系不可读，写入已跳过', 'hint': '授予 read 权限',
                }],
            }

    monkeypatch.setattr(_mut, '_core', _FakeCore)

    events = []
    feedback.set_sink(events.append)
    try:
        _run(_mut._mutation_one('NbDegradedDoc', {'_id': 'x'}, 1))
    finally:
        feedback.set_sink(None)

    ev = next((e for e in events if e.get('type') == 'mutation_degraded'), None)
    assert ev is not None, 'sink 应收到 mutation_degraded 事件'
    assert ev['code'] == 'relationSkipped'
    assert ev['layer'] == 'mutation'
