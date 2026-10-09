"""require_context fail-secure 开关（Registry 级，rust-store M-1 闭环）

验证「上下文强制」三种语义（对标 nodejs-store/tests/require-context.test.js）：
  1. 默认关闭 = fail-open（与 JS 原版 parity：无 ctx 照常查询/写入）；
  2. 开启后缺 ctx 抛 ``ERR_NO_CONTEXT``（fail-secure，读/写全路径）；
  3. 系统上下文（run_as_internal）与用户上下文照常放行；关闭即恢复。

运行：$env:LOCAL_CORE='1'; $env:PYTHONPATH='src'; python -m pytest tests/test_require_context.py -q
"""

import asyncio

import pytest

from py_store import crud as _crud_mod
from py_store import permission as perm
from py_store import schema as _sc


def _register_test_schemas():
    """模块 schema 注册入口（conftest 模块隔离夹具在首用例前调用；import 零副作用）"""
    _sc.register({
        'name': 'RcModel', 'collection': 'rc_model', 'idPrefix': 'RC', 'timestamps': False,
        'fields': {'title': 'string'}, 'relations': {}, 'read': None, 'write': None,
    })


def _run(coro):
    return asyncio.run(coro)


# ── 最小内存驱动 mock（与 test_py_store._MemColl 同构 + replace_one） ──

class _Result:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, length=None):
        return list(self._docs)


class _Coll:
    def __init__(self, docs=None):
        self.docs = docs if docs is not None else []

    def find(self, query=None, projection=None):
        return _Cursor(self.docs)

    async def aggregate(self, pipeline):
        # 对齐 PyMongo async：aggregate 为协程（find 直接返回游标）
        return _Cursor(self.docs)

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

    async def delete_many(self, condition):
        n = len(self.docs)
        self.docs = []
        return _Result(deleted_count=n)


class _FakeDb:
    def __init__(self, docs=None):
        self._coll = _Coll(docs)

    def __getitem__(self, name):
        return self._coll


def _mock(docs=None):
    perm.set_context(None)
    coll = _Coll(docs)
    db = _FakeDb()
    db._coll = coll  # 所有 collection 名共享同一 coll，便于断言
    _crud_mod.set_db(db)
    return coll


# ── 三种语义 ──────────────────────────────────────────────────

def test_require_context_default_off_fail_open():
    _mock()
    assert _sc.require_context() is False
    # 无 ctx：查询/写入照常（fail-open，与 JS parity）
    assert _run(_crud_mod.query('RcModel{title}')) == []
    doc = _run(_crud_mod.insert('RcModel', {'title': '默认放行'}))
    assert doc['_id'].startswith('RC')


def test_require_context_on_blocks_without_ctx():
    _mock([{'_id': '1', 'title': 'a'}])
    _sc.set_require_context(True)
    try:
        with pytest.raises(RuntimeError, match='ERR_NO_CONTEXT'):
            _run(_crud_mod.query('RcModel{title}'))
        with pytest.raises(RuntimeError, match='ERR_NO_CONTEXT'):
            _run(_crud_mod.insert('RcModel', {'title': 'x'}))
        with pytest.raises(RuntimeError, match='ERR_NO_CONTEXT'):
            _run(_crud_mod.update('RcModel', {'_id': '1'}, {'title': 'y'}))
        with pytest.raises(RuntimeError, match='ERR_NO_CONTEXT'):
            _run(_crud_mod.remove('RcModel', {'_id': '1'}))
    finally:
        _sc.set_require_context(False)


def test_require_context_system_ctx_passes():
    coll = _mock()

    async def _inner():
        await _crud_mod.insert('RcModel', {'title': '系统写入'})
        return await _crud_mod.query('RcModel{title}')

    items = _run(perm.run_as_internal(_inner))
    assert len(items) == 1 and items[0]['title'] == '系统写入'
    assert len(coll.docs) == 1


def test_require_context_user_ctx_passes():
    _mock()
    perm.set_context({'userId': 'u1', 'roles': ['user']})
    try:
        _sc.set_require_context(True)
        _run(_crud_mod.insert('RcModel', {'title': '用户写入'}))
        items = _run(_crud_mod.query('RcModel{title}'))
        assert len(items) == 1 and items[0]['title'] == '用户写入'
    finally:
        _sc.set_require_context(False)
        perm.set_context(None)


def test_require_context_off_restores_fail_open():
    _mock()
    _sc.set_require_context(True)
    _sc.set_require_context(False)
    assert _run(_crud_mod.query('RcModel{title}')) == []


# ── secure_mode 统一安全模式（fail-secure 一键入口；见 secure.py） ──

def test_secure_mode_flips_switches_blocks_ctxless():
    from py_store import secure
    _mock()
    secure.secure_mode(admin_roles=['admin'])
    try:
        assert secure.is_secure() is True
        assert _sc.require_context() is True
        # 开关1 require_context：无 ctx 读写拒绝
        with pytest.raises(RuntimeError, match='ERR_NO_CONTEXT'):
            _run(_crud_mod.query('RcModel{title}'))
        with pytest.raises(RuntimeError, match='ERR_NO_CONTEXT'):
            _run(_crud_mod.insert('RcModel', {'title': 'x'}))
        # 开关3 meta closed：无 ctx 注册新定义拒绝
        with pytest.raises(RuntimeError, match='ERR_PERMISSION'):
            _sc.register({'name': 'RcTmp', 'collection': 'rc_tmp',
                          'fields': {}, 'relations': {}})
    finally:
        secure.relax_mode()


def test_secure_mode_admin_register_unconfigured_schema_denied():
    from py_store import secure
    _mock()
    secure.secure_mode(admin_roles=['admin'])
    try:
        # register 的定义门禁只认显式 ctx（不读 ContextVar），须显式传入
        _sc.register({'name': 'RcSecret', 'collection': 'rc_secret', 'timestamps': False,
                      'fields': {'title': 'string'}, 'relations': {}},
                     {'userId': 'admin1', 'roles': ['admin']})
        # 查询面身份走 ContextVar（register 门禁与读写判决的上下文通道不同）
        perm.set_context({'userId': 'admin1', 'roles': ['admin']})
        # 开关2 unconfigured=closed：未配白名单，admin 自己也被拒（判决先于物理执行）。
        # core 抛 ERR_PERMISSION，经 crud/exec._call 归一为 PermissionError（前缀已剥离）。
        with pytest.raises(perm.PermissionError, match='无访问权限'):
            _run(_crud_mod.query('RcSecret{title}'))
        with pytest.raises(perm.PermissionError, match='无写入权限'):
            _run(_crud_mod.insert('RcSecret', {'title': 'a'}))
        # 普通用户同样被拒
        perm.set_context({'userId': 'u2', 'roles': ['user']})
        with pytest.raises(perm.PermissionError, match='无访问权限'):
            _run(_crud_mod.query('RcSecret{title}'))
    finally:
        secure.relax_mode()
        perm.set_context(None)


def test_secure_mode_internal_passes():
    from py_store import secure
    _mock()
    secure.secure_mode()

    async def _inner():
        await _crud_mod.insert('RcModel', {'title': '内部写入'})
        return await _crud_mod.query('RcModel{title}')

    try:
        items = _run(perm.run_as_internal(_inner))
        assert len(items) == 1 and items[0]['title'] == '内部写入'
    finally:
        secure.relax_mode()


def test_secure_mode_relax_restores_fail_open():
    from py_store import secure
    _mock()
    secure.secure_mode()
    secure.relax_mode()
    assert secure.is_secure() is False
    assert _sc.require_context() is False
    assert _run(_crud_mod.query('RcModel{title}')) == []
