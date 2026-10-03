"""
Schema 管理 — 薄适配层

职责（其余全部在 Rust core）：
  1. 把 Python schema 定义同步注册到 Rust core Registry（fn/asyncFn 以占位声明传递）；
  2. 同步 `fn` 计算列回调（core 经 FnRegistry 跨 FFI 回调）；
  3. 保留 asyncFn 原生函数映射（闭包无法跨 FFI，由 Host 在读路径尾处理执行）；
  4. 保留 Host 必需的元数据镜像（collection / idPrefix / indexes / relations），
     供 ID 生成与索引创建使用。
  5. ``text2query`` 档位上下文管理器（档位 set/get 的伴随入口，同模块内聚）。

示例:
    register({
        'name': 'AuctionItem',
        'collection': 'auctionItems',
        'idPrefix': 'AUCT',
        'timestamps': True,
        'fields': {
            '_id': 'string',
            'title': {'type': 'string', 'required': True},
            'auctionType': {'type': 'string', 'default': 'normal'},
        },
        'relations': {
            'bidders': {'model': 'BidRecord', 'type': 'many', 'localField': '_id', 'foreignField': 'itemId'},
        },
        'computes': {
            'statusLabel': {'type': 'string', 'depends': ['status'], 'fn': lambda item: LABELS.get(item.get('status'), '')},
            'bidCount':    {'type': 'int', 'agg': {'$count': 'bidders'}},
        },
        'indexes': [
            {'keys': {'status': 1, 'startTime': -1}},
        ],
    })
"""

from contextlib import contextmanager

from .core import core
from .feedback import emit as _emit_feedback

# 缓存内置 list 类型（本模块的 list() 函数会遮蔽内置名）
_LIST_TYPES = (list, tuple)

# Host 侧元数据镜像
_schemas: dict = {}

# asyncFn 计算列回调映射（fnRef → 原生异步函数）
_async_fns: dict = {}

# 已注入实现的 fn_ref 集合（A3：启动期缺实现校验用；进程级状态，不进 clear_schemas）
_fn_refs: set = set()

# 内部标记：表示「该键需剔除」（对齐 JS JSON round-trip 中函数型 default 被移除）
_DROP = object()


def _to_core_defn(defn):
    """生成可跨 FFI 的 schema 定义：fn/asyncFn → True 占位；函数型值剔除"""

    def walk(value, key=None):
        if callable(value):
            # fn/asyncFn 声明占位（core 按 `fn: True` 识别）；函数型 default 无法跨 FFI，剔除
            return True if key in ('fn', 'asyncFn') else _DROP
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                w = walk(v, k)
                if w is not _DROP:
                    out[k] = w
            return out
        if isinstance(value, _LIST_TYPES):
            # 对齐 JS：数组内被剔除的函数值变成 null
            out_arr = []
            for v in value:
                w = walk(v)
                out_arr.append(None if w is _DROP else w)
            return out_arr
        return value

    return walk(defn)


def register(defn, ctx=None):
    """注册一个 schema（core 注册 + Host 侧元数据镜像）

    ``ctx``：可选定义层门禁上下文（``{'userId','roles',...}`` 或 ``{'internal': True}``）。
    返回 ``None`` 兼容既有调用；门禁策略由 ``set_meta_policy`` 配置，默认 Open（全放行）。
    判决唯一在 core（拒绝抛 ``ERR_PERMISSION:`` 前缀错误，定义不变）。

    归档表 `<Name>Deleted` 由 core 在 register 内**自动派生并注册**
    （rust-store/core/src/schema/registry.rs）；Host 只补 Host 侧镜像，
    **不再调用 core.register** —— 否则同名条目二次进入 core.order，使 list()/
    generate_ddl() 出现重复表（基线实测 list=['User','UserDeleted','UserDeleted']）。
    """
    core.register_with_ctx(_to_core_defn(defn), ctx)

    # 计算列回调：fn → core 回调桥；asyncFn → Host 侧映射
    computes = {}
    for key, val in (defn.get('computes') or {}).items():
        fn_ref = val.get('fnRef') or key
        if val.get('fn'):
            core.set_fn(fn_ref, val['fn'])
        if val.get('asyncFn'):
            _async_fns[fn_ref] = val['asyncFn']
        # 镜像保留声明元数据（callable 白名单外天然剔除）：
        # agg 形态与 read 白名单供 AI 摘要（ask.describe_for_ai）等消费者读取，
        # 可执行物（fn/asyncFn）不入镜像（执行判决唯一在 core 规划 + Host 尾处理）
        computes[key] = {k: val[k] for k in ('type', 'depends', 'agg', 'read') if k in val}
        computes[key]['fnRef'] = fn_ref

    _schemas[defn['name']] = {
        'name': defn['name'],
        'collection': defn.get('collection') or defn['name'],
        'namespace': defn.get('namespace') or None,
        'idPrefix': defn.get('idPrefix') or '',
        'timestamps': defn.get('timestamps') is not False,
        # 时间戳单位（'ms'/'s'/None=不维护）；值合法性由 core.register 校验
        'timestampUnit': 's' if defn.get('timestamps') == 's' else (
            None if defn.get('timestamps') is False else 'ms'),
        'fields': defn.get('fields') or {},
        'relations': defn.get('relations') or {},
        'computes': computes,
        'indexes': defn.get('indexes') or [],
        'read': defn.get('read'),
        'write': defn.get('write'),
        # 数据源绑定（缺省视为 default，见 datasource.py）— Phase 3/4 多后端路由
        'datasource': defn.get('datasource'),
    }

    # 归档表镜像（形状对齐 core archive_defn：registry.rs:395-417）
    # —— 字段 + deletedAt、collection=<c>_deleted、idPrefix=''、timestamps 默认 true
    if not defn.get('_isArchive') and not defn['name'].endswith('Deleted'):
        _schemas[f"{defn['name']}Deleted"] = {
            'name': f"{defn['name']}Deleted",
            'collection': f"{defn.get('collection') or defn['name']}_deleted",
            'namespace': defn.get('namespace') or None,
            'idPrefix': '',
            'timestamps': True,
            'timestampUnit': 'ms',
            'fields': {**(defn.get('fields') or {}), 'deletedAt': {'type': 'number'}},
            'relations': {},
            'computes': {},
            'indexes': defn.get('indexes') or [],
            'read': None,
            'write': None,
            'datasource': defn.get('datasource'),
        }

    return _schemas[defn['name']]


def get(name):
    """按名称获取 Host 侧元数据"""
    s = _schemas.get(name)
    if not s:
        raise KeyError(f'Schema 未注册: {name}')
    return s


def has(name):
    """检查 schema 是否已注册（core 侧判定，含归档表）"""
    return core.has(name)


def clear_schemas():
    """清空 schema 注册表（core 注册表 + Host 镜像 + asyncFn 映射 + 去重告警签名）

    测试隔离 / 动态重建场景的注册表生命周期原语（绑定层 ``clear_schemas`` 同语义）：
    只清 schema 集合，**不动** ``require_context`` / ``profile`` 等配置开关
    （各清各的，与 ``clear_fns`` 对称）。
    """
    core.clear_schemas()
    _schemas.clear()
    _async_fns.clear()
    _dup_signatures.clear()


# 去重告警签名（同一重复形态只告警一次，避免 list() 高频调用刷屏）
_dup_signatures: set = set()


def list():
    """所有已注册 schema 名称（core 侧，含归档表，按注册顺序；同名只保留首次出现）

    去重是纵深防御的第二层：一旦检出重复即说明上游（register/core）失守，
    去重同时 emit 告警（同签名只告警一次），禁静默。
    """
    names = core.list()
    seen = set()
    out = []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    if len(out) != len(names):
        sig = tuple(names)
        if sig not in _dup_signatures:
            _dup_signatures.add(sig)
            _emit_feedback({
                'type': 'schema_duplicate_name',
                'code': 'schemaDuplicateName',
                'layer': 'host',
                'message': f'schema 注册表存在重复名（多 {len(names) - len(out)} 条），已顺序去重',
                'hint': ('上游注册逻辑失守（core.order 同名两次）；'
                         '核查 register 是否重复调用 core.register，或 core.register 未对同名去重'),
                'names': names,
            })
    return out


def set_require_context(require=True):
    """
    开关「上下文强制」（默认关闭 = fail-open，与 JS 原版语义一致）。

    开启后：所有 plan 入口遇 ctx 缺失抛 ``ERR_NO_CONTEXT`` 错误（fail-secure）；
    内部调用（索引创建、归档回填、后台任务等）须在 ``run_as_internal`` 中执行，
    或显式传 ``{'internal': True}`` 上下文。
    """
    core.set_require_context(bool(require))


def set_exempt_roles(roles):
    """豁免角色清单（命中者在一切判决环节直接放行）。默认空——无豁免（清单化语义）"""
    core.set_exempt_roles(roles)


def set_deny_write_roles(roles):
    """拒写角色清单（命中者一切写路径拒绝，读不受影响）。默认空——无拒写"""
    core.set_deny_write_roles(roles)


def set_unconfigured_policy(policy):
    """schema 白名单缺失/为空时的默认姿态："open"（默认，放行）| "closed"（全拒）"""
    core.set_unconfigured_policy(policy)


def set_meta_policy(closed, roles):
    """定义层门禁策略：`closed=True` 时仅 internal 或 `roles` 白名单可注册/覆盖。

    判决唯一在 core；默认 Open（`register` 无 ctx 亦放行，保既有兼容）。
    """
    core.set_meta_policy(bool(closed), roles)


def require_context():
    """「上下文强制」开关当前值"""
    return core.require_context()


def set_profile(profile):
    """设置查询档位：'standard'（默认，功能最大化 + 跨 DB 对齐）/
    'text2query'（功能收缩 + 硬限制）

    判决唯一在 core；未知档位由 core 抛 ValueError 上抛（禁静默回落到默认档）。
    """
    core.set_profile(profile)


def get_profile():
    """当前档位字符串（'standard' / 'text2query'）"""
    return core.profile()


@contextmanager
def text2query():
    """以 text2query 档执行（功能收缩 + 硬限制），退出恢复原档位。

    AI 问数链路入口；与 ``permission.scoped_roles`` 同构（token-set/reset，嵌套安全）。
    进入档位即等效强制携带用户上下文（core `ensure_profile_ctx`，见执行文档 §4.2）。
    """
    prev = get_profile()
    set_profile('text2query')
    try:
        yield
    finally:
        set_profile(prev)


def get_async_fn(fn_ref):
    """取 asyncFn 计算列实现（fnRef 缺省 = 计算列 key 名）"""
    return _async_fns.get(fn_ref)


def set_fn(fn_ref, impl):
    """公开回调注入：fn_ref → impl(item, ctx)（对齐 nodejs-store store.setFn）。

    与 ``register`` 内 ``core.set_fn`` 同语义；impl 返回 ``None`` 即 null（无跨 FFI 归一问题）。
    """
    if not isinstance(fn_ref, str) or not fn_ref:
        raise ValueError("ERR_FN_REF:fn_ref 须为非空字符串")
    if not callable(impl):
        raise ValueError("ERR_FN_IMPL:impl 须可调用")
    core.set_fn(fn_ref, impl)
    _fn_refs.add(fn_ref)


def assert_fns_covered(defns):
    """启动期校验：定义声明的 fn_ref 必须都有实现；缺则显式抛错（不静默）。

    关系聚合（``val['agg']``）由框架处理，无需回调，跳过。
    """
    missing = []
    for defn in defns or ():
        for key, val in ((defn or {}).get('computes') or {}).items():
            if val and val.get('agg'):
                continue
            ref = (val or {}).get('fnRef') or key
            if ref not in _fn_refs:
                missing.append(ref)
    if missing:
        raise RuntimeError(f"ERR_FN_MISSING:未注入回调实现 {', '.join(missing)}")
