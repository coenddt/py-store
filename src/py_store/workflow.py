"""工作流编排（首批：线性步骤 + when 守卫 + fail-fast）

设计见 ``common-store/工作流编排设计文档.md``。两条铁律：
  - 铁律 A（定义即数据）：Workflow defn 是与 schema defn 同构的纯 JSON——同注册模式、
    同校验文化（白名单外显式 Err，文案含 ``WORKFLOW_UNSUPPORTED``）、权限三级白名单内嵌 defn；
  - 铁律 B（运行即数据）：run 记录落库为内建 schema ``__workflowRun``，用户可用普通 GQL
    查询失败/输入/步骤迹——可观测性零新接口。

引擎落在宿主层、core 零改动（执行全是 IO；校验与执行共用同一套步骤语义）。
首批明确不做（检出即 Err，不静默降级）：循环 / 并行 / 子工作流 / 人工审批 / 自动重试 /
自动补偿 / 步骤级宿主回调 / gql 内嵌占位符（参数化请走 params 绑定）/ 数组下标路径。
对齐 ``nodejs-store/src/workflow.js``（双宿主输出逐字节一致由 parity 锚单测守护）。
"""

import json
import re
import time
from contextlib import contextmanager

from . import permission as _permission
from .crud.exec import _call, _sources_of, run_atomic
from .crud.id import _new_id_pool
from .feedback import emit as _emit_feedback
from .permission import get_context
from .schema import core as _core
from .schema import has as _schema_has
from .schema import register as _schema_register
from .schema import require_context as _schema_require_context

# ─── 白名单（首批边界；白名单外注册即 Err，不猜测语义） ──────────

# 缓存内置 list 类型（本模块的 list() 函数会遮蔽内置名；仿 schema.py）
_LIST_TYPES = (list, tuple)

# 列表插值统一 JSON 风格（与 node JSON.stringify 逐字节一致——parity 锚要求错误文案一致）
def _json(x):
    return json.dumps(x, ensure_ascii=False, separators=(',', ':'))

# 步骤类型白名单（§4.2）
_OPS = ('query', 'mutation', 'fail')
# when 算子白名单（§4.2）→ 右操作数参数键（eq 与 is 同义，右值键统一 is）
_WHEN_PARAM = {'exists': 'is', 'is': 'than', 'eq': 'than',
               'ne': 'than', 'lt': 'than', 'lte': 'than', 'gt': 'than', 'gte': 'than'}
# defn 顶层字段白名单（§4.1）
_TOP_FIELDS = frozenset({'name', 'steps', 'read', 'write', 'run', 'description'})
# 步骤字段白名单（按 op；as 语义见 §4.2——query 必填，mutation 可选，fail 无）
_STEP_FIELDS = {
    'query': frozenset({'op', 'as', 'gql', 'params', 'when'}),
    'mutation': frozenset({'op', 'as', 'model', 'data', 'match', 'upsert', 'when'}),
    'fail': frozenset({'op', 'message', 'when'}),
}
# 步骤必填字段
_STEP_REQUIRED = {'query': ('as', 'gql'), 'mutation': ('model', 'data'), 'fail': ('message',)}

# 占位符（§4.3）：整值形态（允许一层嵌套——dec 参数；dec 嵌套 dec 注册即 Err）
_FULL_PH = re.compile(r'^\{\{(?:[^{}]|\{\{[^{}]*\}\})*\}\}$')
_INNER_PH = re.compile(r'\{\{([^{}]*)\}\}')
# 外层完整占位符（含一层 dec 嵌套）——校验扫描用：dec 形态错误只有在外层可见时才拦得住
_OUTER_PH = re.compile(r'\{\{(?:[^{}]|\{\{[^{}]*\}\})*\}\}')
_GQL_PH = re.compile(r'\{\{')
_NUM_SEG = re.compile(r'^\d+$')
_DEC = 'dec:'
_INPUT = 'input.'

# run 终态状态机（§5）：running → succeeded | failed | rejected；dry-run 终态 drySucceeded | dryFailed
_BUILTIN_NAME = '__workflowRun'

# 内建 run 表 schema（§5）。write 显式空名单（R2：core 对显式 [] 拒绝一切写，
# super_admin/admin/internal 保留——实测空白名单写被普通角色放行，不收紧则 run 审计
# 可被 GQL 篡改）；read 缺省 = 跟随既有全局语义（可读可观测，铁律 B 自举查询）。
# 模块内部写入走干净 internal 上下文（不含触发者 roles：core 对 guest 硬拒先于
# internal 放行——permission.rs can_write_schema，故不能复用保留 roles 的 run_as_internal）。
# now 为 number（毫秒，与 mutation 的 now 同型同源）——设计文档 §5 写 string，
# 实现按同构语义取 number，留痕见交付说明。
_RUN_SCHEMA = {
    'name': _BUILTIN_NAME,
    'system': True,
    'collection': _BUILTIN_NAME,
    'idPrefix': 'wfrun',
    'write': [],
    'fields': {
        '_id': {'type': 'string'},
        'workflow': {'type': 'string'},
        'status': {'type': 'string'},
        'input': {'type': 'object'},
        'steps': {'type': 'array'},
        'error': {'type': 'string'},
        'stepIndex': {'type': 'int'},
        'dryRun': {'type': 'bool'},
        'now': {'type': 'number'},
    },
}


def ensure_builtin():
    """注册内建 ``__workflowRun`` schema（幂等；core 对重复三元组显式报错，故先 has 守卫）"""
    if not _schema_has(_BUILTIN_NAME):
        _schema_register(_RUN_SCHEMA)


# ─── 错误 ────────────────────────────────────────────────────

class WorkflowError(ValueError):
    """Workflow defn 校验失败（白名单外 / 结构非法）；文案含 WORKFLOW_UNSUPPORTED 前缀"""


class _StepFailure(Exception):
    """步骤失败内部信号：fail 步骤语义失败（message）或步骤执行 Err——fail-fast + 整体回滚"""

    def __init__(self, message, index=None):
        super().__init__(message)
        self.index = index


# ─── 权限：三级白名单判定（§0.6；core permission.rs::evaluate 同构，doc=Missing 语义） ──

def _evaluate(role_list, ctx):
    """角色白名单评估（与 core evaluate 同构；新建 run 无属主文档 → creator 按 Missing 通过）"""
    if not role_list:
        # 空白名单 = 无权限配置 → 默认行为（ctx 缺失放行；internal 放行；guest 拒绝）
        if ctx is None:
            return True
        if ctx.get('internal'):
            return True
        return 'guest' not in (ctx.get('roles') or [])
    if ctx is None:
        return True
    if ctx.get('internal'):
        return True
    roles = ctx.get('roles') or []
    if any(r in ('super_admin', 'admin') for r in roles):
        return True
    effective = roles if roles else [ctx.get('role')]
    if any(r in role_list for r in effective):
        return True
    return 'creator' in role_list


def _run_whitelist(defn):
    """run 白名单：显式声明优先；缺省回退 write（§0.6，falsy 统一回退）"""
    return defn.get('run') or defn.get('write') or None


# ─── 校验器（注册即静态检查；纯函数，错误全量收集） ──────────────

def _scan_placeholders(value):
    """深扫结构中的全部占位符表达式（外层完整形态，dec 嵌套一并可见），返回 [(inner, whole)]"""
    out = []

    def walk(v):
        if isinstance(v, str):
            for m in _OUTER_PH.finditer(v):
                out.append((m.group(0)[2:-2], m.group(0)))
        elif isinstance(v, _LIST_TYPES):
            for x in v:
                walk(x)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)

    walk(value)
    return out


def _check_expr(inner, defined_as, errors, where):
    """校验单个占位符表达式：input.<path> / dec:<a>,<b> / <as>.<path>（前向）"""
    if inner.startswith(_INPUT):
        path = inner[len(_INPUT):]
        if not path or any(_NUM_SEG.match(s) for s in path.split('.')):
            errors.append(f'{where}: 占位符 {{{{{inner}}}}} 路径非法'
                          '（首批不支持数组下标段，需要逐行处理请走宿主代码编排）')
        return
    if inner.startswith(_DEC):
        rest = inner[len(_DEC):]
        parts = rest.split(',')
        if len(parts) != 2:
            errors.append(f'{where}: 占位符 {{{{{inner}}}}} dec 须恰两个操作数 dec:<a>,<b>')
            return
        for p in parts:
            p = p.strip()
            if p.startswith('{{') and p.endswith('}}'):
                _check_expr(p[2:-2], defined_as, errors, where)
            elif not re.match(r'^-?\d+(\.\d+)?$', p):
                errors.append(f'{where}: 占位符 {{{{{inner}}}}} 操作数 {p!r} 须为占位符或数字字面量')
        return
    # <as>.<path>
    segs = inner.split('.')
    as_name = segs[0]
    if not as_name:
        errors.append(f'{where}: 占位符 {{{{{inner}}}}} 缺少 as 名')
        return
    if as_name not in defined_as:
        avail = _json(sorted(defined_as)) if defined_as else '无'
        errors.append(f'{where}: 占位符 {{{{{inner}}}}} 引用了不存在的 as "{as_name}"'
                      f'（前向可用: {avail}；后向引用不支持）')
        return
    path = '.'.join(segs[1:])
    if any(_NUM_SEG.match(s) for s in path.split('.')):
        errors.append(f'{where}: 占位符 {{{{{inner}}}}} 路径非法'
                      '（首批不支持数组下标段）')


def validate_defn(defn):
    """Workflow defn 静态校验 → 错误列表（空 = 通过）。纯函数，白名单外全量收集。

    检查项：顶层/步骤字段白名单、op 白名单、必填字段、as 唯一、占位符前向引用、
    when 结构（恰一个白名单算子 + 合法参数键）、upsert 必带 match、gql 禁占位符
    （参数化走 params 绑定，防注入）。
    """
    errors = []
    if not isinstance(defn, dict):
        return ['defn 须为 JSON 对象']
    name = defn.get('name')
    if not name or not isinstance(name, str):
        errors.append('name 必填（非空字符串）')
    elif name.startswith('__'):
        errors.append(f'name "{name}" 以 __ 开头（前缀保留给内建 schema，禁止用于工作流）')
    for k in defn:
        if k not in _TOP_FIELDS:
            errors.append(f'未知顶层字段 "{k}"（白名单: {_json(sorted(_TOP_FIELDS))}）')
    for k in ('read', 'write', 'run'):
        v = defn.get(k)
        if v is not None and (not isinstance(v, _LIST_TYPES)
                or not all(isinstance(r, str) for r in v)):
            errors.append(f'{k} 白名单须为字符串数组')

    steps = defn.get('steps')
    if not isinstance(steps, _LIST_TYPES) or not steps:
        errors.append('steps 必填（非空数组，线性步骤序列）')
        return errors

    defined_as: set[str] = set()
    for i, step in enumerate(steps):
        where = f'steps[{i}]'
        if not isinstance(step, dict):
            errors.append(f'{where}: 步骤须为 JSON 对象')
            continue
        op = step.get('op')
        if op not in _OPS:
            errors.append(f'{where}: WORKFLOW_UNSUPPORTED 未知步骤类型 {_json(op)}'
                          f'（首批白名单: {_json([*_OPS])}；循环/并行/子工作流/审批节点均不支持）')
            continue
        for k in step:
            if k not in _STEP_FIELDS[op]:
                errors.append(f'{where}: 未知步骤字段 "{k}"（{op} 白名单: {_json(sorted(_STEP_FIELDS[op]))}）')
        for k in _STEP_REQUIRED[op]:
            if step.get(k) in (None, ''):
                errors.append(f'{where}: {op} 步骤缺少必填字段 "{k}"')
        as_name = step.get('as')
        if op in ('query', 'mutation') and as_name:
            if as_name in defined_as:
                errors.append(f'{where}: as "{as_name}" 重复（引用歧义，禁止覆盖）')
        # 前向引用集合：不含当前步骤自身的 as（自引用在执行期必然悬空——结果尚不存在）
        if op == 'mutation' and step.get('upsert') and not step.get('match'):
            errors.append(f'{where}: upsert: true 须提供 match 条件')
        if op == 'query' and isinstance(step.get('gql'), str) and _GQL_PH.search(step['gql']):
            errors.append(f'{where}: gql 内嵌占位符不支持（注入面）；动态参数请走 params 绑定')
        # 占位符前向引用：扫描该步全部占位符字段（gql 除外——已禁）
        scan_pool = {k: v for k, v in step.items() if k not in ('op', 'gql')}
        for inner, _whole in _scan_placeholders(scan_pool):
            _check_expr(inner, defined_as, errors, where)
        # when 结构
        when = step.get('when')
        if when is not None:
            _validate_when(when, errors, f'{where}.when')
        if op in ('query', 'mutation') and as_name:
            defined_as.add(as_name)
    return errors


def _split_when(when):
    """when → (op, param_key)；无歧义文法：含 exists 键即 op=exists（is 作参数键），
    其余算子的右值键统一 than（is 键已被 exists 参数占用；is/eq 算子同用 than）"""
    if 'exists' in when:
        return 'exists', 'is'
    op = next((k for k in when if k in _WHEN_PARAM), None)
    return (op, _WHEN_PARAM[op]) if op else (None, None)


def _validate_when(when, errors, where):
    """when 守卫结构：恰一个白名单算子键；右操作数参数键按算子定（is/than）"""
    if not isinstance(when, dict):
        errors.append(f'{where}: 须为 JSON 对象')
        return
    op, param_key = _split_when(when)
    if op is None:
        ops_got = [k for k in when if k in _WHEN_PARAM]
        errors.append(f'{where}: 须恰一个白名单算子 {_json([*_WHEN_PARAM])}，收到 '
                      f'{_json(ops_got) if ops_got else "无"}')
        return
    extra = [k for k in when if k != op and k != param_key]
    if extra:
        errors.append(f'{where}: 未知参数键 {_json(extra)}（{op} 算子右值键为 "{param_key}"）')
    if op != 'exists' and param_key not in when:
        errors.append(f'{where}: {op} 算子缺少右值键 "{param_key}"')


# ─── 注册表 ──────────────────────────────────────────────────

_workflows: dict = {}


def register(defn, ctx=None):
    """注册工作流定义（注册即静态校验，白名单外显式 Err；name 全局唯一）。

    ``ctx``：可选定义层门禁上下文（``{'internal': True}`` / ``{'roles': [...]}``）。
    判决唯一在 core（``_core.can_register``，与 ``schema.register`` 同一 MetaPolicy）；
    默认 Open → 全放行。Closed 且 ctx 不过 → 抛 ``ERR_PERMISSION:``（定义不写入）。

    同名同形重复注册幂等通过（对齐 core schema.register 的复跑语义——场景 harness
    每后端复跑同一批用例时必须可重入）；同名异形显式 Err（禁止静默覆盖已注册定义）。
    """
    # 定义层门禁：判决先于静态校验（拒绝即返回，零副作用；与 schema.register 同序）
    if not _core.can_register(ctx):
        name = defn.get('name') if isinstance(defn, dict) else ''
        raise WorkflowError(f'ERR_PERMISSION: 无权注册或覆盖工作流定义 {name or ""}')
    errors = validate_defn(defn)
    if errors:
        raise WorkflowError('WORKFLOW_UNSUPPORTED: ' + '；'.join(errors))
    name = defn['name']
    if name in _workflows:
        if _workflows[name] == defn:
            return defn
        raise WorkflowError(
            f'WORKFLOW_UNSUPPORTED: Workflow 已注册且定义不同，禁止覆盖: {name}')
    _workflows[name] = defn
    return defn


def get(name, ctx=None):
    """按名取 defn（read 白名单过滤；不可见与不存在同形——防枚举）"""
    d = _workflows.get(name)
    if d is None:
        raise KeyError(f'Workflow 未注册: {name}')
    c = get_context() if ctx is None else ctx
    if not _evaluate(d.get('read'), c):
        raise KeyError(f'Workflow 未注册: {name}')
    return d


def list(ctx=None):
    """全部可见工作流名（read 白名单过滤）"""
    c = get_context() if ctx is None else ctx
    return [d['name'] for d in _workflows.values() if _evaluate(d.get('read'), c)]


def clear_workflows():
    """清空工作流注册表（测试隔离 / 动态重建；对齐 ``schema.clear_schemas``）"""
    _workflows.clear()


# ─── 占位符解析（§4.3；执行期） ───────────────────────────────

def _dig(root, path, strict, whole):
    """点路径取值；strict（参数位）取不到显式 Err，非 strict（when 取值位）→ None（三态）"""
    cur = root
    for seg in path.split('.'):
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        else:
            if strict:
                raise _StepFailure(f'占位符 {whole} 解析失败: 路径 "{path}" 不存在')
            return None
    return cur


def _resolve_expr(inner, input_map, ctx_map, strict, whole):
    """解析单个占位符表达式为值（dec 递减；类型不合法显式 Err，不猜）"""
    if inner.startswith(_INPUT):
        return _dig(input_map, inner[len(_INPUT):], strict, whole)
    if inner.startswith(_DEC):
        parts = inner[len(_DEC):].split(',')
        vals = []
        for p in parts:
            p = p.strip()
            if p.startswith('{{') and p.endswith('}}'):
                vals.append(_resolve_expr(p[2:-2], input_map, ctx_map, strict, p))
            else:
                vals.append(float(p) if '.' in p else int(p))
        a, b = vals
        for v, lbl in ((a, 'a'), (b, 'b')):
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise _StepFailure(
                    f'占位符 {whole} dec 操作数 {lbl} 须为数值，收到 {type(v).__name__}')
        r = a - b
        return r
    segs = inner.split('.')
    as_name, path = segs[0], '.'.join(segs[1:])
    if as_name not in ctx_map:
        if strict:
            raise _StepFailure(
                f'占位符 {whole} 引用的 as "{as_name}" 无可用结果'
                '（该步骤可能被 when 跳过或尚未执行）')
        return None
    return _dig(ctx_map[as_name], path, strict, whole)


def _stringify(v):
    """内嵌占位符值的字符串化（仅标量；None/复杂结构显式 Err，禁静默拼 'None'）"""
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, (int, float)):
        return repr(v)
    raise _StepFailure(f'内嵌占位符仅支持标量值，收到 {type(v).__name__}')


def _resolve_str(s, input_map, ctx_map, strict):
    """字符串中的占位符：整值形态 → 保类型替换；内嵌形态 → 字符串化拼接"""
    if _FULL_PH.match(s):
        return _resolve_expr(s[2:-2].strip(), input_map, ctx_map, strict, s)
    if '{{' in s:
        return _INNER_PH.sub(
            lambda m: _stringify(_resolve_expr(m.group(1), input_map, ctx_map, strict, m.group(0))),
            s)
    return s


def _resolve_tree(value, input_map, ctx_map, strict):
    """深度替换结构中的占位符（dict/list 递归；非字符串原样保留）"""
    if isinstance(value, str):
        return _resolve_str(value, input_map, ctx_map, strict)
    if isinstance(value, _LIST_TYPES):
        return [_resolve_tree(v, input_map, ctx_map, strict) for v in value]
    if isinstance(value, dict):
        return {k: _resolve_tree(v, input_map, ctx_map, strict) for k, v in value.items()}
    return value


def _json_eq(a, b):
    """JSON 语义相等：数值类（bool 除外）交叉互比，其余须同型（对齐 true≠1）"""
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if type(a) is type(b):
        return a == b
    return False


# ─── when 守卫判定 ───────────────────────────────────────────

def _eval_when(when, input_map, ctx_map):
    """when 判定（取值位非 strict：路径取不到 → null 三态，服务 exists/is null 断言）"""
    op, param_key = _split_when(when)
    left = _resolve_tree(when[op], input_map, ctx_map, False)
    right = _resolve_tree(when[param_key], input_map, ctx_map, False) if param_key in when else None
    if op == 'exists':
        present = left is not None
        # is 键缺省 = 断言存在；显式 is（null/false → 期望不存在，其余 → 期望存在）
        want = (right is not None and right is not False) if param_key in when else True
        return present == want
    if op in ('is', 'eq'):
        return _json_eq(left, right)
    if op == 'ne':
        return not _json_eq(left, right)
    # lt / lte / gt / gte：数值比较；类型不可比显式 Err（不猜）
    try:
        if op == 'lt':
            return left < right
        if op == 'lte':
            return left <= right
        if op == 'gt':
            return left > right
        return left >= right
    except TypeError:
        raise _StepFailure(
            f'when 算子 {op} 的操作数类型不可比: {type(left).__name__} vs {type(right).__name__}') from None


# ─── 执行器（线性步骤循环 + fail-fast；§2 生命线） ─────────────

def _prescan_sources(defn, route_override, now, ctx):
    """预扫全部 mutation 步骤的数据源集合（run 级原子包络依据；plan 是纯函数不执行）。

    返回 None = 预扫失败 → 降级裸跑（单步骤内原子，emit 反馈事件，禁静默）。
    占位符原样参与规划（plan 只看结构键，不校验占位符字符串值——实测）。
    """
    sources = set()
    for step in defn['steps']:
        if step['op'] != 'mutation':
            continue
        try:
            pool = _new_id_pool(step['model'], step['data'])
            plan = _call(lambda s=step, p=pool: _core.plan_mutation(
                s['model'], s['data'], now, p, ctx, route_override))
            sources |= _sources_of(plan)
        except Exception as e:  # 预扫失败降级裸跑（显式声明，禁静默）
            _emit_feedback({
                'type': 'workflow_prescan_failed',
                'code': 'workflowPrescanFailed',
                'layer': 'workflow',
                'message': f'run 源预扫失败（步骤 model={step["model"]}）：{e}；'
                           '本 run 降级裸跑（跨步骤非原子，单步骤内仍原子）',
                'hint': '检查该 mutation 步骤的 model/结构是否可规划；预扫仅提取数据源，不影响执行',
            })
            return None
    return sources


def _warn_non_atomic(sources):
    """多源 run：无法原子 → 程序化声明（允许顺序执行，禁止静默；对齐 non_atomic_write 语义）"""
    listed = sorted(sources)
    _emit_feedback({
        'type': 'workflow_non_atomic',
        'code': 'workflowNonAtomic',
        'layer': 'workflow',
        'message': f'本 run 的写步骤跨 {len(listed)} 个数据源（{", ".join(listed)}）：'
                   '无法原子，按顺序执行（跨步骤非原子）',
        'hint': '把写步骤收敛到单一数据源；跨源强一致首批请拆为宿主代码编排',
        'sources': listed,
    })


async def _exec_step(step, input_map, ctx_map, route_override, index):
    """执行单步骤（when 已判定通过）：query / mutation / fail"""
    from . import crud  # 延迟导入避免环
    op = step['op']
    if op == 'query':
        params = _resolve_tree(step.get('params'), input_map, ctx_map, True)
        rows = await crud.query(step['gql'], params, route_override)
        if len(rows) > 1:
            raise _StepFailure(
                f'query 步骤 "{step["as"]}" 返回 {len(rows)} 行（期望 ≤1：占位符引用要求唯一结果；'
                '请收紧条件或加 $limit:1）', index)
        return rows[0] if rows else None
    if op == 'mutation':
        data = _resolve_tree(step['data'], input_map, ctx_map, True)
        if step.get('upsert'):
            match = _resolve_tree(step['match'], input_map, ctx_map, True)
            return await crud.upsert(step['model'], match, data, None, route_override)
        return await crud.mutation(step['model'], data, route_override)
    # fail：显式业务断言失败（§4.2）——message 支持内嵌占位符丰富错误文案
    raise _StepFailure(_resolve_str(step['message'], input_map, ctx_map, True), index)


async def _run_steps(defn, input_map, ctx_map, trace, dry, route_override):
    """线性步骤循环：迹与 defn.steps 一一对应（skipped/wouldRun/ran/failed 均留痕）。

    失败抛 _StepFailure（带步骤下标）——外层收尾统一落 failed 终态 + 整体回滚。
    """
    for i, step in enumerate(defn['steps']):
        op = step['op']
        as_name = step.get('as')
        # a. when 判定 → 不满足记 skipped（显式留痕，绝不静默跳过），后续步骤照常
        when = step.get('when')
        if when is not None:
            try:
                ok = _eval_when(when, input_map, ctx_map)
            except _StepFailure as e:
                trace.append({'as': as_name, 'op': op, 'state': 'failed', 'error': str(e)})
                e.index = i
                raise
        else:
            ok = True
        if not ok:
            trace.append({'as': as_name, 'op': op, 'state': 'skipped'})
            continue
        # b. dry-run：mutation / fail 不执行，记 wouldRun（§0.7）
        if dry and op in ('mutation', 'fail'):
            trace.append({'as': as_name, 'op': op, 'state': 'wouldRun'})
            continue
        # c/d. 解析占位符（步骤内）→ 执行 → 结果按 as 入上下文 → 迹追加
        entry = {'as': as_name, 'op': op, 'state': 'ran'}
        try:
            result = await _exec_step(step, input_map, ctx_map, route_override, i)
        except _StepFailure as e:
            trace.append({**entry, 'state': 'failed', 'error': str(e)})
            if e.index is None:
                e.index = i
            raise
        except Exception as e:  # 步骤 Err → fail-fast（包一层定位，错误不吞）
            msg = f'步骤 {i}（{op}）执行失败: {e}'
            trace.append({**entry, 'state': 'failed', 'error': msg})
            raise _StepFailure(msg, i) from e
        if as_name:
            ctx_map[as_name] = result
        entry['result'] = result
        trace.append(entry)


@contextmanager
def _internal_ctx():
    """干净 internal 上下文（{internal: True}，丢弃触发者 roles）

    不用 permission.run_as_internal：它保留原 roles，而 core 对 guest **硬拒写**先于
    internal 放行（permission.rs::can_write_schema）——guest 触发的 rejected 落库会
    被自己的 guest 角色挡住。审计通道的身份必须与触发者权限解耦。
    """
    token = _permission._ctx.set({'internal': True})
    try:
        yield
    finally:
        _permission._ctx.reset(token)


async def _persist_write(doc):
    """run 记录写入（internal 上下文——__workflowRun write 显式空名单仅放行 internal/admin）"""
    def _do():
        from . import crud
        return crud.mutation(_BUILTIN_NAME, doc)
    with _internal_ctx():
        return await _do()


async def _persist_update(rid, patch):
    """run 记录收尾更新（事务外独立提交：业务事务回滚不影响失败 run 可查——铁律 B）"""
    def _do():
        from . import crud
        return crud.update(_BUILTIN_NAME, {'_id': rid}, patch)
    with _internal_ctx():
        return await _do()


async def run(name, input_map=None, *, dry_run=False, route_override=None):
    """触发工作流 → 完整 run 文档（§3）

    统一契约：执行期一切业务失败（步骤 Err / fail 步骤 / 权限拒绝）都表达为 run 终态
    （failed / rejected）+ error 字段返回，不抛异常；编程错误（未注册 / input 非法 /
    defn 校验失败）照常抛错。dry-run 下 query 真实执行（只读安全），mutation/fail 记
    wouldRun。跨步骤原子性：单源 run 整体原子（N1 实测可行——外层 run_atomic 包住整个
    步骤循环，内层 mutation 嵌套并入）；多源 / 预扫失败按顺序执行并发反馈事件（禁静默）。
    run 记录时序（§2 生命线）：先落 running（独立提交，进程崩溃可见）→ 步骤事务 →
    收尾 update 终态（事务外独立提交——业务回滚不影响失败 run 可查，铁律 B）。
    """
    ensure_builtin()
    d = _workflows.get(name)
    if d is None:
        raise KeyError(f'Workflow 未注册: {name}')
    if input_map is not None and not isinstance(input_map, dict):
        raise TypeError(f'run(name, input) 的 input 须为 dict，收到 {type(input_map).__name__}')
    inp = dict(input_map or {})
    ctx = get_context()
    now = int(time.time() * 1000)
    dry = bool(dry_run)

    run_doc = {
        'workflow': name, 'status': 'running', 'input': inp, 'steps': [],
        'error': None, 'stepIndex': None, 'dryRun': dry, 'now': now,
    }

    # 1. 权限（§0.6）：fail-secure 优先于 dry-run（N3 实测裁决）——require_context 开启
    #    且无 ctx → rejected 拒跑；run 白名单外 → rejected（落库可审计，一次落终态）
    require = _schema_require_context()
    rejected = None
    if require and ctx is None:
        rejected = 'ERR_NO_CONTEXT: 上下文强制开启，无 ctx 拒跑（fail-secure 优先于 dry-run）'
    elif not _evaluate(_run_whitelist(d), ctx):
        rejected = (f'run 白名单拦截: 触发者角色不在 run 白名单内'
                    f'（{_json(_run_whitelist(d) or [])}）')
    if rejected is not None:
        run_doc.update(status='rejected', error=rejected)
        saved = await _persist_write(run_doc)
        return _with_meta(run_doc, saved)

    # 2. 状态落库 running（先于步骤：中断可观测）
    saved = await _persist_write(run_doc)
    rid = saved.get('_id')

    # 3. 步骤循环（单源时整体原子；失败 _StepFailure 冒泡出事务 → 整体回滚）
    trace = run_doc['steps']
    ctx_map: dict = {}

    async def _execute():
        await _run_steps(d, inp, ctx_map, trace, dry, route_override)

    status, error, step_index = ('drySucceeded', None, None) if dry else ('succeeded', None, None)
    try:
        if not dry:
            sources = _prescan_sources(d, route_override, now, ctx)
            if sources is not None and len(sources) == 1:
                await run_atomic(sources, _execute)
            else:
                if sources is not None and len(sources) > 1:
                    _warn_non_atomic(sources)
                await _execute()
        else:
            await _execute()
    except _StepFailure as e:
        status = 'dryFailed' if dry else 'failed'
        error = str(e)
        step_index = e.index
    # 4. 收尾终态（事务外独立提交）
    run_doc.update(status=status, error=error, stepIndex=step_index)
    await _persist_update(rid, {'status': status, 'steps': trace,
                                'error': error, 'stepIndex': step_index})
    return _with_meta(run_doc, saved)


def _with_meta(run_doc, saved):
    """内存 run 文档补落库元数据（_id / createdAt / updatedAt）"""
    return {**run_doc, '_id': saved.get('_id'),
            'createdAt': saved.get('createdAt'), 'updatedAt': saved.get('updatedAt')}


# 模块导入即自举内建 run 表（幂等；零配置——import 即可 run）
ensure_builtin()
