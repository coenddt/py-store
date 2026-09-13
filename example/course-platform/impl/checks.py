"""expect.kind = 'raw' 的自定义断言函数。

签名统一 ``async def fn(h, expect) -> (bool, str)``，h 提供：
  - h.store / h.backend / h.ctx / h.result / h.error / h.events
  - h.store 可直接再发查询（当前 ctx 已设定）
"""
import json  # noqa: E402


async def check_page(h, expect):
    """A-18：query_with_count 元数据自洽（items/total/hasMore 与同参 query、count 一致）"""
    gql = expect['gql']
    schema = expect['schema']
    params = dict(expect.get('params') or {})
    page = params.get('page', 0)
    size = params.get('pageSize', 3)
    res = await h.store.query_with_count(gql, params)
    # 不带分页的 query（返回全部命中；owner 注入下即该 ctx 的全部）
    plain = {k: v for k, v in params.items() if k not in ('page', 'pageSize')}
    all_items = await h.store.query(gql, plain)
    ok = True
    msgs = []
    if res['page'] != page or res['pageSize'] != size:
        ok, msgs = False, [f"分页元数据不符 page={res['page']} size={res['pageSize']}"]
    if res['total'] != len(all_items):
        ok = False
        msgs.append(f"total={res['total']} 期望 {len(all_items)}")
    expect_has_more = (page + 1) * size < res['total']
    if res['hasMore'] != expect_has_more:
        ok = False
        msgs.append(f"hasMore={res['hasMore']} 期望 {expect_has_more}")
    item_ids = sorted(i.get('_id') for i in res['items'])
    all_ids = sorted(i.get('_id') for i in all_items)
    if not set(item_ids).issubset(set(all_ids)):
        ok = False
        msgs.append(f'items 越界: {item_ids} 不在 {all_ids}')
    if expect.get('expectedIds') and item_ids != sorted(expect['expectedIds']):
        ok = False
        msgs.append(f'items={item_ids} 期望 {sorted(expect["expectedIds"])}')
    return ok, '；'.join(msgs) or 'page 元数据自洽'


async def check_bool_rows(h, expect):
    """F-06/H-05：'paid' 值必须是真布尔（不得 SQL 0/1 或字符串）"""
    field = expect.get('field', 'paid')
    for i, r in enumerate(h.result or []):
        if field in r and not isinstance(r[field], bool):
            return False, f'第{i}行 {field}={r[field]!r} 非 bool'
    return True, '均为 bool'


async def check_ms_timestamp(h, expect):
    """F-05：timestamps 字段须为 13 位 ms 数值（>=10^12）"""
    fields = expect.get('fields', ['createdAt', 'updatedAt'])
    for i, r in enumerate(h.result or []):
        for f in fields:
            v = r.get(f)
            if v is None or not isinstance(v, (int, float)) or not (1e12 <= v < 1e14):
                return False, f'第{i}行 {f}={v!r} 不是 13 位 ms 值'
    return True, '时间戳为 ms 数值'


async def check_numeric_rows(h, expect):
    """H-04：int/float 类型归一（不得字符串/Decimal）"""
    int_field = expect.get('intField', 'enrolledCount')
    float_field = expect.get('floatField', 'price')
    for i, r in enumerate(h.result or []):
        if int_field in r and r[int_field] is not None and not isinstance(r[int_field], int):
            return False, f'第{i}行 {int_field}={r[int_field]!r} 非 int'
        if float_field in r and r[float_field] is not None and not isinstance(r[float_field], (int, float)):
            return False, f'第{i}行 {float_field}={r[float_field]!r} 非 float'
    return True, '类型归一'


async def check_no_secret(h, expect):
    """D-06：student 请求依赖 secret 的计算列 —— secret 键及 secret 值都不得在结果中透出。
    若整体 Err（引擎因取不到依赖而拒算）也视为通过（显式）。"""
    field = expect.get('field', 'secret')
    secret = expect.get('secret')
    err = h.error
    if err is not None:
        return True, '计算列因依赖不可读被显式拒绝'
    for i, r in enumerate(h.result or []):
        if field in r:
            return False, f'第{i}行泄漏了不可读字段 {field}={r[field]!r}'
        if secret and secret in json.dumps(r, default=str):
            return False, f'第{i}行泄漏了 secret 值片段'
    return True, 'secret 未泄漏'


async def check_privileged_roles(h, expect):
    """E-14：super_admin / admin / internal 三种 ctx 都应能读 AuditLog（read=admin）。
    遍历独立设 ctx 查询，全部放行才通过。"""
    from py_store import permission, store  # noqa: PLC0415
    gql = expect['gql']
    roles = expect.get('roles', ['super_admin', 'admin'])
    bad = []
    setup = [({'userId': 'sys', 'roles': ['super_admin']}, 'super_admin'),
             ({'userId': 'sys', 'roles': ['admin']}, 'admin'),
             ({'internal': True}, 'internal')]
    for ctx, label in setup:
        permission.set_context(ctx)
        try:
            await store.query(gql)
        except Exception as e:  # noqa: BLE001
            bad.append(f'{label}: {e}')
    if bad:
        return False, '特权角色被拒: ' + '；'.join(bad)
    return True, '全部特权角色放行'


async def check_require_context(h, expect):
    """E-13：无损 ctx 时 fail-open 放行；开启 require_context 后无 ctx 抛 ERR_NO_CONTEXT。
    开关由 harness 在用例结束时复原，这里不 restore。"""
    gql = expect['gql']
    permission_set(h, None)
    try:
        await h.store.query(gql)  # fail-open：应放行
    except Exception as e:  # noqa: BLE001
        return False, f'fail-open 下无 ctx 查询竟失败: {e}'
    store_set_require(h, True)
    permission_set(h, None)
    try:
        await h.store.query(gql)
    except Exception as e:  # noqa: BLE001
        if 'ERR_NO_CONTEXT' in str(e):
            return True, '开启强制后无 ctx 被拒（ERR_NO_CONTEXT）'
        return False, f'开启强制后抛的非 ERR_NO_CONTEXT 错误: {e}'
    return False, '开启 require_context 后无 ctx 竟放行（fail-secure 失效）'


def store_set_require(h, value):
    from py_store import store  # noqa: PLC0415
    store.set_require_context(value)


def permission_set(h, ctx):
    from py_store import permission  # noqa: PLC0415
    permission.set_context(ctx)


async def check_relation_trim(h, expect):
    """E-05/E-11：字段(关系)因权限被裁剪 —— 允许不出现或为空，不得返回数据本身"""
    field = expect.get('field')
    for i, r in enumerate(h.result or []):
        if field in r and r[field] not in ([], None, {}):
            return False, f'第{i}行泄漏了不可见字段 {field}={json.dumps(r[field], ensure_ascii=False, default=str)}'
    return True, f'{field} 未泄漏'


async def check_write_owner(h, expect):
    """E-16：u1 试图 update 属于 u2 的 StudyNote n2 —— 必须被拒或 0 行，不得改动"""
    from py_store import permission, store  # noqa: PLC0415
    permission.set_context({'userId': 'u1', 'roles': ['student']})
    try:
        await store.update('StudyNote', {'_id': 'n2'}, {'title': 'hacked'})
    except Exception:  # noqa: BLE001
        return True, '越权 update 被拒（403）'
    permission.set_context({'userId': 'u2', 'roles': ['admin']})
    rows = await store.query('StudyNote($condition:@c0){_id, title}', {'c0': {'_id': 'n2'}})
    if rows and rows[0].get('title') == 'hacked':
        return False, '越权改写了 u2 的 StudyNote'
    return True, 'update 未生效（0 行/被忽略）'