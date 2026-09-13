"""course-platform 场景 pytest 壳。

逐后端调用 ``example/course-platform/impl/harness.py`` 的 ``run_backend``：
先跑 MongoDB 产出 oracle，再跑三个 SQL 后端与 oracle 对拍；把结果写进
``py-store/doc/test-eval/<YYYY>/<MM>/`` 报告（未处理- 前缀）并转成 pytest 判定。

后端不可达 → ``pytest.skip``，原因写入报告，绝不静默。

运行：$env:LOCAL_CORE='1'; $env:PYTHONPATH='py-store/src'; python -m pytest py-store/tests/test_scenario_course_platform.py -q
"""

import asyncio
import datetime
import json
import sys
from pathlib import Path

import pytest

# harness 依赖 impl 目录下的 checks / probes / fns（脚本运行退化为模块级 import）
HARNESS = Path(__file__).resolve().parent.parent / 'example' / 'course-platform' / 'impl'
sys.path.insert(0, str(HARNESS))
sys.path.insert(0, str(HARNESS.parent.parent.parent / 'src'))  # py-store/src

import harness  # noqa: E402

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
    # Mongo 先跑产 oracle；SQL 后端带 oracle 对拍
    mongo = await harness.run_backend('mongodb')
    _RESULTS['mongodb'] = mongo
    oracle = mongo.get('oracle', {})
    for kind in ['postgres', 'mysql', 'sqlite']:
        res = await harness.run_backend(kind, oracle)
        _RESULTS[kind] = res


def _case_ids():
    return [c['id'] for c in harness.load_cases()]


def _tests_for_backend(kind):
    res = _RESULTS.get(kind)
    if res is None:
        return
    if not res.get('available'):
        yield (kind, 'SKIP')
        return
    for r in res['results']:
        yield (kind, r)


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
    """按用例 id 定位失败（例：-k 'E-07'）。每个失败用例为一个失败点。"""
    for kind, r in _tests_for_backend('mongodb'):
        pass
    # 聚合各后端失败用例作为可见失败
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
    path = out_dir / f'未处理-course-platform-场景矩阵-{datetime.date.today().isoformat()}.md'
    lines = []
    lines.append(f'# course-platform 场景矩阵 · 多后端对拍报告（{datetime.date.today().isoformat()}）')
    lines.append('')
    lines.append('## 一、环境与后端可达性')
    lines.append('')
    reach = []
    for kind in BACKENDS:
        res = _RESULTS.get(kind)
        if res is None:
            reach.append(f'- `{kind}`：未执行')
        elif not res.get('available'):
            reach.append(f'- `{kind}`：**skip** —— {res.get("skip_reason")}')
        else:
            p = sum(1 for r in res['results'] if r['status'] == 'pass')
            lines.append(f'- `{kind}`：可达，通过 {p}/{len(res["results"])}')
    for line in reach:
        lines.append(line)
    lines.append('')
    lines.append('> 本报告只出证据，不修实现。判定规则：SQL 结果集与 Mongo(oracle) 逐行相等'
                 '或显式 Err/unsupported+告警；静默不一致判缺陷。')
    lines.append('')
    lines.append('## 二、覆盖度表（A~H 组）')
    lines.append('')
    lines.append('| 组 | 覆盖数 | 后端 | 通过 | 失败 | skip(不可达) |')
    lines.append('|---|----|----|----|----|----|')
    by_group = {}
    for kind in BACKENDS:
        res = _RESULTS.get(kind)
        if not res or not res.get('available'):
            continue
        for r in res['results']:
            g = r['group']
            if r['id'].startswith('B-08b'):
                g = 'B'
            ag = by_group.setdefault(g, {})
            ag[kind] = ag.get(kind, {'pass': 0, 'fail': 0})
            if r['status'] == 'pass':
                ag[kind]['pass'] += 1
            else:
                ag[kind]['fail'] += 1
    total_ids = len(_case_ids())
    for g in sorted(by_group):
        runs = by_group[g]
        max_fail = sum(runs[k]['fail'] for k in runs)
        n = next(iter(runs.values()))['pass'] + next(iter(runs.values()))['fail']
        lines.append(f'| {g} | {n} | {",".join(runs)} | '
                     f'{sum(runs[k]["pass"] for k in runs)} | {max_fail} | - |')
    lines.append('')
    lines.append(f'未覆盖组：**I(联邦)** —— 本场景为单源 harness（每个后端独立进程、`default` 源），'
                 '无法起双可写源；跨源联邦由 `tests/test_federation_e2e.py` 单独覆盖'
                 '（I-01..I-08 对应矩阵）。')
    lines.append('')
    lines.append('## 三、缺陷清单（按 静默失真 > 越权 > 其它 排序）')
    lines.append('')
    defects = []
    for kind in BACKENDS:
        res = _RESULTS.get(kind)
        if not res or not res.get('available'):
            continue
        for r in res['results']:
            if r['status'] != 'pass':
                for s in r['steps']:
                    if not s['ok']:
                        defects.append((kind, r['id'], r['group'], s['note']))
    if not defects:
        lines.append('无失败用例。')
    else:
        for kind, cid, group, note in defects:
            lines.append(f'- **{cid}** `[{kind}]` group={group}：{note}')
    lines.append('')
    lines.append('## 四、与环境变量')
    lines.append('')
    lines.append('- 可复跑：`$env:LOCAL_CORE=1; $env:PYTHONPATH=py-store/src; '
                 'python -m pytest py-store/tests/test_scenario_course_platform.py -q`')
    lines.append('- 后端可达性可用 `MYSQL_URI` / `PG_URI` / `MONGO_URI` 覆盖。')
    lines.append('')
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f'\n[报告] 已写入 {path}')