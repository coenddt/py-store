"""Host 执行路径补测（此前 `--cov` 未触达的真实分支）

缺口证据（`python -m pytest --cov=py_store --cov-report=term-missing`，2026-09-27 实跑）：
  - `crud/query.py` 46-54 / 109-117 / 134-136：两阶段执行、联邦两阶段单元、联邦降级告警；
  - `crud/query.py` 66：asyncFn 计算列缺实现（禁静默跳过）；
  - `__init__.py` 48/78-92/139/230-259/276：Store 门面读路径、索引创建、init 入参校验。

注：`crud/write.py:14-15`（creator 写权限探针）**当前不可达** —— core `can_write_schema`
对 `write: ['creator']` 用 `Doc::Missing` 评估即恒通过，`plan_update/plan_remove` 永不返回
`needsProbe`（实测证据见交接说明）。该缺口属实现缺陷而非测试缺失，单列报告、不写用例固化。

对标 nodejs-store/tests/host-paths.test.js（双端同名同义用例）。
运行：$env:LOCAL_CORE='1'; $env:PYTHONPATH='src'; python -m pytest tests/test_host_paths.py -q
"""

import asyncio
import json

import pytest
from pymongo.errors import OperationFailure

from py_store import crud as _crud_mod
from py_store import datasource, feedback, init, store
from py_store import permission as perm
from py_store import schema as _sc


def _run(coro):
    return asyncio.run(coro)


async def _noop_async(_items, _ctx):
    """asyncFn 占位实现（本文件只用其「已登记」身份，不校验执行语义）"""
    return None


# ─────────────────────────────────────────────────────────────
# schema（两阶段读路径需要「根 + 关系子表」）
# ─────────────────────────────────────────────────────────────

_sc.register({
    'name': 'HpPost', 'collection': 'hp_posts', 'idPrefix': 'HP', 'timestamps': False,
    'fields': {'title': 'string', 'seq': {'type': 'int'}},
    'relations': {
        'comments': {'model': 'HpComment', 'type': 'many',
                     'localField': '_id', 'foreignField': 'postId'},
    },
    'read': None, 'write': None,
})
_sc.register({
    'name': 'HpComment', 'collection': 'hp_comments', 'timestamps': False,
    'fields': {'postId': 'string', 'body': 'string'},
    'relations': {}, 'read': None, 'write': None,
})
# 联邦降级：根与子表分属两个数据源（跨源子级 $limit 无法下推）
_sc.register({
    'name': 'HpUser', 'collection': 'hp_users', 'datasource': 'hp_mongo_a',
    'idPrefix': 'HU', 'timestamps': False,
    'fields': {'name': 'string'},
    'relations': {
        'orders': {'model': 'HpOrder', 'type': 'many',
                   'localField': '_id', 'foreignField': 'userId'},
    },
    'read': None, 'write': None,
})
_sc.register({
    'name': 'HpOrder', 'collection': 'hp_orders', 'datasource': 'hp_mongo_b',
    'timestamps': False,
    'fields': {'userId': 'string', 'code': 'string'},
    'relations': {}, 'read': None, 'write': None,
})


@pytest.fixture(autouse=True)
def _isolate():
    """档位 / 上下文 / 反馈 sink / 连接映射复位 —— 防用例间串扰"""
    _sc.set_profile('standard')
    perm.set_context(None)
    feedback.set_sink(None)
    datasource.set_connections({})
    yield
    _sc.set_profile('standard')
    perm.set_context(None)
    feedback.set_sink(None)
    datasource.set_connections({})


# ─────────────────────────────────────────────────────────────
# 内存驱动 mock：phase-1 取 ID / phase-2 回表（按 pipeline 形态分派）
# ─────────────────────────────────────────────────────────────

class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, length=None):
        return list(self._docs)


def _phase1_ids(pipeline):
    """phase-2 命令首段 `$match._id.$in` → id 列表；非两阶段返回 None"""
    first = pipeline[0] if pipeline else None
    match = first.get('$match') if isinstance(first, dict) else None
    in_clause = (match or {}).get('_id') or {}
    val = in_clause.get('$in') if isinstance(in_clause, dict) else None
    return val if isinstance(val, list) else None


def _apply_phase1(pipeline, docs):
    """极简阶段模拟（仅本用例所需）：$sort / $skip / $limit / $project"""
    cur = [dict(d) for d in docs]
    for stage in pipeline:
        if '$sort' in stage:
            for key, direction in reversed(list(stage['$sort'].items())):
                cur.sort(key=lambda d, k=key: (d.get(k) is None, d.get(k)),
                         reverse=direction < 0)
        elif '$skip' in stage:
            cur = cur[int(stage['$skip']):]
        elif '$limit' in stage:
            cur = cur[:int(stage['$limit'])]
        elif '$project' in stage:
            keys = [k for k, v in stage['$project'].items() if v]
            cur = [{k: d.get(k) for k in keys} for d in cur]
    return cur


class _ScriptedColl:
    """两阶段读路径 mock：phase-1 取 ID、phase-2 按 `$in` 回表（可强制乱序返回）"""

    def __init__(self, docs, reverse_phase2=False):
        self.docs = docs
        self.pipelines = []
        self.reverse_phase2 = reverse_phase2
        self.phase2_raw_ids = None

    def find(self, query=None, projection=None):
        return _Cursor(self.docs)

    async def aggregate(self, pipeline):
        self.pipelines.append(pipeline)
        ids = _phase1_ids(pipeline)
        if ids is None:
            return _Cursor(_apply_phase1(pipeline, self.docs))
        by_id = {d['_id']: dict(d) for d in self.docs}
        picked = [by_id[i] for i in ids if i in by_id]
        if self.reverse_phase2:
            picked.reverse()
        self.phase2_raw_ids = [d['_id'] for d in picked]
        return _Cursor(picked)

    async def find_one(self, query=None, projection=None):
        return dict(self.docs[0]) if self.docs else None

    async def count_documents(self, filter=None):
        return len(self.docs)


class _FakeDb:
    """按 collection 名分派独立 coll；未知名自动建空 coll"""

    def __init__(self, mapping=None):
        self._colls = dict(mapping or {})

    def __getitem__(self, name):
        if name not in self._colls:
            self._colls[name] = _ScriptedColl([])
        return self._colls[name]


_POSTS = [
    {'_id': 'p1', 'title': 'A', 'seq': 1},
    {'_id': 'p2', 'title': 'B', 'seq': 2},
    {'_id': 'p3', 'title': 'C', 'seq': 3},
]


def _db_with_posts(posts=None, reverse_phase2=False):
    coll = _ScriptedColl(_POSTS if posts is None else posts, reverse_phase2=reverse_phase2)
    return _FakeDb({'hp_posts': coll}), coll


# ─────────────────────────────────────────────────────────────
# 1. 两阶段读路径（crud/query.py:_run_query_plan）
# ─────────────────────────────────────────────────────────────

def test_two_phase_executes_id_pass_then_relookup_and_restores_order():
    """root `$limit` + 关系 → core 判 two_phase：先取 ID，再携 `{{phase1.ids}}` 回表，最后还原排序"""
    db, coll = _db_with_posts(reverse_phase2=True)
    _crud_mod.set_db(db)

    items = _run(_crud_mod.query(
        'HpPost($sort:@s,$limit:@l){ _id, title, comments{ _id } }',
        {'s': {'seq': 1}, 'l': 2}))

    assert len(coll.pipelines) == 2, '两阶段应恰发两条命令（取 ID → 回表）'
    phase1, phase2 = coll.pipelines
    assert phase1[-1] == {'$project': {'_id': 1}}, f'phase-1 只取 _id: {json.dumps(phase1)}'
    assert any('$limit' in s for s in phase1), 'phase-1 应带根级 $limit'
    assert phase2[0]['$match']['_id']['$in'] == ['p1', 'p2'], \
        f'phase-2 须携 phase-1 的 ID 列表: {json.dumps(phase2[0], ensure_ascii=False)}'
    assert coll.phase2_raw_ids == ['p2', 'p1'], 'phase-2 原始顺序被 mock 故意颠倒'
    assert [d['_id'] for d in items] == ['p1', 'p2'], 'core restore_sort_order 应还原排序'


def test_two_phase_empty_phase1_short_circuits():
    """phase-1 无命中 → 直接返回空，不再发第二条命令（避免空 `$in` 全表扫）"""
    db, coll = _db_with_posts(posts=[])
    _crud_mod.set_db(db)

    assert _run(_crud_mod.query(
        'HpPost($sort:@s,$limit:@l){ _id, comments{ _id } }',
        {'s': {'seq': 1}, 'l': 2})) == []
    assert len(coll.pipelines) == 1, '空结果应短路，只发 phase-1 一条命令'


def test_sort_by_relation_field_keeps_single_command():
    """排序引用关联字段（sorts_by_relation）→ 不走两阶段优化，单命令聚合"""
    db, coll = _db_with_posts()
    _crud_mod.set_db(db)

    _run(_crud_mod.query(
        'HpPost($sort:@s,$limit:@l){ _id, comments{ _id } }',
        {'s': {'comments.body': 1}, 'l': 2}))

    assert len(coll.pipelines) == 1, '关联字段排序无法先取 ID，须单命令聚合'
    stages = coll.pipelines[0]
    assert any('$lookup' in s for s in stages), '单命令里应含关系 $lookup'
    assert any('$sort' in s for s in stages), '排序仍在同一 pipeline 内下推'


# ─────────────────────────────────────────────────────────────
# 2. 联邦：两阶段取数单元 + 降级告警
# ─────────────────────────────────────────────────────────────

def test_federated_unit_two_phase_restores_order():
    """单源联邦的根取数单元为 two_phase（关系 + 根级 $limit）→ 走 `_run_federated_unit` 两阶段分支"""
    db, coll = _db_with_posts(reverse_phase2=True)
    _crud_mod.set_db(db)

    items = _run(_crud_mod.query_federated(
        'HpPost($sort:@s,$limit:@l){ _id, title, comments{ _id } }',
        {'s': {'seq': 1}, 'l': 2}))

    assert len(coll.pipelines) == 2, '联邦取数单元同样应走两阶段（取 ID → 回表）'
    assert coll.pipelines[1][0]['$match']['_id']['$in'] == ['p1', 'p2']
    assert [d['_id'] for d in items] == ['p1', 'p2']


def test_federated_unit_two_phase_empty_phase1_short_circuits():
    """联邦取数单元 phase-1 空结果 → 直接返回空，不再发第二条命令"""
    db, coll = _db_with_posts(posts=[])
    _crud_mod.set_db(db)

    assert _run(_crud_mod.query_federated(
        'HpPost($sort:@s,$limit:@l){ _id, comments{ _id } }',
        {'s': {'seq': 1}, 'l': 2})) == []
    assert len(coll.pipelines) == 1, '空结果应短路，只发 phase-1 一条命令'


def test_federation_degraded_emits_feedback_and_keeps_result():
    """跨源关系子级 `$limit` 无法下推 → plan.degraded 必须 emit `federation_degraded`（禁静默）"""
    db = _FakeDb({
        'hp_users': _ScriptedColl([{'_id': 'u1', 'name': 'A'}]),
        'hp_orders': _ScriptedColl([{'_id': 'o1', 'userId': 'u1', 'code': 'c1'}]),
    })
    _crud_mod.set_connections({'hp_mongo_a': db, 'hp_mongo_b': db})
    events = []
    feedback.set_sink(events.append)

    items = _run(_crud_mod.query_federated(
        'HpUser($condition:@c0){ name, orders($limit:@l0){ code } }', {'c0': {}, 'l0': 3}))

    degraded = [e for e in events if e.get('type') == 'federation_degraded']
    assert len(degraded) == 1, f'降级须恰好产出一条反馈事件: {events}'
    assert degraded[0]['code'] == 'crossSourceChildPaging'
    assert degraded[0]['layer'] == 'federation'
    assert degraded[0]['message'] and degraded[0]['hint']
    # 降级不阻断：结果仍按内存 join 返回
    assert items == [{'_id': 'u1', 'name': 'A', 'orders': [{'code': 'c1'}]}]


# ─────────────────────────────────────────────────────────────
# 3. asyncFn 计算列缺实现（禁静默跳过）
# ─────────────────────────────────────────────────────────────

def test_async_fn_without_host_impl_raises():
    """core 声明 asyncFn 计算列、Host 未登记实现 → 显式 RuntimeError（不得静默丢列）"""
    _sc.register({
        'name': 'HpAsyncOnly', 'collection': 'hp_async_only', 'timestamps': False,
        'fields': {'a': {'type': 'int'}},
        'computes': {
            'total': {'type': 'int', 'asyncFn': _noop_async, 'fnRef': 'hp_missing_async'},
        },
        'relations': {},
    })
    # 模拟「core 已声明 asyncFn、Host 侧实现未登记」（如回调未就绪）—— 必须显式报错
    saved = _sc._async_fns.pop('hp_missing_async')
    db, _ = _db_with_posts()
    db._colls['hp_async_only'] = _ScriptedColl([{'_id': '1', 'a': 1}])
    _crud_mod.set_db(db)
    try:
        with pytest.raises(RuntimeError, match='未注册实现'):
            _run(_crud_mod.query('HpAsyncOnly{ _id, total }'))
    finally:
        _sc._async_fns['hp_missing_async'] = saved


# ─────────────────────────────────────────────────────────────
# 4. Store 门面读路径 + init 入参校验
# ─────────────────────────────────────────────────────────────

def test_store_facade_read_paths():
    """`store` 门面（类实例）读方法此前未被任何用例触达"""
    db, _ = _db_with_posts()
    _crud_mod.set_db(db)

    assert _run(store.query('HpPost{ _id, title }'))[0]['_id'] == 'p1'
    assert _run(store.queryOne('HpPost{ _id, title }'))['_id'] == 'p1'
    counted = _run(store.queryWithCount('HpPost{ _id }'))
    assert counted['total'] == 3

    built = store.buildPipeline('HpPost{ _id, title }')
    assert built['ast']['model'] == 'HpPost'
    assert built['ast']['fields'] == ['_id', 'title'], built['ast']
    assert built['tokens'], 'build_pipeline 应返回 token 流'


def test_init_rejects_invalid_connections():
    """init(connections) 入参校验：既非映射也无 `__getitem__` → TypeError"""
    with pytest.raises(TypeError, match='需要数据源连接映射'):
        _run(init(123))


# ─────────────────────────────────────────────────────────────
# 5. init 索引创建（Mongo 源；失败必须走统一反馈通道）
# ─────────────────────────────────────────────────────────────

class _IndexColl:
    def __init__(self, existing=None, fail=False, list_fail=False):
        self.existing = existing or []
        self.fail = fail
        self.list_fail = list_fail
        self.created = []

    async def list_indexes(self):
        if self.list_fail:
            raise OperationFailure('模拟索引列举失败（集合尚未存在）')
        return _Cursor(self.existing)

    async def create_index(self, keys, **options):
        if self.fail:
            raise OperationFailure('模拟索引创建失败')
        self.created.append((list(keys), options))


class _IndexDb:
    """按 collection 名分派独立 coll 的 Mongo db 桩（init 会遍历全部已注册 schema）"""

    def __init__(self, factory):
        self._factory = factory
        self._colls = {}

    def __getitem__(self, name):
        if name not in self._colls:
            self._colls[name] = self._factory()
        return self._colls[name]


_sc.register({
    'name': 'HpIndexed', 'collection': 'hp_indexed', 'timestamps': False,
    'fields': {'title': 'string'},
    'relations': {},
    'indexes': [{'keys': {'title': 1}, 'unique': True}],
})
_sc.register({
    'name': 'HpIndexNoKeys', 'collection': 'hp_index_no_keys', 'timestamps': False,
    'fields': {'title': 'string'},
    'relations': {},
    'indexes': [{'options': {'unique': True}}],  # 缺 keys → 应跳过，不报错
})


def test_init_creates_index_and_skips_existing():
    """init 幂等建索引：无同名索引则创建（inline 选项随命令下发），已有同名索引则跳过"""
    db = _IndexDb(_IndexColl)
    _run(init({'default': db}))
    assert db['hp_indexed'].created == [([('title', 1)], {'unique': True})], \
        db['hp_indexed'].created

    db2 = _IndexDb(lambda: _IndexColl(existing=[{'name': 'title_1'}]))
    _run(init({'default': db2}))
    assert db2['hp_indexed'].created == [], '同名索引已存在时不得重复创建'


def test_index_create_failure_emits_feedback_and_does_not_block_init():
    """索引创建失败不阻塞 init，但必须经统一反馈通道告警（对齐 nodejs-store index-feedback 用例）"""
    events = []
    feedback.set_sink(events.append)
    _run(init({'default': _IndexDb(lambda: _IndexColl(fail=True))}))

    failed = [e for e in events if e.get('type') == 'index_create_failed']
    assert failed, f'索引创建失败须 emit index_create_failed: {events}'
    assert failed[0]['code'] == 'indexCreateFailed'
    assert failed[0]['layer'] == 'host'
    assert 'hp_indexed' in failed[0]['message']
    assert failed[0]['hint']


def test_index_list_failure_treated_as_no_index_and_keys_less_entry_skipped():
    """列举索引失败（集合尚未存在）→ 视为无索引继续创建；无 keys 的索引项直接跳过"""
    db = _IndexDb(lambda: _IndexColl(list_fail=True))
    _run(init({'default': db}))
    assert db['hp_indexed'].created == [([('title', 1)], {'unique': True})], \
        '列举失败应退化为「无既有索引」，不阻断创建'

    db2 = _IndexDb(_IndexColl)
    _run(init({'default': db2}))
    assert db2['hp_index_no_keys'].created == [], '无 keys 的索引项应跳过'
