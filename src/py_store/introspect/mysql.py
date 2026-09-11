"""MySQL introspection（驱动：asyncmy）

只发 ``information_schema`` 只读查询（铁律 6：绝不写 DDL 回库），产出规范化行 JSON，
交 core ``schema_from_rows`` 做纯映射。建议使用只读账号；库名取当前连接的 ``DATABASE()``。
对齐 ``nodejs-store/src/introspect/mysql.js``。
"""

from ..executors.mysql import acquire as _acquire

_TABLES = """
  SELECT table_name AS name
  FROM information_schema.tables
  WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE'
  ORDER BY table_name"""

_COLUMNS = """
  SELECT table_name  AS "table",
         column_name AS name,
         column_type AS type,
         is_nullable AS nullable,
         column_key  AS columnKey
  FROM information_schema.columns
  WHERE table_schema = DATABASE()
  ORDER BY table_name, ordinal_position"""

# MySQL 8 中 `column` 是保留字，别名需反引号
_FKS = """
  SELECT table_name            AS "table",
         column_name           AS `column`,
         referenced_table_name AS "refTable",
         referenced_column_name AS "refColumn"
  FROM information_schema.key_column_usage
  WHERE table_schema = DATABASE() AND referenced_table_name IS NOT NULL
  ORDER BY table_name, ordinal_position"""

_INDEXES = """
  SELECT table_name  AS "table",
         index_name  AS name,
         non_unique  AS nonUnique,
         seq_in_index AS seq,
         column_name AS `column`
  FROM information_schema.statistics
  WHERE table_schema = DATABASE()
  ORDER BY table_name, index_name, seq_in_index"""


def _group_indexes(rows):
    """把 ``{table,name,nonUnique,column}`` 行按索引名归并出 columns 数组"""
    by_key = {}
    for r in rows:
        key = f"{r['table']}::{r['name']}"
        entry = by_key.get(key)
        if entry is None:
            entry = {
                'table': r['table'],
                'name': r['name'],
                'columns': [],
                'unique': 0 if int(r['nonUnique']) else 1,
            }
            by_key[key] = entry
        entry['columns'].append(r['column'])
    return list(by_key.values())


async def introspect(driver, options=None):
    if driver is None:
        raise TypeError('mysql introspection 需要 asyncmy 的连接或连接池')

    from asyncmy.cursors import DictCursor

    async def run(sql):
        async with _acquire(driver) as conn:
            async with conn.cursor(DictCursor) as cur:
                await cur.execute(sql)
                return await cur.fetchall()

    tables = await run(_TABLES)
    columns = await run(_COLUMNS)
    fks = await run(_FKS)
    index_rows = await run(_INDEXES)

    return {
        'tables': list(tables),
        'columns': [
            {
                'table': c['table'],
                'name': c['name'],
                'type': c['type'] or '',
                'notnull': 1 if c['nullable'] == 'NO' else 0,
                'pk': 1 if c['columnKey'] == 'PRI' else 0,
            }
            for c in columns
        ],
        'fks': list(fks),
        'indexes': _group_indexes(index_rows),
    }
