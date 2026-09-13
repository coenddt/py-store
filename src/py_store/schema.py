"""
Schema 管理 — 薄适配层

职责（其余全部在 Rust core）：
  1. 把 Python schema 定义同步注册到 Rust core Registry（fn/asyncFn 以占位声明传递）；
  2. 同步 `fn` 计算列回调（core 经 FnRegistry 跨 FFI 回调）；
  3. 保留 asyncFn 原生函数映射（闭包无法跨 FFI，由 Host 在读路径尾处理执行）；
  4. 保留 Host 必需的元数据镜像（collection / idPrefix / indexes / relations），
     供 ID 生成与索引创建使用。

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

from .core import core

# 缓存内置 list 类型（本模块的 list() 函数会遮蔽内置名）
_LIST_TYPES = (list, tuple)

# Host 侧元数据镜像
_schemas: dict = {}

# asyncFn 计算列回调映射（fnRef → 原生异步函数）
_async_fns: dict = {}

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


def register(defn):
    """注册一个 schema（自动派生 `<Name>Deleted` 归档表镜像），返回 Host 侧元数据"""
    core.register(_to_core_defn(defn))

    # 计算列回调：fn → core 回调桥；asyncFn → Host 侧映射
    computes = {}
    for key, val in (defn.get('computes') or {}).items():
        fn_ref = val.get('fnRef') or key
        if val.get('fn'):
            core.set_fn(fn_ref, val['fn'])
        if val.get('asyncFn'):
            _async_fns[fn_ref] = val['asyncFn']
        computes[key] = {'fnRef': fn_ref}

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

    # 归档表镜像（与 core register 的自动派生保持一致，供 Host 查询元数据）
    if not defn.get('_isArchive') and not defn['name'].endswith('Deleted'):
        register({
            'name': f"{defn['name']}Deleted",
            'collection': f"{defn.get('collection') or defn['name']}_deleted",
            'idPrefix': '',
            '_isArchive': True,
            # 归档表与原表同 (source, namespace)
            'datasource': defn.get('datasource'),
            'namespace': defn.get('namespace') or None,
            'fields': {**(defn.get('fields') or {}), 'deletedAt': {'type': 'number'}},
            'indexes': defn.get('indexes') or [],
        })

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


def list():
    """所有已注册 schema 名称（core 侧，含归档表，按注册顺序）"""
    return core.list()


def set_require_context(require=True):
    """
    开关「上下文强制」（默认关闭 = fail-open，与 JS 原版语义一致）。

    开启后：所有 plan 入口遇 ctx 缺失抛 ``ERR_NO_CONTEXT`` 错误（fail-secure）；
    内部调用（索引创建、归档回填、后台任务等）须在 ``run_as_internal`` 中执行，
    或显式传 ``{'internal': True}`` 上下文。
    """
    core.set_require_context(bool(require))


def require_context():
    """「上下文强制」开关当前值"""
    return core.require_context()


def get_async_fn(fn_ref):
    """取 asyncFn 计算列实现（fnRef 缺省 = 计算列 key 名）"""
    return _async_fns.get(fn_ref)
