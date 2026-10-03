"""工作流编排单测（首批）：校验器白名单 / 占位符解析 / when 真值表 / 权限真值表 / 双宿主 parity。

parity 锚：nodejs-store/tests/workflow.test.js 用同一组输入断言相同输出（逐字节一致）；
锚值由 py 侧纯函数生成（tmp/wf_anchors.py 固化至此）。
运行：$env:LOCAL_CORE='1'; $env:PYTHONPATH='py-store/src'; python -m pytest py-store/tests/test_workflow.py -q
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from py_store import schema as sc
from py_store import workflow as wf
from py_store.workflow import WorkflowError


# B1 前置：GOOD 的 gql/mutation 引用 Inventory，注册期可规划性校验要求其已注册。
# 本仓 conftest 的模块级夹具会在每模块清空注册表（clear_schemas），故按约定入口
# `_register_test_schemas` 提供（只定义不执行，由夹具在清场后重放）。
def _register_test_schemas():
    sc.register({
        'name': 'Inventory', 'collection': 'inventory', 'idPrefix': 'inv',
        'fields': {
            '_id': {'type': 'string'}, 'stock': {'type': 'int'},
            'productId': {'type': 'string'}, 'warehouse': {'type': 'string'},
        },
    })

GOOD = {
    'name': 'placeOrder',
    'read': ['admin', 'ops'],
    'write': ['admin'],
    'run': ['admin', 'ops', 'seller'],
    'steps': [
        {'op': 'query', 'as': 'inv',
         'gql': 'Inventory($condition:@c0){_id, stock}',
         'params': {'c0': {'productId': '{{input.productId}}',
                           'warehouse': '{{input.warehouse}}'}}},
        {'op': 'fail', 'when': {'exists': '{{inv._id}}', 'is': None},
         'message': '库存记录不存在'},
        {'op': 'fail', 'when': {'lt': '{{inv.stock}}', 'than': '{{input.qty}}'},
         'message': '库存不足'},
        {'op': 'mutation', 'model': 'Inventory',
         'data': {'_id': '{{inv._id}}',
                  'stock': '{{dec:{{inv.stock}},{{input.qty}}}}'}},
    ],
}

# ─── parity 锚：校验器（与 nodejs-store/tests/workflow.test.js 同输入同输出） ──

_BAD_DEFS = {
    'B1_unknown_op': dict(GOOD, steps=[{'op': 'loop'}]),
    'B2_backward_ref': dict(GOOD, steps=[
        {'op': 'mutation', 'as': 'm1', 'model': 'Inventory', 'data': {'stock': '{{m2._id}}'}},
        {'op': 'mutation', 'as': 'm2', 'model': 'Inventory', 'data': {'stock': 1}}]),
    'B3_dangling_as': dict(GOOD, steps=[
        {'op': 'mutation', 'model': 'Inventory', 'data': {'stock': '{{ghost._id}}'}}]),
    'B4_dec_shape': dict(GOOD, steps=[
        {'op': 'mutation', 'model': 'Inventory', 'data': {'stock': '{{dec:{{inv.stock}}}}'}}]),
    'B5_gql_ph': dict(GOOD, steps=[
        {'op': 'query', 'as': 'inv',
         'gql': 'Inventory($condition:@c0){_id, {{input.field}}}',
         'params': {'c0': {'w': '{{input.warehouse}}'}}}]),
    'B13_self_ref': dict(GOOD, steps=[
        {'op': 'query', 'as': 'inv', 'gql': 'Inventory{_id}',
         'params': {'c0': {'_id': '{{inv._id}}'}}}]),
    'B6_upsert_no_match': dict(GOOD, steps=[
        {'op': 'mutation', 'model': 'Inventory', 'upsert': True, 'data': {'stock': 1}}]),
    'B7_when_shape': dict(GOOD, steps=[
        {'op': 'fail', 'when': {'lt': '{{input.qty}}'}, 'message': 'x'}]),
    'B8_dunder_name': dict(GOOD, name='__mine'),
    'B9_unknown_top': dict(GOOD, retry=3),
    'B10_dup_as': dict(GOOD, steps=[
        {'op': 'query', 'as': 'inv', 'gql': 'Inventory{_id}'},
        {'op': 'query', 'as': 'inv', 'gql': 'Inventory{_id}'}]),
    'B11_index_path': dict(GOOD, steps=[
        {'op': 'mutation', 'model': 'Inventory', 'data': {'stock': '{{input.items.0.id}}'}}]),
    'B12_missing_required': dict(GOOD, steps=[{'op': 'query', 'gql': 'Inventory{_id}'}]),
}

_BAD_ERRORS = {
    'B1_unknown_op': [
        'steps[0]: WORKFLOW_UNSUPPORTED 未知步骤类型 "loop"'
        '（首批白名单: ["query","mutation","fail"]；循环/并行/子工作流/审批节点均不支持）'],
    'B2_backward_ref': [
        'steps[0]: 占位符 {{m2._id}} 引用了不存在的 as "m2"'
        '（前向可用: 无；后向引用不支持）'],
    'B3_dangling_as': [
        'steps[0]: 占位符 {{ghost._id}} 引用了不存在的 as "ghost"'
        '（前向可用: 无；后向引用不支持）'],
    'B4_dec_shape': [
        'steps[0]: 占位符 {{dec:{{inv.stock}}}} dec 须恰两个操作数 dec:<a>,<b>'],
    'B5_gql_ph': [
        'steps[0]: gql 内嵌占位符不支持（注入面）；动态参数请走 params 绑定'],
    'B13_self_ref': [
        'steps[0]: 占位符 {{inv._id}} 引用了不存在的 as "inv"'
        '（前向可用: 无；后向引用不支持）'],
    'B6_upsert_no_match': ['steps[0]: upsert: true 须提供 match 条件'],
    'B7_when_shape': ['steps[0].when: lt 算子缺少右值键 "than"'],
    'B8_dunder_name': ['name "__mine" 以 __ 开头（前缀保留给内建 schema，禁止用于工作流）'],
    'B9_unknown_top': [
        '未知顶层字段 "retry"（白名单: ["description","name","read","run","steps","write"]）'],
    'B10_dup_as': ['steps[1]: as "inv" 重复（引用歧义，禁止覆盖）'],
    'B11_index_path': [
        'steps[0]: 占位符 {{input.items.0.id}} 路径非法'
        '（首批不支持数组下标段，需要逐行处理请走宿主代码编排）'],
    'B12_missing_required': ['steps[0]: query 步骤缺少必填字段 "as"'],
}


def test_parity_validate_good():
    assert wf.validate_defn(GOOD) == []


def test_parity_validate_bad():
    for key, defn in _BAD_DEFS.items():
        assert wf.validate_defn(defn) == _BAD_ERRORS[key], key


def test_register_rejects_and_duplicates():
    with pytest.raises(WorkflowError, match='WORKFLOW_UNSUPPORTED'):
        wf.register(_BAD_DEFS['B1_unknown_op'])
    wf.register(GOOD)
    # 同名同形重复注册幂等通过（对齐 core schema.register 的复跑语义）
    wf.register(GOOD)
    # 同名异形显式 Err（禁止静默覆盖）
    with pytest.raises(WorkflowError, match='已注册且定义不同'):
        wf.register(dict(GOOD, description='changed'))
    # 清理：便于重复运行（注册表进程内）
    wf._workflows.pop(GOOD['name'], None)


def test_register_and_read_filter():
    try:
        wf.register(GOOD)
        assert wf.get('placeOrder') == GOOD
        assert 'placeOrder' in wf.list()
        # read 白名单外不可见（与不存在同形——防枚举）
        with pytest.raises(KeyError):
            wf.get('placeOrder', {'roles': ['user']})
        assert wf.list({'roles': ['user']}) == []
        with pytest.raises(KeyError):
            wf.get('no-such')
    finally:
        wf._workflows.pop(GOOD['name'], None)


def test_register_meta_gate():
    from py_store import schema as sc

    gate = {'name': 'gateWf', 'steps': [{'op': 'fail', 'message': 'x'}]}
    wf._workflows.pop('gateWf', None)
    try:
        # Open（缺省）：无 ctx 放行
        sc.set_meta_policy(False, [])
        wf.register(gate)
        wf._workflows.pop('gateWf', None)

        # Closed：无 ctx → 显式 ERR_PERMISSION，且定义不写入
        sc.set_meta_policy(True, [])
        with pytest.raises(WorkflowError, match='ERR_PERMISSION:'):
            wf.register(gate)
        assert 'gateWf' not in wf._workflows

        # Closed：internal 放行
        wf.register(gate, {'internal': True})
        assert 'gateWf' in wf._workflows
    finally:
        wf._workflows.pop('gateWf', None)
        sc.set_meta_policy(False, [])  # 复位，防污染后续用例


# ─── parity 锚：占位符解析 ───────────────────────────────────

_RESOLVE_CTX = {'inv': {'_id': 'inv1', 'stock': 100}}
_RESOLVE_INPUT = {'qty': 30, 'user': {'name': '张三'}, 'items': [{'id': 'i0'}]}
# 每条: (expr, strict, val, err)——val/err 二选一（err 非 None 即期望报错文案）
_RESOLVE_CASES = [
    ('{{input.qty}}', False, 30, None),
    ('{{input.user.name}}', False, '张三', None),
    ('{{inv._id}}', False, 'inv1', None),
    ('{{dec:{{inv.stock}},{{input.qty}}}}', False, 70, None),
    ('{{dec:10,4}}', False, 6, None),
    ('n={{input.qty}}', False, 'n=30', None),
    ('{{inv.missing}}', True, None,
     '占位符 {{inv.missing}} 解析失败: 路径 "missing" 不存在'),
    ('{{ghost.x}}', True, None,
     '占位符 {{ghost.x}} 引用的 as "ghost" 无可用结果（该步骤可能被 when 跳过或尚未执行）'),
    ('{{inv.missing}}', False, None, None),
    ('{{input.items.0.id}}', True, None,
     '占位符 {{input.items.0.id}} 解析失败: 路径 "items.0.id" 不存在'),
    ('{{dec:{{input.user}},1}}', True, None,
     '占位符 {{dec:{{input.user}},1}} dec 操作数 a 须为数值，收到 dict'),
    ('{{input.qty}}', True, 30, None),
]


def test_parity_resolve():
    for s, strict, val, err in _RESOLVE_CASES:
        if err is not None:
            with pytest.raises(wf._StepFailure) as ei:
                wf._resolve_str(s, _RESOLVE_INPUT, _RESOLVE_CTX, strict)
            assert str(ei.value) == err, s
        else:
            assert wf._resolve_str(s, _RESOLVE_INPUT, _RESOLVE_CTX, strict) == val, s


def test_resolve_full_value_keeps_type():
    """整值占位符保类型（int 30 而非 '30'）——占位符替换进 params/data 不字符串化"""
    v = wf._resolve_str('{{input.qty}}', _RESOLVE_INPUT, _RESOLVE_CTX, True)
    assert v == 30 and isinstance(v, int)


# ─── parity 锚：when 真值表 ──────────────────────────────────

_WHEN_CASES = [
    ({'exists': '{{inv._id}}', 'is': None}, {'inv': {'_id': 'x'}}, False),
    ({'exists': '{{inv._id}}'}, {'inv': {'_id': 'x'}}, True),
    ({'lt': '{{inv.stock}}', 'than': 10}, {'inv': {'stock': 5}}, True),
    ({'lt': '{{inv.stock}}', 'than': 10}, {'inv': {'stock': 15}}, False),
    ({'is': '{{a.v}}', 'than': 5}, {'a': {'v': 5}}, True),
    ({'ne': '{{a.v}}', 'than': 5}, {'a': {'v': 5}}, False),
    ({'lte': '{{a.v}}', 'than': 5.5}, {'a': {'v': 5.5}}, True),
    ({'gte': '{{a.v}}', 'than': 6}, {'a': {'v': 5.5}}, False),
    ({'gt': '{{a.v}}', 'than': '{{b.v}}'}, {'a': {'v': 5}, 'b': {'v': 3}}, True),
    ({'eq': '{{a.v}}', 'than': 5}, {'a': {'v': 5}}, True),
]


def test_parity_when():
    for when, ctx, expect in _WHEN_CASES:
        assert wf._eval_when(when, _RESOLVE_INPUT, ctx) is expect, when
    with pytest.raises(wf._StepFailure, match='操作数类型不可比'):
        wf._eval_when({'lt': '{{a.v}}', 'than': 5}, {}, {'a': {'v': 'x'}})


def test_when_null_three_state():
    """when 取值位三态：inv 无结果时路径取值为 null（服务 exists ... is null 断言）"""
    assert wf._eval_when({'exists': '{{inv._id}}', 'is': None}, {}, {}) is True


# ─── parity 锚：权限真值表 ───────────────────────────────────

_PERM_CASES = [
    (None, None, True),
    (None, {'roles': ['guest']}, False),
    (None, {'roles': ['user']}, True),
    (['ops'], None, True),
    (['ops'], {'roles': ['ops']}, True),
    (['ops'], {'roles': ['user']}, False),
    (['ops'], {'roles': ['user', 'ops']}, True),
    (['ops'], {'roles': ['admin']}, True),
    (['ops'], {'roles': ['super_admin']}, True),
    (['ops'], {'roles': ['guest']}, False),
    (['ops'], {'internal': True, 'roles': ['guest']}, True),
    (['creator'], {'userId': 'u1'}, True),
    (['ops'], {'roles': [], 'role': 'ops'}, True),
]


def test_parity_perm():
    for wl, ctx, expect in _PERM_CASES:
        assert wf._evaluate(wl, ctx) is expect, (wl, ctx)


# ─── 内建 run 表自举 ─────────────────────────────────────────

def test_builtin_schema_registered():
    from py_store import schema as sc
    assert sc.has('__workflowRun')
    mirror = sc.get('__workflowRun')
    assert mirror['idPrefix'] == 'wfrun'
    # Host 镜像只含 defn 声明字段；timestamps（createdAt/updatedAt）由 core register 追加
    fields = set(mirror['fields'])
    assert fields == {'_id', 'workflow', 'status', 'input', 'steps',
                      'error', 'stepIndex', 'dryRun', 'now'}
    # write 显式空名单（R2：拒普通角色 GQL 篡改 run 审计；internal/admin 保留）
    assert mirror['write'] == []


def test_run_whitelist_fallback():
    """run 缺省回退 write；显式 run 优先（§0.6）"""
    assert wf._run_whitelist({'write': ['w1']}) == ['w1']
    assert wf._run_whitelist({'run': ['r1'], 'write': ['w1']}) == ['r1']
    assert wf._run_whitelist({}) is None


# ─── parity 锚：run 文档与步骤迹形状（双宿主逐字段一致） ─────────

def _run_shape_async():
    import asyncio

    async def _main():
        from py_store import executors, init, permission, store
        from py_store import schema as sc
        sc.register({'name': 'ShapeItem', 'collection': 'shape_items', 'idPrefix': 'sh',
                     'fields': {'_id': {'type': 'string'}, 'n': {'type': 'int'}}})
        import aiosqlite

        from py_store import ddl as ddl_mod
        db = await aiosqlite.connect(':memory:')
        for st in [x.strip() for x in str(ddl_mod.generate('sqlite', ['__workflowRun'])).split('\n\n')
                   if x.strip()]:
            await db.execute(st)
        await db.execute(
            'CREATE TABLE shape_items (_id VARCHAR(64) PRIMARY KEY, n INTEGER, '
            'createdAt BIGINT, updatedAt BIGINT, deletedAt BIGINT, __present TEXT)')
        await db.commit()
        await init({'default': executors.create_connection('sqlite', db)})
        permission.set_context({'userId': 'u1', 'roles': ['admin']})
        await store.insert('ShapeItem', {'_id': 'sh1', 'n': 5})
        wf.register({'name': 'shapeWf', 'steps': [
            {'op': 'query', 'as': 'it', 'gql': 'ShapeItem{_id, n}'},
            {'op': 'fail', 'when': {'lt': '{{it.n}}', 'than': 0}, 'message': 'neg'},
            {'op': 'mutation', 'as': 'w', 'model': 'ShapeItem', 'data': {'n': 1}},
        ]})
        run_doc = await store.runWorkflow('shapeWf', {})
        await db.close()
        return run_doc

    return asyncio.new_event_loop().run_until_complete(_main())


def test_parity_run_doc_shape():
    try:
        run_doc = _run_shape_async()
        assert sorted(run_doc) == ['_id', 'createdAt', 'dryRun', 'error', 'input',
                                   'now', 'status', 'stepIndex', 'steps', 'updatedAt',
                                   'workflow']
        assert run_doc['status'] == 'succeeded' and run_doc['error'] is None
        assert [st['state'] for st in run_doc['steps']] == ['ran', 'skipped', 'ran']
        assert sorted(run_doc['steps'][0]) == ['as', 'op', 'result', 'state']
        assert sorted(run_doc['steps'][1]) == ['as', 'op', 'state']
    finally:
        wf._workflows.pop('shapeWf', None)


# ─── B1：注册期 GQL 可规划性校验（结构 + 参数键完整） ─────────

def test_b1_planable_unplanned_rejected():
    bad = {'name': 'noModelWf', 'steps': [
        {'op': 'query', 'as': 'a', 'gql': 'NoSuchModel{_id}', 'params': {}}]}
    assert len(wf.validate_planable(bad)) == 1
    with pytest.raises(wf.WorkflowError, match=r'WORKFLOW_UNSUPPORTED: steps\[0\]: gql 不可规划'):
        wf.register(bad)


def test_b1_planable_missing_param_key():
    bad = {'name': 'missParamWf', 'steps': [
        {'op': 'query', 'as': 'a', 'gql': 'Inventory($condition:@c0){_id}', 'params': {}}]}
    assert wf.validate_planable(bad) == [
        'steps[0]: gql 引用了未提供的参数 @c0（params 须提供该键）']


def test_b1_planable_ok():
    assert wf.validate_planable({'name': 'okWf', 'steps': [
        {'op': 'query', 'as': 'a', 'gql': 'Inventory($condition:@c0){_id, stock}',
         'params': {'c0': {'stock': 1}}}]}) == []
