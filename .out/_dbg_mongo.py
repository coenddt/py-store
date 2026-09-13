import json, sys, os
sys.path.insert(0, r"f:\独立开发者\项目\mongo-store\py-store\src")
sys.path.insert(0, r"f:\独立开发者\项目\mongo-store\py-store\example\course-platform\impl")
import asyncio, harness
from harness import setup_backend, register_all, reset, seed
from py_store import init, permission

async def main():
    driver, conn = await setup_backend('mongodb')
    register_all()
    await init({'default': conn})
    permission.set_context({'userId': 'u1', 'roles': ['admin']})
    await reset('mongodb', driver)
    await seed('mongodb', driver)
    from py_store.datasource import get_connection
    coll = get_connection('default')['courses']
    pipe = [
        {"$match": {"_id": "c3"}},
        {"$lookup": {"from": "categories", "let": {"rel_categoryId": {"$ifNull": ["$categoryId", None]}},
                     "pipeline": [{"$match": {"$expr": {"$eq": ["$_id", "$$rel_categoryId"]}}}, {"$project": {"_id": 1, "name": 1}}],
                     "as": "category"}},
        {"$unwind": {"path": "$category", "preserveNullAndEmptyArrays": True}},
    ]
    cur = await coll.aggregate(pipe)
    docs = [doc async for doc in cur]
    print("RAW mongo aggregate (bypass our exec):")
    print(json.dumps(docs, ensure_ascii=False, default=str))

asyncio.run(main())