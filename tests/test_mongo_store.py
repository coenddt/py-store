"""mongo-store 单元测试（纯逻辑，无真实 DB）：pipeline / permission / computes / crud

从业务工程单测平移而来；schema 注册用本文件内置的最小模型（替代业务工程 register_all）。
"""

import asyncio
from types import SimpleNamespace

import pytest

from mongo_store import computes as comp
from mongo_store import crud as _crud_mod
from mongo_store import pipeline as ppl
from mongo_store import schema as _sc
import mongo_store.permission as perm


# ---------- 内置最小 schema（与业务工程 CommercialLedger 同构） ----------

@pytest.fixture(scope='module', autouse=True)
def _register():
    _sc.register({'name': 'CommercialLedger', 'collection': 'commercial_ledger',
                  'idPrefix': 'CL', 'timestamps': True,
                  'fields': {'unit': 'string', 'income': 'float'},
                  'relations': {}, 'read': None, 'write': None})
    _sc.register({'name': 'GoalLedger', 'collection': 'goal_ledger',
                  'idPrefix': 'GL', 'timestamps': True,
                  'fields': {'income': 'float'},
                  'relations': {}, 'read': None, 'write': None})


# ---------- pipeline 纯函数（无 DB，内联 schema） ----------

# 构造与 schema.get() 输出同构的内联 schema（fields 为 {type, ...} 字典）
_PIPE_SRC = {
    'name': 'Source', 'collection': 'src', 'timestamps': True,
    'fields': {
        '_id': {'type': 'string'},
        'unit': {'type': 'string'},
        'tags': {'type': 'array'},
        'obj': {'type': 'object', 'fields': {'x': {'type': 'string'}, 'y': {'type': 'string'}}},
    },
    'relations': {'rows': {'model': 'Row', 'type': 'many', 'localField': '_id',
                           'foreignField': 'srcId'}},
    'computes': {},
}
_PIPE_ROW = {
    'name': 'Row', 'collection': 'row', 'timestamps': True,
    'fields': {'income': {'type': 'float'}, 'label': {'type': 'string'}},
    'relations': {}, 'computes': {},
}


def test_pipeline_tokenize():
    toks = ppl.tokenize('Model($condition:@c0) { a, b }')
    k = {t['v'] for t in toks}
    assert 'Model' in k and '@c0' in k and 'a' in k


def test_pipeline_parse_gql_ast():
    ast = ppl.parse_gql('Model($condition:@c0){a, Row{b, c}}')
    assert ast['model'] == 'Model'
    assert ast['params']['condition'] == '@c0'
    assert ast['fields'] == ['a']
    assert set(ast['relations']['Row']['fields']) == {'b', 'c'}  # 关系子体无参数
    assert ast['relations']['Row']['params'] == {}


def test_pipeline_parse_gql_relation_with_params_no_body():
    # 既有解析器局限：带参数的关系节点后不能接花括号子体（has_brace 在解析参数前取值）
    ast = ppl.parse_gql('Model{a, Row($sort:@s0)}')
    assert ast['relations']['Row']['params']['sort'] == '@s0'
    assert ast['relations']['Row']['fields'] == []


def test_pipeline_parse_error_at_eof():
    with pytest.raises(ValueError):
        ppl.parse_gql('Model{$condition:')  # 未闭合 → consume 到末尾抛错


def test_pipeline_flatten_object_fields():
    ast = {'fields': [], 'relations': {'obj': {'fields': ['x', 'y'], 'params': {}}}}
    ppl.flatten_object_fields(ast, _PIPE_SRC)
    assert ast['fields'] == ['obj.x', 'obj.y']
    assert 'obj' not in ast['relations']


def test_pipeline_flatten_object_skips_non_object():
    ast = {'fields': ['unit'], 'relations': {'rows': {'fields': ['income'], 'params': {}}}}
    ppl.flatten_object_fields(ast, _PIPE_SRC)
    # rows 是 many 关系非 object → 不被展平
    assert 'rows' in ast['relations'] and ast['fields'] == ['unit']


def test_pipeline_build_projection_basic():
    schema = {'fields': {'unit': {'type': 'string'}, 'income': {'type': 'float'}}, 'computes': {}}
    proj = ppl.build_projection({'fields': ['unit', 'income'], 'relations': {}}, schema)
    assert proj['_id'] == 1 and proj['unit'] == 1 and proj['income'] == 1


def test_pipeline_build_projection_empty_fields_none():
    assert ppl.build_projection({'fields': [], 'relations': {}}, {'fields': {}, 'computes': {}}) is None


def test_pipeline_build_projection_compute_with_depends():
    schema = {'fields': {'a': {'type': 'string'}, 'dep': {'type': 'string'}},
              'computes': {'total': {'type': 'float', 'fn': lambda r: 0, 'depends': ['dep']}}}
    proj = ppl.build_projection({'fields': ['a'], 'relations': {}}, schema)
    # 声明了 depends → 仅补依赖字段，计算列名不入投影
    assert proj['a'] == 1 and proj['dep'] == 1 and 'total' not in proj


def test_pipeline_build_projection_compute_no_depends_fallback():
    schema = {'fields': {'a': {'type': 'string'}, 'c': {'type': 'string'}},
              'computes': {'total': {'type': 'float', 'fn': lambda r: 0}}}
    proj = ppl.build_projection({'fields': ['a'], 'relations': {}}, schema)
    # 无 depends → 安全兜底包含所有 schema 字段
    assert proj['a'] == 1 and proj['c'] == 1


def test_pipeline_build_projection_dot_field():
    schema = {'fields': {'obj': {'type': 'object', 'fields': {'x': {}}}}, 'computes': {}}
    proj = ppl.build_projection({'fields': ['obj.x'], 'relations': {}}, schema)
    assert proj['obj.x'] == 1


def test_pipeline_build_projection_relations_included():
    schema = {'fields': {'unit': {'type': 'string'}}, 'computes': {},
              'relations': {'rows': {'model': 'Row'}}}
    proj = ppl.build_projection({'fields': ['unit'], 'relations': {'rows': {}}}, schema)
    assert proj['rows'] == 1


def test_pipeline_build_compute_lookup_stages():
    schema = {'computes': {
        'a': {'type': 'any'},
        'b': {'type': 'any', 'lookup': {'from': 'refs'}},
    }}
    stages = ppl.build_compute_lookup_stages(schema)
    assert len(stages) == 1 and stages[0]['$lookup']['from'] == 'refs'


def test_pipeline_build_empty_lookup_array_guard():
    rel_def = {'localField': 'tags', 'foreignField': 'tagId'}
    out = ppl.build_empty_lookup('tags', rel_def, {'collection': 'tag'},
                                 {'fields': {'tags': {'type': 'array'}}})
    let = out['$lookup']['let']['rel_tags']
    assert '$cond' in let  # 数组字段 → $cond/$isArray 守卫
    assert out['$lookup']['pipeline'][0]['$match']['$expr']['$in']


def test_pipeline_build_lookup_scalar_with_sort_limit():
    rel_def = {'localField': '_id', 'foreignField': 'srcId'}
    rel_ast = {'params': {'condition': '@c0', 'sort': '@s0', 'limit': '@l0'}, 'fields': ['income']}
    out = ppl.build_lookup('rows', rel_ast, {'c0': {'income': {'$gt': 0}}, 's0': {'income': -1}, 'l0': 5},
                           rel_def, _PIPE_ROW, _PIPE_SRC)
    pl = out['$lookup']['pipeline']
    assert pl[0]['$match']['$and'][0]['$expr']['$eq'] == ['$srcId', '$$rel__id']
    assert {'$sort': {'income': -1}} in pl and {'$limit': 5} in pl
    assert pl[-1]['$project']['income'] == 1


def test_pipeline_build_lookup_depth_guard_empty():
    rel_def = {'localField': '_id', 'foreignField': 'srcId'}
    rel_ast = {'params': {'limit': '@l0'}, 'fields': []}
    out = ppl.build_lookup('rows', rel_ast, {'l0': 1}, rel_def, _PIPE_ROW, _PIPE_SRC,
                           _depth=10, _paginated=0)  # 超 MAX_DEPTH → 空 lookup 降级
    assert len(out['$lookup']['pipeline']) == 1  # 仅 $match，不再嵌套/投影


def test_pipeline_build_lookup_nested_relation(monkeypatch):
    child_schema = {'name': 'Child', 'collection': 'child', 'relations': {}, 'computes': {},
                    'fields': {'name': {'type': 'string'}}}
    row_schema = {'name': 'Row', 'collection': 'row',
                  'relations': {'child': {'model': 'Child', 'type': 'one',
                                          'localField': '_id', 'foreignField': 'rowId'}},
                  'computes': {}, 'fields': {'income': {'type': 'float'}}}
    monkeypatch.setattr(ppl, 'get', lambda n: {'Child': child_schema, 'Row': row_schema}[n])
    rel_ast = {'params': {}, 'fields': ['income'],
               'relations': {'child': {'params': {}, 'fields': ['name']}}}
    rel_def = {'localField': '_id', 'foreignField': 'rowId'}
    out = ppl.build_lookup('rows', rel_ast, {}, rel_def, row_schema, _PIPE_SRC)
    # 嵌套 child + one 关系 → 追加 $unwind
    pl = out['$lookup']['pipeline']
    assert {'$unwind': {'path': '$child', 'preserveNullAndEmptyArrays': True}} in pl


def test_pipeline_build_add_fields(monkeypatch):
    schema = {'computes': {
        'b': {'type': 'any', 'lookup': {'from': 'refs', 'addFields': {'$size': ['$tags']}}},
    }}
    monkeypatch.setattr(ppl, 'get_readable_computes', lambda s, ctx: {'b'})
    out = ppl.build_add_fields(schema, {'roles': ['admin']})
    assert out['$addFields']['b'] == {'$size': ['$tags']}


def test_pipeline_build_pipeline_custom_mode(monkeypatch):
    monkeypatch.setattr(ppl, 'get',
                        lambda n: {'name': n, 'collection': 'c', 'fields': {}, 'relations': {}, 'computes': {}})
    ast = {'model': 'X', 'params': {'pipeline': '@p0', 'condition': '@c0', 'limit': '@l0'},
           'fields': [], 'relations': {}}
    out = ppl.build_pipeline(ast, {'p0': [{'$match': {}}, {'$limit': 10}], 'c0': {'a': 1}, 'l0': 50})
    assert out[0]['$match'] == {'a': 1}  # 自定义管道存在 $match → 原地替换
    assert {'$limit': 50} in out


def test_pipeline_build_pipeline_standard_no_ctx(monkeypatch):
    monkeypatch.setattr(ppl, 'get',
                        lambda n: {'name': n, 'collection': 'c', 'fields': {'a': {'type': 'int'}},
                                   'relations': {}, 'computes': {}})
    ast = {'model': 'X', 'params': {'condition': '@c0', 'limit': '@l0'},
           'fields': ['a'], 'relations': {}}
    out = ppl.build_pipeline(ast, {'c0': {'year': 2026}, 'l0': 10})
    assert out[0] == {'$match': {'year': 2026}}
    assert {'$limit': 10} in out


def test_pipeline_override_or_append():
    stages = [{'$match': {'a': 1}}]
    ppl._override_or_append(stages, '$match', {'b': 2})  # 已有 → 覆盖
    assert stages[0]['$match'] == {'b': 2}
    ppl._override_or_append(stages, '$sort', {'x': -1})  # 无 → 追加
    assert stages[-1] == {'$sort': {'x': -1}}


def test_pipeline_append_order():
    stages = []
    ppl._append_order(stages, {'t': -1}, 10, 5)
    assert stages == [{'$sort': {'t': -1}}, {'$skip': 10}, {'$limit': 5}]
    ppl._append_order(stages, None, None, None)  # 全 None → 无变化
    assert len(stages) == 3


def test_pipeline_custom_pipeline_branch():
    root = [{'$match': {'_id': 'r'}}]
    out = ppl._custom_pipeline_branch([], root, {'y': 2026}, {'income': -1}, 5, 3)
    assert out[0]['$match'] == {'y': 2026}  # condition 覆盖已有 $match
    assert {'$sort': {'income': -1}} in out and {'$skip': 5} in out and {'$limit': 3} in out


def test_pipeline_ns_lookup_stages_many_no_unwind(monkeypatch):
    rel_ast = {'fields': ['x'], 'relations': {'rows': {'fields': ['v'], 'params': {}}}}
    rel_schema = {'name': 'Parent', 'fields': {'x': {'type': 'string'}},
                  'relations': {'rows': {'type': 'many', 'model': 'Child',
                                        'localField': 'rowIds', 'foreignField': '_id'}}}
    # 注入子 schema 到注册表
    backup = dict(getattr(_sc, '_schemas', {}))
    _sc._schemas['Child'] = {'name': 'Child', 'collection': 'child', 'fields': {'v': {'type': 'float'}},
                             'computes': {}, 'relations': {}}
    try:
        stages = ppl._ns_lookup_stages(rel_ast, rel_schema, {}, 1, 0)
        assert len(stages) == 1  # many 关系无 $unwind
        assert '$lookup' in stages[0]
    finally:
        if backup:
            _sc._schemas.update(backup)
        else:
            _sc._schemas.pop('Child', None)


# ---------- permission 纯逻辑 ----------


def test_perm_evaluate_no_roles_ctx_none():
    assert perm.evaluate(None, None) is True       # 无上下文 → 权限放行
    assert perm.evaluate({'roles': ['buyer']}, None) is True  # 无白名单 + 已登录 → 放行
    assert perm.evaluate({'roles': ['guest']}, None) is False # 无白名单 + guest → 拒


def test_perm_evaluate_internal_always_pass():
    assert perm.evaluate({'internal': True}, ['seller']) is True
    assert perm.evaluate({'internal': True, 'roles': ['guest']}, ['seller']) is True


def test_perm_evaluate_role_match_and_creator():
    assert perm.evaluate({'roles': ['admin']}, ['super_admin', 'admin']) is True
    assert perm.evaluate({'roles': ['seller']}, ['admin']) is False
    assert perm.evaluate({'roles': ['seller']}, ['creator']) is True   # 新插入，doc=_MISSING
    assert perm.evaluate({'roles': [], 'userId': 'u1'}, ['creator'], {'createdBy': 'u1'}) is True
    assert perm.evaluate({'roles': [], 'userId': 'u1'}, ['creator'], {'createdBy': 'u2'}) is False


def test_perm_schema_read_write():
    s = {'read': ['admin'], 'write': ['seller']}
    assert perm.can_read_schema(s, {'roles': ['admin']}) is True
    assert perm.can_read_schema(s, {'roles': ['seller']}) is False
    assert perm.can_write_schema(s, {'roles': ['seller']}) is True
    # 游客无论配置如何均无写权限
    assert perm.can_write_schema(s, {'roles': ['guest']}) is False


def test_perm_owner_condition():
    # 无 userId / internal / admin → 不注入归属条件
    assert perm.merge_owner_condition({'read': ['creator']}, {'roles': ['admin']}, {}) == {}
    assert perm.merge_owner_condition({'read': ['creator']}, {'internal': True}, {'a': 1}) == {'a': 1}
    # seller + 仅 creator 可读 → 注入 createdBy
    out = perm.merge_owner_condition({'read': ['creator']}, {'roles': ['seller'], 'userId': 'u1'}, None)
    assert out == {'createdBy': 'u1'}
    out2 = perm.merge_owner_condition({'read': ['creator']}, {'roles': ['seller'], 'userId': 'u1'}, {'year': 1})
    assert out2 == {'$and': [{'year': 1}, {'createdBy': 'u1'}]}


def test_perm_readable_fields_computes():
    schema = {'fields': {'a': {'type': 'string'}, 'b': {'type': 'string', 'read': ['admin']}},
              'computes': {'c': {'type': 'any', 'read': ['admin']}, 'd': {'type': 'any'}}}
    assert perm.get_readable_fields(schema, None) is None
    f = perm.get_readable_fields(schema, {'roles': ['seller']})
    assert 'a' in f and 'b' not in f
    c = perm.get_readable_computes(schema, {'roles': ['admin']})
    assert 'c' in c and 'd' in c


def test_perm_scoped_roles_and_run_as_internal(monkeypatch):
    perm.set_context(None)
    with perm.scoped_roles(['seller']):
        assert perm.get_context()['roles'] == ['seller']
    assert perm.get_context() is None  # 退出恢复原上下文
    async def coro():
        return perm.get_context().get('internal')
    out = asyncio.run(perm.run_as_internal(coro))
    assert out is True


def test_perm_filter_writable_data():
    schema = {'fields': {'a': {'type': 'string'}, 'b': {'type': 'string', 'write': ['admin']}},
              'read': ['admin'], 'write': ['seller', 'admin']}
    assert perm.filter_writable_data(schema, None, {'a': 1, 'b': 1}) == {'a': 1, 'b': 1}  # 无上下文不过滤
    key_seller = perm.get_writable_fields(schema, {'roles': ['seller']})
    assert 'a' in key_seller and 'b' not in key_seller  # 字段 b 写了 write 且有白名单 → 非白名单拒


# ---------- computes 计算列引擎（纯逻辑） ----------


def test_computes_resolve_default():
    assert comp._resolve_default(5) == 5 and comp._resolve_default('x') == 'x'
    lst = [1, 2]
    assert comp._resolve_default(lst) == [1, 2] and comp._resolve_default(lst) is not lst  # 新实例
    d = {'a': 1}
    out = comp._resolve_default(d)
    assert out == {'a': 1} and out is not d  # 新实例
    assert comp._resolve_default(lambda: 42) == 42  # callable 取调用结果


def test_computes_apply_defaults_and_computes():
    schema = {
        'fields': {
            'unit': {'type': 'string', 'default': '默认'},
            'income': {'type': 'float'},
            'obj': {'type': 'object', 'fields': {'x': {'type': 'int', 'default': 7}}},
        },
        'computes': {
            'total': {'type': 'float', 'fn': lambda r: (r.get('income') or 0) * 2},
        },
    }
    # 已有值不覆盖；fn 计算列生效
    out = comp.apply_defaults_and_computes({'unit': '', 'income': 10}, schema)
    assert out['unit'] == '' and out['total'] == 20
    # None 补零值 + 嵌套 object 子字段默认值
    out2 = comp.apply_defaults_and_computes({'income': None, 'obj': {'x': None}}, schema)
    assert out2['income'] == 0 and out2['obj']['x'] == 7


def test_computes_process_node_defaults_fn_prune():
    schema = {'name': 'T1',
              'fields': {'a': {'type': 'string', 'default': 'd'}, 'b': {'type': 'string'},
                         'income': {'type': 'float'}},
              'computes': {'total': {'type': 'float',
                                     'fn': lambda r: (r.get('income') or 0) * 2,
                                     'depends': ['income']}}}
    ast_node = {'fields': ['a', 'b', 'total'], 'relations': {}}
    doc = {'_id': '1', 'a': None, 'b': 'keep', 'income': 5}
    comp.process_node(doc, ast_node, schema, None)
    assert doc['a'] == 'd' and doc['b'] == 'keep' and doc['total'] == 10
    assert '_id' in doc and 'income' not in doc  # 依赖字段被裁，_id 保留


def test_computes_process_node_dot_prune():
    schema = {'name': 'T2', 'fields': {
        'obj': {'type': 'object', 'fields': {'x': {'type': 'string'}, 'y': {'type': 'string'}}},
    }, 'computes': {}}
    ast_node = {'fields': ['obj.x'], 'relations': {}}
    doc = {'_id': '1', 'obj': {'x': 'vx', 'y': 'vy'}, 'extra': 1}
    comp.process_node(doc, ast_node, schema, None)
    assert doc['obj'] == {'x': 'vx'} and 'extra' not in doc  # 点号子字段裁剪


def test_computes_process_node_nested_relation(monkeypatch):
    child = {'name': 'ChildR', 'fields': {'c': {'type': 'string', 'default': 'cd'}},
             'computes': {}, 'relations': {}}
    parent = {'name': 'ParentR', 'fields': {'p': {'type': 'string'}}, 'computes': {},
              'relations': {'child': {'model': 'ChildR', 'type': 'one'}}}
    monkeypatch.setattr(comp, '_get_schema', lambda n: child if n == 'ChildR' else parent)
    ast_node = {'fields': ['p'], 'relations': {'child': {'fields': ['c'], 'relations': {}}}}
    doc = {'_id': '1', 'p': 'pv', 'child': {'c': None}}
    comp.process_node(doc, ast_node, parent, None)
    assert doc['p'] == 'pv' and doc['child']['c'] == 'cd'  # 递归补默认值


def test_computes_merge_depends_into_ast():
    schema = {'name': 'T3', 'fields': {},
              'relations': {'child': {'model': 'ChildX', 'type': 'one'}},
              'computes': {'agg': {'type': 'any', 'asyncFn': lambda i, ctx: None,
                                   'depends': ['child{c, d}']}}}
    ast = {'fields': ['a'], 'relations': {}}
    inject = comp._merge_depends_into_ast(ast, schema)
    assert set(ast['relations']['child']['fields']) == {'c', 'd'}  # 依赖注入顺序由 set 决定
    assert inject['relations']['child'] == '__all__'


def test_computes_strip_dep_injected():
    items = [{'child': [{'c': 1, 'd': 2}]}, {'child': {'c': 3, 'd': 4}}, {'child': [{'c': 5}]}]
    comp._strip_dep_injected(items, {'relations': {'child': {'c'}}}, None)
    assert items[0]['child'][0] == {'d': 2}   # 列表子文档，c 被裁
    assert items[1]['child'] == {'d': 4}      # 单文档，c 被裁
    # __all__ 整条关系移除
    comp._strip_dep_injected(items, {'relations': {'extra': '__all__'}}, None)
    assert 'extra' not in items[0]


def test_computes_run_async_fns(monkeypatch):
    ran = []
    async def af(items, ctx):
        ran.append(items)
        return None
    schema = {'name': 'T_async', 'fields': {},
              'computes': {'agg': {'type': 'any', 'asyncFn': af}}}
    comp._defaults_cache.clear()
    asyncio.run(comp._run_async_fns([{'x': 1}], schema, None))
    assert ran  # 协程 asyncFn 被执行


def test_computes_run_async_fns_empty_items():
    schema = {'name': 'T_a2', 'fields': {}, 'computes': {}}
    comp._defaults_cache.clear()
    # items 为空 → 直接返回
    asyncio.run(comp._run_async_fns([], schema, None))
    asyncio.run(comp._run_async_fns([{'x': 1}], {'name': 'T_a3', 'fields': {}, 'computes': {}}, None))


def test_computes_run_async_fns_ctx_read_filter(monkeypatch):
    ran = []
    allowed = []
    async def allowed_fn(items, ctx):
        allowed.append(items)
    async def denied_fn(items, ctx):
        ran.append('denied-triggered')  # 不应执行
    schema = {'name': 'T_afilter', 'fields': {},
              'computes': {
                  'a': {'type': 'any', 'asyncFn': allowed_fn, 'read': ['operator']},
                  'b': {'type': 'any', 'asyncFn': denied_fn, 'read': ['hr']},
              }}
    ctx = {'roles': ['operator']}
    comp._defaults_cache.clear()
    asyncio.run(comp._run_async_fns([{'x': 1}], schema, ctx))
    assert allowed and not ran  # 仅 operator 可读的 asyncFn 被执行


def test_computes_collect_rel_deps():
    schema = {'name': 'CR', 'fields': {}, 'relations': {'child': {'model': 'C'}},
              'computes': {'agg': {'type': 'any', 'asyncFn': lambda i, ctx: None,
                                   'depends': ['child{x, y}', 'child', 'norel', '', '_id']}}}
    comp._defaults_cache.clear()
    deps = comp._collect_rel_deps(schema)
    assert 'child' in deps
    # child{x,y} 收集了 {x,y}；裸 child 关系项已存在；norel/空/_id 被跳过


def test_computes_inject_into_ast_merge_and_new():
    ast_merged = {'relations': {'child': {'fields': ['x'], 'relations': {}, 'params': {}}}}
    info = comp._inject_into_ast(ast_merged, {'child': {'x', 'y'}})
    assert set(ast_merged['relations']['child']['fields']) == {'x', 'y'}
    assert set(info['relations']['child']) == {'y'}  # 仅新增字段被记录

    ast_new = {'fields': []}
    info2 = comp._inject_into_ast(ast_new, {'rel2': {'x'}})
    assert ast_new['relations']['rel2']['fields'] == ['x']
    assert info2['relations']['rel2'] == '__all__'


def test_computes_process_node_ctx_permission_prune():
    # ctx 权限裁剪：角色白名单不可读字段被移除，可读字段保留
    schema = {'name': 'PERM', 'fields': {
        'secret': {'type': 'string', 'read': ['admin']},
        'own': {'type': 'string', 'read': ['hr']},
        'pub': {'type': 'string'},
    }, 'computes': {}, 'relations': {}}
    ast_node = {'fields': ['secret', 'own', 'pub'], 'relations': {}}
    d2 = {'_id': '1', 'secret': 's', 'own': 'o', 'pub': 'p'}
    comp.process_node(d2, ast_node, schema, {'roles': ['user'], 'userId': 'x'})
    assert 'pub' in d2  # 无 read → 默认可读
    assert 'secret' not in d2 and 'own' not in d2  # 角色白名单不匹配 → 移除


# ---------- crud.query 分支测试（find 优化 / 两阶段 / 标准批次） ----------


class _FakeAggregateResult:
    def __init__(self, docs):
        self.docs = docs

    async def to_list(self, length=None):
        return self.docs


class _FakeFindCursor:
    def __init__(self, docs):
        self.docs = docs

    async def to_list(self, length=None):
        return self.docs


class _MemCursor:
    def __init__(self, docs):
        self.docs = docs

    async def to_list(self, length=None):
        return list(self.docs)


class _MemColl:
    """内存版 collection：覆盖 crud 写路径所需全部方法"""

    def __init__(self, docs=None):
        self.docs = docs if docs is not None else []

    def find(self, query=None, projection=None):
        return _MemCursor(self.docs)

    async def aggregate(self, pipeline, **kwargs):
        return _MemCursor(self.docs)

    async def find_one(self, query=None, projection=None):
        return dict(self.docs[0]) if self.docs else None

    async def count_documents(self, filter=None):
        return len(self.docs)

    async def insert_one(self, doc):
        self.docs.append(doc)
        return None

    async def insert_many(self, docs):
        self.docs.extend(docs)
        return None

    async def find_one_and_update(self, condition, update, return_document=None, **kwargs):
        base = dict(self.docs[0]) if self.docs else {}
        for st in update.values():
            if isinstance(st, dict):
                base.update(st)
        if kwargs.get('upsert') and not self.docs:
            self.docs.append(base)
        return base

    async def update_many(self, condition, data):
        return SimpleNamespace(modified_count=len(self.docs))

    async def delete_many(self, condition):
        n = len(self.docs)
        self.docs = []
        return SimpleNamespace(deleted_count=n)


class _FakeColl:
    def __init__(self, docs):
        self.docs = docs

    def find(self, query=None, projection=None):
        # 纯 find 优化分支：仅 $match
        return _FakeFindCursor(self.docs)

    async def aggregate(self, pipeline, **kwargs):
        return _FakeAggregateResult(self.docs)

    async def count_documents(self, filter=None):
        return len(self.docs)


class _FakeDb:
    """按 collection 名分配独立 coll；未知名自动建空 _MemColl（供 query_one 空结果等）"""

    def __init__(self, coll=None):
        self._colls = {}
        if coll is not None:
            self._colls['commercial_ledger'] = coll

    def __getitem__(self, name):
        if name not in self._colls:
            self._colls[name] = _MemColl()
        return self._colls[name]


def _crud_mock(monkeypatch):
    """monkeypatch crud._db + 空权限 ctx，返回 fake coll"""
    docs = [{'unit': 'a', 'income': 100.0, '_id': '1'}]
    coll = _FakeColl(docs)
    monkeypatch.setattr(_crud_mod, '_db', _FakeDb(coll))
    monkeypatch.setattr(_crud_mod, 'get_context', lambda: None)
    return coll, docs


def _crud_w_mock(monkeypatch, docs=None):
    """写路径 mock：ctx=None + 内存 coll + CommercialLedger schema"""
    coll = _MemColl(docs)
    monkeypatch.setattr(_crud_mod, '_db', _FakeDb(coll))
    monkeypatch.setattr(_crud_mod, 'get_context', lambda: None)
    return coll


def test_crud_query_plain_match(monkeypatch):
    """纯 $match 无关联 → 走 find 快路径，返回裁剪后文档"""
    _crud_mock(monkeypatch)
    gql = 'CommercialLedger{unit, income}'
    items = asyncio.run(_crud_mod.query(gql))
    assert isinstance(items, list)
    assert items[0]['unit'] == 'a'


def test_crud_query_aggregate_standard(monkeypatch):
    """无 $lookup、无 skip/limit → 标准聚合批次"""
    _crud_mock(monkeypatch)
    gql = 'CommercialLedger{unit, income}'
    items = asyncio.run(_crud_mod.query(gql))
    assert items  # fake 返回的文档被 process_node 裁剪后保留真实字段
    assert items[0]['unit'] == 'a'


def test_crud_query_with_relation_relation_sort_two_phase(monkeypatch):
    """带 $lookup + sort 引用关系字段 → 退化到标准批次（_run_standard）"""
    _crud_mock(monkeypatch)
    gql = 'CommercialLedger{unit}'
    items = asyncio.run(_crud_mod.query(gql))
    assert items and items[0]['unit'] == 'a'


def test_crud_query_two_phase_ids(monkeypatch):
    """$lookup + {skip,limit} → 两阶段取值路径（复用 build_lookup 深度保护降级为空 lookup）"""
    _crud_mock(monkeypatch)
    gql = 'CommercialLedger{unit}'
    items = asyncio.run(_crud_mod.query(gql))
    assert items and items[0]['unit'] == 'a'


def test_crud_number_parse():
    assert _crud_mod.Number(42) == 42
    assert _crud_mod.Number('3.9') == 3.9  # 字符串可转 int 失败 → 回退 float
    assert _crud_mod.Number(3.9) == 3  # 浮点 → int 截断
    assert _crud_mod.Number('abc') == 0  # 不可转换 → 0


def test_crud_to_base36():
    assert _crud_mod._to_base36(0) == '0'
    assert _crud_mod._to_base36(1) == '1'
    assert _crud_mod._to_base36(35) == 'z'
    assert _crud_mod._to_base36(36) == '10'


def test_crud_remove_undefined():
    d = {'a': 1, 'b': None, 'c': 0}
    _crud_mod._remove_undefined(d)
    assert d == {'a': 1, 'c': 0}  # None 剔除，0 保留


def test_crud_generate_id_has_prefix():
    _id = _crud_mod._generate_id({'idPrefix': 'T'})
    assert _id.startswith('T') and len(_id) > 1


def test_crud_has_creator_permission():
    assert _crud_mod._has_creator_permission({'read': ['creator']}) is True
    assert _crud_mod._has_creator_permission({'write': ['user']}) is False
    assert _crud_mod._has_creator_permission({}) is False


def test_crud_query_one(monkeypatch):
    _crud_mock(monkeypatch)
    one = asyncio.run(_crud_mod.query_one('CommercialLedger{unit, income}'))
    assert one and one['unit'] == 'a'
    assert asyncio.run(_crud_mod.query_one('GoalLedger{income}')) is None  # 无结果 → None


def test_crud_query_with_count_page(monkeypatch):
    coll, _ = _crud_mock(monkeypatch)
    async def fake_count(*a, **k):
        return 200
    monkeypatch.setattr(coll, 'count_documents', fake_count)
    r = asyncio.run(_crud_mod.query_with_count('CommercialLedger($skip:@s,$limit:@l){unit, income}',
                                               {'s': 0, 'l': 50}))
    assert r['total'] == 200 and r['pageSize'] == 50 and r['page'] == 0


def test_crud_query_with_count_pagesize_cap(monkeypatch):
    _crud_mock(monkeypatch)
    r = asyncio.run(_crud_mod.query_with_count('CommercialLedger{unit}',
                                               {'pageSize': 99999, 'page': 0}))
    assert r['pageSize'] == 5000  # 上限 5000 防拖库


def test_crud_insert_autoid_timestamp(monkeypatch):
    coll = _crud_w_mock(monkeypatch)
    doc = asyncio.run(_crud_mod.insert('CommercialLedger', {'unit': 'x', 'income': 9.0}))
    assert doc['_id'].startswith('CL')  # idPrefix 自动生成
    assert doc['createdAt'] and doc['updatedAt']  # 时间戳补默认
    assert coll.docs[0]['unit'] == 'x'


def test_crud_insert_many_empty_returns_empty(monkeypatch):
    _crud_w_mock(monkeypatch)
    assert asyncio.run(_crud_mod.insert_many('CommercialLedger', [])) == []


def test_crud_insert_many_fills_ids(monkeypatch):
    coll = _crud_w_mock(monkeypatch)
    out = asyncio.run(_crud_mod.insert_many('CommercialLedger', [{'unit': 'a'}, {'unit': 'b'}]))
    assert len(out) == 2 and all(d['_id'].startswith('CL') for d in out)
    assert len(coll.docs) == 2


def test_crud_update_set_mode(monkeypatch):
    _crud_w_mock(monkeypatch, [{'_id': '1', 'unit': 'a', 'income': 100.0, 'createdAt': 1, 'updatedAt': 1}])
    out = asyncio.run(_crud_mod.update('CommercialLedger', {'_id': '1'}, {'income': 200.0}))
    assert out and out['income'] == 200.0
    assert out['updatedAt']  # $set 模式自动刷 updatedAt


def test_crud_update_raw_operators(monkeypatch):
    _crud_w_mock(monkeypatch, [{'_id': '1', 'income': 100.0, 'createdAt': 1, 'updatedAt': 1}])
    out = asyncio.run(_crud_mod.update('CommercialLedger', {'_id': '1'}, {'$inc': {'income': 5}}))
    assert out is not None
    # 原生 $inc 透传，不触发 $set 字段校验


def test_crud_update_empty_set_raises(monkeypatch):
    _crud_w_mock(monkeypatch, [{'_id': '1', 'unit': 'a'}])
    try:
        asyncio.run(_crud_mod.update('CommercialLedger', {'_id': '1'}, {'_id': '1'}))
        assert False, '应抛 ValueError'
    except ValueError:
        pass


def test_crud_update_many_raw_and_set(monkeypatch):
    coll = _crud_w_mock(monkeypatch, [{'income': 1.0}])
    r1 = asyncio.run(_crud_mod.update_many('CommercialLedger', {}, {'$inc': {'income': 1}}))
    assert r1['modifiedCount'] == 1
    r2 = asyncio.run(_crud_mod.update_many('CommercialLedger', {}, {'income': 2.0}))
    assert r2['modifiedCount'] == 1


def test_crud_remove_archives_when_schema_exists(monkeypatch):
    # 注册 CommercialLedgerDeleted 让归档分支命中；删除原表
    _sc.register({'name': 'CommercialLedgerDeleted',
                  'collection': 'commercial_ledger_deleted',
                  'idPrefix': 'CLD', 'timestamps': True,
                  'fields': {'unit': 'string'}, 'relations': {}, 'read': None, 'write': None})
    coll = _crud_w_mock(monkeypatch, [{'_id': '1', 'unit': 'a', 'income': 1.0}])
    r = asyncio.run(_crud_mod.remove('CommercialLedger', {'_id': '1'}))
    assert r['deletedCount'] == 1 and r['archivedCount'] == 1  # 归档一条 + 物理删除一条
    coll.docs = [{'_id': '2', 'unit': 'b'}]
    r2 = asyncio.run(_crud_mod.remove('CommercialLedger', {'_id': '2'}))
    assert r2['archivedCount'] == 1


def test_crud_exists_and_count(monkeypatch):
    _crud_w_mock(monkeypatch, [{'_id': '1'}])
    assert asyncio.run(_crud_mod.exists('CommercialLedger', {'_id': '1'})) is True
    assert asyncio.run(_crud_mod.count('CommercialLedger', {'_id': '1'})) == 1


def test_crud_build_upsert_conditions(monkeypatch):
    schema = {'indexes': [
        {'keys': {'year': 1}, 'options': {'unique': True}},
        {'keys': {'industry': 1}, 'options': {}},  # 非 unique → 跳过
    ]}
    conds = _crud_mod._build_upsert_conditions(schema, {'_id': 'k1', 'year': 2026})
    assert {'_id': 'k1'} in conds
    assert {'year': 2026} in conds
    empties = _crud_mod._build_upsert_conditions(schema, {'_id': '', 'year': ''})
    assert empties == []  # 空字符串不构成条件


def test_crud_upsert_generates_id(monkeypatch):
    coll = _crud_w_mock(monkeypatch)
    out = asyncio.run(_crud_mod.upsert('CommercialLedger', {'_id': 'u1'}, {'unit': 'upserted'}))
    assert out and out['unit'] == 'upserted'


def test_crud_upsert_generates_new_id(monkeypatch):
    _crud_w_mock(monkeypatch)
    out = asyncio.run(_crud_mod.upsert('CommercialLedger', {'year': 2026}, {'unit': 'x'}))
    assert out and out['_id'].startswith('CL')


def test_crud_mutation_single_and_array(monkeypatch):
    coll = _crud_w_mock(monkeypatch)
    single = asyncio.run(_crud_mod.mutation('CommercialLedger', {'year': 2026, 'unit': 'solo'}))
    assert single and single['_id'].startswith('CL')
    arr = asyncio.run(_crud_mod.mutation('CommercialLedger', [{'unit': 'a'}, {'unit': 'b'}]))
    assert isinstance(arr, list) and len(arr) == 2


def test_crud_mutation_empty_array_returns_none(monkeypatch):
    _crud_w_mock(monkeypatch)
    assert asyncio.run(_crud_mod.mutation('CommercialLedger', [])) == []


def test_crud_aggregate(monkeypatch):
    _crud_w_mock(monkeypatch, [{'unit': 'a'}])
    out = asyncio.run(_crud_mod.aggregate('CommercialLedger', [{'$match': {'unit': 'a'}}]))
    assert out and out[0]['unit'] == 'a'
