"""AI 问数（L1）—— ``ask()`` 编排闭环 + ``describe_for_ai`` 摘要（验收矩阵 T1-T12）

对照《AI能力接入设计-L1问数档.md》§7 矩阵逐条落用例；单测一律假 LLM 注入 +
内存 mock 驱动（同 test_profile._FakeDb 同构），不依赖网络与真实厂商；
档位/权限/硬限判决全部经真实 Rust core（LOCAL_CORE 产物），mock 只替 IO。

与矩阵原文的三处按实现取证调整（均已在用例 docstring 留痕）：
  - T2：A15/档位清单定义根级超限为 **clamp**（force/clamp_t2q_limit，不 Err），
    矩阵原文「ProfileViolation」与之冲突 —— 按 core 实现验证硬限 clamp 生效；
  - T4：route_override 通道在编排面硬编码封闭（D5），LLM 于 params 夹带的
    route_override 键是普通未引用参数、不触达路由通道 —— 按「护栏不可触」
    断言编排面（crud.query 第三参恒 None），而非依赖夹带触发拦截；
  - T8：字段级越权（guest 投影 admin 专属字段）的 core 判决形态是**投影静默剥离**
    （is_field_readable 过滤，数据不出）而非报错 —— 「被权限层拒」按数据形态断言。

沿革：U1~U4 / 8c-2 档位收缩 Err 曾缺 ``ERR_TEXT2QUERY:`` 稳定前缀（回喂 code 落
``planError``，缺 core 层 ``profileBlocked`` 告警），已在 rust-store core 统一补齐
（``forbid_t2q_shape`` 单点包装，文案带 ``[feature]`` 门禁项标签）——T5/T11 按
``profileBlocked``（layer='core'）断言。

运行：LOCAL_CORE=1 PYTHONPATH=src python -m pytest tests/test_ask.py -q
"""

import asyncio
import copy
import json

import pytest

from py_store import ask as _ask_mod
from py_store import crud as _crud_mod
from py_store import feedback, store
from py_store import permission as perm
from py_store import schema as _sc
from py_store.llm import get_llm, register_llm

# ── 场景 schema（前缀 Ak；关系链深度 4 供 T3） ─────────────────

_AK_SCHEMAS = [
    {
        'name': 'AkUser', 'collection': 'ak_users', 'idPrefix': 'aku', 'timestamps': True,
        # guest 显式可读模型（read:[] 时 guest 默认禁止），但 role 字段仍 admin 专属 → T8 验证列级过滤
        'read': ['user', 'guest'],
        'fields': {
            '_id': 'string',
            'name': 'string',
            'role': {'type': 'string', 'read': ['admin']},
            'profile': {'type': 'object', 'fields': {'city': 'string'}},
        },
        'relations': {
            'posts': {'model': 'AkPost', 'type': 'many',
                      'localField': '_id', 'foreignField': 'userId'},
        },
        'computes': {
            # read 白名单计算列：core-py 未导出 readable_computes 判决 → 摘要收窄 + 告警
            'displayName': {'type': 'string', 'read': ['admin'],
                            'fn': lambda item, ctx=None: item.get('name')},
            'upperName': {'type': 'string',
                          'fn': lambda item, ctx=None: (item.get('name') or '').upper()},
        },
    },
    {
        'name': 'AkOrder', 'collection': 'ak_orders', 'idPrefix': 'ako', 'timestamps': True,
        'read': [],
        'fields': {'_id': 'string', 'code': 'string', 'amount': 'float'},
        'relations': {'items': {'model': 'AkOrderItem', 'type': 'many',
                                'localField': '_id', 'foreignField': 'orderId'}},
        'computes': {'itemCount': {'type': 'int', 'agg': {'$count': 'items'}}},
    },
    {
        'name': 'AkOrderItem', 'collection': 'ak_order_items', 'idPrefix': 'aki',
        'timestamps': False, 'read': [],
        'fields': {'_id': 'string', 'sku': 'string', 'qty': 'int'},
        'relations': {'stock': {'model': 'AkStock', 'type': 'one',
                                'localField': 'sku', 'foreignField': 'sku'}},
    },
    {
        'name': 'AkStock', 'collection': 'ak_stocks', 'idPrefix': 'aks',
        'timestamps': False, 'read': [],
        'fields': {'_id': 'string', 'sku': 'string', 'qty': 'int'},
        'relations': {'owner': {'model': 'AkUser', 'type': 'one',
                                'localField': 'ownerId', 'foreignField': '_id'}},
    },
    {
        'name': 'AkPost', 'collection': 'ak_posts', 'idPrefix': 'akp',
        'timestamps': False, 'read': [],
        'fields': {'_id': 'string', 'title': 'string'},
    },
]

def _register_test_schemas():
    """模块 schema 注册入口（conftest 模块隔离夹具在首用例前调用；import 零副作用）"""
    for d in _AK_SCHEMAS:
        _sc.register(d)

CTX_USER = {'userId': 'u1', 'roles': ['user']}
CTX_GUEST = {'userId': 'g1', 'roles': ['guest']}
CTX_ADMIN = {'userId': 'a1', 'roles': ['admin']}

_GOOD = ('AkOrder($condition:@c0){_id, code}', {'c0': {}})
_DOCS = [{'_id': 'ako1', 'code': 'A1', 'amount': 9.9}]
# core 按投影剥离字段后的返回形态
_PROJ_DOCS = [{'_id': 'ako1', 'code': 'A1'}]


def _run(coro):
    return asyncio.run(coro)


def _out(gql, params):
    return json.dumps({'gql': gql, 'params': params}, ensure_ascii=False)


class FakeLlm:
    """脚本化假 LLM：按轮次吐预置输出；记录每轮收到的 messages（断言回喂与护栏零暴露）"""

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []

    async def __call__(self, messages):
        self.calls.append(copy.deepcopy(messages))
        if not self.outputs:
            raise AssertionError('假 LLM 输出脚本耗尽（出现了意外的额外轮次）')
        return self.outputs.pop(0)


# ── 最小内存驱动 mock（与 test_profile._Coll 同构；另记录收到的命令） ──

class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, length=None):
        return list(self._docs)


class _Coll:
    def __init__(self, docs=None):
        self.docs = docs if docs is not None else []
        self.aggregations = []

    def find(self, query=None, projection=None):
        return _Cursor(self.docs)

    async def aggregate(self, pipeline, **kwargs):
        self.aggregations.append(pipeline)
        return _Cursor(self.docs)


class _FakeDb:
    def __init__(self, docs=None):
        self.coll = _Coll(docs)

    def __getitem__(self, name):
        return self.coll


def _mock(docs=None):
    perm.set_context(None)
    db = _FakeDb(docs)
    _crud_mod.set_db(db)
    return db.coll


@pytest.fixture(autouse=True)
def _reset():
    """每用例复位档位/上下文/反馈 sink —— 进程级全局，防用例间串扰"""
    _sc.set_profile('standard')
    perm.set_context(None)
    feedback.set_sink(None)
    yield
    _sc.set_profile('standard')
    perm.set_context(None)
    feedback.set_sink(None)


# ── T1 正常问数（正向断言：成功态轨迹无任何错误字段） ──────────

def test_t1_success_track_has_no_error_fields():
    _mock(_DOCS)
    llm = FakeLlm(_out(*_GOOD))
    res = _run(store.ask('列出全部订单', llm=llm, ctx=CTX_USER))
    assert res.data == _PROJ_DOCS
    assert len(res.attempts) == 1
    a = res.attempts[0]
    assert a['gql'] == _GOOD[0] and a['params'] == _GOOD[1] and a['rows'] == 1
    # no-error-masking 正向断言：成功态轨迹中不得出现任何错误字段
    assert all('error' not in x for x in res.attempts)
    # 无拦截 → 无反馈事件
    assert res.events == []
    # 沙箱退出恢复：档位 / 上下文 / sink 全部还原（token-set/reset）
    assert _sc.get_profile() == 'standard'
    assert perm.get_context() is None
    assert feedback.get_sink() is None


def test_t1_messages_guardrails_not_exposed():
    """D5：受信物零暴露——ctx 值不出现在任何 LLM 消息；user 通道不含受信参数字样。

    （system 知识文本中「禁 route_override」属护栏教育性提及，非受信物暴露——
    通道封闭本身由 test_t4 的 crud.query 第三参恒 None 断言。）"""
    _mock(_DOCS)
    llm = FakeLlm(_out(*_GOOD))
    _run(store.ask('列出全部订单', llm=llm, ctx=CTX_USER))
    assert len(llm.calls) == 1
    system = llm.calls[0][0]
    assert system['role'] == 'system' and 'json' in system['content'].lower()
    assert llm.calls[0][1] == {'role': 'user', 'content': '列出全部订单'}
    dumped = json.dumps(llm.calls[0], ensure_ascii=False)
    assert 'u1' not in dumped and 'userId' not in dumped


# ── T2 行数硬限（按实现取证：根级 clamp，见模块 docstring） ────

def test_t2_root_limit_clamped_to_1000_without_error():
    coll = _mock(_DOCS)
    llm = FakeLlm(_out('AkOrder($condition:@c0,$limit:@l){_id, code}',
                       {'c0': {}, 'l': 5000}))
    res = _run(store.ask('取前 5000 条订单', llm=llm, ctx=CTX_USER))
    assert res.data == _PROJ_DOCS
    # core force/clamp_t2q_limit：根级显式超限夹到 T2Q_MAX_ROWS=1000（A15）
    assert any(st.get('$limit') == 1000 for st in coll.aggregations[-1])
    assert res.events == []


# ── T3 关系深度 4 → 档位判决 → 回喂重试 ────────────────────────

def test_t3_depth4_blocked_then_retry_success():
    _mock(_DOCS)
    llm = FakeLlm(
        _out('AkOrder{items{stock{owner{posts{title}}}}}', {}),
        _out(*_GOOD),
    )
    res = _run(store.ask('订单连同明细库存和作者帖子', llm=llm, ctx=CTX_USER))
    assert len(res.attempts) == 2
    err = res.attempts[0]['error']
    # 深度超限走稳定前缀（lookup.rs:181 ERR_TEXT2QUERY）→ ProfileViolation 事件原样回喂
    assert err['code'] == 'profileBlocked' and err['layer'] == 'core'
    assert '深度' in err['message']
    assert res.attempts[1]['rows'] == 1
    assert [e['code'] for e in res.events] == ['profileBlocked']


# ── T4 route_override 夹带（按 D5 取证：通道封闭，见模块 docstring） ──

def test_t4_route_override_smuggling_is_inert(monkeypatch):
    captured = {}
    real_query = _crud_mod.query

    async def spy(gql, params=None, route_override=None):
        captured['route_override'] = route_override
        return await real_query(gql, params, route_override)

    monkeypatch.setattr(_crud_mod, 'query', spy)
    _mock(_DOCS)
    llm = FakeLlm(_out('AkOrder($condition:@c0){_id, code}',
                       {'c0': {}, 'route_override': {'source': 'evil'}}))
    res = _run(store.ask('列出全部订单', llm=llm, ctx=CTX_USER))
    # 编排面硬编码：crud.query 收到的 route_override 实参恒为 None（LLM 不可触）
    assert captured['route_override'] is None
    # 夹带键只是未引用参数，不产生任何路由效果，查询照常成功
    assert res.data == _PROJ_DOCS


# ── T5 object 字段条件（U2 收缩）→ 回喂重试 ────────────────────

def test_t5_object_field_condition_blocked_then_retry():
    _mock([{'_id': 'aku1', 'name': 'bob'}])
    llm = FakeLlm(
        _out('AkUser($condition:@c0){_id, name}', {'c0': {'profile': {'city': 'x'}}}),
        _out('AkUser($condition:@c0){_id, name}', {'c0': {'name': 'bob'}}),
    )
    res = _run(store.ask('查住在 x 城的用户', llm=llm, ctx=CTX_USER))
    err = res.attempts[0]['error']
    # U2 收缩：core Err 携带 ERR_TEXT2QUERY 稳定前缀 → ProfileViolation → profileBlocked
    assert err['code'] == 'profileBlocked' and err['layer'] == 'core'
    assert err['feature'] == 'U2 对象字段条件'
    assert 'U2' in err['message'] and 'text2query' in err['message']
    assert res.data == [{'_id': 'aku1', 'name': 'bob'}]
    assert [e['code'] for e in res.events] == ['profileBlocked']


# ── T6 非法 JSON / 非法 GQL → 结构化回喂 ───────────────────────

def test_t6_bad_json_then_bad_gql_fed_back_verbatim():
    _mock(_DOCS)
    llm = FakeLlm(
        '这不是 json',
        '{"gql": "AkOrder((((", "params": {}}',
        _out(*_GOOD),
    )
    res = _run(store.ask('列出全部订单', llm=llm, ctx=CTX_USER))
    assert [a['error']['code'] for a in res.attempts[:2]] == ['badLlmOutput', 'planError']
    assert res.attempts[0]['error']['raw'] == '这不是 json'
    assert len(res.attempts) == 3
    # 回喂消息面：assistant 原文 + user {"error": ...}（§4.3 契约）
    third = llm.calls[2]
    assert third[-2] == {'role': 'assistant', 'content': '{"gql": "AkOrder((((", "params": {}}'}
    assert third[-1]['role'] == 'user'
    assert json.loads(third[-1]['content'])['error']['code'] == 'planError'


# ── T7 重试耗尽 → AskExhausted 携带全轨迹，不返回空结果 ─────────

def test_t7_exhausted_raises_with_full_track():
    _mock(_DOCS)
    llm = FakeLlm(*(['坏输出'] * 4))
    with pytest.raises(store.AskExhausted) as ei:
        _run(store.ask('列出全部订单', llm=llm, ctx=CTX_USER, max_retries=3))
    # 总尝试 = 1 + max_retries；全轨迹在案；异常形态（非数据）即「不返回空结果」
    assert len(ei.value.attempts) == 4
    assert all(a['error']['code'] == 'badLlmOutput' for a in ei.value.attempts)
    assert len(llm.calls) == 4
    assert 'badLlmOutput' in str(ei.value)


def test_t7_max_retries_zero_single_attempt():
    _mock(_DOCS)
    llm = FakeLlm('坏输出')
    with pytest.raises(store.AskExhausted) as ei:
        _run(store.ask('列出全部订单', llm=llm, ctx=CTX_USER, max_retries=0))
    assert len(ei.value.attempts) == 1


# ── T8 guest：摘要过滤 + 越权查询被权限层拒 ────────────────────

def test_t8_guest_summary_filters_admin_only_and_overreach_denied():
    s = store.describe_for_ai(CTX_GUEST)
    assert all(not m['name'].endswith('Deleted') for m in s)  # 归档表排除（A12）
    aku = next(m for m in s if m['name'] == 'AkUser')
    assert 'role' not in aku['fields']            # admin 专属字段被 A11 过滤
    assert 'name' in aku['fields']
    assert 'displayName' not in aku['computes']   # read 白名单计算列收窄
    assert 'upperName' in aku['computes']
    assert all(k not in m for m in s for k in ('indexes', 'datasource', 'namespace'))
    # 越权查询：guest 投影 admin 专属字段 role → 权限层剥离（数据不出，见模块 docstring 留痕）
    _mock([{'_id': 'aku1', 'name': 'bob', 'role': 'admin'}])
    llm = FakeLlm(_out('AkUser($condition:@c0){_id, role}', {'c0': {}}))
    res = _run(store.ask('列出用户和角色', llm=llm, ctx=CTX_GUEST))
    assert all('role' not in row for row in res.data)
    assert res.attempts[0]['rows'] == 1


def test_t8_summary_without_ctx_names_only():
    """无 ctx：仅暴露模型名与字段名，不暴露类型细节（防探针，§4.1）"""
    s = store.describe_for_ai()
    aku = next(m for m in s if m['name'] == 'AkUser')
    assert isinstance(aku['fields'], list)
    assert set(aku['fields']) == {'_id', 'name', 'profile', 'role'}
    assert 'relations' not in aku and 'computes' not in aku


def test_t8_admin_summary_and_compute_skip_warning():
    """admin 视角：role 进摘要；read 白名单计算列无论角色一律收窄并告警（同签名去重）"""
    _ask_mod._COMPUTE_SKIP_SIGS.clear()  # 告警去重为进程级，单测内复位以保证可断言
    # 清单化语义（设计 §11.5）：AkUser.read 白名单不含 admin，admin 视角需显式豁免
    from py_store import permission as _perm
    _perm.set_exempt_roles(['admin'])
    try:
        events = []
        feedback.set_sink(events.append)
        s = store.describe_for_ai(CTX_ADMIN)
        aku = next(m for m in s if m['name'] == 'AkUser')
        assert aku['fields']['role'] == 'string'
        assert 'displayName' not in aku['computes']
        assert 'upperName' in aku['computes']
        assert 'itemCount' in next(m for m in s if m['name'] == 'AkOrder')['computes']
        warns = [e for e in events if e['type'] == 'ask_summary_compute_skipped']
        assert len(warns) == 1
        assert warns[0]['model'] == 'AkUser' and warns[0]['compute'] == 'displayName'
        # 同签名只告警一次（再次 describe 不重复）
        store.describe_for_ai(CTX_ADMIN)
        assert len([e for e in events if e['type'] == 'ask_summary_compute_skipped']) == 1
    finally:
        _perm.set_exempt_roles([])


# ── T9 无 ctx 直接拒绝（fail-secure，A4） ──────────────────────

def test_t9_missing_ctx_rejected_before_llm_call():
    llm = FakeLlm(_out(*_GOOD))
    with pytest.raises(ValueError):
        _run(store.ask('列出全部订单', llm=llm, ctx=None))
    assert llm.calls == []  # 未发生任何 LLM 调用
    assert _sc.get_profile() == 'standard'


# ── T10 聚合放行 + 省略 $limit 视为上限 1000（A15/W1） ─────────

def test_t10_group_agg_allowed_and_capped():
    coll = _mock(_DOCS)
    llm = FakeLlm(_out('AkOrder($group:@g0,$sort:@s0){code, n}',
                       {'g0': {'by': ['code'], 'agg': {'n': {'$count': '*'}}},
                        's0': {'n': -1}}))
    res = _run(store.ask('按 code 分组计数', llm=llm, ctx=CTX_USER))
    assert res.data == _DOCS
    pipe = coll.aggregations[-1]
    assert any('$group' in st for st in pipe)
    assert any(st.get('$limit') == 1000 for st in pipe)


def test_t10_omitted_limit_treated_as_cap():
    coll = _mock(_DOCS)
    llm = FakeLlm(_out('AkOrder{_id, code}', {}))
    res = _run(store.ask('全部订单', llm=llm, ctx=CTX_USER))
    assert res.data == _PROJ_DOCS
    # 省略 $limit → force_t2q_limit 注入根级 1000（不因 stages.len()==1 退化丢 limit）
    assert any(st.get('$limit') == 1000 for st in coll.aggregations[-1])


# ── T11 关系聚合谓词嵌套路径（8c-2 收缩）→ 回喂重试 ────────────

def test_t11_nested_relation_predicate_blocked_then_retry():
    _mock(_DOCS)
    llm = FakeLlm(
        _out('AkOrder($condition:@c0){_id}', {
            'c0': {'items': {'filter': {'stock.sku': 's1'},
                             'agg': {'n': {'$count': '*'}},
                             'having': {'n': {'$gt': 0}}}}}),
        _out(*_GOOD),
    )
    res = _run(store.ask('库存里有 s1 的订单', llm=llm, ctx=CTX_USER))
    err = res.attempts[0]['error']
    # 8c-2 收缩：core Err 携带 ERR_TEXT2QUERY 稳定前缀 → ProfileViolation → profileBlocked
    # （message 为剥离前缀后的原样文案，feature 取自 [8c-2 嵌套关系路径] 门禁项标签）
    assert err['code'] == 'profileBlocked' and err['layer'] == 'core'
    assert err['feature'] == '8c-2 嵌套关系路径'
    assert '嵌套关系路径' in err['message']
    assert res.data == _PROJ_DOCS
    assert [e['code'] for e in res.events] == ['profileBlocked']


# ── T12 $pipeline 直通（forbid_t2q）→ 回喂重试 ─────────────────

def test_t12_pipeline_direct_blocked_then_retry():
    _mock(_DOCS)
    llm = FakeLlm(
        _out('AkOrder($pipeline:@p0){_id, code}', {'p0': [{'$match': {}}]}),
        _out(*_GOOD),
    )
    res = _run(store.ask('自定义管道查询', llm=llm, ctx=CTX_USER))
    err = res.attempts[0]['error']
    assert err['code'] == 'profileBlocked'
    assert err['feature'] == '$pipeline 直通'
    assert res.data == _PROJ_DOCS
    assert [e['code'] for e in res.events] == ['profileBlocked']


# ── D6 插拔注册表：注册名与客户端两路同一入口 ──────────────────

def test_d6_registry_name_entrypoint_and_unregistered_error():
    _mock(_DOCS)
    register_llm('ak-test-llm', FakeLlm(_out(*_GOOD)))
    res = _run(store.ask('列出全部订单', llm='ak-test-llm', ctx=CTX_USER))
    assert res.data == _PROJ_DOCS
    with pytest.raises(KeyError):
        get_llm('ak-not-registered')
    with pytest.raises(ValueError):
        register_llm('ak-test-llm', FakeLlm())  # 同名重复注册显式报错
