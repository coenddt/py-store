"""py-store 多 db 联合压力测试 —— 测试 py-store 宿主层产品

四库并载（MongoDB / MySQL / PostgreSQL / SQLite）：
  - StressUser(mongo_e2e) --orders--> StressOrder(mysql_e2e)  [联邦跨源]
  - StressUser(mongo_e2e) --invoices--> StressInvoice(pg_e2e) [联邦跨源]
  - StressLog(default=sqlite) 独立写读

每 worker 每轮 5 个操作：
  1. query_federated 跨 3 源联合查询（多 db 联合核心）
  2. SQLite 写 insert(StressLog)
  3. PostgreSQL 读 count(StressInvoice)
  4. MongoDB 读 query(StressUser)
  5. MySQL 写 update(StressOrder, $inc)

用法: python stress.py [--workers N] [--rounds N]
表名后缀固定 'py'，与 nodejs-store('js') / rust-store('rs') 的压测表隔离。
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from py_store import executors, init, permission, schema as _sc, store  # noqa: E402

import aiosqlite  # noqa: E402
import asyncmy  # noqa: E402
import asyncpg  # noqa: E402
from pymongo import AsyncMongoClient  # noqa: E402


def _arg(name, default):
    argv = sys.argv[1:]
    if f'--{name}' in argv:
        return int(argv[argv.index(f'--{name}') + 1])
    return default


WORKERS = _arg('workers', 8)
ROUNDS = _arg('rounds', 200)
SUFFIX = 'py'  # 表隔离后缀（本仓库专用）

# 表 / collection / schema 名后缀：多仓库同时压测时隔离数据（避免 DDL 互踩）
def T(base):
    return base + SUFFIX


def S(base):
    return base + SUFFIX[:1].upper() + SUFFIX[1:]


MYSQL_URI = os.environ.get('MYSQL_URI', 'mysql://e2e:e2e123@127.0.0.1:3306/mongo_store_e2e?charset=utf8mb4')
PG_URI = os.environ.get('PG_URI', 'postgres://e2e:e2e123@127.0.0.1:5432/mongo_store_e2e')
MONGO_URI = os.environ.get('MONGO_URI', 'mongodb://127.0.0.1:27017/mongo_store_e2e')

SEED_USERS = 30
SEED_PER_USER = 2

USER_SCHEMA = S('StressUser')
FED_GQL = f'{USER_SCHEMA}($condition:@c0){{_id, name, orders{{code, amount}}, invoices{{title, value}}}}'
FED_PARAMS = {'c0': {}}


def _summarize(latencies):
    if not latencies:
        return {'ops': 0, 'total_ms': 0, 'p50_ms': 0, 'p95_ms': 0, 'max_ms': 0, 'avg_ms': 0}
    s = sorted(latencies)

    def percentile(p):
        idx = min(len(s) - 1, max(0, int(p * len(s)) - 1))
        return s[idx]

    return {
        'ops': len(s),
        'total_ms': round(sum(s)),
        'p50_ms': percentile(0.5),
        'p95_ms': percentile(0.95),
        'max_ms': s[-1],
        'avg_ms': round(sum(s) / len(s), 2),
    }


def _parse_mysql_uri(uri):
    from urllib.parse import unquote, urlparse
    u = urlparse(uri)
    return {
        'host': u.hostname or '127.0.0.1',
        'port': u.port or 3306,
        'user': unquote(u.username or ''),
        'password': unquote(u.password or ''),
        'db': (u.path or '/').lstrip('/'),
    }


async def main():
    permission.set_context(None)

    # 1. 连接四库
    client = AsyncMongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
    mongo_db = client.get_default_database()
    m_pool = await asyncmy.create_pool(autocommit=True, **_parse_mysql_uri(MYSQL_URI))
    pg_pool = await asyncpg.create_pool(dsn=PG_URI)
    sqlite_conn = await aiosqlite.connect(':memory:')
    sqlite_lock = asyncio.Lock()  # SQLite 单连接串行化

    # 2. 建表 / 清数据（幂等）
    await mongo_db[T('stress_users')].delete_many({})
    async with m_pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(f'DROP TABLE IF EXISTS {T("stress_orders")}')
        await cur.execute(
            f'CREATE TABLE {T("stress_orders")} (_id VARCHAR(64) NOT NULL, `userId` VARCHAR(64), '
            '`code` VARCHAR(255), `amount` DOUBLE, PRIMARY KEY (_id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4')
    await pg_pool.execute(f'DROP TABLE IF EXISTS {T("stress_invoices")}')
    await pg_pool.execute(
        f'CREATE TABLE {T("stress_invoices")} (_id TEXT PRIMARY KEY, "userId" TEXT, "title" TEXT, "value" DOUBLE PRECISION)')
    await sqlite_conn.execute(f'DROP TABLE IF EXISTS {T("stress_logs")}')
    await sqlite_conn.execute(f'CREATE TABLE {T("stress_logs")} (_id TEXT PRIMARY KEY, msg TEXT, level TEXT)')
    await sqlite_conn.commit()

    # 3. schema 注册（跨源 relation = 联邦路径）
    _sc.register({
        'name': S('StressUser'), 'collection': T('stress_users'), 'idPrefix': 'u_', 'datasource': 'mongo_e2e',
        'timestamps': False,
        'fields': {'name': {'type': 'string'}},
        'relations': {
            'orders': {'model': S('StressOrder'), 'type': 'many', 'localField': '_id', 'foreignField': 'userId'},
            'invoices': {'model': S('StressInvoice'), 'type': 'many', 'localField': '_id', 'foreignField': 'userId'},
        },
    })
    _sc.register({
        'name': S('StressOrder'), 'collection': T('stress_orders'), 'idPrefix': 'o_', 'datasource': 'mysql_e2e',
        'timestamps': False,
        'fields': {'userId': {'type': 'string'}, 'code': {'type': 'string'}, 'amount': {'type': 'number'}},
        'relations': {},
    })
    _sc.register({
        'name': S('StressInvoice'), 'collection': T('stress_invoices'), 'idPrefix': 'v_', 'datasource': 'pg_e2e',
        'timestamps': False,
        'fields': {'userId': {'type': 'string'}, 'title': {'type': 'string'}, 'value': {'type': 'number'}},
        'relations': {},
    })
    _sc.register({
        'name': S('StressLog'), 'collection': T('stress_logs'), 'idPrefix': 'l_', 'datasource': 'default',
        'timestamps': False,
        'fields': {'msg': {'type': 'string'}, 'level': {'type': 'string'}},
        'relations': {},
    })

    # 4. init 四源
    await init({
        'mongo_e2e': mongo_db,
        'mysql_e2e': executors.create_connection('mysql', m_pool),
        'pg_e2e': executors.create_connection('postgres', pg_pool),
        'default': executors.create_connection('sqlite', sqlite_conn),
    })

    # 5. 预热（种子数据）
    order_ids = []
    for u in range(SEED_USERS):
        user = await store.insert(S('StressUser'), {'name': f'u_{u}'})
        for i in range(SEED_PER_USER):
            o = await store.insert(S('StressOrder'), {'userId': user['_id'], 'code': f'c_{u}_{i}', 'amount': 1})
            order_ids.append(o['_id'])
            await store.insert(S('StressInvoice'), {'userId': user['_id'], 'title': f'v_{u}_{i}', 'value': u + i})
    print(f'[prewarm] users={SEED_USERS} orders={len(order_ids)} invoices={SEED_USERS * SEED_PER_USER} ready', flush=True)

    # 6. 冒烟：跨 3 源联邦一次，验证链路
    smoke = await store.query_federated(FED_GQL, FED_PARAMS)
    if not smoke or not smoke[0].get('orders') or not smoke[0].get('invoices'):
        raise RuntimeError(f'联邦冒烟失败: 结果形状异常 {json.dumps((smoke[0] if smoke else {}), ensure_ascii=False)[:200]}')
    print(f"[smoke] federation ok: {len(smoke)} users, sample orders={len(smoke[0]['orders'])} invoices={len(smoke[0]['invoices'])}", flush=True)

    # 7. 并发压测
    all_times = []
    fed_times = []
    errors_by_type = {}

    def record_error(op, e):
        key = f'{op}: {e}'
        errors_by_type[key] = errors_by_type.get(key, 0) + 1

    async def worker(w):
        for r in range(ROUNDS):
            # op1: 联合查询（跨 Mongo+MySQL+PG）
            t0 = time.perf_counter()
            try:
                await store.query_federated(FED_GQL, FED_PARAMS)
                all_times.append((time.perf_counter() - t0) * 1000)
                fed_times.append((time.perf_counter() - t0) * 1000)
            except Exception as e:
                record_error('federated', e)
            # op2: SQLite 写
            t0 = time.perf_counter()
            try:
                async with sqlite_lock:
                    await store.insert(S('StressLog'), {'msg': f'w{w}_r{r}', 'level': 'info'})
                all_times.append((time.perf_counter() - t0) * 1000)
            except Exception as e:
                record_error('sqlite-insert', e)
            # op3: PG 读
            t0 = time.perf_counter()
            try:
                await store.count(S('StressInvoice'), {})
                all_times.append((time.perf_counter() - t0) * 1000)
            except Exception as e:
                record_error('pg-count', e)
            # op4: Mongo 读
            t0 = time.perf_counter()
            try:
                await store.query(f'{S("StressUser")}($condition:@c0){{_id, name}}',
                                  {'c0': {'name': f'u_{(w + r) % SEED_USERS}'}})
                all_times.append((time.perf_counter() - t0) * 1000)
            except Exception as e:
                record_error('mongo-query', e)
            # op5: MySQL 写
            t0 = time.perf_counter()
            try:
                oid = order_ids[(w * ROUNDS + r) % len(order_ids)]
                await store.update(S('StressOrder'), {'_id': oid}, {'$inc': {'amount': 1}})
                all_times.append((time.perf_counter() - t0) * 1000)
            except Exception as e:
                record_error('mysql-update', e)

    t_start = time.perf_counter()
    await asyncio.gather(*(worker(w) for w in range(WORKERS)))
    total_ms = (time.perf_counter() - t_start) * 1000

    # 8. 汇总输出
    summary = _summarize(all_times)
    fed = _summarize(fed_times)
    out = {
        'product': 'py-store',
        'suffix': SUFFIX,
        'workers': WORKERS,
        'rounds': ROUNDS,
        'ops_per_round': 5,
        'total_ops': summary['ops'],
        'errors': sum(errors_by_type.values()),
        'errors_by_type': errors_by_type,
        'total_ms': round(total_ms),
        'qps': round(summary['ops'] / (total_ms / 1000), 2),
        'all_ops': summary,
        'federation_ops': fed,
    }
    print('[RESULT]' + json.dumps(out))

    # 清理
    m_pool.close()  # asyncmy 为同步 close
    await pg_pool.close()
    await sqlite_conn.close()
    await client.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as e:  # noqa: BLE001
        print(f'[stress py-store] FAIL: {e}', file=sys.stderr)
        sys.exit(1)
