"""并发原语测试（C1/A2）：claim 功能用例 + 双进程压测零重复领取 + leader_elector 等价用例

对应执行文档 02 §4.4：
  1. SQLite 单进程功能用例：领取数 = limit、置标落库、防重复领取（filter 排除条件）；
  2. **双进程压测**（A2 定论实验，文档二待核实项 1）：multiprocessing 起 2 worker
     各持独立连接循环 claim 同一 100 行队列（缺省 10 分钟，``CLAIM_STRESS_SECONDS``
     可调），断言零重复领取——判定依据是**各 worker 实际领取到的 _id 列表无交集**
     （库内 claimed_by 会被后写覆盖，不能作为重复判定来源）；
  3. leader_elector 等价用例：upsert 幂等插入两次仅一行；CAS 抢占（旧持有者过期
     条件）matched=1、败者 matched=0；renew_lease 归属条件续约。

SQLite 为文件库（双进程共享）；MySQL/PG 用例见 test_real_backends_e2e 的同款惯例，
无真库环境显式 skip（禁静默通过）。
"""

import asyncio
import json
import multiprocessing
import os
import tempfile
import time
import uuid

import pytest

from py_store import executors, store
from py_store import schema as _sc

aiosqlite = pytest.importorskip('aiosqlite')

# 压测时长（秒）：缺省 600 = 10 分钟（A2 定论）；CI/本地调试可设短
STRESS_SECONDS = int(os.environ.get('CLAIM_STRESS_SECONDS', '600'))
QUEUE_ROWS = 100

SCHEMA = {
    'name': 'ClaimJob', 'collection': 'claim_jobs', 'timestamps': False,
    'fields': {
        'status': {'type': 'string'},
        'claimedBy': {'type': 'string'},
        'leaseUntil': {'type': 'int'},
    },
    'relations': {},
}

DDL = """CREATE TABLE IF NOT EXISTS claim_jobs (
    _id TEXT PRIMARY KEY, status TEXT, claimed_by TEXT, lease_until INT,
    __present TEXT)"""


def _register_schema():
    _sc.register(SCHEMA)


async def _make_file_db(rows):
    """建临时文件库并 seed N 行 pending（返回 db 路径；调用方负责清理）"""
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    db = await aiosqlite.connect(path)
    await db.execute('DROP TABLE IF EXISTS claim_jobs')
    await db.execute(DDL)
    await db.executemany(
        'INSERT INTO claim_jobs (_id, status, __present) VALUES (?, ?, ?)',
        [(f'j{i:03d}', 'pending', 'Y') for i in range(rows)])
    await db.commit()
    await db.close()
    return path


async def _memory_db(rows):
    db = await aiosqlite.connect(':memory:')
    await db.execute(DDL)
    await db.executemany(
        'INSERT INTO claim_jobs (_id, status, __present) VALUES (?, ?, ?)',
        [(f'j{i:03d}', 'pending', 'Y') for i in range(rows)])
    await db.commit()
    return db


# ── 用例 1：单进程功能（领取数 = limit、置标落库、防重复） ──────────────────

def test_claim_functional_sqlite():
    async def _main():
        db = await _memory_db(5)
        _register_schema()
        from py_store.crud import set_db
        set_db(executors.create_connection('sqlite', db))
        try:
            got = await store.claim('ClaimJob', {'status': 'pending'},
                                    {'claimedBy': 'w1', 'status': 'claimed'},
                                    limit=3, sort=[['_id', 1]])
            assert len(got) == 3, f'领取数应为 limit=3，实际 {len(got)}'
            assert [r['_id'] for r in got] == ['j000', 'j001', 'j002'], got
            # 置标落库（领取后最新值：RETURNING 行）
            assert all(r['claimedBy'] == 'w1' and r['status'] == 'claimed'
                       for r in got), got
            # 重复 claim：pending 已排除 → 只剩 2 行可领
            rest = await store.claim('ClaimJob', {'status': 'pending'},
                                     {'claimedBy': 'w2', 'status': 'claimed'},
                                     limit=10)
            assert len(rest) == 2, rest
            # 领完后再 claim → 空
            again = await store.claim('ClaimJob', {'status': 'pending'},
                                      {'claimedBy': 'w3', 'status': 'claimed'},
                                      limit=10)
            assert again == [], again
        finally:
            await db.close()

    asyncio.run(_main())


# ── 用例 2：双进程压测（A2 定论实验） ────────────────────────────────────────

def _worker_entry(db_path, worker_id, duration_s, out_path):
    """子进程入口（Windows spawn 要求模块级可 picklable）：独立连接循环 claim"""
    asyncio.run(_worker_loop(db_path, worker_id, duration_s, out_path))


async def _worker_loop(db_path, worker_id, duration_s, out_path):
    import time as _time

    db = await aiosqlite.connect(db_path, timeout=10)
    _register_schema()
    from py_store.crud import set_db
    set_db(executors.create_connection('sqlite', db))
    stop_ts = _time.monotonic() + duration_s
    claimed = []
    try:
        while _time.monotonic() < stop_ts:
            try:
                rows = await store.claim('ClaimJob', {'status': 'pending'},
                                         {'claimedBy': worker_id,
                                          'status': 'claimed'},
                                         limit=1)
            except Exception:
                # SQLite 写锁忙（另一 worker 正在写）：显式拦截后重试（禁静默吞——
                # 本用例统计的是「实际领取列表」，被锁跳过不影响正确性判定）
                await asyncio.sleep(0.01)
                continue
            for r in rows:
                claimed.append(r['_id'])
                with open(out_path, 'a', encoding='utf-8') as f:
                    f.write(json.dumps({'id': r['_id'], 'ts': _time.time()}) + '\n')
            if not rows:
                await asyncio.sleep(0.005)  # 队列领完：让出写锁窗口
    finally:
        await db.close()


def test_claim_two_process_stress_sqlite():
    """双进程并发 claim 压测：零重复领取（A2 定论实验）"""
    db_path = asyncio.run(_make_file_db(QUEUE_ROWS))
    out_dir = tempfile.mkdtemp(prefix='claim_stress_')
    outs = [os.path.join(out_dir, f'w{i}.jsonl') for i in range(2)]
    try:
        start = time.monotonic()
        workers = [
            multiprocessing.Process(
                target=_worker_entry,
                args=(db_path, f'w{i}', STRESS_SECONDS, outs[i]))
            for i in range(2)
        ]
        for w in workers:
            w.start()
        for w in workers:
            w.join(STRESS_SECONDS + 120)  # 余量：启动/收尾开销
            assert not w.is_alive(), f'worker {w.name} 未在时限内退出'
        elapsed = time.monotonic() - start

        per_worker = []
        for p in outs:
            ids = []
            if os.path.exists(p):
                with open(p, encoding='utf-8') as f:
                    ids = [json.loads(line)['id'] for line in f if line.strip()]
            per_worker.append(ids)

        all_ids = [i for ids in per_worker for i in ids]
        # 零重复：两 worker 实际领取列表无交集
        dup = set(per_worker[0]) & set(per_worker[1])
        assert not dup, f'发现重复领取 {sorted(dup)[:10]}...（A2 违约）'
        # 队列全部领完（10 分钟对 100 行远超充裕；若不足说明吞吐异常）
        assert len(all_ids) == QUEUE_ROWS, (
            f'应领完全部 {QUEUE_ROWS} 行，实际 {len(all_ids)}'
            f'（{STRESS_SECONDS}s，重复 {len(all_ids) - len(set(all_ids))}）')
        # 库内终态：全部 claimed
        async def _check():
            db = await aiosqlite.connect(db_path)
            cur = await db.execute(
                "SELECT COUNT(*) FROM claim_jobs WHERE status='claimed'")
            (n,) = await cur.fetchone()
            await db.close()
            return n
        claimed_n = asyncio.run(_check())
        assert claimed_n == QUEUE_ROWS, f'库内 claimed 终态 {claimed_n}/{QUEUE_ROWS}'
        print(f'\n[压测] {STRESS_SECONDS}s 双进程 × {QUEUE_ROWS} 行队列：'
              f'总领取 {len(all_ids)}，零重复，耗时 {elapsed:.1f}s（A2 定论：零重复领取）')
    finally:
        os.remove(db_path)


# ── 用例 3：leader_elector 等价（upsert 幂等 + CAS 抢占 + 续约） ─────────────

def test_leader_elector_equivalent():
    async def _main():
        db = await _memory_db(0)
        await db.execute('ALTER TABLE claim_jobs ADD COLUMN payload TEXT')
        _register_schema()
        from py_store.crud import set_db
        set_db(executors.create_connection('sqlite', db))
        try:
            # 幂等插入：upsert 两次仅一行（leader 行固定 _id，按主键 upsert）
            await store.upsert('ClaimJob', {'_id': 'leader1'},
                               {'_id': 'leader1', 'status': 'leader-hold',
                                'claimedBy': 'n1', 'leaseUntil': 100})
            await store.upsert('ClaimJob', {'_id': 'leader1'},
                               {'_id': 'leader1', 'status': 'leader-hold',
                                'claimedBy': 'n1', 'leaseUntil': 100})
            cnt = await store.count('ClaimJob', {'status': 'leader-hold'})
            assert cnt == 1, f'upsert 幂等：应仅 1 行，实际 {cnt}'

            # CAS 抢占：旧持有者过期（leaseUntil < now）→ 竞选者 matched=1
            r = await store.compare_and_set(
                'ClaimJob',
                {'_id': 'leader1', 'status': 'leader-hold', 'leaseUntil': 100},
                {'claimedBy': 'n2', 'leaseUntil': 200})
            assert r == {'matched': 1, 'modified': 1}, r
            # 败者（持有者归属条件不满足）matched=0
            r2 = await store.compare_and_set(
                'ClaimJob',
                {'_id': 'leader1', 'status': 'leader-hold', 'claimedBy': 'n1'},
                {'claimedBy': 'n3'})
            assert r2 == {'matched': 0, 'modified': 0}, r2

            # renew_lease：归属条件由 filter 携带；持有者续约 matched=1，他人 matched=0
            rl = await store.renew_lease('ClaimJob',
                                         {'_id': 'leader1', 'claimedBy': 'n2'},
                                         {'leaseUntil': 300})
            assert rl == {'matched': 1, 'modified': 1}, rl
            rl_bad = await store.renew_lease('ClaimJob',
                                             {'_id': 'leader1', 'claimedBy': 'nX'},
                                             {'leaseUntil': 999})
            assert rl_bad['matched'] == 0, rl_bad
        finally:
            await db.close()

    asyncio.run(_main())
