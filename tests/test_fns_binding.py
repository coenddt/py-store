"""L2 计算列包绑定契约（共享分层与多落点择优 05 / A6）—— Python 侧，与 Node 同构。

覆盖：
  1. A6：schema 逻辑 fn_ref="Order.total" 与 Py 实现 order_total 归一匹配；
  2. A6 默认复合名：无 fnRef 时逻辑 ref = <name>.<key>；
  3. 缺失实现 ⇒ ERR_FN_MISSING（不静默丢列）；
  4. 归一后实现名重复 ⇒ ERR_FN_CONFLICT（不静默覆盖）。

依赖 core 透出的 ``native.canonical``（02 落地）——须以 ``LOCAL_CORE=1`` 从相邻
``rust-store/core-py/dist`` 加载调试产物运行（与其余宿主用例同口径）。
JS 侧对拍：``nodejs-store/tests/fns-binding.test.js``。
"""

import pytest

from py_store import schema as _sc


def test_a6_explicit_fnref_matches_snake_impl():
    _sc.set_fn('order_total', lambda it, ctx=None: it['a'] + it['b'])
    defn = {
        'name': 'Order',
        'collection': 'order',
        'fields': {'_id': {'type': 'string'}, 'a': {'type': 'int'}, 'b': {'type': 'int'}},
        'computes': {'total': {'type': 'int', 'fn': True, 'fnRef': 'Order.total', 'depends': ['a', 'b']}},
    }
    _sc.assert_fns_covered([defn])   # 不抛即通过


def test_a6_default_composite_ref():
    _sc.set_fn('order_total', lambda it, ctx=None: it['a'] + it['b'])
    _sc.assert_fns_covered([
        {'name': 'Order', 'computes': {'total': {'type': 'int', 'fn': True}}},
    ])


def test_missing_impl_raises():
    with pytest.raises(RuntimeError, match='ERR_FN_MISSING'):
        _sc.assert_fns_covered([
            {'name': 'Nope', 'computes': {'zzz': {'type': 'int', 'fn': True}}},   # 逻辑 ref Nope.zzz 无实现
        ])


def test_normalized_conflict_raises():
    with pytest.raises(ValueError, match='ERR_FN_CONFLICT'):
        _sc.set_fn('order_total', lambda it, ctx=None: 1)
        _sc.set_fn('orderTotal', lambda it, ctx=None: 2)
