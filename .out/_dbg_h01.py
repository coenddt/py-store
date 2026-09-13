"""C-05 与 H-01 探查：seed 后 mongo 存储 shape + store.query 输出，区分 null/缺失。"""
import asyncio, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
IMPL = ROOT / 'example' / 'course-platform' / 'impl'
sys.path.insert(0, str(IMPL))
sys.path.insert(0, str(ROOT / 'src'))

import harness
from py_store import init, permission, schema as sc
from py_store.schema import core

harness.register_all()
permission.set_context({'userId': 'u1', 'roles': ['admin']})

async def main():
    # 用 harness 连接器拿 mongo db
    driver_kind = 'mongodb'
    db, _ = await harness.setup_backend(driver_kind)
    await init({'default': db})
    await harness.reset(driver_kind, db)
    await harness.seed(driver_kind, db)

    # 1) 原始存储 shape
    cur = db['courses'].find({'_id': {'$in': ['c2', 'c4']}}, {'summary': 1, 'categoryId': 1})
    stored = await cur.to_list(length=None)
    print('STORED courses c2/c4:', stored)

    # 2) 走 store.query 跑 H-01 GQL
    gql = 'Course($sort:@s0){_id, title, price, enrolledCount, summary, status, categoryId, createdBy}'
    harness.permission.set_context({'userId': 'u1', 'roles': ['admin']})
    rows = await harness.store.query(gql, {'s0': {'_id': 1}})
    print('QUERY c2:', [r for r in rows if r['_id'] == 'c2'])
    print('QUERY c4:', [r for r in rows if r['_id'] == 'c4'])


asyncio.run(main())