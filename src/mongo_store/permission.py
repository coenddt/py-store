"""
权限引擎 — ContextVar 上下文 + 角色评估 + 字段过滤

角色清单（共 9 种）:
    super_admin / admin / internal  ← 自动放行，无所不能
    seller / user / verified / buyer ← 大部分公共模型默认开放
    court ← TBD
    guest ← 默认禁止
    creator 是伪角色，由 evaluate() 根据 doc.createdBy === ctx.userId 动态判断

权限规则:
    - read/write 不配置 → 所有已登录角色默认可读/可写（guest 除外）
    - 无上下文时 evaluate() 返回 True（权限检查禁用，向后兼容）
"""

import contextvars
from contextlib import contextmanager

_ctx: contextvars.ContextVar = contextvars.ContextVar('mongo_store_ctx', default=None)

_MISSING = object()


def set_context(ctx):
    """设置当前请求的上下文，每次请求开始时调用一次"""
    _ctx.set(ctx)


def get_context():
    """获取当前请求的上下文，无则 None"""
    return _ctx.get()


# ─── 权限评估核心 ─────────────────────────────────────────────


def evaluate(ctx, role_list, doc=_MISSING):
    """评估当前用户是否满足指定角色白名单

    doc 语义（用于 creator 伪角色）:
        _MISSING → 新插入（无已有文档，creator 自动通过）
        None     → 查询无结果（creator 不通过）
        dict     → 已有文档
    """
    # 无角色白名单 = schema/字段无权限配置 → 按角色取默认行为
    if not role_list:
        if ctx is None:
            return True
        if ctx.get('internal'):
            return True
        return 'guest' not in (ctx.get('roles') or [])

    if ctx is None:
        return True
    if ctx.get('internal'):
        return True

    # super_admin / admin 自动放行
    roles = ctx.get('roles') or []
    if 'super_admin' in roles or 'admin' in roles:
        return True

    # ① 角色匹配
    effective_roles = roles if roles else [ctx.get('role')]
    for r in role_list:
        if r in effective_roles:
            return True

    # ② 创作者匹配
    return _match_creator(ctx, doc, role_list)


def _match_creator(ctx, doc, role_list):
    """创作者伪角色匹配（doc 语义见 evaluate 文档）"""
    if 'creator' not in role_list:
        return False
    if doc is _MISSING:
        return True
    return bool(doc and ctx.get('userId') and doc.get('createdBy') == ctx.get('userId'))


# ─── Schema 级检查 ────────────────────────────────────────────


def can_read_schema(schema, ctx):
    return evaluate(ctx, schema.get('read'))


def can_write_schema(schema, ctx):
    """游客无论 schema.write 如何配置，均无写入权限"""
    if ctx and 'guest' in (ctx.get('roles') or []):
        return False
    return evaluate(ctx, schema.get('write'))


# ─── 所有者条件注入 ──────────────────────────────────────────


def should_inject_owner_condition(schema, ctx):
    if not ctx or not ctx.get('userId'):
        return False
    if ctx.get('internal'):
        return False
    roles = ctx.get('roles') or []
    if 'super_admin' in roles or 'admin' in roles:
        return False
    effective_roles = roles if roles else [ctx.get('role')]
    read = schema.get('read')
    if read:
        real_roles = [r for r in read if r != 'creator']
        if any(r in effective_roles for r in real_roles):
            return False
    return bool(read and 'creator' in read)


def merge_owner_condition(schema, ctx, condition):
    if not should_inject_owner_condition(schema, ctx):
        return condition
    owner_condition = {'createdBy': ctx['userId']}
    if not condition:
        return owner_condition
    return {'$and': [condition, owner_condition]}


# ─── 字段级过滤（读） ─────────────────────────────────────────


def get_readable_fields(schema, ctx):
    if ctx is None:
        return None
    allowed = set()
    for key, field in schema['fields'].items():
        if field.get('read'):
            if evaluate(ctx, field['read']):
                allowed.add(key)
        else:
            allowed.add(key)
    return allowed


def get_readable_computes(schema, ctx):
    if ctx is None:
        return None
    allowed = set()
    for key, comp in (schema.get('computes') or {}).items():
        if comp.get('read'):
            if evaluate(ctx, comp['read']):
                allowed.add(key)
        else:
            allowed.add(key)
    return allowed


def get_readable_relations(schema, ctx):
    if ctx is None:
        return None
    allowed = set()
    for key, rel in (schema.get('relations') or {}).items():
        if rel.get('read'):
            if evaluate(ctx, rel['read']):
                allowed.add(key)
        else:
            allowed.add(key)
    return allowed


# ─── 字段级过滤（写） ─────────────────────────────────────────


def get_writable_fields(schema, ctx):
    if ctx is None:
        return None
    allowed = set()
    for key, field in schema['fields'].items():
        if field.get('write'):
            if evaluate(ctx, field['write']):
                allowed.add(key)
        else:
            if can_write_schema(schema, ctx):
                allowed.add(key)
    return allowed


def filter_writable_data(schema, ctx, data):
    """过滤写入数据：只保留当前用户可写的字段"""
    if ctx is None:
        return data
    writable = get_writable_fields(schema, ctx)
    if writable is None:
        return data
    result = {}
    for key in data:
        # 支持点号嵌套路径，按 root 字段检查写权限
        root = key.split('.', 1)[0] if '.' in key else key
        if root in writable:
            result[key] = data[key]
    return result


# ─── 内部上下文执行 ──────────────────────────────────────────


@contextmanager
def scoped_roles(roles):
    """以指定角色进入临时权限上下文（token-set/reset，嵌套安全），退出自动恢复原上下文。

    与 run_as_internal 同构，供 AI 查询执行等显式角色注入场景使用，
    取代 set_context + finally set_context(None) 的清空式写法（后者嵌套时会误清外层上下文，
    且"无上下文=权限全放行"，清空即静默失守方向）。
    """
    token = _ctx.set({**(get_context() or {}), 'roles': roles})
    try:
        yield
    finally:
        _ctx.reset(token)


async def run_as_internal(fn):
    """在内部上下文中执行操作（绕过权限检查），结束后自动恢复上下文"""
    prev = get_context()
    new_ctx = {**(prev or {}), 'internal': True}
    token = _ctx.set(new_ctx)
    try:
        import inspect
        if inspect.iscoroutinefunction(fn):
            return await fn()
        return fn()
    finally:
        _ctx.reset(token)


# ─── 自定义错误 ──────────────────────────────────────────────


class PermissionError(Exception):
    def __init__(self, message, status=403):
        super().__init__(message)
        self.status = status
