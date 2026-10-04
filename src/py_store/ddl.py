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

from .core import native as _native
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


def _pname(backend, logical):
    """逻辑名 → 本后端物理标识符（设计 §6）：SQL 后端恒 snake_case。

    保留名（I3）：``_id`` 物理主键、``__``/``^__`` 前缀哨兵列不翻译。
    唯一算法在 core::naming（禁自研）。对齐 nodejs-store/src/ddl.js::pname。
    """
    if not isinstance(logical, str) or logical == '':
        return logical
    if logical == '_id' or logical.startswith('__') or logical.startswith('^__'):
        return logical
    return _native.translate_name(logical, backend)


def _columns(defn, backend):
    """返回 [(name, sql_type, pk, auto)]，顺序：声明的字段（标量 / object·array JSON 列）→
    timestamps → __present；name 为该后端物理列名（snake_case）。"""
    i = _idx(backend)
    cols = []
    fields = defn.get('fields') or {}
    for name, fdef in fields.items():
        ftype = _declared_type(fdef)
        col = _pname(backend, name)
        if name == '_id':
            fdef = fdef if isinstance(fdef, dict) else {}
            if fdef.get('strategy') == 'autoincrement':
                cols.append((col, _ID_AUTO_TYPE[i], True, True))
            else:
                cols.append((col, _ID_TYPE[i], True, False))
            continue
        if ftype in _NON_COLUMN:
            # object/array → 单列 JSON 文本（同 core field_column_ref::Json）
            cols.append((col, _JSON_TYPE[i], False, False))
            continue
        if ftype not in _TYPES:
            raise ValueError(
                f'DDL 生成：字段 "{defn["name"]}.{name}" 类型 {ftype!r} 未知，'
                f'支持 {sorted(_TYPES)}')
        cols.append((col, _TYPES[ftype][i], False, False))
    if not any(c[2] for c in cols):
        raise ValueError(f'DDL 生成：schema "{defn["name"]}" 缺少 _id 字段')
    if defn.get('timestamps') is not False:
        for ts in _TIMESTAMP_FIELDS:
            col = _pname(backend, ts)
            if not any(c[0] == col for c in cols):
                cols.append((col, _TYPES['number'][i], False, False))
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
    table = _pname(backend, defn.get('collection') or defn.get('name'))
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
    table = _pname(backend, defn.get('collection') or defn.get('name'))
    for idx in defn.get('indexes') or []:
        if not isinstance(idx, dict):
            continue
        keys = idx.get('keys')
        if not isinstance(keys, dict) or not keys:
            continue
        unique = bool(idx.get('unique') or (idx.get('options') or {}).get('unique'))
        phys_keys = [_pname(backend, k) for k in keys]
        cols = ', '.join(
            f"{_q(backend, pk)} {'DESC' if v == -1 else 'ASC'}"
            for pk, v in zip(phys_keys, keys.values(), strict=True))
        name = 'idx_' + table + '_' + '_'.join(phys_keys)
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


# ══════════════════════════════════════════════════════════════════
# 声明式 schema 迁移（阶段 4；设计见 common-store/迁移设计文档-阶段4.md）
#
# 原则：diff 后端无关（纯函数）；SQL per-dialect 生成；**只产文本、不连库、不执行**
# （与 generate 同边界）；首批白名单外一律显式 Err（`MIGRATION_UNSUPPORTED`）——
# 破坏性变更绝不静默跳过，也绝不生成猜测语义的 SQL（实测 PG/MySQL 对收窄均不拦截，
# 拦截只能放在生成器）。
# ══════════════════════════════════════════════════════════════════

# 类型放宽映射（首批）：值域安全扩大的单向变更；跨大类（string↔数值等）不在映射内 → Err
_WIDEN = {
    'int': {'long', 'float', 'double'},
    'long': {'float', 'double'},
    'float': {'double'},
}


def _field_sql_type(backend, fdef):
    """字段 def → 列 SQL 类型（与 _columns 同源；object/array → JSON 文本列）"""
    i = _idx(backend)
    ftype = _declared_type(fdef)
    if ftype in _NON_COLUMN:
        return _JSON_TYPE[i]
    if ftype not in _TYPES:
        raise ValueError(
            f'迁移生成：字段类型 {ftype!r} 未知（支持 {sorted(_TYPES) + list(_NON_COLUMN)}）')
    return _TYPES[ftype][i]


def _sql_literal(v):
    """default 字面量 → SQL 文本（仅 JSON 标量；SQLite 禁非常量表达式 DEFAULT，N1 实测）"""
    if isinstance(v, bool):
        return 'TRUE' if v else 'FALSE'
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    raise ValueError(
        f'迁移生成：default 仅支持 JSON 标量字面量，收到 {type(v).__name__}')


def _archive_defn_of(defn):
    """主 def → 归档表 def（列 = 主列 + deletedAt；剔除 _id 自增策略；indexes 继承；
    对齐 core registry.rs::archive_defn）"""
    fields = dict(defn.get('fields') or {})
    fields['deletedAt'] = {'type': 'number'}
    idf = fields.get('_id')
    if isinstance(idf, dict):
        idf = {k: v for k, v in idf.items() if k != 'strategy'}
        fields['_id'] = idf
    arch = dict(defn)
    arch['name'] = defn['name'] + 'Deleted'
    arch['collection'] = (defn.get('collection') or defn['name']) + '_deleted'
    arch['fields'] = fields
    arch['timestamps'] = False
    arch['_isArchive'] = True
    return arch


def diff_defs(old_defn, new_defn):
    """新旧 schema def 对比 → {'changes': [...], 'errors': [...]}（后端无关，纯函数）。

    首批白名单：addColumn / widenColumn / addIndex（addTable 由 generate_migration 在
    old_defn 为 None 时处理）。白名单外变更记入 errors（调用方必须显式处理，禁静默）。
    """
    changes: list = []
    errors: list[str] = []
    old_fields = (old_defn or {}).get('fields') or {}
    new_fields = new_defn.get('fields') or {}

    if old_defn is None:
        return {'changes': [{'op': 'addTable', 'defn': new_defn}], 'errors': errors}

    # ── 字段：新增 / 放宽 / 删除（白名单外）──
    for name, nf in new_fields.items():
        if name == '_id':
            of = old_fields.get('_id')
            if of is not None and _declared_type(of) != _declared_type(nf):
                errors.append(f'_id 主键类型变更不支持（{_declared_type(of)} → {_declared_type(nf)}）')
            o_strat = of.get('strategy') if isinstance(of, dict) else None
            n_strat = nf.get('strategy') if isinstance(nf, dict) else None
            if o_strat != n_strat:
                errors.append(f'_id 主键策略变更不支持（{o_strat!r} → {n_strat!r}）')
            continue
        nf_type = _declared_type(nf)
        if name not in old_fields:
            if nf_type not in _TYPES and nf_type not in _NON_COLUMN:
                errors.append(f'新列 "{name}" 类型 {nf_type!r} 未知')
                continue
            changes.append({
                'op': 'addColumn', 'name': name,
                'sqlType': None,  # generate 侧按 backend 解析（保持 diff 后端无关）
                'field': nf if isinstance(nf, dict) else {'type': nf},
            })
            continue
        of = old_fields[name]
        of_type = _declared_type(of)
        if of_type == nf_type:
            continue  # 恒等：不上报（含 object 内嵌定义差异——首批不展开，见 N3）
        if nf_type in _WIDEN.get(of_type, ()):
            changes.append({
                'op': 'widenColumn', 'name': name,
                'from': of_type, 'to': nf_type,
            })
        else:
            errors.append(
                f'字段 "{name}" 类型 {of_type!r} → {nf_type!r} 非放宽变更'
                '（首批仅支持单向放宽：int→long/float/double、long→float/double、float→double）')
    for name in old_fields:
        if name not in new_fields:
            errors.append(f'删除字段 "{name}" 不支持（破坏性变更；请显式走数据迁移脚本）')

    # ── 索引：new 有 old 无 → addIndex ──
    def _index_keys(defn):
        out = []
        for idx in defn.get('indexes') or []:
            if isinstance(idx, dict) and isinstance(idx.get('keys'), dict) and idx['keys']:
                out.append(idx)
        return out

    old_idx = _index_keys(old_defn)
    for idx in _index_keys(new_defn):
        if idx not in old_idx:
            changes.append({'op': 'addIndex', 'index': idx})

    # collection 改名 = 建新表丢旧表（破坏性）→ Err
    old_coll = old_defn.get('collection') or old_defn.get('name')
    new_coll = new_defn.get('collection') or new_defn.get('name')
    if old_coll != new_coll:
        errors.append(f'collection 改名不支持（{old_coll!r} → {new_coll!r}；破坏性变更）')

    return {'changes': changes, 'errors': errors}


def generate_migration(backend, old_defn, new_defn):
    """新旧 schema def → 迁移 SQL 文本列表（per-dialect；只产文本、不执行）。

    顺序固定：建表 → 加列（主表 + 归档表跟随）→ 放宽类型 → 加索引。
    白名单外 / SQLite 类型变更 / 不支持的后端 → 显式 ValueError（含 MIGRATION_UNSUPPORTED）。
    """
    if backend not in _BACKENDS:
        raise ValueError(f'迁移生成：不支持的后端 {backend!r}（支持 {list(_BACKENDS)}）')
    plan = diff_defs(old_defn, new_defn)
    if plan['errors']:
        raise ValueError(
            'MIGRATION_UNSUPPORTED: ' + '；'.join(plan['errors']) +
            '（首批白名单：加表/加列/类型放宽/加索引；破坏性变更请走显式数据迁移脚本）')

    table = _pname(backend, new_defn.get('collection') or new_defn.get('name'))
    stmts = []
    # 固定生成序：加列 → 放宽类型 → 加索引（addTable 与其余 op 互斥，天然居首）
    order = {'addColumn': 0, 'widenColumn': 1, 'addIndex': 2}
    for ch in sorted(plan['changes'], key=lambda c: order.get(c['op'], 9)):
        op = ch['op']
        if op == 'addTable':
            stmts.append(_create_table(new_defn, backend))
            stmts.extend(_index_stmts(new_defn, backend))
            # 归档表跟随（addTable 一次性建全：列 = 主列 + deletedAt，剔除自增策略）
            arch = _archive_defn_of(new_defn)
            stmts.append(_create_table(arch, backend))
            stmts.extend(_index_stmts(arch, backend))
        elif op == 'addColumn':
            name = ch['name']
            field = ch['field']
            ftype = _declared_type(field)
            col_type = _field_sql_type(backend, field)
            default = field.get('default') if isinstance(field, dict) else None
            col_sql = f'{_q(backend, _pname(backend, name))} {col_type}'
            if default is not None:
                if ftype in _NON_COLUMN:
                    raise ValueError(
                        f'MIGRATION_UNSUPPORTED: 新列 "{name}"（object/array JSON 列）不支持 DEFAULT'
                        '（存量行缺失语义由 __present 哨兵表达；default 仅影响新写入）')
                col_sql += ' DEFAULT ' + _sql_literal(default)
            stmts.append(f'ALTER TABLE {_q(backend, table)} ADD COLUMN {col_sql}')
            # 归档表跟随：不同步则归档 insert（显式拷贝全列）因缺列报错
            stmts.append(f'ALTER TABLE {_q(backend, table + "_deleted")} ADD COLUMN {col_sql}')
        elif op == 'widenColumn':
            name = ch['name']
            nf = new_defn['fields'][name]
            col = _pname(backend, name)
            col_type = _field_sql_type(backend, nf)
            if backend == 'sqlite':
                # N1 实测：SQLite 无 ALTER COLUMN（官方重建表 12 步流程），首批显式拒绝
                raise ValueError(
                    f'MIGRATION_UNSUPPORTED: SQLite 不支持类型变更 '
                    f'（{ch["from"]} → {ch["to"]} 需重建表）；加列/加索引/加表已支持')
            if backend == 'mysql':
                stmts.append(f'ALTER TABLE {_q(backend, table)} MODIFY COLUMN {_q(backend, col)} {col_type}')
            else:
                stmts.append(
                    f'ALTER TABLE {_q(backend, table)} ALTER COLUMN {_q(backend, col)} '
                    f'TYPE {col_type} USING {_q(backend, col)}::{col_type}')
        elif op == 'addIndex':
            stmts.extend(_index_stmts({'collection': table, 'indexes': [ch['index']]}, backend))
        else:  # 防御：diff 层不会产出其他 op
            raise ValueError(f'MIGRATION_UNSUPPORTED: 未知变更 {op!r}')
    return stmts
