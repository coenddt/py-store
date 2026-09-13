"""course-platform 场景 harness —— 真实库逐后端执行 A~H 组矩阵。

设计约束：
  - Rust core Registry 为进程级单例、无 reset API → 每个后端在**独立进程**里以
    `default` 源复用同一套 schema/seed 运行（A~H 组）。联邦 I 组不走本场景。
  - MongoDB 为语义基准(orcacle)：先跑 mongodb 产出 oracle(每个 rows 步骤的规范化结果集)，
    再跑 SQL 后端比对（sqlPolicy=explicit-or-parity 时允许 Err/unsupported，静默不一致判失败）。
  - 不可达后端 → available=false + skip_reason，绝不静默。
  - 本脚本只出证据，不修实现。

用法：
  python impl/harness.py --backend mongodb --out ./.out/mongodb.json
  python impl/harness.py --backend mysql    --out ./.out/mysql.json    --oracle ./.out/mongodb.json
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import checks
import fns as fns_mod
import probes as probes_mod

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(ROOT.parent.parent / 'src'))  # py_store/src

from py_store import executors, init, permission, schema as sc, store  # noqa: E402

from py_store import feedback as fb  # noqa: E402


# 主场景 + 探针的 reset 表/集合（探针表无归档 DDL，不参与删除）
MAIN_TABLES = [
    'users', 'categories', 'courses', 'lessons', 'enrollments',
    'reviews', 'study_notes', 'audit_logs',
    'probe_grades', 'probe_holders', 'probe_memos', 'probe_notes', 'probe_computes',
]
ARCHIVE_TABLES = [f'{t}_deleted' for t in [
    'users', 'categories', 'courses', 'lessons', 'enrollments',
    'reviews', 'study_notes', 'audit_logs']]

MYSQL_URI = os.environ.get(
    'MYSQL_URI', 'mysql://e2e:e2e123@127.0.0.1:3306/mongo_store_e2e?charset=utf8mb4')
PG_URI = os.environ.get('PG_URI', 'postgres://e2e:e2e123@127.0.0.1:5432/mongo_store_e2e')
MONGO_URI = os.environ.get('MONGO_URI', 'mongodb://127.0.0.1:27017/mongo_store_e2e')


# ──────────────────────────────────────────────────────────── 装载 ──
def load_cases():
    cases = []
    for p in sorted((ROOT / 'cases').glob('*.json')):
        cases.extend(json.loads(p.read_text(encoding='utf-8')))
    return cases


def load_seed():
    return json.loads((ROOT / 'seed' / 'seed.json').read_text(encoding='utf-8'))


def load_schemas():
    return json.loads((ROOT / 'schema.json').read_text(encoding='utf-8'))


def register_all():
    for defn in load_schemas():
        _apply_fns(defn)
        sc.register(defn)
    for defn in probes_mod.PROBES:
        _apply_fns(defn)
        sc.register(defn)


def _apply_fns(defn):
    for key, val in (defn.get('computes') or {}).items():
        if not (val.get('fn') or val.get('asyncFn')):
            continue  # lookup 计算列由引擎内联，无 Host fn 实现，跳过
        ref = val.get('fnRef') or key
        impl = fns_mod.FNS.get(ref)
        if impl is None:
            raise RuntimeError(f'计算列 {defn["name"]}.{key} 缺少 FNS 实现 {ref}')
        if val.get('fn'):
            val['fn'] = impl
        if val.get('asyncFn'):
            val['asyncFn'] = impl


# ──────────────────────────────────────────────────────────── 后端 ──
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


async def setup_backend(kind):
    """连接 + 建表，返回 (driver, conn) 与失败原因；不可达返回 (None, reason)。"""
    if kind == 'sqlite':
        try:
            import aiosqlite
        except ImportError as e:
            return None, f'缺少 aiosqlite: {e}'
        db = await aiosqlite.connect(':memory:')
        for stmt in _ddl(kind):
            await db.execute(stmt)
        await db.commit()
        return db, executors.create_connection('sqlite', db)
    if kind == 'mysql':
        try:
            import asyncmy
        except ImportError as e:
            return None, f'缺少 asyncmy: {e}'
        try:
            pool = await asyncio.wait_for(
                asyncmy.create_pool(autocommit=True, charset='utf8mb4',
                                    **{k: v for k, v in _parse_mysql_uri(MYSQL_URI).items()}),
                timeout=6)
            async with pool.acquire() as c:
                async with c.cursor() as cur:
                    await cur.execute('SELECT 1')
        except Exception as e:
            return None, f'MySQL 不可达（{MYSQL_URI}）: {e}'
        for stmt in _ddl(kind):
            async with pool.acquire() as c:
                async with c.cursor() as cur:
                    await cur.execute(stmt)
        return pool, executors.create_connection('mysql', pool)
    if kind == 'postgres':
        try:
            import asyncpg
        except ImportError as e:
            return None, f'缺少 asyncpg: {e}'
        try:
            pool = await asyncio.wait_for(asyncpg.create_pool(dsn=PG_URI, timeout=5), timeout=8)
            await pool.execute('SELECT 1')
        except Exception as e:
            return None, f'PostgreSQL 不可达（{PG_URI}）: {e}'
        for stmt in _ddl(kind):
            await pool.execute(stmt)
        return pool, executors.create_connection('postgres', pool)
    if kind == 'mongodb':
        try:
            from pymongo import AsyncMongoClient
        except ImportError as e:
            return None, f'缺少 pymongo: {e}'
        client = AsyncMongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        try:
            await client.admin.command({'ping': 1})
        except Exception as e:
            await client.close()
            return None, f'MongoDB 不可达（{MONGO_URI}）: {e}'
        db = client.get_default_database()
        return db, db
    raise RuntimeError(f'未知后端 {kind}')


def _ddl(kind):
    path = ROOT / 'ddl' / f'{kind}.sql'
    return [s.strip() for s in path.read_text(encoding='utf-8').split(';') if s.strip()]


async def reset(kind, driver):
    if kind == 'mongodb':
        for c in MAIN_TABLES + ARCHIVE_TABLES:
            await driver[c].delete_many({})
        return
    if kind == 'mysql':
        async with driver.acquire() as conn:
            async with conn.cursor() as cur:
                for t in list(ARCHIVE_TABLES) + list(MAIN_TABLES):
                    await cur.execute(f'DELETE FROM {t}')
        return
    for t in list(ARCHIVE_TABLES) + list(MAIN_TABLES):
        await driver.execute(f'DELETE FROM {t}')
    if kind == 'sqlite':
        await driver.commit()


async def seed(kind, driver):
    for batch in load_seed():
        permission.set_context(batch.get('ctx'))
        for row in batch['rows']:
            await store.insert(batch['schema'], row)


# ──────────────────────────────────────────────────────────── 断言 ──
def _deep_sort_value(v):
    """递归把「列表元素为 dict 且含 _id」的子数组按 _id 归一，保证关系子数组跨后端可比。"""
    if isinstance(v, dict):
        return {k: _deep_sort_value(x) for k, x in v.items()}
    if isinstance(v, list):
        out = [_deep_sort_value(x) for x in v]
        dlist = [x for x in out if isinstance(x, dict)]
        if dlist and len(dlist) == len(out) and all('_id' in x for x in dlist):
            out.sort(key=lambda x: x['_id'])
        return out
    return v


def norm_rows(rows, normalize):
    rows = [dict(r) for r in (rows or [])]
    ignore = (normalize or {}).get('ignore') or []
    if ignore:
        rows = [{k: v for k, v in r.items() if k not in ignore} for r in rows]
    rows = [_deep_sort_value(r) for r in rows]
    sort_by = (normalize or {}).get('sortBy')
    if sort_by:
        rows.sort(key=lambda r: tuple(r.get(k) for k in sort_by))
    else:
        rows.sort(key=lambda r: json.dumps(r, sort_keys=True, default=str))
    return rows


def _codes(events):
    return [e.get('code') for e in events]


async def run_step(h, step):
    """执行单个 op，返回 (result, error, events)"""
    before = len(h.events_all)
    op = step['op']
    error = None
    result = None
    try:
        if op == 'query':
            result = await h.store.query(step['gql'], step.get('params'))
        elif op == 'query_one':
            result = await h.store.query_one(step['gql'], step.get('params'))
        elif op == 'query_with_count':
            result = await h.store.query_with_count(step['gql'], step.get('params'))
        elif op == 'count':
            result = await h.store.count(step['schema'], step.get('filter'))
        elif op == 'exists':
            result = await h.store.exists(step['schema'], step['condition'])
        elif op == 'insert':
            result = await h.store.insert(step['schema'], step['data'])
        elif op == 'insert_many':
            result = await h.store.insert_many(step['schema'], step['rows'])
        elif op == 'update':
            result = await h.store.update(step['schema'], step['condition'], step['data'])
        elif op == 'update_many':
            result = await h.store.update_many(step['schema'], step['condition'], step['data'])
        elif op == 'remove':
            result = await h.store.remove(step['schema'], step['condition'])
        elif op == 'upsert':
            result = await h.store.upsert(step['schema'], step['condition'], step['data'])
        elif op == 'mutation':
            result = await h.store.mutation(step['schema'], step['data'])
        elif op == 'set_flag':
            _set_flag(step['name'], step['value'])
            result = None
        else:
            raise RuntimeError(f'未知 op: {op}')
    except Exception as e:  # noqa: BLE001 统一捕获作为"显式报错"证据
        error = e
    events = h.events_all[before:]
    return result, error, events


def _set_flag(name, value):
    if name == 'require_context':
        store.set_require_context(value)
    else:
        raise RuntimeError(f'未知开关 {name}')


async def assert_step(h, step, oracle_rows):
    """返回 (ok, message)。is_sql 由 h.backend 判定；oracle_rows 来自 mongo 相同步骤。"""
    expect = step.get('expect') or {}
    if not expect.get('kind'):
        return True, 'no assertion'
    kind = expect['kind']
    is_sql = h.backend in ('mysql', 'postgres', 'sqlite')
    # sqlPolicy 支持 case 级声明（现状用例都写在 case 上）；step 级优先覆盖
    policy = step.get('sqlPolicy') or getattr(h, 'case_policy', None) or 'parity'
    err = h.error
    events = h.events

    if kind == 'error':
        if err is None:
            return False, '期望抛错但未抛错'
        return _match_error(err, expect), f'实际错误: {err}'
    if kind == 'feedback':
        codes = _codes(events)
        ok = any(c == expect.get('code') for c in codes)
        return ok, f'事件 codes: {codes}'
    if kind == 'unsupported':
        # Mongo 是基准，必然支持；SQL 必须 Err 或 feedback 二择一，否则静默判失败
        if not is_sql:
            return True, 'mongo 基准支持'
        codes = _codes(events)
        if err is not None or any(c for c in codes):
            return True, f'显式(err={type(err).__name__ if err else None}, events={codes})'
        return False, '不可翻译却静默返回了结果（既无错误也无告警）'

    # 结果类断言（rows/count/exists/inserted/updated/removed）
    if err is not None:
        if is_sql and policy == 'explicit-or-parity':
            return True, f'SQL 显式报错（{type(err).__name__}）可接受'
        return False, f'执行报错: {err}'

    if kind == 'raw':
        fn = getattr(checks, expect['fn'])
        return await fn(h, expect)
    if kind == 'count':
        return h.result == expect.get('value'), f'count={h.result} 期望 {expect.get("value")}'
    if kind == 'exists':
        return bool(h.result) == bool(expect.get('value')), f'exists={h.result}'
    if kind == 'inserted':
        ok = bool(h.result) and h.result.get('_id', '').startswith(expect['idPrefix'])
        if expect.get('hasTimestamps'):
            ok = ok and isinstance(h.result.get('createdAt'), (int, float)) and h.result.get('createdAt') > 0
        return ok, f'insert 结果: {h.result}'
    if kind == 'updated':
        val = (h.result or {}).get('modifiedCount')
        exp = expect.get('modifiedCount')
        return (val == exp) if exp is not None else True, f'modifiedCount={val}'
    if kind == 'removed':
        return ((h.result or {}).get('deletedCount') == expect.get('deletedCount')
                and (h.result or {}).get('archivedCount') == expect.get('archivedCount')), \
            f'remove 结果: {h.result}'

    # rows / page：与 expect.rows 或 mongo oracle 比对
    if kind == 'rows':
        expected = expect.get('rows')
        if expected is None:
            expected = oracle_rows
            if expected is None and is_sql:
                return False, '缺少期望行集且无 mongo oracle'
        if expected is None and not is_sql:
            return True, 'mongo 基准（无显式期望，仅作 oracle）'
        act = norm_rows(h.result, expect.get('normalize'))
        exp = norm_rows(expected, expect.get('normalize'))
        if act == exp:
            return True, f'{len(act)} 行'
        return False, f'行集不一致\n  实际: {json.dumps(act, ensure_ascii=False, default=str)}\n  期望: {json.dumps(exp, ensure_ascii=False, default=str)}'

    return False, f'未知断言 kind={kind}'


def _match_error(err, expect):
    msg = str(err)
    code = expect.get('code')
    if code:
        if code == 'ERR_PERMISSION' and isinstance(err, store.PermissionError):
            return True
        if isinstance(err, store.PermissionError) and expect.get('status'):
            return err.status == expect['status']
        return code in msg
    return True


# ──────────────────────────────────────────────────────────── 执行 ──
async def run_backend(kind, oracle=None):
    """统一 feedback sink；对每个 case 执行 reset→set_context→steps→断言。"""
    fb.set_sink(h.events_all.append)
    h.backend = kind
    return await _run_backend_inner(kind, oracle)


async def _run_backend_inner(kind, oracle):
    h.events_all.clear()
    driver, conn = await setup_backend(kind)
    if driver is None:
        return {'backend': kind, 'available': False, 'skip_reason': conn, 'results': []}
    register_all()
    await init({'default': conn})
    cases = load_cases()
    case_oracle = oracle or {}
    results = []
    collected_oracle = {}
    try:
        for idx, case in enumerate(cases):
            reset_mode = case.get('reset', 'seed')
            if reset_mode == 'seed':
                await reset(kind, driver)
                await seed(kind, driver)
            permission.set_context(case.get('ctx'))
            h.case_policy = case.get('sqlPolicy')  # case 级 sqlPolicy 透传给各 step
            # 快照开关，用例结束复原（E-13 会改 require_context）
            _saved_require = store.require_context()
            steps_out = []
            case_oracle_steps = []
            try:
                for si, step in enumerate(case['steps']):
                    if step['op'] == 'raw':
                        # raw 断言复用上一步的 h.result/h.error（自身不改动）
                        before = len(h.events_all)
                        h.events = h.events_all[before:]
                    else:
                        result, err, events = await run_step(h, step)
                        h.result = result
                        h.error = err
                        h.events = events
                    oracle_rows = None
                    if kind == 'mongodb' and step['op'] != 'raw':
                        if step.get('expect', {}).get('kind') == 'rows':
                            oracle_rows = norm_rows(result, step.get('expect', {}).get('normalize'))
                            case_oracle_steps.append(oracle_rows)
                        else:
                            case_oracle_steps.append(None)
                    elif kind == 'mongodb':
                        case_oracle_steps.append(None)
                    elif case_oracle and si < len(case_oracle.get(case['id'], [])):
                        oracle_rows = case_oracle[case['id']][si]
                    ok, msg = await assert_step(h, step, oracle_rows)
                    steps_out.append({'idx': si, 'ok': ok, 'note': msg,
                                      'op': step['op'], 'expectKind': step.get('expect', {}).get('kind')})
            finally:
                store.set_require_context(_saved_require)
            results.append({'id': case['id'], 'backend': kind, 'group': case.get('group'),
                            'status': 'pass' if all(s['ok'] for s in steps_out) else 'fail',
                            'steps': steps_out})
            if kind == 'mongodb':
                collected_oracle[case['id']] = case_oracle_steps
    finally:
        await teardown(kind, driver)
    out = {'backend': kind, 'available': True, 'skip_reason': None, 'results': results}
    if kind == 'mongodb':
        out['oracle'] = collected_oracle
    return out


async def teardown(kind, driver):
    try:
        if kind == 'mongodb':
            await driver.client.close()
        elif kind == 'mysql':
            driver.close()
            await driver.wait_closed()
        elif kind == 'postgres':
            await driver.close()
        elif kind == 'sqlite':
            await driver.close()
    except Exception:  # noqa: BLE001
        pass


# 进程级 helper（raw 断言用）
h = type('H', (), {})()
h.store = store
h.events_all = []
fb.set_sink(h.events_all.append)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--backend', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--oracle')
    args = ap.parse_args()

    oracle = None
    if args.oracle:
        oracle = json.loads(Path(args.oracle).read_text(encoding='utf-8'))
        oracle = oracle.get('oracle', {})
    result = await run_backend(args.backend, oracle)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, default=str, indent=1),
                              encoding='utf-8')
    print(f"[{args.backend}] 可用={result['available']} "
          f"通过={sum(1 for r in result['results'] if r['status']=='pass')}/"
          f"{len(result['results'])}"
          + (f"  skip={result['skip_reason']}" if not result['available'] else ''))


if __name__ == '__main__':
    asyncio.run(main())