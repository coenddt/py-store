"""执行作用域并发隔离用例（R2 03 §5 步骤 5）——py 宿主 A1/A2/A3/A5 + A4 子集

与 node 侧 ``nodejs-store/tests/policy-isolation.test.js`` 逐例同构（03 §5 步骤 5：
py 宿主同构），验证「一请求/一安全域一份 ``{view, sink, meta, secure}``」在并发下互不串扰：

  A1 profile 视图与 sink 事件隔离（``await`` 让出后各归各）；
  A2 两视图不同 rbac 判决各自稳定（``rbac_can`` 为 rbac 判决入口；``can_read`` 为静态面，
     core 语义不叠加 rbac，两者一并断言——口径同 03 步骤 4 留痕）；
  A3 作用域内 ``secure_mode()`` 只作用于本作用域（base 与并发其它作用域均不受影响）；
  A4 作用域内 ``schema.register`` 报 ``ERR_POLICY_VIEW_READONLY``（前缀原样，不落 base）；
  A5 作用域 sink/meta 隔离（含落库 sink 的 tenant/env 取作用域 meta）。

注意：py 的 ``with_scope`` 是**上下文管理器**（``with`` 语句，退出即 reset），非 async；
并发隔离由 ContextVar 在 ``asyncio.gather`` 各 task 的独立上下文副本保证。

运行：$env:LOCAL_CORE='1'; $env:PYTHONPATH='src'; python -m pytest tests/test_policy_isolation.py -q
"""

import asyncio

import pytest

from py_store import current_scope, feedback, permission, schema, store, with_scope

PROFILE_STD = 'standard'
PROFILE_AI = 'text2query'
CTX_VIEWER = {'userId': 'u1', 'roles': ['viewer']}
CTX_OTHER = {'userId': 'u2', 'roles': ['other']}


def _register_test_schemas():
    """conftest 模块隔离约定：模块级只定义不执行，由隔离夹具重放（import 零副作用）"""
    schema.register({
        'name': 'IsoPost', 'collection': 'iso_posts', 'timestamps': False,
        'fields': {'title': {'type': 'string'}}, 'relations': {},
    })
    # 静态读白名单 schema：用于断言 can_read 静态面不被 rbac 视图扰动
    schema.register({
        'name': 'IsoAudit', 'collection': 'iso_audits', 'timestamps': False,
        'read': ['viewer'], 'fields': {'title': {'type': 'string'}}, 'relations': {},
    })


# rbac 策略：viewer 可读 IsoPost / 无任何授权（enforce 下该角色被拒）
RBAC_GRANTED = {
    'mode': 'enforce',
    'roles': {'viewer': {}},
    'grants': [{'role': 'viewer', 'model': 'IsoPost', 'actions': ['read']}],
}
RBAC_DENIED = {'mode': 'enforce', 'roles': {'viewer': {}}, 'grants': []}


def view_of(overrides):
    """从 base 派生策略视图（作用域 ``view`` 的构造入口）"""
    return schema.core.with_policy(overrides)


def barrier(n):
    """n 方栅栏：全部到达后同时放行——保证两个作用域真实重叠（并发交错，而非先后串行）"""
    arrived = 0
    waiters = []

    async def wait():
        nonlocal arrived
        arrived += 1
        if arrived >= n:
            for fut in waiters:
                if not fut.done():
                    fut.set_result(None)
            return
        fut = asyncio.get_running_loop().create_future()
        waiters.append(fut)
        await fut

    return wait


def _run(coro):
    return asyncio.run(coro)


def _run_all(*coros):
    """在单一事件循环内并发跑多个协程（gather 须在**运行中**的循环内创建）"""
    async def _main():
        return await asyncio.gather(*coros)

    return asyncio.run(_main())


@pytest.fixture(autouse=True)
def _reset_globals():
    """每用例复位进程级全局（档位 / rbac / 三开关 / sink）——防用例间串扰"""
    _reset_base()
    yield
    _reset_base()


def _reset_base():
    permission.set_rbac(None)
    schema.set_profile('standard')
    permission.set_context(None)
    feedback.set_sink(None)
    schema.set_require_context(False)
    schema.set_unconfigured_policy('open')
    schema.set_meta_policy(False, [])


# ── A1：档位视图 + sink 事件隔离 ──────────────────────────────

def test_policy_isolation_a1_profile_view_per_scope():
    """两并发作用域 profile 视图各归各（await 让出后不串）+ 域外零变更"""
    wait = barrier(2)
    observed = {}

    async def worker(tag, profile):
        view = view_of({'profile': profile})
        seen = []
        with with_scope(view):
            assert schema.get_core() is view, f'{tag}: 域内 get_core() 应为本作用域视图'
            seen.append(schema.get_core().profile())
            await wait()                                   # 两作用域在此真实重叠且各自让出
            seen.append(schema.get_core().profile())        # 让出后档位仍各归各
        observed[tag] = seen

    _run_all(worker('a', PROFILE_AI), worker('b', PROFILE_STD))
    assert observed['a'] == [PROFILE_AI, PROFILE_AI]
    assert observed['b'] == [PROFILE_STD, PROFILE_STD]

    # 域外回退 base（零默认变更）
    assert current_scope() is None
    assert schema.get_core() is schema.core
    assert schema.core.profile() == PROFILE_STD, 'base 档位不得被作用域视图改动'


def test_policy_isolation_a1_sink_event_per_scope():
    """两并发作用域 sink 事件隔离（events 互不含对方事件）"""
    wait = barrier(2)
    collected = {}

    async def worker(tag):
        events = []
        with with_scope(view_of({}), sink=events.append):
            feedback.emit({'type': 'isolation', 'code': f'{tag}-1', 'layer': 'host'})
            await wait()
            feedback.emit({'type': 'isolation', 'code': f'{tag}-2', 'layer': 'host'})
        collected[tag] = events

    _run_all(worker('a'), worker('b'))
    assert [e['code'] for e in collected['a']] == ['a-1', 'a-2']
    assert [e['code'] for e in collected['b']] == ['b-1', 'b-2']
    assert not [e for e in collected['a'] if e['code'].startswith('b-')], 'a 侧不得含 b 的事件'
    assert not [e for e in collected['b'] if e['code'].startswith('a-')], 'b 侧不得含 a 的事件'


# ── A2：两视图不同 rbac 判决各自稳定 ─────────────────────────

def test_policy_isolation_a2_rbac_view_decisions_stable():
    """rbac_can 主判 + can_read 静态面（core 的 can_read 不叠加 rbac，口径同步骤 4 留痕）"""
    view_granted = view_of({'rbac': RBAC_GRANTED})
    view_denied = view_of({'rbac': RBAC_DENIED})
    wait = barrier(2)
    seen = {}

    def snapshot():
        """[rbac_can(read), can_read(IsoPost 无白名单), can_read(IsoAudit,viewer/other)]"""
        return [
            store.rbac_can('IsoPost', 'read', CTX_VIEWER),
            permission.can_read_schema('IsoPost', CTX_VIEWER),
            permission.can_read_schema('IsoAudit', CTX_VIEWER),
            permission.can_read_schema('IsoAudit', CTX_OTHER),
        ]

    async def worker(tag, view):
        rows = []
        with with_scope(view):
            rows.append(snapshot())
            await wait()
            rows.append(snapshot())                        # 让出后判决不变（视图不可变）
        seen[tag] = rows

    _run_all(worker('granted', view_granted), worker('denied', view_denied))

    # granted：rbac 授权 → rbac_can true；静态面照旧（无白名单 fail-open、IsoAudit 按角色）
    assert seen['granted'][0] == [True, True, True, False]
    assert seen['granted'][1] == [True, True, True, False]
    # denied：同一 ctx 在该视图被拒；静态面与 granted 视图逐字段一致（互不串扰）
    assert seen['denied'][0] == [False, True, True, False]
    assert seen['denied'][1] == [False, True, True, False]

    # base 仍未注入 rbac → 不介入
    assert store.rbac_enabled() is False, 'base 不得被视图注入污染'
    assert store.rbac_can('IsoPost', 'read', CTX_VIEWER) is True


def test_policy_isolation_a2_view_rbac_no_base_pollution():
    """视图注入 rbac 不污染 base 与其它视图"""
    granted = view_of({'rbac': RBAC_GRANTED})
    denied = view_of({'rbac': RBAC_DENIED})
    assert granted.rbac_enabled() is True
    assert denied.rbac_enabled() is True
    assert schema.core.rbac_enabled() is False, 'base 视图无 rbac'

    with with_scope(granted):
        assert schema.get_core().rbac_enabled() is True
        assert store.rbac_can('IsoPost', 'read', CTX_VIEWER) is True
    with with_scope(denied):
        assert schema.get_core().rbac_enabled() is True
        assert store.rbac_can('IsoPost', 'read', CTX_VIEWER) is False
    assert schema.core.rbac_enabled() is False, '两个视图退出后 base 仍无 rbac'
    assert store.rbac_can('IsoPost', 'read', CTX_VIEWER) is True


# ── A3：secure_mode 并视图只作用本作用域 ──────────────────────

def test_policy_isolation_a3_secure_mode_scoped_only():
    """作用域内 secure_mode 只作用于本作用域（base 三开关零污染）"""
    async def body():
        with with_scope(view_of({})):
            assert store.is_secure() is False, '进入作用域时本作用域尚未 secure'
            store.secure_mode(admin_roles=['admin'])
            assert store.is_secure() is True, '域内 is_secure 取本作用域标志'
            assert schema.get_core().require_context() is True, '三开关已并视图（require_context 已翻）'
            assert store.get_profile() == PROFILE_STD, '并视图不影响档位'
            await asyncio.sleep(0)                         # 让出后本作用域标志仍在
            assert store.is_secure() is True
            assert schema.get_core().require_context() is True

    _run(body())

    # base 零污染（base 三开关原样 fail-open）
    assert store.is_secure() is False, 'base 进程级标志不变'
    assert schema.core.require_context() is False, 'base require_context 仍关闭'
    assert store.require_context() is False


def test_policy_isolation_a3_secure_mode_no_concurrent_leak():
    """一处 secure_mode 不污染并发另一作用域"""
    wait = barrier(2)
    seen = {}

    async def worker(tag, secure):
        with with_scope(view_of({})):
            if secure:
                store.secure_mode(admin_roles=['admin'])
            await wait()                                   # 两者重叠：secured 与 plain 同时存活
            seen[tag] = {
                'is_secure': store.is_secure(),
                'require_context': schema.get_core().require_context(),
            }

    _run_all(worker('secured', True), worker('plain', False))
    assert seen['secured'] == {'is_secure': True, 'require_context': True}
    assert seen['plain'] == {'is_secure': False, 'require_context': False}
    assert store.is_secure() is False
    assert schema.core.require_context() is False


# ── A4：视图只读守卫 ────────────────────────────────────────

def test_policy_isolation_a4_register_in_view_is_readonly():
    """作用域内 register 抛 ERR_POLICY_VIEW_READONLY 且不落 base"""
    async def body():
        with with_scope(view_of({})):
            with pytest.raises(Exception) as ei:
                schema.register({
                    'name': 'IsoNope', 'collection': 'iso_nope', 'timestamps': False,
                    'fields': {}, 'relations': {},
                })
            assert str(ei.value).startswith('ERR_POLICY_VIEW_READONLY:'), \
                f'错误前缀须原样保留：{ei.value}'

    _run(body())
    assert schema.core.has('IsoNope') is False, '被拒定义不得落 base 目录'
    assert schema.has('IsoNope') is False


# ── A5：sink / meta 隔离 ───────────────────────────────────

def test_policy_isolation_a5_sink_meta_per_scope():
    """两并发作用域 sink/meta 隔离"""
    wait = barrier(2)
    seen = {}

    async def worker(tag, tenant, env):
        events = []
        metas = []
        with with_scope(view_of({}), sink=events.append, meta={'tenant': tenant, 'env': env}):
            metas.append(dict(current_scope().meta))
            feedback.emit({'type': 'isolation', 'code': f'{tag}-1', 'layer': 'host'})
            await wait()
            metas.append(dict(current_scope().meta))        # 让出后 meta 仍各归各
            feedback.emit({'type': 'isolation', 'code': f'{tag}-2', 'layer': 'host'})
        seen[tag] = {'events': [e['code'] for e in events], 'metas': metas}

    _run_all(worker('a', 't1', 'e1'), worker('b', 't2', 'e2'))
    assert seen['a']['events'] == ['a-1', 'a-2']
    assert seen['b']['events'] == ['b-1', 'b-2']
    assert seen['a']['metas'] == [{'tenant': 't1', 'env': 'e1'}, {'tenant': 't1', 'env': 'e1'}]
    assert seen['b']['metas'] == [{'tenant': 't2', 'env': 'e2'}, {'tenant': 't2', 'env': 'e2'}]
    assert current_scope() is None


class _SinkStore:
    """最小落库 store：``enable_feedback_table`` 只需 ``insert(name, row)``"""

    def __init__(self, rows):
        self.rows = rows

    async def insert(self, name, row):
        self.rows.append({'name': name, 'row': row})
        return row


def test_policy_isolation_a5_persisted_sink_uses_scope_meta():
    """落库 sink 的 tenant/env 取作用域 meta（并发各归各）"""
    rows = []
    dispose = feedback.enable_feedback_table(_SinkStore(rows))
    try:
        wait = barrier(2)

        async def worker(tag, tenant, env):
            with with_scope(view_of({}), meta={'tenant': tenant, 'env': env}):
                feedback.emit({'type': 'isolation', 'code': f'{tag}-1', 'layer': 'host'})
                await wait()
                feedback.emit({'type': 'isolation', 'code': f'{tag}-2', 'layer': 'host'})

        async def body():
            await asyncio.gather(worker('a', 't1', 'e1'), worker('b', 't2', 'e2'))
            await feedback.flush()

        _run(body())
    finally:
        dispose()                                          # 恢复原 sink

    by_code = {r['row'].get('code'): r['row'] for r in rows}
    for code in ('a-1', 'a-2', 'b-1', 'b-2'):
        assert code in by_code, f'应落库事件 {code}（实际 {sorted(by_code)}）'
    assert [by_code['a-1']['tenant'], by_code['a-1']['env']] == ['t1', 'e1']
    assert [by_code['a-2']['tenant'], by_code['a-2']['env']] == ['t1', 'e1']
    assert [by_code['b-1']['tenant'], by_code['b-1']['env']] == ['t2', 'e2']
    assert [by_code['b-2']['tenant'], by_code['b-2']['env']] == ['t2', 'e2']
    assert all(r['name'] == '__feedback' for r in rows)


def test_policy_isolation_a5_scope_sink_precedes_process_sink():
    """作用域 sink 优先于进程级 sink"""
    proc_events = []
    scoped_events = []
    feedback.set_sink(proc_events.append)
    try:
        async def body():
            with with_scope(view_of({}), sink=scoped_events.append):
                feedback.emit({'type': 'isolation', 'code': 'scoped', 'layer': 'host'})

        _run(body())
        assert [e['code'] for e in scoped_events] == ['scoped']
        assert proc_events == [], '作用域内不得回落到进程级 sink'

        feedback.emit({'type': 'isolation', 'code': 'proc', 'layer': 'host'})
        assert [e['code'] for e in proc_events] == ['proc'], '域外仍走进程级 sink'
    finally:
        feedback.set_sink(None)                            # 恢复默认 stderr，避免污染同进程后续用例


# ── 域外零变更（与步骤 2/3/4 交叉验证）─────────────────────────

def test_policy_isolation_out_of_scope_zero_change():
    """域外零变更（get_core 回退 base，current_scope 为 None）"""
    assert current_scope() is None
    assert schema.get_core() is schema.core

    async def body():
        view = view_of({'profile': PROFILE_AI})
        with with_scope(view):
            assert current_scope().view is view
            assert schema.get_core() is view

    _run(body())

    assert current_scope() is None
    assert schema.get_core() is schema.core
    assert schema.core.profile() == PROFILE_STD
