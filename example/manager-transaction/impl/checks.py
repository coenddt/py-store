"""manager-transaction 场景 raw 断言实现（expect.kind="raw" / 裸步骤 fn）。

py_store 只允许在函数体内 import：本模块被 harness 在其插入 sys.path 之前 import，
顶层 import py_store 会在 CLI 直跑（python impl/harness.py）时失败。
"""

# 进程级：上一次 autoincrement insert 生成的 _id（check_autoincrement_increments 用）
_last_auto_id = None


def _auto_def():
    return {
        "name": "AutoOrder",
        "collection": "auto_orders",
        "idPrefix": "",
        "timestamps": False,
        "read": ["admin", "seller", "buyer"],
        "write": ["admin", "seller"],
        "fields": {
            "_id": {"type": "int", "strategy": "autoincrement"},
            "orderNo": {"type": "string"},
            "amount": {"type": "float"},
        },
        "relations": {},
        "computes": {},
    }


async def register_auto_schema(h, expect):
    """注册 autoincrement 探针 schema（幂等：已注册则跳过）。
    对应物理表 auto_orders 已在 ddl/*.sql 手工预置自增主键列（阶段2 落地前引擎不会生成）。"""
    from py_store import schema as sc

    try:
        sc.register(_auto_def())
        return True, 'AutoOrder 已注册'
    except Exception as e:  # noqa: BLE001
        if 'AutoOrder' in sc.list() or '已注册' in str(e) or 'exists' in str(e).lower():
            return True, f'AutoOrder 已存在（{e}）'
        return False, f'AutoOrder 注册失败: {e}'


async def check_autoincrement_started(h, expect):
    """insert（无 _id）后：返回 _id 必须是 int（数据库自增列赋值）。"""
    global _last_auto_id
    result = h.result
    if not isinstance(result, dict):
        return False, f'insert 返回非 dict: {result!r}'
    val = result.get('_id')
    if not isinstance(val, int):
        return False, f'_id 非 int（目标态=数据库自增赋值）: {val!r}'
    _last_auto_id = val
    return True, f'首个自增 _id={val}'


async def check_autoincrement_increments(h, expect):
    """第二次 insert 后：_id 为 int 且严格大于上一次（连续自增）。"""
    global _last_auto_id
    result = h.result
    if not isinstance(result, dict):
        return False, f'insert 返回非 dict: {result!r}'
    val = result.get('_id')
    if not isinstance(val, int):
        return False, f'_id 非 int: {val!r}'
    if _last_auto_id is None:
        return False, '未先执行 check_autoincrement_started'
    if val <= _last_auto_id:
        return False, f'_id 未递增: 上次 {_last_auto_id}，本次 {val}'
    _last_auto_id = val
    return True, f'自增连续: {_last_auto_id} < {val}'


async def check_ddl_create_index(h, expect):
    """ddl.generate 必须为 schema.indexes 产出 CREATE [UNIQUE] INDEX。
    SQL 后端查自身方言；Mongo 后端无 DDL，退而要求三个 SQL 方言全部产出。"""
    from py_store import ddl as ddl_mod

    backends = ['mysql', 'postgres', 'sqlite'] if h.backend == 'mongodb' else [h.backend]
    bad = []
    for bk in backends:
        # py 的 ddl.generate 返回多表拼接文本（'\n\n' 分隔），切分为语句数组
        sqls = str(ddl_mod.generate(bk, names=['Order'])).split('\n\n')
        hits = [s for s in sqls if 'CREATE INDEX' in s.upper() or 'CREATE UNIQUE INDEX' in s.upper()]
        if not hits:
            bad.append(f'{bk}: 0 条 CREATE INDEX（共 {len(sqls)} 条语句）')
        elif h.backend != 'mongodb' and 'orders' not in ' '.join(hits).lower():
            bad.append(f'{bk}: CREATE INDEX 未落在 orders 表: {hits}')
    if bad:
        return False, '；'.join(bad)
    return True, f'{backends} 方言均产出 CREATE INDEX'


# ══════════════════════════════════════════════════════════════════
# 阶段 4：声明式迁移 e2e（T4 组）。设计见 common-store/迁移设计文档-阶段4.md：
# 纯函数 diff/generate + 宿主执行；用例内 ALTER 后必须复原（用例可复跑）。
# ══════════════════════════════════════════════════════════════════

import copy as _copy
import json as _json
from pathlib import Path as _Path

_SCEN = _Path(__file__).resolve().parent.parent
_ORDER_DEFS = None


def _order_def():
    """场景 Order schema def（从 schema.json 读，避免与 register_all 的镜像耦合）"""
    global _ORDER_DEFS
    if _ORDER_DEFS is None:
        _ORDER_DEFS = {d['name']: d for d in
                       _json.loads((_SCEN / 'schema.json').read_text(encoding='utf-8'))}
    return _ORDER_DEFS['Order']


async def _exec_stmts(h, stmts):
    """按后端执行 DDL 文本列表（harness 手工 DDL 同款执行路径）"""
    if h.backend == 'mongodb':
        return
    if h.backend == 'mysql':
        async with h.driver.acquire() as conn:
            async with conn.cursor() as cur:
                for s in stmts:
                    await cur.execute(s)
        return
    for s in stmts:
        await h.driver.execute(s)
    if h.backend == 'sqlite':
        await h.driver.commit()


def _order_with_channel(channel_field):
    """Order def + channel 列"""
    d = _copy.deepcopy(_order_def())
    d['fields']['channel'] = channel_field
    return d


async def migrate_add_column(h, step):
    """T4-01：加列 → 存量行缺失语义 / 新插入行生效 / 归档跟随；末尾 DROP COLUMN 复原。"""
    from py_store import ddl as ddl_mod, store
    old_def = _copy.deepcopy(_order_def())
    # 无 default 列：存量行读出为「缺失」（__present 哨兵无 token）；default 只影响新写入
    new_def = _order_with_channel({'type': 'string'})
    stmts = ddl_mod.generate_migration(h.backend, old_def, new_def)
    await _exec_stmts(h, stmts)
    from py_store import schema as sc
    sc.register(new_def)  # 契约同步：新列过写白名单（否则 channel 被剔除）
    try:
        # 旧行（seed o1）：键缺失（$exists:false 命中）
        n = await store.count('Order', {'channel': {'$exists': False}})
        if n != 2:
            return False, f'存量行缺失语义断言失败：$exists:false 命中 {n}（期望 2）'
        # 新插入行：显式值生效
        await store.insert('Order', {'_id': 'oT4', 'orderNo': 'T4001',
                                     'buyerId': 'u1', 'status': 'paid', 'channel': 'app'})
        doc = await store.query_one('Order($condition:@c){_id, channel}',
                                    {'c': {'_id': 'oT4'}})
        if not doc or doc.get('channel') != 'app':
            return False, f'新插入行 channel 断言失败: {doc!r}'
        # 归档跟随：remove 不因归档表缺列报错
        r = await store.remove('Order', {'_id': 'oT4'})
        if r.get('archivedCount') != 1:
            return False, f'归档跟随断言失败: {r!r}'
        return True, f'加列迁移 + 存量缺失语义 + 归档跟随通过（{h.backend}，{len(stmts)} 条语句）'
    finally:
        await _exec_stmts(h, [
            f'ALTER TABLE {_tb(h, "orders")} DROP COLUMN {_col(h, "channel")}',
            f'ALTER TABLE {_tb(h, "orders_deleted")} DROP COLUMN {_col(h, "channel")}',
        ])
        sc.register(_order_def())  # 契约复原


def _tb(h, t):
    return f'`{t}`' if h.backend == 'mysql' else f'"{t}"'


def _col(h, c):
    return _tb(h, c)


async def migrate_add_unique_index(h, step):
    """T4-02：加唯一索引迁移 → 重复值显式报错；末尾 DROP INDEX 复原。

    载体用 status unique 索引（库里尚不存在；orderNo/buyerId 已由阶段3 的建表
    闭环落过，不能作为迁移目标）。"""
    from py_store import ddl as ddl_mod, store
    old_def = _copy.deepcopy(_order_def())
    new_def = _copy.deepcopy(_order_def())
    new_def['indexes'] = list(new_def.get('indexes') or []) + [{'keys': {'status': 1}, 'unique': True}]
    stmts = ddl_mod.generate_migration(h.backend, old_def, new_def)
    if len(stmts) != 1 or 'idx_orders_status' not in stmts[0]:
        return False, f'加索引迁移语句断言失败: {stmts!r}'
    await _exec_stmts(h, stmts)
    try:
        dup = {'_id': 'oDup', 'orderNo': 'B900', 'buyerId': 'u1', 'status': 'paid'}
        try:
            await store.insert('Order', dup)
            return False, 'unique 索引落库后重复 status 未报错'
        except Exception as e:  # noqa: BLE001
            return True, f'unique 索引生效（{h.backend}）: {type(e).__name__}'
    finally:
        await _exec_stmts(h, [f'DROP INDEX {_tb(h, "idx_orders_status")}']
                          if h.backend != 'mysql' else
                          ['DROP INDEX idx_orders_status ON `orders`'])


async def migrate_widen(h, step):
    """T4-03：int→float 放宽（MySQL/PG 生效；SQLite 显式 MIGRATION_UNSUPPORTED）。"""
    from py_store import ddl as ddl_mod, store
    # 用 Inventory.stock（int）做放宽载体
    inv_old = {'name': 'Inventory', 'collection': 'inventories', 'idPrefix': 'inv',
               'timestamps': False,
               'fields': {'_id': {'type': 'string'}, 'productId': {'type': 'string'},
                          'warehouse': {'type': 'string'}, 'stock': {'type': 'int'},
                          'warnLine': {'type': 'int'}, 'createdBy': {'type': 'string'}}}
    inv_new = _copy.deepcopy(inv_old)
    inv_new['fields']['stock'] = {'type': 'float'}
    if h.backend == 'sqlite':
        try:
            ddl_mod.generate_migration('sqlite', inv_old, inv_new)
            return False, 'SQLite 类型变更未拒绝'
        except ValueError as e:
            if 'MIGRATION_UNSUPPORTED' not in str(e):
                return False, f'SQLite 拒绝文案缺语义码: {e}'
            return True, 'SQLite 显式拒绝类型变更（MIGRATION_UNSUPPORTED）'
    stmts = ddl_mod.generate_migration(h.backend, inv_old, inv_new)
    await _exec_stmts(h, stmts)
    try:
        from py_store.crud.exec import _exec
        await _exec({'kind': 'insertOne', 'collection': 'inventories', 'source': 'default',
                     'doc': {'_id': 'invT4', 'productId': 'p1', 'warehouse': 'w9',
                             'stock': 3.5, 'warnLine': 1, '__present': ',_id,productId,warehouse,stock,warnLine,'}})
        rows = await store.query('Inventory($condition:@c){_id, stock}',
                                 {'c': {'_id': 'invT4'}})
        val = rows[0].get('stock') if rows else None
        if val != 3.5:
            return False, f'放宽后 float 写读断言失败: {rows!r}'
        return True, f'int→float 放宽生效（{h.backend}）'
    finally:
        await _exec_stmts(h, [
            f'DELETE FROM {_tb(h, "inventories")} WHERE {_col(h, "_id")} = \'invT4\''])


async def migrate_destructive(h, step):
    """T4-04：破坏性变更（删列/收窄/改名/主键变更）→ diff errors 全命中（纯函数）。"""
    from py_store import ddl as ddl_mod
    old_def = _copy.deepcopy(_order_def())
    dropped = _copy.deepcopy(old_def)
    dropped['fields'].pop('status')
    plan = ddl_mod.diff_defs(old_def, dropped)
    if not any('删除字段' in e for e in plan['errors']):
        return False, f'删列未被拒: {plan["errors"]}'

    narrowed = _copy.deepcopy(old_def)
    narrowed['fields']['status'] = {'type': 'int'}  # string → int 跨大类
    plan = ddl_mod.diff_defs(old_def, narrowed)
    if not any('非放宽' in e for e in plan['errors']):
        return False, f'收窄未被拒: {plan["errors"]}'

    renamed = _copy.deepcopy(old_def)
    renamed['collection'] = 'orders_v2'
    plan = ddl_mod.diff_defs(old_def, renamed)
    if not any('collection 改名' in e for e in plan['errors']):
        return False, f'改名未被拒: {plan["errors"]}'

    id_changed = _copy.deepcopy(old_def)
    id_changed['fields']['_id'] = {'type': 'int', 'strategy': 'autoincrement'}
    plan = ddl_mod.diff_defs(old_def, id_changed)
    if not plan['errors']:
        return False, '主键变更未被拒'
    return True, f'破坏性变更全部显式拒绝（{len(plan["errors"])} 类）'


async def migrate_add_table(h, step):
    """T4-05（sqlite 内存库）：old=None → 建表+索引+归档；注册可读写自增；无需复原。"""
    from py_store import ddl as ddl_mod, schema as sc, store
    fresh = {'name': 'MigOrder', 'collection': 'mig_orders', 'idPrefix': '', 'timestamps': False,
             'read': ['admin', 'seller', 'buyer'], 'write': ['admin', 'seller'],
             'fields': {'_id': {'type': 'int', 'strategy': 'autoincrement'},
                        'orderNo': {'type': 'string'}, 'amount': {'type': 'float'}},
             'relations': {}, 'computes': {},
             'indexes': [{'keys': {'orderNo': 1}, 'unique': True}]}
    stmts = ddl_mod.generate_migration('sqlite', None, fresh)
    if len(stmts) != 4:
        return False, f'新表语句数断言失败: {len(stmts)}'
    await _exec_stmts(h, stmts)
    sc.register(fresh)
    r1 = await store.insert('MigOrder', {'orderNo': 'M1', 'amount': 1.5})
    if not isinstance(r1.get('_id'), int):
        return False, f'新表自增 _id 断言失败: {r1!r}'
    r2 = await store.remove('MigOrder', {'orderNo': 'M1'})
    if r2.get('archivedCount') != 1:
        return False, f'新表归档跟随断言失败: {r2!r}'
    return True, '新表迁移 + 自增 + 归档跟随通过（sqlite 内存库）'
