"""读路径 —— find 快路径 / 两阶段（取 ID → 关联 → 还原排序）/ 标准聚合 + asyncFn 尾处理
/ 跨库联邦（逐源执行 → 内存 hash join）"""

import inspect

from ..feedback import emit as _emit_feedback
from ..schema import core as _core, get_async_fn
from .exec import _call, _ctx, _exec, _exec_on, resolve_placeholders


async def _run_query_plan(plan):
    """执行读命令序列"""
    if plan['mode'] == 'two_phase':
        id_docs = await _exec(plan['commands'][0])
        ids = [d['_id'] for d in id_docs]
        if not ids:
            return []
        cmd2 = resolve_placeholders(plan['commands'][1], ids=ids)
        items = await _exec(cmd2)
        return _core.restore_sort_order(items, ids, plan.get('sort'))['items']
    return await _exec(plan['commands'][0])


async def _finalize(plan, items):
    """读路径尾处理两段式：core 后处理 → Host 执行 asyncFn → core 剥离注入依赖"""
    post = plan.get('postprocess')
    if not post:
        return items
    prepared = _core.prepare_query(post, items, _ctx())
    for ref in prepared['fnRefs']:
        fn = get_async_fn(ref)
        if not fn:
            raise RuntimeError(f'asyncFn 计算列 {ref} 未注册实现')
        out = fn(prepared['items'], _ctx())
        if inspect.isawaitable(out):
            await out
    return _core.strip_query(post, prepared['items'])['items']


async def query(gql, params=None):
    """
    GQL 查询（返回数组）

    支持的 params 键（通过 GQL 的 @key 引用）:
      $condition / $sort / $skip / $limit / $pipeline
    使用 $pipeline 时，框架不追加 compute 层、不补默认值、不裁剪，完全由用户控制。
    """
    plan = _call(lambda: _core.plan_query(
        gql, params if params is not None else {}, _ctx()))
    return await _finalize(plan, await _run_query_plan(plan))


async def query_one(gql, params=None):
    """GQL 查询（返回单条）"""
    items = await query(gql, params)
    return items[0] if items else None


async def _run_federated_unit(unit):
    """执行单个联邦取数单元（按 ``sources[].source`` 精确路由；two_phase 走两阶段）

    与单库 ``_run_query_plan`` 同形：只差路由键（单元自带 source，不按 collection 反查）。
    """
    commands = unit.get('commands') or []
    if unit.get('mode') == 'two_phase':
        id_docs = await _exec_on(unit['source'], commands[0])
        ids = [d['_id'] for d in id_docs]
        if not ids:
            return []
        cmd2 = resolve_placeholders(commands[1], ids=ids)
        items = await _exec_on(unit['source'], cmd2)
        return _core.restore_sort_order(items, ids, unit.get('sort'))['items']
    return await _exec_on(unit['source'], commands[0])


async def query_federated(gql, params=None):
    """
    跨库联邦查询（返回嵌套文档数组）

    Host 四步：core ``plan_federated`` 拆源 → 逐源执行 → core ``merge_federated``
    内存 hash join → 统一后处理（``_finalize``，与单库同一路径）。

    ``postprocess`` 取自根单元快照（含全部关系），因此结果形状与单库 ``query`` 完全一致。
    每源取数上限 ``MAX_FEDERATION_ROWS`` 由 core 强制（超限即报错，拒绝静默全表拉取）；
    无法下推的分页/排序进 ``plan['degraded']`` 并告警，不阻断查询。
    """
    plan = _call(lambda: _core.plan_federated(
        gql, params if params is not None else {}, _ctx()))

    for d in plan.get('degraded') or []:
        # 降级事件走统一反馈通道（无 sink 时打 stderr，允许拦截，禁止静默失守）
        _emit_feedback(dict(d or {}, type='federation_degraded'))

    results = []
    for unit in plan.get('sources') or []:
        results.append(await _run_federated_unit(unit))

    merged = _call(lambda: _core.merge_federated(plan, results))
    return await _finalize(plan, merged)


async def query_with_count(gql, params=None):
    """
    GQL 查询（返回 items + total + 分页元数据）

    支持两种分页参数方式：
      1. page/pageSize（推荐）— 自动计算 skip/limit，page 默认 0，pageSize 默认 50
      2. 传统 $skip/$limit — 从 GQL 参数推导 page/pageSize
    pageSize 上限 5000，防止拖库。
    """
    plan = _call(lambda: _core.plan_query_with_count(
        gql, params if params is not None else {}, _ctx(), None))
    items = await _finalize(plan, await _run_query_plan(plan))
    total = await _exec(plan['countCommand'])
    return {
        'items': items,
        'total': total,
        'hasMore': (plan['page'] + 1) * plan['pageSize'] < total,
        'page': plan['page'],
        'pageSize': plan['pageSize'],
    }
