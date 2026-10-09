"""AI 问数（L1）—— ``ask()`` 唯一入口 + ``describe_for_ai`` schema 摘要生成器

《AI能力接入设计-L1问数档.md》§4 的宿主侧两个新组件，把既有能力接线：

    用户问题 → ① describe_for_ai(ctx) 权限过滤摘要
             → ② LLM（注入式客户端）翻译为 {"gql","params"}
             → ③ text2query 档位视图内规划期校验（core 判决：语法/档位/权限/硬限）
             → ④ crud.query 执行（只读）
             → ⑤ 失败结构化回喂 LLM 重试（≤ max_retries 次），耗尽抛 AskExhausted

护栏面（D5，服务端硬编码，LLM 零可触）：
  - 档位 = text2query：本模块硬编码以作用域档位视图（with_scope + with_policy）包裹全部执行；
  - 用户上下文 = ctx：服务端注入参数，经 ``permission.scoped_context`` 进执行面，
    绝不进入任何 LLM 消息；core 档位门禁强制无 ctx 即拒（fail-secure，A4）；
  - route_override = None：硬编码（CWE-639；Host 兜底 ``_guard_route_override`` 双保险）；
  - 行数/深度/联邦硬限：core 常量（T2Q_MAX_ROWS=1000 / T2Q_MAX_DEPTH=3 / ...，A5）；
  - 只读：编排面仅 ``crud.query``（mutation/remove 属 L2，禁入）。

LLM 输出永远当不可信输入：唯一产出形状 ``{"gql","params"}`` 单 JSON 对象（D3），
严格 JSON 解析、禁正则容错提取；幻觉最坏后果是「规划失败 + 结构化错误回喂」，
不可能变成不受控命令。一切失败结构化显式暴露（no-error-masking：是错就是错，
禁降级、禁返回空结果——「问数失败」就是失败，交上层裁决，D4）。

并发隔离（R2）：``text2query`` 档位与反馈 sink 不再全局切换，改由 ``with_scope`` 承载
「一请求一档位视图 / 一 sink」（派生视图见 rust-store 02，作用域形态见 03 §4.4）；
同一进程内并发调用 ``ask()`` 各自独立、互不串扰——「宿主须串行化 / 每任务独享进程」
的要求随之解除（fail-open 姿态与 core 判决语义均不变）。
"""

from __future__ import annotations

import importlib.resources
import json
from dataclasses import dataclass, field

from . import crud, feedback, permission, schema
from .llm import get_llm as _get_llm
from .scope import with_scope

__all__ = ['AskExhausted', 'AskResult', 'ask', 'describe_for_ai']


# ─── 轨迹载体 ──────────────────────────────────────────────────


@dataclass
class AskResult:
    """问数成功结果：查询数据 + 全部尝试轨迹 + 反馈事件

    - ``data``：查询结果数组（crud.query 原样返回）；
    - ``attempts``：每次尝试一条（``llm_raw`` / ``gql`` / ``params`` / ``rows``），
      最后一轮为成功轮——**不含任何 error 键**（成功态无错误字段，no-error-masking 正向断言）；
    - ``events``：执行期接管到的反馈事件（拦截/降级告警；无拦截时为空列表）。
    """

    data: list
    attempts: list = field(default_factory=list)
    events: list = field(default_factory=list)


class AskExhausted(Exception):
    """重试耗尽：max_retries 次回喂重试后仍失败（D4：显式失败，不降级、不返回空结果）

    ``attempts`` / ``events`` 携带全部尝试轨迹；消息内嵌最后一轮结构化错误，
    上游不读属性也能看到失败原因。
    """

    def __init__(self, attempts, events):
        last_error = (attempts[-1].get('error') if attempts else None) or {'code': 'unknown'}
        super().__init__(
            f'ask() 重试耗尽（共 {len(attempts)} 次尝试全部失败），不降级、不返回空结果；'
            f'最后一轮错误: {json.dumps(last_error, ensure_ascii=False)}；'
            '完整轨迹见 .attempts / .events'
        )
        self.attempts = attempts
        self.events = events


# ─── 组件 A：schema 摘要生成器 ─────────────────────────────────

# 计算列收窄告警去重（同 (model, compute) 只告警一次，对齐 schema.list 的 _dup_signatures 先例）
_COMPUTE_SKIP_SIGS: set = set()


def _field_type(fdef):
    """字段类型字符串（str 形态原样；dict 形态取 type；其余显式 None，不伪造）"""
    if isinstance(fdef, str):
        return fdef
    if isinstance(fdef, dict):
        return fdef.get('type')
    return None


def _emit_compute_skipped(model, compute):
    sig = (model, compute)
    if sig in _COMPUTE_SKIP_SIGS:
        return
    _COMPUTE_SKIP_SIGS.add(sig)
    feedback.emit({
        'type': 'ask_summary_compute_skipped',
        'code': 'askSummaryComputeSkipped',
        'layer': 'host',
        'message': (f'AI 摘要收窄：模型 {model} 的计算列 {compute} 配置了 read 白名单，'
                    'core-py 绑定未导出 readable_computes 判决，为不越权暴露已从摘要排除'),
        'hint': ('去掉该计算列的 read 配置可进摘要；或在 core-py 导出 readable_computes '
                 '后接入 describe_for_ai（执行面权限判决始终在 core，此处仅摘要暴露面收窄）'),
        'model': model,
        'compute': compute,
    })


def describe_for_ai(ctx=None) -> list[dict]:
    """输出 LLM 可读的 schema 摘要（紧凑 JSON 数组，每模型一条）。

    过滤规则（顺序固定，设计文档 §4.1）：
      1. 排除归档表（名称以 ``Deleted`` 结尾，对齐 store-api 派生路由先例）；
      2. 模型级按 core ``can_read``；字段/关系按 core 角色可读集（``readable_fields`` /
         ``readable_relations``，列级白名单）；计算列按声明 read 白名单**保守收窄**——
         core-py 未导出 ``readable_computes`` 判决，配了 read 的计算列不进摘要并 emit
         告警（宁缺勿泄；执行面判决始终在 core，收窄只影响摘要暴露面）；
         无 ctx → 仅暴露模型名与字段名，不暴露类型细节（防探针）；
      3. 不输出 indexes / datasource / database / schema（运维细节不进 prompt）。
    """
    summaries = []
    for name in schema.list():
        if name.endswith('Deleted'):
            continue
        mirror = schema.get(name)
        if ctx is None:
            summaries.append({'name': name, 'fields': sorted(mirror['fields'])})
            continue
        if not permission.can_read_schema(name, ctx):
            continue
        readable = set(permission.get_readable_fields(name, ctx))
        fields = {fname: _field_type(fdef)
                  for fname, fdef in mirror['fields'].items() if fname in readable}
        for fname in sorted(readable - set(mirror['fields'])):
            # core 自动补的时间戳字段（createdAt/updatedAt）不在 Host 镜像——类型显式留白
            fields[fname] = None
        relations = {}
        readable_rels = set(permission.get_readable_relations(name, ctx))
        for rname, rdef in mirror['relations'].items():
            if rname in readable_rels:
                relations[rname] = {'model': rdef.get('model'), 'type': rdef.get('type')}
        computes = {}
        for cname, cdef in mirror['computes'].items():
            if cdef.get('read') is not None:
                _emit_compute_skipped(name, cname)
                continue
            entry = {}
            if cdef.get('agg') is not None:
                entry['agg'] = cdef['agg']
            if cdef.get('type') is not None:
                entry['type'] = cdef['type']
            computes[cname] = entry
        summaries.append({
            'name': name, 'fields': fields,
            'relations': relations, 'computes': computes,
        })
    return summaries


# ─── 组件 B：ask() 编排器 ──────────────────────────────────────

_KNOWLEDGE_CACHE: str | None = None


class _BadLlmOutput(Exception):
    """LLM 输出未通过严格解析（携带结构化 detail 供回喂）——内部控制流，不外穿"""

    def __init__(self, detail):
        super().__init__(detail.get('detail', 'badLlmOutput'))
        self.detail = detail


def _load_knowledge(knowledge):
    """知识文本：传参优先（测试/自定义）；缺省读包内 ask_knowledge.md（text-to-query 裁剪版）"""
    global _KNOWLEDGE_CACHE
    if knowledge is not None:
        return knowledge
    if _KNOWLEDGE_CACHE is None:
        _KNOWLEDGE_CACHE = (
            importlib.resources.files(__package__) / 'ask_knowledge.md'
        ).read_text(encoding='utf-8')
    return _KNOWLEDGE_CACHE


def _build_system_prompt(summary, knowledge):
    """system prompt = 翻译知识 + 摘要 + 输出契约（D3；含 "json" 字样满足 json_mode 预检）"""
    return (
        f'{knowledge}\n\n'
        '## 可用模型摘要（当前用户可见；字段/关系/计算列已按权限过滤）\n'
        f'{json.dumps(summary, ensure_ascii=False)}\n\n'
        '## 输出契约（唯一产出形状）\n'
        '只输出一个 json 对象：{"gql": "<GQL 查询串>", "params": {<参数对象>}}。\n'
        '- 条件值一律参数化：GQL 内用 @key 引用，真实值放 params（键 = 去掉 @ 的引用名）；\n'
        '  禁止内联值进 GQL 串。\n'
        '- 禁止输出解释文字、markdown 代码围栏、或除 gql/params 外的任何键。\n'
    )


def _parse_llm_output(raw):
    """严格 JSON 解析 + 形状校验（D3）；失败抛 ``_BadLlmOutput``（禁正则容错提取）"""
    def fail(detail):
        return _BadLlmOutput({
            'code': 'badLlmOutput',
            'message': 'LLM 输出不是合法的 {"gql","params"} 单 JSON 对象',
            'detail': detail,
            'raw': (raw or '')[:500],
        })

    if not isinstance(raw, str):
        raise fail(f'输出不是字符串: {type(raw).__name__}')
    try:
        parsed = json.loads(raw)
    except ValueError as e:
        raise fail(f'JSON 解析失败: {e}') from e
    if not isinstance(parsed, dict):
        raise fail(f'顶层不是 JSON 对象: {type(parsed).__name__}')
    if set(parsed) != {'gql', 'params'}:
        raise fail(f'键集合必须恰为 gql/params，实际: {sorted(parsed)}')
    gql, params = parsed['gql'], parsed['params']
    if not isinstance(gql, str) or not gql.strip():
        raise fail('gql 必须为非空字符串')
    if not isinstance(params, dict):
        raise fail('params 必须为 JSON 对象')
    return gql, params


def _error_from_exception(e, round_events):
    """执行失败 → 回喂错误对象：core/Host 拦截事件**原样透传**（禁改写禁摘要）；
    无事件兜底构造时只给确证字段（feature/layer 留白不伪造）"""
    for ev in round_events:
        if ev.get('type') == 'profile_blocked':
            return dict(ev)
    if isinstance(e, permission.PermissionError):
        return {'code': 'permissionDenied', 'message': str(e)}
    if isinstance(e, crud.ProfileViolation):
        return {'code': 'profileBlocked', 'message': str(e)}
    return {'code': 'planError', 'message': str(e)}


async def _ask_body(*, client, messages, attempts, events, round_events, max_retries):
    """ask 执行主体（作用域内运行：档位视图 / 反馈 sink / 用户 ctx 均已就位）。

    R2 起本函数（及整个执行面）**不得**出现 ``set_profile`` / ``set_sink`` / ``set_meta``
    全局切换——档位与 sink 由 ``with_scope`` 承载（03 §4.4，grep 核销为零）。
    """
    for _ in range(1 + max_retries):
        round_events.clear()
        # LLM 客户端异常（llmNetworkError/llmHttpError/llmEmptyContent）原样穿透
        raw = await client(messages)
        attempt = {'llm_raw': raw}
        try:
            gql, params = _parse_llm_output(raw)
        except _BadLlmOutput as e:
            attempt['error'] = e.detail
            attempts.append(attempt)
            messages.append({'role': 'assistant', 'content': raw})
            messages.append({'role': 'user', 'content': json.dumps(
                {'error': e.detail}, ensure_ascii=False)})
            continue
        attempt['gql'] = gql
        attempt['params'] = params
        try:
            # route_override 硬编码 None（受信参数，禁 AI 侧指定，D5/CWE-639）
            data = await crud.query(gql, params, None)
        except Exception as e:
            attempt['error'] = _error_from_exception(e, round_events)
            attempts.append(attempt)
            messages.append({'role': 'assistant', 'content': raw})
            messages.append({'role': 'user', 'content': json.dumps(
                {'error': attempt['error']}, ensure_ascii=False)})
            continue
        attempt['rows'] = len(data)
        attempts.append(attempt)
        return AskResult(data=data, attempts=attempts, events=events)
    # 循环走完（重试耗尽）→ None，由入口抛 AskExhausted
    return None


async def ask(question: str, *, llm, ctx: dict, max_retries: int = 3,
              knowledge: str | None = None) -> AskResult:
    """AI 问数唯一入口（L1 只读）：自然语言 → LLM 翻译 → 受控沙箱执行 → 结构化回喂。

    参数：
      question    : 自然语言问题（非空字符串）。
      llm         : 注册名（str，经 ``llm.get_llm``）或现成客户端
                    （协议 ``async def(messages) -> str``，D2/D6 注入式，py-store 零 SDK）。
      ctx         : 服务端构造的用户上下文（``{'userId': ..., 'roles': [...]}``）；
                    None 直接拒绝（fail-secure）；LLM 永远碰不到本参数（D5）。
      max_retries : 失败后的最大**重试**次数（总尝试 ≤ 1 + max_retries）；耗尽抛 ``AskExhausted``。
      knowledge   : 覆盖 system prompt 知识文本（缺省用包内 ask_knowledge.md）。

    返回 ``AskResult(data, attempts, events)``；LLM 客户端自身的异常（网络/HTTP/空 content，
    ``RuntimeError(dict)`` 结构化）**原样穿透**——回喂循环只裁决「翻译质量/查询合法性」，
    链路故障显式失败不重试。

    轨迹契约：成功轮 attempt 不含 error 键；每次失败以 user 消息追加
    ``{"error": {...}}``（core/Host 结构化错误原样透传，§4.3）。
    """
    if not isinstance(question, str) or not question.strip():
        raise ValueError('ask(question) 需要非空自然语言问题字符串')
    if ctx is None:
        # fail-secure：core text2query 档强制 ctx（ensure_profile_ctx）——入口先拒，
        # 免一次注定失败的 LLM 调用；空 dict 等其余边界交 core/权限层如实判决
        raise ValueError(
            'ask() 需要服务端构造的用户上下文 ctx（如 {"userId": ..., "roles": [...]}）；'
            'ctx 属受信参数，LLM 永远碰不到（D5）')
    if not isinstance(max_retries, int) or max_retries < 0:
        raise ValueError(f'max_retries 必须为非负整数，实际: {max_retries!r}')
    client = llm if callable(llm) else _get_llm(llm)
    system = _build_system_prompt(describe_for_ai(ctx), _load_knowledge(knowledge))

    messages = [
        {'role': 'system', 'content': system},
        {'role': 'user', 'content': question},
    ]
    attempts: list[dict] = []
    events: list[dict] = []
    round_events: list[dict] = []

    def _collect(event):
        events.append(event)
        round_events.append(event)

    # R2（03 §4.4）：一次性派生 text2query 档位视图，档位与反馈 sink 随作用域承载——
    # 不再全局 set_profile / set_sink，「进入即改进程档位 + 退出恢复」的串扰根源随之消除。
    view = schema.get_core().with_policy({'profile': 'text2query'})
    # sink：本作用域 emit 的事件只进本次 events（feedback.emit 优先取 current_scope().sink）；
    # meta（ns 标签）不在此伪造——ask 无 tenant/env 入参，沿用进程级 feedback.set_meta 注入值。
    # ctx 经 permission.scoped_context 注入执行面（core 档位门禁强制 ctx，fail-secure，A4）
    with with_scope(view, sink=_collect), permission.scoped_context(ctx):
        result = await _ask_body(
            client=client, messages=messages, attempts=attempts,
            events=events, round_events=round_events, max_retries=max_retries)
    if result is not None:
        return result
    raise AskExhausted(attempts, events)
