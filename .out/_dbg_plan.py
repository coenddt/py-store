import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, r"f:\独立开发者\项目\mongo-store\py-store\example\course-platform\impl")
import json
import harness
harness.register_all()
from py_store.schema import core
from py_store.crud.exec import _ctx

for gql, params in [
    ('Course($sort:@s0){_id, category{_id, name}}', {'s0': {'_id': 1}}),
    ('Course($condition:@c0){_id, category{_id}}', {'c0': {'_id': 'c3'}}),
]:
    plan = core.plan_query(gql, params, _ctx(), None)
    print('GQL:', gql)
    print('mode:', plan['mode'])
    print(json.dumps(plan, ensure_ascii=False)[:2500])
    print('---')