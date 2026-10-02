"""manager-transaction 场景 pytest 壳（复刻 test_scenario_course_platform.py）。

逐后端调用 ``example/manager-transaction/impl/harness.py`` 的 ``run_backend``：
先跑 MongoDB 产出 oracle，再跑三个 SQL 后端与 oracle 对拍；报告落
``py-store/doc/test-eval/<YYYY>/<MM>/`` 并转成 pytest 判定。

后端不可达 → ``pytest.skip``，原因写入报告，绝不静默。

阶段0 基线预期：T1-02/T1-03/T1-04/T2-01/T2-02/T3-01/T3-02 为**红**（能力缺失），
T1-01/T1-05 为**守护绿**。运行：
$env:LOCAL_CORE='1'; $env:PYTHONPATH='py-store/src'; python -m pytest py-store/tests/test_scenario_manager_transaction.py -q
"""

import asyncio
import datetime
from pathlib import Path

import pytest

from _scenario_harness import load_scenario_harness

# 场景 harness 以唯一模块名显式加载（sys.modules 同名冲突防线，见 _scenario_harness.py）：
# 同进程批跑时 course-platform 场景先收集并占住 `harness`/`checks` 缓存键，
# `import harness` 会静默命中缓存——本场景将实际执行 course 的用例集（对拍失效）。
harness = load_scenario_harness(
    'manager_transaction_harness',
    Path(__file__).resolve().parent.parent / 'example' / 'manager-transaction' / 'impl')

BACKENDS = ['mongodb', 'postgres', 'mysql', 'sqlite']
_LOOP = None
_RESULTS = {}


@pytest.fixture(scope='module', autouse=True)
def _boot():
    global _LOOP
    try:
        asyncio.get_running_loop()
        _LOOP = asyncio.get_event_loop()
    except RuntimeError:
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    try:
        _LOOP.run_until_complete(_run_all())
        yield _LOOP
    finally:
        try:
            _write_report()
        finally:
            _LOOP.close()
            asyncio.set_event_loop(None)


def _run(coro):
    return _LOOP.run_until_complete(coro)


async def _run_all():
    mongo = await harness.run_backend('mongodb')
    _RESULTS['mongodb'] = mongo
    oracle = mongo.get('oracle', {})
    for kind in ['postgres', 'mysql', 'sqlite']:
        res = await harness.run_backend(kind, oracle)
        _RESULTS[kind] = res


@pytest.mark.parametrize('backend', BACKENDS)
def test_backend_scenario(backend):
    res = _RESULTS.get(backend)
    assert res is not None, f'{backend} 未执行'
    if not res.get('available'):
        pytest.skip(res.get('skip_reason') or '后端不可达')
    fails = [r['id'] for r in res['results'] if r['status'] != 'pass']
    total = len(res['results'])
    passed = total - len(fails)
    assert not fails, f'{backend}: 失败 {fails}（{passed}/{total}）'


def test_case_specific():
    """按用例 id 定位失败（例：-k 'T2-01'）。每个失败用例为一个失败点。"""
    any_fail = False
    for kind in BACKENDS:
        res = _RESULTS.get(kind)
        if not res or not res.get('available'):
            continue
        for r in res['results']:
            if r['status'] == 'fail':
                any_fail = True
                steps = '；'.join(
                    f"step{s['idx']}({s['expectKind']})×{s['op']}: {s['note'][:120]}"
                    for s in r['steps'] if not s['ok'])
                print(f"\n[{kind}] {r['id']} 失败: {steps}")
    assert not any_fail


def _write_report():
    year, month = datetime.date.today().strftime('%Y %m').split()
    out_dir = Path(__file__).resolve().parent.parent / 'doc' / 'test-eval' / year / month
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f'manager-transaction-场景矩阵-{datetime.date.today().isoformat()}.md'
    lines = []
    lines.append(f'# manager-transaction 场景矩阵 · 多后端对拍报告（{datetime.date.today().isoformat()}）')
    lines.append('')
    lines.append('## 一、环境与后端可达性')
    lines.append('')
    for kind in BACKENDS:
        res = _RESULTS.get(kind)
        if res is None:
            lines.append(f'- `{kind}`：未执行')
        elif not res.get('available'):
            lines.append(f'- `{kind}`：**skip** —— {res.get("skip_reason")}')
        else:
            p = sum(1 for r in res['results'] if r['status'] == 'pass')
            lines.append(f'- `{kind}`：可达，通过 {p}/{len(res["results"])}')
    lines.append('')
    lines.append('> 本报告只出证据，不修实现。判定规则：SQL 结果集与 Mongo(oracle) 逐行相等'
                 '或显式 Err/unsupported+告警；静默不一致判缺陷。')
    lines.append('')
    lines.append('## 二、逐用例结果（阶段0：T1/T2/T3 组，红用例为增补路线验收标尺）')
    lines.append('')
    lines.append('| 用例 | 组 | mongodb | postgres | mysql | sqlite |')
    lines.append('|---|---|---|---|---|---|')
    by_case = {}
    for kind in BACKENDS:
        res = _RESULTS.get(kind) or {}
        for r in res.get('results', []):
            by_case.setdefault(r['id'], {})[kind] = r['status']
    all_ids = []
    for kind in BACKENDS:
        for r in (_RESULTS.get(kind) or {}).get('results', []):
            if r['id'] not in all_ids:
                all_ids.append(r['id'])
    for cid in all_ids:
        row = by_case.get(cid, {})
        cells = [row.get(k, '—') for k in BACKENDS]
        lines.append(f'| {cid} | {cid.split("-")[0]} | ' + ' | '.join(cells) + ' |')
    lines.append('')
    lines.append('## 三、失败明细（证据原样摘录）')
    lines.append('')
    for kind in BACKENDS:
        res = _RESULTS.get(kind) or {}
        for r in res.get('results', []):
            if r['status'] != 'pass':
                lines.append(f'### [{kind}] {r["id"]}')
                lines.append('')
                for s in r['steps']:
                    if not s['ok']:
                        lines.append(f'- step{s["idx"]} × {s["op"]}（期望 {s["expectKind"]}）：{s["note"]}')
                lines.append('')
    path.write_text('\n'.join(lines), encoding='utf-8')
    print(f'报告: {path}')
