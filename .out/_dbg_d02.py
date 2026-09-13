import sys, json
sys.path.insert(0, r'f:\独立开发者\项目\mongo-store\py-store\src')
sys.path.insert(0, r'f:\独立开发者\项目\mongo-store\py-store\example\course-platform\impl')
from py_store.schema import core, register as sc_register
import fns as fns_mod, probes as probes_mod

ROOT = r'f:\独立开发者\项目\mongo-store\py-store\example\course-platform'

def load(path):
    with open(ROOT + '\\' + path, encoding='utf-8') as f:
        return json.load(f)

def reg(defn):
    for k, v in (defn.get('computes') or {}).items():
        if not (v.get('fn') or v.get('asyncFn')): continue
        ref = v.get('fnRef') or k
        impl = fns_mod.FNS.get(ref)
        if v.get('fn'): v['fn'] = impl
        if v.get('asyncFn'): v['asyncFn'] = impl
    sc_register(defn)  # 复用 schema.register（fn→True 占位 + set_fn 回调 + async map）

for d in load('schema.json'):
    reg(d)
for d in probes_mod.PROBES:
    core.register(d)

ctx = {'userId':'u1','roles':['admin']}
gql = 'User($condition:@c0){_id, displayName}'
params = {'c0': {'_id':'u1'}}
try:
    plan = core.plan_query(gql, params, ctx, None)
    print(json.dumps(plan, ensure_ascii=False, default=str))
except Exception as e:
    print('ERR', repr(e))