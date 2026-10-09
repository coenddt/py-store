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

from .core import core, native
from .feedback import emit as _emit_feedback
from .scope import current_scope

# 缓存内置 list 类型（本模块的 list() 函数会遮蔽内置名）
_LIST_TYPES = (list, tuple)


def get_core():
    """取当前生效的 core 门面（R2 双端宿主作用域）：作用域内回退该作用域的派生视图，否则 base。

    - 未进入作用域（无 ``with_scope``）→ 恒等 base ``core``（零行为变化，fail-open 姿态不变）；
    - 进入作用域 → 返回 ``current_scope().view``（派生 Registry，策略覆盖只作用于本作用域）；
    - 视图为只读：写类调用（register/set_xxx）由 core 守卫抛 ``ERR_POLICY_VIEW_READONLY:``。

    禁模块级/跨 await 缓存返回值（03 §7）：每个调用点现场取。
    """
    s = current_scope()
    return s.view if s is not None else core

# Host 侧元数据镜像
_schemas: dict = {}

# asyncFn 计算列回调映射（core_key → 原生异步函数）
_async_fns: dict = {}

# L2 实现池：归一 key → (name, impl)（§8.2 匹配靠归一；进程级状态，不进 clear_schemas）
_fn_impls: dict = {}


def _norm_key(name) -> str:
    """归一 key：用 core 透出算法（禁宿主自实现，总纲 §5）。"""
    return ''.join(native.canonical(str(name)))


def _logical_fn_ref(schema_name, key, comp):
    """逻辑 fnRef：显式优先，否则 ``<schema.name>.<key>``（§6.5）。"""
    return (comp or {}).get('fnRef') or f"{schema_name}.{key}"


def _core_fn_key(key, comp):
    """core 侧回调查找键：显式 fnRef 优先，否则 key（保持 core 语义，cache.rs:47）。"""
    return (comp or {}).get('fnRef') or key

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


def _loc_of(defn):
    """单条 defn 的落点注入（Location）。

    发布契约是「定义零落点」；但**既有** defn 仍可能带 ``datasource``/``database``/``schema``
    （多源路由的历史写法）。01 起 core 不再解析定义内落点字段 ⇒ 本层把它们**显式**注入
    ``Location``（与 06 装载器 / 07 federation 夹具的落点注入同构），否则 ``source`` 恒为
    ``default``、多源路由失效。缺省（无 ``datasource``）⇒ ``default``。
    """
    return {
        'source': (defn or {}).get('datasource') or None,
        'database': (defn or {}).get('database') or None,
        'schema': (defn or {}).get('schema') or None,
    }


def register(defn, ctx=None):
    """注册一个 schema（core 注册 + Host 侧元数据镜像）

    ``ctx``：可选定义层门禁上下文（``{'userId','roles',...}`` 或 ``{'internal': True}``）。
    返回 ``None`` 兼容既有调用；门禁策略由 ``set_meta_policy`` 配置，默认 Open（全放行）。
    判决唯一在 core（拒绝抛 ``ERR_PERMISSION:`` 前缀错误，定义不变）。

    归档表 `<Name>Deleted` 由 core 在 register 内**自动派生并注册**
    （rust-store/core/src/schema/registry.rs）；Host 只补 Host 侧镜像，
    **不再调用 core.register** —— 否则同名条目二次进入 core.order，使 list()/
    generate_ddl() 出现重复表（基线实测 list=['User','UserDeleted','UserDeleted']）。

    落点经 ``_loc_of`` 显式注入（与 nodejs-store store.register 同构）：01 起 core 不再
    解析定义内 ``datasource``/``database``/``schema``，须经 Location 传入否则 source 恒 default。
    """
    core.register_batch(
        [{'defn': _to_core_defn(defn), 'location': _loc_of(defn)}],
        ctx,
    )
    return _mirror(defn)


def _mirror(defn):
    """Host 侧元数据镜像 + 内嵌回调绑定（``register`` 与 ``register_batch`` 共用）"""
    # 计算列回调：fn → core 回调桥；asyncFn → Host 侧映射
    computes = {}
    for key, val in (defn.get('computes') or {}).items():
        core_key = _core_fn_key(key, val)          # core 查找键（本步不改 core 语义）
        # 注2：仅「可调用」才走内嵌绑定；纯 JSON `"fn": true`（bool）不在此绑定，
        #      由 assert_fns_covered 从 L2 实现池解析绑定（否则会绑定出坏回调）。
        if callable(val.get('fn')):
            core.set_fn(core_key, val['fn'])
        if callable(val.get('asyncFn')):
            _async_fns[core_key] = val['asyncFn']
        # 镜像保留声明元数据（callable 白名单外天然剔除）：
        # agg 形态与 read 白名单供 AI 摘要（ask.describe_for_ai）等消费者读取，
        # 可执行物（fn/asyncFn）不入镜像（执行判决唯一在 core 规划 + Host 尾处理）
        computes[key] = {k: val[k] for k in ('type', 'depends', 'agg', 'read') if k in val}
        computes[key]['fnRef'] = _logical_fn_ref(defn['name'], key, val)

    _schemas[defn['name']] = {
        'name': defn['name'],
        'collection': defn.get('collection') or defn['name'],
        # 落点定位（定义文件零落点；由装载器注入 defn）：database = 连接内库
        # （Mongo/MySQL/SQLite/PG），schema = 仅 PG 的 schema 层
        'database': defn.get('database') or None,
        'schema': defn.get('schema') or None,
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
            'database': defn.get('database') or None,
            'schema': defn.get('schema') or None,
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


def register_batch(items, ctx=None):
    """带定位批量注册（D13 批次唯一）：``items = [{'defn', 'location'}]``。

    判决唯一在 core（``core.register_batch``：分组 / 主唯一 / 链路 / 版本）；本层只转发 +
    对**主定义**做 Host 元数据镜像（从定义 ``replica: true`` 是链路声明，非独立结构，不入镜像）。
    落点**不进 defn**（定义文件零落点）。
    """
    core.register_batch(
        [{'defn': _to_core_defn(it['defn']), 'location': it['location']} for it in items],
        ctx,
    )
    for it in items:
        defn = it.get('defn')
        if defn and not defn.get('replica'):
            _mirror(defn)


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
    from . import naming as _naming  # 局部导入规避包内循环导入
    _naming.clear_cache()


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
    """取 asyncFn 计算列实现（入参为 core 侧 fnRefs 查找键，即 core_key）"""
    return _async_fns.get(fn_ref)


def set_fn(impl_name, impl):
    """公开回调注入：``impl_name → impl(item, ctx)``（来自 L2 包扁平字典）。

    实现名与 schema 逻辑 fn_ref 由 ``_norm_key`` 归一后匹配（§6.5）；
    归一后重复 ⇒ ValueError（禁静默覆盖）。绑定 core 由 ``assert_fns_covered`` 统一完成。
    """
    if not isinstance(impl_name, str) or not impl_name:
        raise ValueError("ERR_FN_REF:impl_name 须为非空字符串")
    if not callable(impl):
        raise ValueError("ERR_FN_IMPL:impl 须可调用")
    k = _norm_key(impl_name)
    prev = _fn_impls.get(k)
    if prev and prev[0] != impl_name:
        raise ValueError(f'ERR_FN_CONFLICT:实现名 "{impl_name}" 与 "{prev[0]}" 归一后相同（{k}）')
    _fn_impls[k] = (impl_name, impl)


def _bind_one(schema_name, key, comp):
    """解析单个回调计算列的实现并绑定 core；无实现返回 False。"""
    embedded = comp and (comp.get('fn') or comp.get('asyncFn'))
    if callable(embedded):
        impl = embedded
    else:
        impl = (_fn_impls.get(_norm_key(_logical_fn_ref(schema_name, key, comp))) or (None, None))[1]
    if not callable(impl):
        return False
    core_key = _core_fn_key(key, comp)
    if comp.get('fn'):
        core.set_fn(core_key, impl)
    if comp.get('asyncFn'):
        _async_fns[core_key] = impl
    return True


def assert_fns_covered(defns):
    """启动期：解析每个回调计算列的实现并绑定 core；缺实现 ⇒ 显式抛 ``ERR_FN_MISSING``（不静默）。

    关系聚合（``val['agg']``）由框架处理，无需回调，跳过。
    """
    missing = []
    for defn in defns or ():
        for key, val in ((defn or {}).get('computes') or {}).items():
            if val and val.get('agg'):
                continue
            if not _bind_one(defn['name'], key, val):
                missing.append(_logical_fn_ref(defn['name'], key, val))
    if missing:
        raise RuntimeError(f"ERR_FN_MISSING:未注入回调实现 {', '.join(missing)}")
