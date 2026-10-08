"""触发链执行器（Host 侧）：占位符替换 → 命中判定 → 依序执行（命令式 / 回调式）。

边界：core 已产出 steps（含占位符、onFields、when）；本模块只做**运行时事实判定**
（before/after 实际值）与执行，落在调用方的 ``run_atomic`` 作用域内 → 单源同事务。
去重：同一次顶层调用内 ``(step.name, _id)`` 只执行一次（防重复触发）。
不做级联：触发写不再触发任何触发器（首批契约，见总纲）。

对齐 ``nodejs-store/src/crud/triggers.js``（同语义蛇形实现）。
"""

import re
from typing import Set

from .exec import _exec

_store = None                 # 装配期由 py_store.__init__ 注入（避免循环导入）
_trigger_fns: dict = {}
_fn_refs: Set[str] = set()


def set_store(s):
    """装配门面（``py_store.__init__`` 在 ``store = Store()`` 后调用一次）"""
    global _store
    _store = s


def set_trigger_fn(fn_ref, impl):
    """回调注入：``fn_ref → impl(args, ctx, {'store': store})``（可为 async）"""
    if not isinstance(fn_ref, str) or not fn_ref:
        raise ValueError('ERR_TRIGGER_FN_REF:fnRef 须为非空字符串')
    if not callable(impl):
        raise ValueError('ERR_TRIGGER_FN_IMPL:impl 须为函数')
    _trigger_fns[fn_ref] = impl
    _fn_refs.add(fn_ref)


def assert_trigger_fns_covered(defns):
    """启动期校验：schema 的 triggers 里声明的 fnRef 必须都有实现（缺则显式抛错，不静默）。

    注意：声明形状的 fnRef 在顶层（规划展开后才包成 ``callback:{fnRef,args}``），
    见 core ``schema/triggers.rs``。
    """
    missing = []
    for defn in defns or ():
        for trigger_list in ((defn or {}).get('triggers') or {}).values():
            for t in trigger_list or ():
                ref = t.get('fnRef') if isinstance(t, dict) else None
                if isinstance(ref, str) and ref and ref not in _fn_refs:
                    missing.append(ref)
    if missing:
        raise RuntimeError(f'ERR_TRIGGER_FN_MISSING:未注入触发器回调实现 {", ".join(missing)}')


# ─── 占位符（整值替换；禁内嵌拼接） ───────────────────────────

_ROOT_PH = re.compile(r'^\{\{root\.([A-Za-z0-9_.]+)\}\}$')
_BEFORE_PH = re.compile(r'^\{\{before\.([A-Za-z0-9_.]+)\}\}$')
_NOW_PH = re.compile(r'^\{\{now\}\}$')


def _dig(obj, path):
    """点路径取值；缺失 → None（不抛）"""
    if obj is None:
        return None
    cur = obj
    for seg in str(path).split('.'):
        if cur is None:
            return None
        if not isinstance(cur, dict):
            return None
        cur = cur.get(seg)
    return cur


def _deep_eq(a, b):
    """深比较（结构相等；顺序无关的键集合比较）"""
    if a is b:
        return True
    if a is None or b is None or not isinstance(a, dict) or not isinstance(b, dict):
        return a == b
    if list(a.keys()) != list(b.keys()) and set(a.keys()) != set(b.keys()):
        return False
    return all(_deep_eq(a[k], b[k]) for k in a)


def resolve_trigger_placeholders(value, scope):
    """深替换触发器步骤中的占位符。

    未命中的占位符**显式报错**（``ERR_TRIGGER_PLACEHOLDER``）——占位符必须独占字符串值，
    不支持 ``"order-{{root._id}}"`` 这类内嵌拼接（禁静默漂移）。
    """
    if isinstance(value, str):
        if _NOW_PH.match(value):
            return scope['now']
        m = _ROOT_PH.match(value)
        if m:
            return _dig(scope['root'], m.group(1))
        m = _BEFORE_PH.match(value)
        if m:
            return _dig(scope['before'], m.group(1))
        if '{{root.' in value or '{{before.' in value or '{{now}}' in value:
            raise ValueError(
                f'ERR_TRIGGER_PLACEHOLDER:占位符必须独占字符串值（不支持内嵌拼接）：{value}')
        return value
    if isinstance(value, list):
        return [resolve_trigger_placeholders(v, scope) for v in value]
    if isinstance(value, dict):
        return {k: resolve_trigger_placeholders(v, scope) for k, v in value.items()}
    return value


# ─── when 求值（极简文法；未知算子显式 Err） ──────────────────

_WHEN_OPS = frozenset({'eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'in', 'and', 'or', 'not'})


def eval_when(when, scope):
    if when is None:
        return True
    keys = list(when.keys())
    if len(keys) != 1:
        raise ValueError('ERR_TRIGGER_WHEN:when 必须且只能有一个算子键')
    op = keys[0]
    if op not in _WHEN_OPS:
        raise ValueError(f'ERR_TRIGGER_WHEN:未知算子 {op}')
    arg = when[op]

    def rv(v):
        return resolve_trigger_placeholders(v, scope)

    if op == 'eq':
        return rv(arg[0]) == rv(arg[1])
    if op == 'ne':
        return rv(arg[0]) != rv(arg[1])
    if op == 'gt':
        return rv(arg[0]) > rv(arg[1])
    if op == 'gte':
        return rv(arg[0]) >= rv(arg[1])
    if op == 'lt':
        return rv(arg[0]) < rv(arg[1])
    if op == 'lte':
        return rv(arg[0]) <= rv(arg[1])
    if op == 'in':
        return rv(arg[0]) in (rv(arg[1]) or [])
    if op == 'and':
        return all(eval_when(w, scope) for w in (arg or []))
    if op == 'or':
        return any(eval_when(w, scope) for w in (arg or []))
    return not eval_when(arg, scope)  # not


# ─── 命中判定 + 执行器 ───────────────────────────────────────

def hit_trigger(step, *, before, after, scope):
    """命中判定：``onFields 值真的变化`` → ``when 成立``。

    ``onFields`` 为空（记录级）或事件为 insert（无 before）时跳过字段级检查。
    """
    on = step.get('onFields') or []
    if on and before is not None:
        changed = any(not _deep_eq(_dig(before, f), _dig(after, f)) for f in on)   # no-op 抑制
        if not changed:
            return False
    return eval_when(step.get('when'), scope)


async def run_triggers(steps, *, root, before, now, ctx, executed):
    """依序执行触发链。

    ``steps`` 为 core 产出的 triggers 数组；``executed`` 为本次顶层调用的去重集合
    （键 ``name#_id``）；``_store`` 为门面，回调经 ``host['store']`` 消费。
    """
    for step in steps or ():
        rid = _dig(root, '_id')
        key = f"{step['name']}#{'' if rid is None else rid}"
        if key in executed:
            continue                                        # 同事务去重
        scope = {'root': root, 'before': before, 'now': now}
        if not hit_trigger(step, before=before, after=root, scope=scope):
            continue
        executed.add(key)
        if step.get('command'):
            await _exec(resolve_trigger_placeholders(step['command'], scope))
        elif step.get('callback'):
            fn_ref = step['callback'].get('fnRef')
            impl = _trigger_fns.get(fn_ref)
            if impl is None:
                raise RuntimeError(f'ERR_TRIGGER_FN_MISSING:未注入触发器回调 {fn_ref}')
            args = resolve_trigger_placeholders(step['callback'].get('args'), scope)
            # 回调内 store.* 经 resolve_connection 落到当前事务连接 → 与主写同事务
            await impl(args, ctx, {'store': _store})
