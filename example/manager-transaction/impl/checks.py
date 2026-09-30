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
