"""触发链执行器单元测试（crud/triggers.py，与 nodejs-store/src/crud/triggers.js 同构）。

覆盖基线 D8 点名的低覆盖分支：回调注册契约校验、fnRef 覆盖校验、占位符深替换
（含内嵌拼接显式报错）、when 文法全算子、命中判定（onFields/no-op 抑制）、
执行链去重与 callback 缺实现显式报错。纯内存，不依赖真库（真库 e2e 见 test_triggers.py）。
"""

import asyncio

import pytest

from py_store.crud import triggers as T


def _run(coro):
    return asyncio.run(coro)


# ---------- set_trigger_fn 契约 ----------


def test_set_trigger_fn_rejects_bad_ref():
    with pytest.raises(ValueError, match="ERR_TRIGGER_FN_REF"):
        T.set_trigger_fn("", lambda *a: None)
    with pytest.raises(ValueError, match="ERR_TRIGGER_FN_REF"):
        T.set_trigger_fn(None, lambda *a: None)


def test_set_trigger_fn_rejects_non_callable():
    with pytest.raises(ValueError, match="ERR_TRIGGER_FN_IMPL"):
        T.set_trigger_fn("x.noimpl", "not-callable")


# ---------- assert_trigger_fns_covered ----------


def test_assert_fns_covered_missing_raises():
    defns = [{"triggers": {"insert": [{"fnRef": "mod.gone"}]}}]
    with pytest.raises(RuntimeError, match="ERR_TRIGGER_FN_MISSING.*mod\\.gone"):
        T.assert_trigger_fns_covered(defns)


def test_assert_fns_covered_ok_and_skips_non_dict():
    T.set_trigger_fn("mod.here", lambda *a: None)
    T._fn_refs.discard("mod.gone")  # 防其他用例残留
    defns = [
        {
            "triggers": {
                "insert": [{"fnRef": "mod.here"}, "not-a-dict"],
            }
        }
    ]
    T.assert_trigger_fns_covered(defns)
    T._trigger_fns.pop("mod.here", None)
    T._fn_refs.discard("mod.here")


# ---------- 占位符深替换 ----------


_SCOPE = {"root": {"_id": "o1", "amount": 7, "nested": {"k": "v"}}, "before": None}


def test_resolve_placeholders_now_root_before():
    scope = {"root": {"_id": "o1"}, "before": {"amount": 5}, "now": 123}
    assert T.resolve_trigger_placeholders("{{now}}", scope) == 123
    assert T.resolve_trigger_placeholders("{{root._id}}", scope) == "o1"
    assert T.resolve_trigger_placeholders("{{before.amount}}", scope) == 5


def test_resolve_placeholders_missing_digs_to_none():
    # 点路径缺失 → None（不抛）；嵌套路径同理；中间层为 None / 非 dict 同样 None
    assert T.resolve_trigger_placeholders("{{root.absent}}", _SCOPE) is None
    assert T.resolve_trigger_placeholders(
        "{{root.absent.deeper}}", _SCOPE
    ) is None  # 中间层缺失（_dig cur=None 短路）
    assert T.resolve_trigger_placeholders(
        "{{root.nested.deep.absent}}", _SCOPE
    ) is None
    assert T.resolve_trigger_placeholders(
        "{{root.nested.k.x}}", _SCOPE
    ) is None  # 中间层为标量（非 dict，_dig isinstance 短路）
    assert T._dig(None, "any.path") is None  # 整个对象为 None


def test_resolve_placeholders_embedded_concat_raises():
    for v in ("order-{{root._id}}", "{{root._id}}-tail", "x{{now}}"):
        with pytest.raises(ValueError, match="ERR_TRIGGER_PLACEHOLDER"):
            T.resolve_trigger_placeholders(v, _SCOPE)


def test_resolve_placeholders_recurses_lists_dicts_and_scalars():
    scope = {"root": {"n": 1}, "before": {}, "now": 9}
    out = T.resolve_trigger_placeholders(
        {"a": ["{{root.n}}", 2, None], "b": {"c": "{{now}}"}, "d": "plain", "e": 0.5},
        scope,
    )
    assert out == {"a": [1, 2, None], "b": {"c": 9}, "d": "plain", "e": 0.5}


# ---------- when 文法 ----------


def test_eval_when_none_is_true():
    assert T.eval_when(None, _SCOPE) is True


def test_eval_when_comparison_ops():
    scope = {"root": {"n": 3, "s": "b", "list": [1, 2]}, "before": {}, "now": 0}
    assert T.eval_when({"eq": ["{{root.n}}", 3]}, scope)
    assert T.eval_when({"ne": ["{{root.n}}", 4]}, scope)
    assert T.eval_when({"gt": ["{{root.n}}", 2]}, scope)
    assert T.eval_when({"gte": ["{{root.n}}", 3]}, scope)
    assert T.eval_when({"lt": ["{{root.n}}", 4]}, scope)
    assert T.eval_when({"lte": ["{{root.n}}", 3]}, scope)
    assert T.eval_when({"in": [1, "{{root.list}}"]}, scope)
    assert not T.eval_when({"in": [9, "{{root.list}}"]}, scope)


def test_eval_when_logic_ops():
    scope = {"root": {"n": 3}, "before": {}, "now": 0}
    assert T.eval_when({"and": [{"eq": ["{{root.n}}", 3]}]}, scope)
    assert T.eval_when({"and": []}, scope)  # all([]) = True（空合取为真）
    assert not T.eval_when({"or": []}, scope)  # any([]) = False（空析取为假）
    assert T.eval_when({"or": [{"eq": [1, 2]}, {"eq": [3, 3]}]}, scope)
    assert not T.eval_when({"not": {"eq": ["{{root.n}}", 3]}}, scope)


def test_eval_when_unknown_op_and_multi_key_raise():
    with pytest.raises(ValueError, match="未知算子 regex"):
        T.eval_when({"regex": ["a", "b"]}, _SCOPE)
    with pytest.raises(ValueError, match="必须且只能有一个算子键"):
        T.eval_when({"eq": [1, 1], "ne": [1, 2]}, _SCOPE)


# ---------- _deep_eq / 命中判定 ----------


def test_deep_eq_cases():
    same = {"a": 1}
    assert T._deep_eq(same, same)  # 同引用
    assert T._deep_eq(None, None)
    assert not T._deep_eq({"a": 1}, None)
    assert T._deep_eq({"a": 1, "b": 2}, {"b": 2, "a": 1})  # 键序无关
    assert not T._deep_eq({"a": {"x": 1}}, {"a": {"x": 2}})  # 嵌套不等
    assert not T._deep_eq({"a": 1}, "not-dict")  # dict vs 标量（isinstance 短路分支）
    assert not T._deep_eq({"a": 1}, {"b": 1})  # 顶层键集合不等（键集合比较分支）


def test_hit_trigger_onfields_suppresses_noop():
    step = {"name": "s", "onFields": ["status"]}
    scope = {"root": {}, "before": {}, "now": 0}
    before = {"status": "open"}
    after_same = {"status": "open"}
    after_changed = {"status": "paid"}
    assert T.hit_trigger(step, before=before, after=after_same, scope=scope) is False
    assert T.hit_trigger(step, before=before, after=after_changed, scope=scope) is True


def test_hit_trigger_insert_skips_field_check():
    # insert（before=None）：跳过 onFields 字段级检查，直接 when
    step = {"name": "s", "onFields": ["status"]}
    assert T.hit_trigger(step, before=None, after={"status": "x"}, scope={}) is True


def test_hit_trigger_record_level_when_no_onfields():
    step = {"name": "s"}
    scope = {"root": {"n": 1}, "before": {}, "now": 0}
    assert T.hit_trigger(step, before={}, after={}, scope=scope) is True


# ---------- run_triggers 执行链 ----------


def _mk_step(name, on_fields=None, command=None, callback=None):
    return {
        "name": name,
        "onFields": on_fields or [],
        **({"command": command} if command else {}),
        **({"callback": callback} if callback else {}),
    }


def test_run_triggers_dedup_within_top_level_call():
    executed = set()
    called = []

    async def _cmd(command):  # 命令式执行走 _exec —— 经 monkeypatch 记录
        called.append(command)

    orig_exec = T._exec
    T._exec = _cmd
    try:
        step = _mk_step("s1", command={"kind": "audit"})
        root = {"_id": "a"}
        _run(T.run_triggers([step], root=root, before=None, now=0, ctx=None,
                            executed=executed))
        # 同一顶层调用内重复出现：去重跳过
        _run(T.run_triggers([step], root=root, before=None, now=0, ctx=None,
                            executed=executed))
    finally:
        T._exec = orig_exec
    assert len(called) == 1
    assert executed == {"s1#a"}


def test_run_triggers_callback_missing_impl_raises():
    ref = "mod.not_registered_probe"
    T._fn_refs.discard(ref)
    T._trigger_fns.pop(ref, None)
    step = _mk_step("s2", callback={"fnRef": ref, "args": {}})
    with pytest.raises(RuntimeError, match="ERR_TRIGGER_FN_MISSING"):
        _run(T.run_triggers([step], root={"_id": "x"}, before=None, now=0,
                            ctx=None, executed=set()))


def test_run_triggers_callback_invoked_with_host_store():
    calls = []

    async def impl(args, ctx, host):
        calls.append((args, ctx, host))

    ref = "probe.cb"
    T._trigger_fns[ref] = impl
    try:
        step = _mk_step(
            "s3", callback={"fnRef": ref, "args": {"oid": "{{root._id}}"}}
        )
        root = {"_id": "o9"}
        _run(T.run_triggers([step], root=root, before=None, now=0, ctx={"u": 1},
                            executed=set()))
        assert calls == [({"oid": "o9"}, {"u": 1}, {"store": T._store})]
    finally:
        T._trigger_fns.pop(ref, None)


def test_run_triggers_skips_missed_when():
    called = []

    async def _cmd(command):
        called.append(command)

    orig_exec = T._exec
    T._exec = _cmd
    try:
        step = {
            "name": "s4",
            "onFields": [],
            "when": {"eq": ["{{root.n}}", 999]},
            "command": {"kind": "audit"},
        }
        _run(T.run_triggers([step], root={"_id": "z", "n": 1}, before=None,
                            now=0, ctx=None, executed=set()))
    finally:
        T._exec = orig_exec
    assert called == []
