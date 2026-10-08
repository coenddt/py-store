"""触发器规划输出跨端对拍（A10）—— py 侧（nodejs-store/scripts/parity-triggers.js 镜像）

读 ``rust-store/fixtures/triggers/cases.json`` → 新建原生 Registry 注册 schemas
→ 逐 case 调 plan_insert / plan_update（update 首次取 needsProbe，再携 found/doc 重入）
→ 输出 ``{"<caseKey>": <plan 片段>}``（JSON，键排序，紧凑分隔符）。

与 node 侧输出逐字节比对：
    python scripts/parity_triggers.py > tmp/parity-triggers-py.json
    node scripts/parity-triggers.js > tmp/parity-triggers-node.json
    diff 两侧文件（必须零差异）
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / 'src'))

from py_store.core import native  # noqa: E402

FIXTURE = (pathlib.Path(__file__).resolve().parent.parent.parent
           / 'rust-store' / 'fixtures' / 'triggers' / 'cases.json')
fx = json.loads(FIXTURE.read_text(encoding='utf-8'))

reg = native.Registry()
for s in fx['schemas']:
    reg.register(s)

out = {}
for i, c in enumerate(fx['cases']):
    if c['fn'] == 'planInsert':
        plan = reg.plan_insert(c['schema'], c['input'], c['now'], c.get('newId') or '', None, None)
        out[f'case{i}.triggers'] = plan.get('triggers')
    elif c['fn'] == 'planUpdate':
        first = reg.plan_update(
            c['schema'], c['condition'], c['input'], {}, c['now'], None, None, None, None)
        out[f'case{i}.first'] = first
        plan = reg.plan_update(
            c['schema'], c['condition'], c['input'], {}, c['now'], None,
            c.get('found') is True, c.get('doc'), None)
        out[f'case{i}.triggers'] = plan.get('triggers')
    else:
        raise ValueError(f"未知 case.fn: {c['fn']}")

print(json.dumps(out, sort_keys=True, separators=(',', ':'), ensure_ascii=False))
