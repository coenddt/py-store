"""宿主定时任务插件 —— schema ``triggers.schedule`` 的运行时

职责：自写 5 段 cron 匹配（分 时 日 月 周；``* , - /`` 与数字；禁秒级/宏/新依赖）
→ 分钟对齐循环 → 到点经 core ``expand_schedule_triggers`` 枚举 step 并复用触发链
执行器（``crud/triggers.py``）执行。判决唯一在 core（cron 合法性注册期已校验），
本模块只消费 step（总纲 §5）。

语义边界：
- ``{{now}}`` 占位符 = 毫秒时间戳（schedule body 仅 ``{{now}}`` 合法，core 注册期
  已禁 ``{{root.`` / ``{{before.``）；``root``/``before`` 恒 None；
- 到点执行失败**上抛**（tick_once fail-fast）；分钟循环内捕获后走统一反馈通道
  告警并继续下一轮（禁静默跳过，也不让循环死掉）；
- 错过即跳过、不补跑（无持久化，见总纲 §0）；
- 不做级联：schedule step 的触发写不再触发任何触发器（与写链一致）。

对齐 ``nodejs-store/src/scheduler/index.js``（同语义蛇形实现）。
"""

import asyncio
import re
import time
from datetime import datetime

from ..crud.exec import _ctx
from ..crud.triggers import run_triggers
from ..feedback import emit as _emit_feedback
from ..schema import core as _core

# ─── cron 5 段匹配器（分 时 日 月 周） ─────────────────────────

# 各段取值范围（周 0-7：0 与 7 均为周日，归一到 0）
_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_INT = re.compile(r'^\d+$')


def _bad(msg):
    return ValueError(f'ERR_CRON:{msg}')


def _parse_field(spec, idx):
    """解析单段为取值集合（``*`` | ``n`` | ``a-b`` | ``*/s`` | ``a-b/s`` | ``a/s``；列表 ``,``）"""
    lo_min, hi_max = _RANGES[idx]
    out = set()
    for part in str(spec).split(','):
        if not part:
            raise _bad(f'第 {idx + 1} 段存在空项：「{spec}」')
        slash = part.split('/')
        if len(slash) > 2:
            raise _bad(f'步长段非法：「{part}」')
        base = slash[0]
        step = 1
        if len(slash) == 2:
            if not _INT.match(slash[1]) or int(slash[1]) < 1:
                raise _bad(f'步长非法：「{part}」')
            step = int(slash[1])
        if base == '*':
            lo, hi = lo_min, hi_max
        elif '-' in base:
            a, b = base.split('-', 1)
            if not _INT.match(a) or not _INT.match(b):
                raise _bad(f'范围非法：「{part}」')
            lo, hi = int(a), int(b)
            if lo > hi:
                raise _bad(f'范围起点大于终点：「{part}」')
        else:
            if not _INT.match(base):
                raise _bad(f'非法字符：「{part}」')
            lo = int(base)
            # ``a/s`` 等价 ``a-max/s``（从 a 起按步长到段末）
            hi = hi_max if len(slash) == 2 else lo
        if lo < lo_min or hi > hi_max:
            raise _bad(f'超出范围（{lo_min}-{hi_max}）：「{part}」')
        v = lo
        while v <= hi:
            out.add(0 if idx == 4 and v == 7 else v)
            v += step
    return out


def parse_cron(expr):
    """解析 5 段 cron → ``{'sets', 'any_dom', 'any_dow'}``（cron_matches 消费）"""
    segs = str(expr).split()
    if len(segs) != 5:
        raise _bad(f'必须为 5 段（分 时 日 月 周）：「{expr}」')
    return {
        'sets': [_parse_field(s, i) for i, s in enumerate(segs)],
        # 日/周组合（Vixie 语义）：两段都限定（非 ``*``）时任一命中即触发，否则都要命中
        'any_dom': segs[2] != '*',
        'any_dow': segs[4] != '*',
    }


def cron_matches(expr, dt):
    """cron 与时刻（本地时间分量）是否匹配"""
    p = parse_cron(expr)
    sets = p['sets']
    if dt.minute not in sets[0]:
        return False
    if dt.hour not in sets[1]:
        return False
    if dt.month not in sets[3]:
        return False
    dom_ok = dt.day in sets[2]
    # weekday()：0=周一 → cron 语义（0=周日）：(weekday + 1) % 7
    dow_ok = ((dt.weekday() + 1) % 7) in sets[4]
    if p['any_dom'] and p['any_dow']:
        return dom_ok or dow_ok
    return dom_ok and dow_ok


# ─── tick_once：枚举到期 schedule 触发器并执行 ──────────────────


async def tick_once(now_ms=None):
    """执行一轮：枚举全部 schedule 触发器，cron 命中 ``now`` 的依序执行 step。

    ``now_ms`` 为毫秒时刻（缺省当前时间；测试注入固定时刻）；返回触发清单
    ``[{'schema', 'name'}]``。
    """
    now = _now_ms() if now_ms is None else now_ms
    dt = datetime.fromtimestamp(now / 1000)
    entries = _core.expand_schedule_triggers(_ctx())
    fired = []
    for entry in entries:
        if not cron_matches(entry['cron'], dt):
            continue
        # 每条独立去重集合：跨条目同名 step 互不去重（去重键不含 schema，撞键会误跳）
        await run_triggers([entry['step']], root=None, before=None, now=now, ctx=_ctx(),
                           executed=set())
        fired.append({'schema': entry['schema'], 'name': entry['name']})
    return fired


def _now_ms():
    """当前毫秒时间戳（与 node Date.now() 同语义）"""
    return int(time.time() * 1000)


# ─── 分钟对齐循环 ─────────────────────────────────────────────

_task = None


def _ms_to_next_minute():
    return (60 - datetime.now().second) * 1000 - datetime.now().microsecond // 1000


async def _loop():
    while True:
        try:
            await tick_once()
        except Exception as e:
            _emit_feedback({
                'type': 'schedule_tick_failed',
                'code': 'scheduleTickFailed',
                'layer': 'host',
                'message': f'定时触发器执行失败：{e}',
                'hint': 'schedule step 执行失败已上抛并在循环内告警；修复触发器声明或回调实现后重启循环',
            })
        await asyncio.sleep(_ms_to_next_minute() / 1000)


def start():
    """启动分钟对齐循环（幂等）；单轮失败走反馈通道告警，循环继续（禁静默失守）"""
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.get_event_loop().create_task(_loop())


def stop():
    """停止循环（幂等）"""
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
