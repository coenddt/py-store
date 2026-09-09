"""类型系统 — 类型 → 零值映射"""

TYPE_MAP = {
    'string': '',
    'int': 0,
    'long': 0,
    'float': 0,
    'double': 0,
    'boolean': False,
    'array': list,   # 工厂标记：每次返回新实例
    'object': dict,  # 工厂标记：每次返回新实例
    'date': None,
    'any': None,
}


def get_default(field_type):
    """获取指定类型的零值"""
    if field_type not in TYPE_MAP:
        return None
    value = TYPE_MAP[field_type]
    # array/object 需要每次返回新实例，避免多个文档共享同一引用
    if value is list:
        return []
    if value is dict:
        return {}
    return value


def is_numeric(field_type):
    return field_type in ('int', 'long', 'float', 'double')


def is_primitive(field_type):
    return field_type in ('string', 'int', 'long', 'float', 'double', 'boolean')
