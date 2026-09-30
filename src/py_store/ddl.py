"""
DDL 生成（schema def → CREATE TABLE 文本；纯函数，不连库、不回写）

与 core 契约严格对齐（schema→DDL 单向映射）：
  - 标量字段按声明类型建列；object/array 字段建 **JSON 列**（MySQL `JSON` / PG `jsonb` /
    SQLite `TEXT`，同 core dialect::Backend::json_type_name）——落单列存 JSON 文本，
    读侧由 core row::parse_json_col 还原为嵌套对象，跨后端对齐 Mongo 嵌套文档；
  - 每表必建 __present 哨兵列（形态 ,f1,f2,；同 core write/insert.rs::present_value）；
  - timestamps !== false → 追加 createdAt / updatedAt（同 core schema/registry.rs::add_timestamp_fields）；
  - 归档表 <collection>_deleted 由 registry 自动派生，本模块按已注册 def 逐表生成（不特判）；
  - schema.indexes（Mongo 形态 `{keys: {f: 1|-1}, options/inline}`）→ CREATE [UNIQUE] INDEX
    （阶段 3 索引落地；原「仅元数据不建索引」铁律 6 子项按用户裁决放开，见
    common-store/事务型能力增补执行文档.md 附录 D）。

生成器只产出文本、不执行 —— 不违反铁律 6（绝不写 DDL 回库）。
对齐 nodejs-store/src/ddl.js（两端输出逐字节一致）。
"""

from .feedback import emit as _emit_feedback
from .schema import get as _get_schema
from .schema import list as _list_schema

_BACKENDS = ('mysql', 'postgres', 'sqlite')

# schema 声明类型 → (mysql, postgres, sqlite) 列类型
_TYPES = {
    'string': ('VARCHAR(255)', 'TEXT', 'TEXT'),
    'int': ('INT', 'INTEGER', 'INTEGER'),
    'long': ('BIGINT', 'BIGINT', 'INTEGER'),
    'number': ('BIGINT', 'BIGINT', 'INTEGER'),
    'float': ('DOUBLE', 'DOUBLE PRECISION', 'REAL'),
    'double': ('DOUBLE', 'DOUBLE PRECISION', 'REAL'),
    'bool': ('TINYINT(1)', 'BOOLEAN', 'INTEGER'),
    'boolean': ('TINYINT(1)', 'BOOLEAN', 'INTEGER'),
    'datetime': ('BIGINT', 'BIGINT', 'INTEGER'),
    'date': ('BIGINT', 'BIGINT', 'INTEGER'),
}
_NON_COLUMN = ('object', 'array')
# object/array 字段的列类型（JSON 文本列；同 core Backend::json_type_name）
_JSON_TYPE = ('JSON', 'jsonb', 'TEXT')
_ID_TYPE = ('VARCHAR(64)', 'TEXT', 'TEXT')
# 阶段2：`_id` 声明 strategy=autoincrement 时的自增列类型（MySQL AUTO_INCREMENT 列
# 须被索引 —— 表级 PRIMARY KEY 满足；SQLite 语法要求 PRIMARY KEY AUTOINCREMENT 相邻，
# 由 _create_table 的 pk+auto 分支拼接；PG 用 SERIAL）
_ID_AUTO_TYPE = ('INT AUTO_INCREMENT', 'SERIAL', 'INTEGER')
_PRESENT_TYPE = ('VARCHAR(255)', 'TEXT', 'TEXT')
_TIMESTAMP_FIELDS = ('createdAt', 'updatedAt')
_MYSQL_PRESENT_MAX = 255


def _idx(backend):
    return _BACKENDS.index(backend)


def _q(backend, ident):
    """标识符引用（与 core dialect::Backend::quote_ident 一致：mysql 反引号，其余双引号）"""
    if backend == 'mysql':
        return '`' + ident.replace('`', '``') + '`'
    return '"' + ident.replace('"', '""') + '"'


def _declared_type(field_def):
    if isinstance(field_def, dict):
        return field_def.get('type')
    return field_def


def _columns(defn, backend):
    """返回 [(name, sql_type, pk)]，顺序：声明的字段（标量 / object·array JSON 列）→ timestamps → __present"""
    i = _idx(backend)
    cols = []
    fields = defn.get('fields') or {}
    for name, fdef in fields.items():
        ftype = _declared_type(fdef)
        if name == '_id':
            fdef = fdef if isinstance(fdef, dict) else {}
            if fdef.get('strategy') == 'autoincrement':
                cols.append((name, _ID_AUTO_TYPE[i], True, True))
            else:
                cols.append((name, _ID_TYPE[i], True, False))
            continue
        if ftype in _NON_COLUMN:
            # object/array → 单列 JSON 文本（同 core field_column_ref::Json）
            cols.append((name, _JSON_TYPE[i], False, False))
            continue
        if ftype not in _TYPES:
            raise ValueError(
                f'DDL 生成：字段 "{defn["name"]}.{name}" 类型 {ftype!r} 未知，'
                f'支持 {sorted(_TYPES)}')
        cols.append((name, _TYPES[ftype][i], False, False))
    if not any(c[2] for c in cols):
        raise ValueError(f'DDL 生成：schema "{defn["name"]}" 缺少 _id 字段')
    if defn.get('timestamps') is not False:
        for ts in _TIMESTAMP_FIELDS:
            if not any(c[0] == ts for c in cols):
                cols.append((ts, _TYPES['number'][i], False, False))
    cols.append(('__present', _PRESENT_TYPE[i], False, False))
    return cols


def _warn_present_overflow(defn, cols):
    """MySQL __present VARCHAR(255) 容量校验：超限即告警（不静默）"""
    length = sum(len(c[0]) for c in cols) + len(cols) + 1
    if length > _MYSQL_PRESENT_MAX:
        _emit_feedback({
            'type': 'ddl_present_overflow',
            'code': 'ddlPresentOverflow',
            'layer': 'host',
            'message': (f'表 {defn.get("collection")} 的 __present 预估长度 {length} '
                        f'超过 MySQL VARCHAR(255)'),
            'hint': ('为该表改用 TEXT 列，或减少标量字段；否则写入会被截断/报错，'
                     '导致 $eq:null / $exists 三态判定错误'),
            'schema': defn.get('name'),
        })


def _create_table(defn, backend):
    table = defn.get('collection') or defn.get('name')
    cols = _columns(defn, backend)
    if backend == 'mysql':
        _warn_present_overflow(defn, cols)
    lines = []
    for name, ctype, pk, auto in cols:
        if pk and auto and backend == 'sqlite':
            # SQLite 语法要求 AUTOINCREMENT 紧跟 PRIMARY KEY
            lines.append(f'  {_q(backend, name)} {ctype} PRIMARY KEY AUTOINCREMENT')
        elif pk and backend == 'mysql':
            lines.append(f'  {_q(backend, name)} {ctype} NOT NULL')
        elif pk:
            lines.append(f'  {_q(backend, name)} {ctype} PRIMARY KEY')
        else:
            lines.append(f'  {_q(backend, name)} {ctype}')
    if backend == 'mysql':
        lines.append(f'  PRIMARY KEY ({_q(backend, "_id")})')
    return f'CREATE TABLE {_q(backend, table)} (\n' + ',\n'.join(lines) + '\n);'


def _index_stmts(defn, backend):
    """schema.indexes → CREATE [UNIQUE] INDEX 语句列表（阶段 3 索引落地）。

    索引名 `idx_<collection>_<f1>_<f2>`（对齐 SQL 常规命名）；keys 值 1/-1 → ASC/DESC。
    """
    out = []
    table = defn.get('collection') or defn.get('name')
    for idx in defn.get('indexes') or []:
        if not isinstance(idx, dict):
            continue
        keys = idx.get('keys')
        if not isinstance(keys, dict) or not keys:
            continue
        unique = bool(idx.get('unique') or (idx.get('options') or {}).get('unique'))
        cols = ', '.join(
            f"{_q(backend, k)} {'DESC' if v == -1 else 'ASC'}" for k, v in keys.items())
        name = 'idx_' + table + '_' + '_'.join(keys)
        out.append(
            f"CREATE {'UNIQUE ' if unique else ''}INDEX {_q(backend, name)} "
            f"ON {_q(backend, table)} ({cols})")
    return out


def generate(backend, names=None):
    """生成 DDL 文本（多表以空行分隔）；backend ∈ mysql/postgres/sqlite

    按表名去重：同名表只出一次 CREATE TABLE（防御 core 注册表出现重复名 ——
    上游失守即告警，禁静默）。
    """
    if backend not in _BACKENDS:
        raise ValueError(f'DDL 生成：不支持的后端 {backend!r}（支持 {list(_BACKENDS)}）')
    targets = list(names) if names else list(_list_schema())
    blocks = []
    seen_tables = set()
    dup = []
    for n in targets:
        defn = _get_schema(n)
        table = defn.get('collection') or n
        if table in seen_tables:
            dup.append(table)
            continue
        seen_tables.add(table)
        blocks.append(_create_table(defn, backend))
        blocks.extend(_index_stmts(defn, backend))
    if dup:
        _emit_feedback({
            'type': 'ddl_duplicate_table',
            'code': 'ddlDuplicateTable',
            'layer': 'host',
            'message': f'DDL 生成：表 {sorted(set(dup))} 重复注册，已去重',
            'hint': 'schema 注册表出现重复名（见 schemaDuplicateName 告警）；修复注册侧根因',
            'backend': backend,
        })
    return '\n\n'.join(blocks)
