"""
Schema 管理 — 注册、解析、查询

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
            'bidCount':    {'type': 'int', 'lookup': {'$size': {'$ifNull': ['$bidders', []]}}},
        },
        'indexes': [
            {'keys': {'status': 1, 'startTime': -1}},
        ],
    })
"""

# 已注册的 schema 映射
_schemas: dict = {}


def register(defn):
    """注册一个 schema，返回规范化后的 schema 对象"""
    # 规范化 fields
    fields = {}
    for key, val in (defn.get('fields') or {}).items():
        if isinstance(val, str):
            # 简写: 'string' → {'type': 'string'}
            fields[key] = {'type': val, 'required': False}
        else:
            fields[key] = {
                'type': val.get('type'),
                'required': val.get('required', False),
                'default': val.get('default'),
                'read': val.get('read'),
                'write': val.get('write'),
                'fields': val.get('fields'),
            }

    # 自动注册时间戳字段（timestamps: True 时，createdAt/updatedAt 由框架自动管理）
    timestamps_enabled = defn.get('timestamps') is not False
    if timestamps_enabled:
        if 'createdAt' not in fields:
            fields['createdAt'] = {'type': 'number'}
        if 'updatedAt' not in fields:
            fields['updatedAt'] = {'type': 'number'}

    # 规范化 relations
    relations = {}
    for key, val in (defn.get('relations') or {}).items():
        relations[key] = {
            'model': val.get('model'),
            'type': val.get('type', 'many'),
            'localField': val.get('localField', '_id'),
            'foreignField': val.get('foreignField', key),
            'read': val.get('read'),
        }

    # 规范化 computes
    computes = {}
    for key, val in (defn.get('computes') or {}).items():
        computes[key] = {
            'type': val.get('type', 'any'),
            'fn': val.get('fn'),
            'lookup': val.get('lookup'),
            'asyncFn': val.get('asyncFn'),
            'depends': val.get('depends', []),
            'read': val.get('read'),
        }

    schema = {
        'name': defn['name'],
        'collection': defn.get('collection', defn['name']),
        'idPrefix': defn.get('idPrefix', ''),
        'timestamps': defn.get('timestamps') is not False,
        'fields': fields,
        'relations': relations,
        'computes': computes,
        'indexes': defn.get('indexes', []),
        'read': defn.get('read'),
        'write': defn.get('write'),
    }

    _schemas[schema['name']] = schema

    # 自动注册删除附表 schema —— 每个业务表对应一个 `<collection>_deleted` 归档表
    # 删除时原表数据先完整写入附表（附 deletedAt），再物理删除原表数据
    if not defn.get('_isArchive') and not schema['name'].endswith('Deleted'):
        register({
            'name': f"{schema['name']}Deleted",
            'collection': f"{schema['collection']}_deleted",
            'idPrefix': '',
            '_isArchive': True,
            # 归档表保留原表全部字段 + deletedAt（删除时间，毫秒），不做关联/计算列
            'fields': {
                **(defn.get('fields') or {}),
                'deletedAt': {'type': 'number'},
            },
            'indexes': defn.get('indexes') or [],
        })

    return schema


def get(name):
    """按名称获取 schema"""
    s = _schemas.get(name)
    if not s:
        raise KeyError(f'Schema 未注册: {name}')
    return s


def has(name):
    """检查 schema 是否已注册"""
    return name in _schemas


def list():
    """获取所有已注册 schema 名称"""
    return [*_schemas.keys()]
