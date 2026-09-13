import json, sys
sys.path.insert(0, r'f:\独立开发者\项目\mongo-store\py-store\example\course-platform\impl')
from py_store import schema as sc
defs = json.load(open(r'f:\独立开发者\项目\mongo-store\py-store\example\course-platform\schema.json', encoding='utf-8'))
for d in defs:
    sc.register(d)
gql = 'Course($condition:@c0){_id, lessonCount}'
p = sc.core.plan_query(gql, {'c0': {'_id': 'c1'}}, None, None)
import pprint
pprint.pprint(p, width=220)