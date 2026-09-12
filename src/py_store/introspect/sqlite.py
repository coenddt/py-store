"""SQLite introspection（驱动：aiosqlite）

只发 ``sqlite_master`` / ``PRAGMA`` 只读查询（铁律 6：绝不写 DDL 回库），产出规范化
行 JSON，交 core ``schema_from_rows`` 做纯映射（本模块不做任何 schema 推断）。
对齐 ``nodejs-store/src/introspect/sqlite.js``。
"""


def _quote(name):
    """PRAGMA 不支持参数化，需内联表名；标识符来自 sqlite_master（非用户输入），并做引号转义"""
    return '"' + str(name).replace('"', '""') + '"'


async def _all(db, sql):
    cur = await db.execute(sql)
    try:
        return await cur.fetchall()
    finally:
        await cur.close()


async def introspect(db, options=None):
    if db is None or not hasattr(db, 'execute'):
        raise TypeError('sqlite introspection 需要 aiosqlite 的 Connection 实例')

    tables = []
    columns = []
    fks = []
    indexes = []

    # attached db 过滤：PRAGMA database_list 校验库名存在（main/temp/ATTACH 的库名），
    # 表清单改从 `<db>.sqlite_master` 读取；显式库名作为 namespace 透出到 def。
    database = (options or {}).get('database')
    master_from = 'sqlite_master'
    if database is not None:
        known = {r[1] for r in await _all(db, 'PRAGMA database_list')}
        if database not in known:
            raise RuntimeError(
                f'SQLite attached db 不存在: {database}（当前 attached: {", ".join(sorted(known))}）')
        master_from = f'{_quote(database)}.sqlite_master'

    table_rows = await _all(
        db, f"SELECT name FROM {master_from} WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")

    for (name,) in table_rows:
        tables.append({'name': name, 'namespace': database}
                      if database is not None else {'name': name})

        for c in await _all(db, f'PRAGMA table_info({_quote(name)})'):
            # (cid, name, type, notnull, dflt_value, pk)
            columns.append({
                'table': name,
                'name': c[1],
                'type': c[2] or '',
                'notnull': 1 if c[3] else 0,
                'pk': 1 if c[5] else 0,
            })

        for f in await _all(db, f'PRAGMA foreign_key_list({_quote(name)})'):
            # (id, seq, table, from, to, on_update, on_delete, match)
            fks.append({
                'table': name,
                'column': f[3],
                'refTable': f[2],
                'refColumn': f[4] or '_id',
            })

        for idx in await _all(db, f'PRAGMA index_list({_quote(name)})'):
            # (seq, name, unique, origin, partial)
            idx_name, idx_unique, origin = idx[1], idx[2], idx[3]
            if origin == 'pk':
                continue  # 主键索引不重复登记
            info = await _all(db, f'PRAGMA index_info({_quote(idx_name)})')
            indexes.append({
                'table': name,
                'name': idx_name,
                'columns': [i[2] for i in info],
                'unique': 1 if idx_unique else 0,
            })

    return {'tables': tables, 'columns': columns, 'fks': fks, 'indexes': indexes}
