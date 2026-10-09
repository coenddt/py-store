"""宿主定时任务插件 e2e（04；镜像 nodejs-store/tests/scheduler.test.js）

覆盖总纲 A6 + 禁静默失守。全程只走 store 统一入口：
register(triggers.schedule) → tick_once → core expand_schedule_triggers 枚举
→ cron 匹配 → 复用触发链执行器。真库 = aiosqlite 内存库。
cron 对拍数据 = rust-store/fixtures/triggers/cases.json 的 cronCases（两宿主共用）。

运行：LOCAL_CORE=1 python -m pytest tests/test_scheduler.py -q
"""

import asyncio
import json
import pathlib

import aiosqlite

from py_store import executors, init, scheduler, store
from py_store import schema as _sc

SRC = 'sch_a'
# 本地开发：rust-store 与 py-store 平级（common-store 布局）；CI：checkout 到 workspace 内
_here = pathlib.Path(__file__).resolve()
for _root in (_here.parents[2] / 'rust-store', _here.parents[1] / 'rust-store'):
    if (_root / 'fixtures' / 'triggers' / 'cases.json').exists():
        break
FX = _root / 'fixtures' / 'triggers' / 'cases.json'
_cron_cases = json.loads(FX.read_text(encoding='utf-8'))['cronCases']

_DDL = """
CREATE TABLE t_s_log (_id TEXT PRIMARY KEY, kind TEXT, __present TEXT);
CREATE TABLE t_s_log_deleted (_id TEXT PRIMARY KEY, kind TEXT, deleted_at INTEGER, __present TEXT);
CREATE TABLE t_s_boom (_id TEXT PRIMARY KEY, __present TEXT);
CREATE TABLE t_s_boom_deleted (_id TEXT PRIMARY KEY, deleted_at INTEGER, __present TEXT);
"""


def _register_test_schemas():
    """测试 schema 注册（conftest 模块夹具重放）"""
    _sc.register({
        'name': 'SOrder', 'collection': 'tSOrder', 'idPrefix': 'so_', 'timestamps': False,
        'datasource': SRC, 'fields': {'amount': {'type': 'number'}},
        'triggers': {
            'schedule': [
                # A6：* * * * * 必到点（回调式 + {{now}} 占位符）
                {'name': 'sch_cb', 'cron': '* * * * *', 'fnRef': 'schRecorder',
                 'args': {'at': '{{now}}'}},
                # 02:00 到点（tick_once 注入 12:34 时不命中）
                {'name': 'sch_cb2', 'cron': '0 2 * * *', 'fnRef': 'schRecorder',
                 'args': {'at': '{{now}}'}},
                # 命令式：到点执行 step 命令落库
                {'name': 'sch_cmd', 'cron': '* * * * *', 'into': 'SLog', 'op': 'insert',
                 'data': {'_id': '{{now}}', 'kind': 'sch'}},
            ],
        },
    })
    _sc.register({
        'name': 'SLog', 'collection': 'tSLog', 'idPrefix': 'sl_', 'timestamps': False,
        'datasource': SRC, 'fields': {'kind': {'type': 'string'}},
    })
    _sc.register({
        'name': 'SBoom', 'collection': 'tSBoom', 'idPrefix': 'sb_', 'timestamps': False,
        'datasource': SRC, 'fields': {},
        # schedule step 执行失败 → tick_once 上抛（禁静默跳过）；
        # cron 锁 12-31 23:59，只在失败用例的注入时刻命中（不污染其他用例）
        'triggers': {'schedule': [
            {'name': 'sch_boom', 'cron': '59 23 31 12 *', 'fnRef': 'schBoom'}]},
    })


# ─── 回调实现注入（启动期一次；运行期计数在用例内自管） ─────────

cb_calls = []


async def _sch_recorder(args, ctx, host):
    cb_calls.append(args)


async def _sch_boom(args, ctx, host):
    raise RuntimeError('boom-err')


store.set_trigger_fn('schRecorder', _sch_recorder)
store.set_trigger_fn('schBoom', _sch_boom)


def _run(coro):
    return asyncio.run(coro)


async def _setup():
    """环境复位（node beforeEach 镜像）：计数清零 + 内存库连接"""
    cb_calls.clear()
    db = await aiosqlite.connect(':memory:')
    db.row_factory = aiosqlite.Row
    await db.executescript(_DDL)
    await init({SRC: executors.create_connection('sqlite', db)})
    return db


def _at_ms(a):
    """fixture 时刻分量（本地时区）→ 毫秒（与 node new Date(y, mo-1, d, h, mi).getTime() 同语义）"""
    from datetime import datetime
    return int(datetime(a['y'], a['mo'], a['d'], a['h'], a['mi']).timestamp() * 1000)


# ─── cron 匹配器 ─────────────────────────────────────────────

def test_cron_matches_fixture_cases():
    """cron 匹配器：fixture cronCases 逐组断言（分量构造，时区无关）"""
    from datetime import datetime
    for c in _cron_cases:
        a = c['at']
        dt = datetime(a['y'], a['mo'], a['d'], a['h'], a['mi'])
        assert scheduler.cron_matches(c['cron'], dt) is c['expect'], \
            f"{c['cron']} @ {a}"


def test_cron_invalid_explicit_err():
    """cron 非法表达式显式报错（ERR_CRON）"""
    from datetime import datetime
    d = datetime(2026, 1, 1)
    for bad in ('* * * *', 'a * * * *', '99 * * * *', '0 0 * * 8', '5-1 * * * *'):
        try:
            scheduler.cron_matches(bad, d)
        except ValueError as e:
            assert 'ERR_CRON' in str(e), bad
        else:
            raise AssertionError(f'未报错：{bad}')


# ─── A6：tick_once 触发执行链 ─────────────────────────────────

def test_a6_tick_once_fires_command_and_callback():
    """A6 tick_once：* * * * * 到点触发执行链（回调 + 命令式落库；未到点不触发）"""

    async def scenario():
        db = await _setup()
        now = _at_ms({'y': 2026, 'mo': 1, 'd': 1, 'h': 12, 'mi': 34})
        fired = await scheduler.tick_once(now)
        cur = await db.execute("SELECT COUNT(*) FROM t_s_log WHERE kind = 'sch'")
        row = await cur.fetchone()
        await cur.close()
        await db.close()
        return now, fired, row[0]

    now, fired, n = _run(scenario())
    # sch_cb2（0 2 * * *）在 12:34 不命中 → 未到点不触发
    assert sorted(f['name'] for f in fired) == [
        'SOrder.schedule.sch_cb', 'SOrder.schedule.sch_cmd']
    assert len(cb_calls) == 1, '回调执行一次'
    assert cb_calls[0]['at'] == now, '{{now}} 整值替换为毫秒时刻'
    assert n == 1, '命令式 step 已落库'


def test_a6_tick_once_two_rounds_independent():
    """A6 tick_once：连续两轮各独立执行（去重集合按次独立，无跨轮状态）"""

    async def scenario():
        await _setup()
        await scheduler.tick_once(_at_ms({'y': 2026, 'mo': 1, 'd': 1, 'h': 12, 'mi': 34}))
        fired = await scheduler.tick_once(_at_ms({'y': 2026, 'mo': 1, 'd': 1, 'h': 12, 'mi': 35}))
        return fired

    fired = _run(scenario())
    assert sorted(f['name'] for f in fired) == [
        'SOrder.schedule.sch_cb', 'SOrder.schedule.sch_cmd'], '第二轮照常触发'
    assert len(cb_calls) == 2, '每轮各执行一次（错过即跳过、无补跑判定）'


def test_tick_once_fired_set_varies_by_time():
    """tick_once：到点集合随时刻变化（02:00 时 0 2 * * * 亦命中）"""

    async def scenario():
        await _setup()
        return await scheduler.tick_once(_at_ms({'y': 2026, 'mo': 1, 'd': 1, 'h': 2, 'mi': 0}))

    fired = _run(scenario())
    assert sorted(f['name'] for f in fired) == [
        'SOrder.schedule.sch_cb', 'SOrder.schedule.sch_cb2', 'SOrder.schedule.sch_cmd']


def test_tick_once_failure_raises():
    """tick_once：schedule step 执行失败上抛（禁静默跳过）"""

    async def scenario():
        await _setup()
        await scheduler.tick_once(_at_ms({'y': 2026, 'mo': 12, 'd': 31, 'h': 23, 'mi': 59}))
        return  # pragma: no cover —— 必须失败

    try:
        _run(scenario())
    except RuntimeError as e:
        assert 'boom-err' in str(e)
    else:
        raise AssertionError('失败未上抛')


# ─── 启动期校验覆盖 schedule ─────────────────────────────────

def test_assert_trigger_fns_covered_covers_schedule():
    """assert_trigger_fns_covered 覆盖 schedule 的 fnRef（缺实现显式报错）"""
    try:
        store.assert_trigger_fns_covered([{
            'name': 'X', 'triggers': {'schedule': [
                {'name': 's', 'cron': '* * * * *', 'fnRef': 'noSuchFn'}]},
        }])
    except RuntimeError as e:
        assert 'ERR_TRIGGER_FN_MISSING' in str(e)
    else:
        raise AssertionError('缺实现未报错')
    # 已注入的 fnRef 不误报
    store.assert_trigger_fns_covered([{
        'name': 'Y', 'triggers': {'schedule': [
            {'name': 's', 'cron': '* * * * *', 'fnRef': 'schRecorder'}]},
    }])


# ─── 循环控制 ────────────────────────────────────────────────

def test_start_stop_idempotent():
    """start/stop 幂等（asyncio 循环在 running loop 内挂起/取消）"""

    async def scenario():
        await _setup()
        scheduler.start()
        scheduler.start()
        scheduler.stop()
        scheduler.stop()

    _run(scenario())
