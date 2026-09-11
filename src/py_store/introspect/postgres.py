"""PostgreSQL introspection（驱动：asyncpg）

只发 ``information_schema`` / ``pg_catalog`` 只读查询（铁律 6：绝不写 DDL 回库），
产出规范化行 JSON，交 core ``schema_from_rows`` 做纯映射。建议使用只读账号。
对齐 ``nodejs-store/src/introspect/postgres.js``。
"""

_TABLES = """
  SELECT table_name AS name
  FROM information_schema.tables
  WHERE table_schema = $1 AND table_type = 'BASE TABLE'
  ORDER BY table_name"""

_COLUMNS = """
  SELECT c.table_name AS "table",
         c.column_name AS name,
         c.data_type   AS type,
         c.is_nullable AS nullable,
         CASE WHEN pk.column_name IS NULL THEN 0 ELSE 1 END AS pk
  FROM information_schema.columns c
  LEFT JOIN (
    SELECT kcu.table_schema, kcu.table_name, kcu.column_name
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON kcu.constraint_name = tc.constraint_name
     AND kcu.table_schema = tc.table_schema
    WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = $1
  ) pk
    ON pk.table_schema = c.table_schema
   AND pk.table_name = c.table_name
   AND pk.column_name = c.column_name
  WHERE c.table_schema = $1
  ORDER BY c.table_name, c.ordinal_position"""

_FKS = """
  SELECT src.relname AS "table",
         sa.attname  AS column,
         ref.relname AS "refTable",
         ra.attname  AS "refColumn"
  FROM pg_constraint con
  JOIN pg_class src ON src.oid = con.conrelid
  JOIN pg_class ref ON ref.oid = con.confrelid
  JOIN pg_namespace ns ON ns.oid = src.relnamespace
  JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
  JOIN LATERAL unnest(con.confkey) WITH ORDINALITY AS f(attnum, ord) ON f.ord = k.ord
  JOIN pg_attribute sa ON sa.attrelid = con.conrelid AND sa.attnum = k.attnum
  JOIN pg_attribute ra ON ra.attrelid = con.confrelid AND ra.attnum = f.attnum
  WHERE con.contype = 'f' AND ns.nspname = $1
  ORDER BY src.relname, k.ord"""

_INDEXES = """
  SELECT t.relname AS "table",
         i.relname AS name,
         CASE WHEN ix.indisunique THEN 1 ELSE 0 END AS unique,
         a.attname AS column
  FROM pg_index ix
  JOIN pg_class t ON t.oid = ix.indrelid
  JOIN pg_class i ON i.oid = ix.indexrelid
  JOIN pg_namespace ns ON ns.oid = t.relnamespace
  JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(ix.indkey)
  WHERE ns.nspname = $1 AND NOT ix.indisprimary
  ORDER BY t.relname, i.relname"""


def _group_indexes(rows):
    """把 ``{table,name,unique,column}`` 行按索引名归并出 columns 数组"""
    by_key = {}
    for r in rows:
        key = f"{r['table']}::{r['name']}"
        entry = by_key.get(key)
        if entry is None:
            entry = {
                'table': r['table'],
                'name': r['name'],
                'columns': [],
                'unique': int(r['unique']),
            }
            by_key[key] = entry
        entry['columns'].append(r['column'])
    return list(by_key.values())


async def introspect(driver, options=None):
    if driver is None or not hasattr(driver, 'fetch'):
        raise TypeError('postgres introspection 需要 asyncpg 的连接或连接池')

    schema = (options or {}).get('schema', 'public')

    tables = await driver.fetch(_TABLES, schema)
    columns = await driver.fetch(_COLUMNS, schema)
    fks = await driver.fetch(_FKS, schema)
    index_rows = await driver.fetch(_INDEXES, schema)

    return {
        'tables': [dict(r) for r in tables],
        'columns': [
            {
                'table': c['table'],
                'name': c['name'],
                'type': c['type'] or '',
                'notnull': 1 if c['nullable'] == 'NO' else 0,
                'pk': int(c['pk']) or 0,
            }
            for c in columns
        ],
        'fks': [dict(r) for r in fks],
        'indexes': _group_indexes(index_rows),
    }
