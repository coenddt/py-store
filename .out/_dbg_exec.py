import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, r"f:\独立开发者\项目\mongo-store\py-store\example\course-platform\impl")
sys.path.insert(0, r"f:\独立开发者\项目\mongo-store\py-store\src")
import harness
from harness import register_all, setup_backend
from py_store import init
driver, conn = __import__('asyncio').run(setup_backend('mongodb'))
register_all()
__import__('asyncio').run(init({'default': conn}))
from py_store.schema import core
from py_store.schema import get_async_fn
from py_store.crud.exec import _ctx, _exec
from py_store import permission
import asyncio, json

permission.set_context({'userId': 'u1', 'roles': ['admin']})
gql = 'Course($condition:@c0){_id, category{_id, name}}'
params = {'c0': {'_id': 'c3'}}

async def main():
    plan = core.plan_query(gql, params, _ctx(), None)
    cmd = plan['commands'][0]
    # seed 需要 c3 存在；先灌基础课程数据
    from py_store import store
    try:
        await store.insert('Category', {'_id': 'cat2', 'name': '后端'})
    except Exception:
        pass
    docs = await _exec(cmd)
    print('RAW docs (DB stage output):')
    print(json.dumps(docs, ensure_ascii=False))
    post = plan.get('postprocess')
    prep = core.prepare_query(post, docs, _ctx())
    print('after prepare_query:', json.dumps(prep['items'], ensure_ascii=False))
    out = core.strip_query(post, prep['items'])['items']
    print('final:', json.dumps(out, ensure_ascii=False))

asyncio.run(main())