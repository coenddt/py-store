"""索引键点号路径物理化回归（嵌套数组字段索引被整串拼接）

schema 声明索引 ``signers.userId``（嵌套数组元素内字段）时，原实现对整串做
``naming.physical`` → ``signersUserId``；索引虽能建成，但物理键与文档真实嵌套路径
不符，按该字段查询永远走不到索引。

修复后索引键按段翻译并保留点号结构（``signers.userId``）。本模块用真实 MongoDB
断言：① 索引键与文档嵌套路径一致；② 按该字段查询的执行计划命中该索引（IXSCAN）。

需本机 127.0.0.1:27017 MongoDB（库 mongo_store_e2e，可用 MONGO_URI 覆盖）；
不可达时自动 skip。集合名带进程级 token 后缀，避免与其他进程 / 跨仓用例互相污染。

运行：$env:PYTHONPATH='src'; python -m pytest tests/test_index_nested_path.py -q
"""

import asyncio
import os
import uuid

import pytest

from py_store import init, naming, permission, store
from py_store import schema as _sc

E2E_TOKEN = os.environ.get('PYSTORE_E2E_TOKEN') or uuid.uuid4().hex[:8]
MONGO_URI = os.environ.get('MONGO_URI', 'mongodb://127.0.0.1:27017/mongo_store_e2e')

SCHEMA = 'IdxSigner'
COLLECTION = f'idx_signers_{E2E_TOKEN}'
NESTED_PATH = 'signers.userId'


class _State:
    def __init__(self):
        self.ready = False
        self.reason = 'MongoDB 不可达'
        self.client = None
        self.db = None


state = _State()


async def _setup():
    permission.set_context(None)
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
    state.client = client
    state.db = client.get_default_database()

    _sc.register({
        'name': SCHEMA,
        'collection': COLLECTION,
        'idPrefix': 'is_',
        'datasource': 'mongo_e2e',
        'timestamps': False,
        'fields': {'signers': {'type': 'array'}},
        'relations': {},
        'indexes': [{'keys': {NESTED_PATH: 1}}],
    })
    # 物理集合名与生产同源（core::naming）；先清掉上次运行残留，保证索引断言干净
    await state.db[naming.physical(COLLECTION)].drop()
    await init({'mongo_e2e': state.db, 'default': state.db})
    state.ready = True


async def _teardown():
    if state.db is not None:
        await state.db[naming.physical(COLLECTION)].drop()
    if state.client is not None:
        await state.client.close()


_LOOP = None


@pytest.fixture(scope='module', autouse=True)
def _boot():
    global _LOOP
    try:
        asyncio.get_running_loop()
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


def _stages(plan):
    """递归收集执行计划各阶段（Mongo 用 inputStage / inputStages 串联）"""
    out = []
    if not isinstance(plan, dict):
        return out
    if plan.get('stage'):
        out.append(plan)
    for key in ('inputStage',):
        out.extend(_stages(plan.get(key)))
    for stage in plan.get('inputStages') or []:
        out.extend(_stages(stage))
    return out


async def _reset():
    await state.db[naming.physical(COLLECTION)].delete_many({})


def test_nested_index_key_matches_document_path():
    """建索引后 key 必须等于文档真实嵌套路径，而非整串拼接名"""
    if not state.ready:
        pytest.skip(state.reason)

    async def _main():
        await _reset()
        await store.insert(SCHEMA, {'signers': [{'userId': 'u1'}, {'userId': 'u2'}]})
        return await state.db[naming.physical(COLLECTION)].index_information()

    info = _run(_main())
    index_keys = {(k, d) for v in info.values() for k, d in v['key']}
    assert (NESTED_PATH, 1) in index_keys, \
        f'索引键应为嵌套路径 {NESTED_PATH}，实际索引：{index_keys}'
    # 整串 physical 会产出 signersUserId；该键不存在才是真修复
    assert ('signersUserId', 1) not in index_keys, \
        f'不应出现整串拼接的索引键 signersUserId，实际索引：{index_keys}'


def test_nested_index_used_by_field_query():
    """按嵌套字段查询必须命中该索引（执行计划含 IXSCAN），且能取到数据。

    取数走原生逃生口（``execute_native``）：GQL ``$condition`` 对「数组根字段的
    点号路径」有 core 侧形态守卫（跨后端数组语义不一致，两档一律 Err），该守卫
    与本缺陷无关，故此处用原生 ``$match`` 验证索引本身的可用性。
    """
    if not state.ready:
        pytest.skip(state.reason)

    phys_coll = naming.physical(COLLECTION)

    async def _main():
        await _reset()
        await store.insert(SCHEMA, {'signers': [{'userId': 'u1'}, {'userId': 'u2'}]})
        got = await store.execute_native(
            'mongo_e2e', phys_coll, [{'$match': {NESTED_PATH: 'u2'}}])
        plan = await state.db.command({
            'explain': {
                'find': phys_coll,
                'filter': {NESTED_PATH: 'u2'},
            },
            'verbosity': 'queryPlanner',
        })
        return got, plan

    got, plan = _run(_main())
    assert len(got['rows']) == 1, f'按嵌套字段应查到 1 条，实际：{got["rows"]}'

    winning = plan.get('queryPlanner', {}).get('winningPlan') or {}
    ixscans = [s for s in _stages(winning) if s.get('stage') == 'IXSCAN']
    assert ixscans, f'按 {NESTED_PATH} 查询应走索引，实际执行计划：{winning}'
    assert ixscans[0].get('indexName', '').startswith(NESTED_PATH), \
        f'应命中 {NESTED_PATH} 索引，实际：{ixscans[0].get("indexName")}'
