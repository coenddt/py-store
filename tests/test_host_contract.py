"""Host 同构契约测试（Python 侧）

依据执行文档 6.3：定义一份 Host 契约表，JS 与 Python 各实现同名语义方法，
以**共享 fixture**（``rust-store/fixtures/host/*.json``）分别喂两侧 Host，
断言两侧产出的结果深比较相等。

  1. placeholders.json    → ``crud.exec.resolve_placeholders``
  2. truthy.json          → ``crud.id._truthy``
  3. id_pool.json         → ``crud.id._new_id_pool``
  4. callback_bridge.json → ``schema.register`` 回调桥（fn 走 core / asyncFn 留 Host）

JS 侧对拍：``nodejs-store/tests/host-contract.test.js``。
运行：python -m pytest py-store/tests/test_host_contract.py
"""

import asyncio
import json
import os

import pytest

from py_store import crud as _crud_mod
from py_store import permission as perm
from py_store.crud.exec import resolve_placeholders
from py_store.crud.id import _new_id_pool, _truthy
from py_store.schema import register

_HOST_FIXTURES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'rust-store', 'fixtures', 'host')


def _load(name):
    # I-7：fixture 位于仓库外（rust-store），单仓 clone / 单包 CI 时不存在——
    # 此时 skip 而非抛 FileNotFoundError，保证测试可独立运行
    path = os.path.join(_HOST_FIXTURES, name)
    if not os.path.exists(path):
        pytest.skip(f'缺少 rust-store fixture: {path}（需同时 clone rust-store 仓库）')
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def _run(coro):
    return asyncio.run(coro)


# ─── 1. 占位符替换 ──────────────────────────────────────────

def test_resolve_placeholders():
    fx = _load('placeholders.json')
    for c in fx['cases']:
        got = resolve_placeholders(c['command'], ids=c.get('ids'), steps=c.get('steps'))
        assert got == c['expected'], f"占位符用例不一致: {c['name']}"


# ─── 2. is_truthy 语义 ──────────────────────────────────────

def test_truthy():
    fx = _load('truthy.json')
    for c in fx['cases']:
        assert _truthy(c['value']) == c['expected'], f"truthy 用例不一致: {c['name']}"


# ─── 3. mutation ID 池遍历 ──────────────────────────────────

def test_new_id_pool():
    fx = _load('id_pool.json')
    for s in fx['schemas']:
        register(s)

    for c in fx['cases']:
        pool = _new_id_pool(c['model'], c['data'])
        assert len(pool) == c['expectCount'], f"ID 池长度不一致: {c['name']}"
        for i, prefix in enumerate(c['expectPrefixes']):
            assert pool[i].startswith(prefix), (
                f"ID 池第 {i} 项前缀不符: {c['name']}（期望 {prefix}，实际 {pool[i]}）")


# ─── 4. 回调桥（fn + asyncFn） ──────────────────────────────

def _hc_sum_ab(doc):
    """fixture 桩语义：sum(a, b)"""
    return (doc.get('a') or 0) + (doc.get('b') or 0)


def _hc_tag_skus(items, ctx=None):
    """fixture 桩语义：join(items[].sku, '|')"""
    for it in items:
        it['skuTag'] = '|'.join(s.get('sku') for s in (it.get('items') or []))


_STUBS = {'hc_sum_ab': _hc_sum_ab, 'hc_tag_skus': _hc_tag_skus}


class _MemCursor:
    def __init__(self, docs):
        self.docs = docs

    async def to_list(self, length=None):
        return list(self.docs)


class _MemColl:
    def __init__(self, docs=None):
        self.docs = docs if docs is not None else []

    def find(self, query=None, projection=None):
        return _MemCursor(self.docs)

    async def aggregate(self, pipeline):
        # 对齐 PyMongo async：``aggregate`` 为协程（``find`` 则直接返回游标）
        return _MemCursor(self.docs)

    async def find_one(self, query=None, projection=None):
        return dict(self.docs[0]) if self.docs else None


class _FakeDb:
    def __init__(self, docs):
        self._docs = docs
        self._colls = {}

    def __getitem__(self, name):
        if name not in self._colls:
            # Mongo 物理集合名（core::naming）：逻辑 hc_posts → hcPosts
            self._colls[name] = _MemColl(self._docs if name == 'hcPosts' else [])
        return self._colls[name]


def test_callback_bridge_fn_and_asyncfn():
    fx = _load('callback_bridge.json')

    # 把 fixture 的声明式 computes 替换为真实桩函数后注册
    for s in fx['schemas']:
        defn = json.loads(json.dumps(s))
        for key, comp in (defn.get('computes') or {}).items():
            ref = comp.get('fnRef') or key
            if comp.get('fn'):
                comp['fn'] = _STUBS[ref]
            if comp.get('asyncFn'):
                comp['asyncFn'] = _STUBS[ref]
        register(defn)

    c = fx['cases'][0]
    perm.set_context(None)
    _crud_mod.set_db(_FakeDb(c['docs']))

    out = _run(_crud_mod.query(c['gql']))
    assert out == c['expected'], f"回调桥结果不一致: {c['name']}"


# ─── 5. 批量注册 / 注销门面（插件 bootstrap 接入） ─────────────

def _batch_defn(name):
    return {
        'name': name, 'collection': name.lower(), 'idPrefix': 'bx',
        'fields': {'_id': {'type': 'string'}, 'title': {'type': 'string'}},
    }


def _core_unregister():
    from py_store.schema import get_core
    fn = getattr(get_core(), 'unregister', None)
    return fn if callable(fn) else None


def test_register_batch_atomic_and_visible():
    """批量注册一次落库：两张表同时可见；批内同名冲突 ⇒ 零副作用。"""
    import uuid

    from py_store import store as _store

    u = uuid.uuid4().hex[:8]
    a, b = f'BatchA{u}', f'BatchB{u}'
    _store.register_batch([{'defn': _batch_defn(a), 'location': None},
                           {'defn': _batch_defn(b)}])
    assert _store.has(a) and _store.has(b), '批量注册后两张表都应可见'

    c = f'BatchC{u}'
    with pytest.raises(RuntimeError):
        _store.register_batch([{'defn': _batch_defn(c)}, {'defn': _batch_defn(c)}])
    assert not _store.has(c), '批内冲突应零副作用（不得留下半注册）'


def test_unregister_unknown_schema_is_idempotent():
    """注销未知 schema 幂等成功（补偿清单可整份重放）。"""
    if _core_unregister() is None:
        pytest.skip('当前 rust-store-py 绑定未导出 Registry.unregister'
                    '（core 源码已实现，待重建绑定后生效）')
    from py_store import store as _store
    _store.unregister('NotRegisteredAnywhere')
    _store.unregister('NotRegisteredAnywhere')


def test_unregister_without_binding_method_reports_explicitly(monkeypatch):
    """绑定缺 unregister 时必须显式报错（不静默、不改 Host 镜像）。"""
    from py_store import schema as _schema

    class _NoUnregisterCore:
        pass

    monkeypatch.setattr(_schema, 'get_core', lambda: _NoUnregisterCore())
    with pytest.raises(RuntimeError, match=r'未导出 Registry\.unregister'):
        _schema.unregister('AnySchema')
