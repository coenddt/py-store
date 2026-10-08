"""
命令执行（唯一 IO 边界） + 占位符替换 + core 调用包装

全部纯逻辑（GQL 解析、权限、命令规划、结果后处理）都在 Rust core；
本模块只做 Host 三件事里最底层的一件：把 core 产出的 Command JSON
路由到对应数据源连接并执行。不确定性输入由本层供给（now 时钟、newId 随机 ID）。

路由规则见 ``..datasource``：命令自带 ``source`` / ``database`` / ``schema`` / ``collection``
定位四元组，按 ``source`` 选连接、``database``（PG 另加 ``schema``）定位连接内的库/schema
（Mongo 双形态严格校验），
Mongo 走原生驱动，SQL 走 ``translate → exec``。对齐 ``nodejs-store/src/crud/exec.js``。
"""

import re
import time
from collections.abc import Mapping

from .. import datasource as _datasource
from ..executors.mongo import exec_mongo as _exec_mongo
from ..feedback import emit as _emit_feedback
from ..naming import _to_logical, _to_mongo
from ..permission import PermissionError, get_context
from ..schema import get as _schema_get

_PHASE1_IDS = re.compile(r'^\{\{phase1\.ids\}\}$')
_STEP_PH = re.compile(r'^\{\{step\.(\d+)\._id\}\}$')

# 权限类错误识别：core 权限错误统一携带 `ERR_PERMISSION:` 稳定前缀（见 core
# `command/mod.rs::ERR_PERM_PREFIX`），按**前缀**映射而非具体文案 —— core 文案
# 可自由调整，映射不随文案漂移而静默失效。构造 PermissionError 时剥离前缀。
_PERM_PREFIX = 'ERR_PERMISSION:'

# 档位类错误识别：core text2query 档门禁统一携带 `ERR_TEXT2QUERY:` 稳定前缀
# （见 core `command/mod.rs::ERR_TEXT2QUERY`），同上按前缀映射。命中即 emit
# 反馈事件 `profile_blocked`（自动反馈原则：允许拦截，禁止静默）。
_PROFILE_PREFIX = 'ERR_TEXT2QUERY:'

# 从 core 文案 `... [$feature]（功能收缩）` 中提取门禁项名；无 `[..]` 时留白（None），
# 不伪造 feature —— 缺值必须显式暴露（禁静默兜底）。
_FEATURE_RE = re.compile(r'\[(.+?)\]')

# 档位拦截反馈的统一提示（反馈事件契约 §4.6 的一部分；单点定义防文案漂移）
_PROFILE_HINT = ('上游（LLM 产出的 GQL / 调用方入参）越界；'
                 'text2query 档白名单见 SKILL.md §后端无关性与边界')


class ProfileViolation(Exception):
    """档位（profile）拒绝：text2query 档违反功能收缩 / 硬限制

    与权限错误（``PermissionError``，403）区分：档位拒绝是**调用方合约违反**（400），
    非授权问题（见执行文档 §4.4）。实参 ``status`` 供上层（HTTP 网关等）映射响应码。
    """

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def set_db(db):
    """单库简写：等价于 ``set_connections({'default': db})``"""
    _datasource.set_connections({_datasource.DEFAULT_SOURCE: db})


def set_connections(connections):
    """设置数据源连接映射（见 ``..datasource.set_connections``）"""
    _datasource.set_connections(connections)


def _get_db(source=None):
    """取指定数据源连接（缺省 ``default``）"""
    return _datasource.get_connection(source or _datasource.DEFAULT_SOURCE)


def _now_for(schema_name):
    """按 schema 的 timestamps 单位产出当前时间戳（'s' → 秒，其余/未启用 → 毫秒）"""
    unit = _schema_get(schema_name).get('timestampUnit')
    return int(time.time()) if unit == 's' else int(time.time() * 1000)


def _ctx():
    return get_context()


def _call(fn):
    """绑定层调用包装：

      - 权限类错误（``ERR_PERMISSION:`` 前缀）→ ``PermissionError``
      - 档位类错误（``ERR_TEXT2QUERY:`` 前缀）→ emit ``profile_blocked`` 反馈 + ``ProfileViolation``

    按前缀映射而非具体文案（core 文案可自由调整，映射不随文案漂移而静默失效）。
    其余异常原样上抛（不吞错）。
    """
    try:
        return fn()
    except Exception as e:
        msg = str(e)
        if msg.startswith(_PERM_PREFIX):
            raise PermissionError(msg[len(_PERM_PREFIX):]) from e
        if msg.startswith(_PROFILE_PREFIX):
            detail = msg[len(_PROFILE_PREFIX):]
            m = _FEATURE_RE.search(detail)
            _emit_feedback({
                'type': 'profile_blocked',
                'code': 'profileBlocked',
                'layer': 'core',
                'profile': 'text2query',
                'feature': m.group(1) if m else None,
                'message': detail,
                'hint': _PROFILE_HINT,
            })
            raise ProfileViolation(detail) from e
        raise


# ─── 命令执行（唯一 IO 边界） ────────────────────────────────

async def _exec_on(source, cmd):
    """在指定数据源上执行命令（Mongo 走原生驱动，SQL 走 translate → exec；
    事务 / 会话作用域内经 datasource.resolve_connection 落到事务专用连接）"""
    connection = await _datasource.resolve_connection(source, _datasource.is_write_cmd(cmd))
    if isinstance(connection, Mapping) and connection.get('kind') == 'mongo':
        # Mongo 事务视图：db 按命令 database 解析，session 透传给驱动
        db = _datasource.mongo_db(connection['conn'], source, cmd.get('database'))
        return _to_logical(await _exec_mongo(db, _to_mongo(cmd), session=connection.get('session')), cmd)
    db = _datasource.mongo_db(connection, source, cmd.get('database'))
    if db is not None:
        # Mongo 物理名翻译（逻辑 → camelCase）；执行后按 schema 逆表回映射（物理 → 逻辑）
        return _to_logical(await _exec_mongo(db, _to_mongo(cmd)), cmd)
    return await _datasource.exec_sql(source, connection, cmd)


async def _exec(cmd):
    """Command JSON → 按命令自带的 ``source`` 路由（不按 collection 反查）"""
    return await _exec_on(cmd.get('source') or _datasource.DEFAULT_SOURCE, cmd)


def _sources_of(plan):
    """从规划结果中提取数据源集合（探针 / 写 / 查 / 删 / mutation 步骤命令）

    ``sources`` 必须由规划结果提取，不得写死 ``default``（多租户路由场景下的
    源由 core 规划决定）。
    """
    out = set()
    for key in ('needsProbe', 'command', 'findCommand', 'deleteCommand'):
        cmd = (plan or {}).get(key) or {}
        if cmd:
            out.add(cmd.get('source') or _datasource.DEFAULT_SOURCE)
    for step in (plan or {}).get('steps') or []:
        cmd = (step or {}).get('command') or {}
        if cmd:
            out.add(cmd.get('source') or _datasource.DEFAULT_SOURCE)
    # 触发链命令的源（plan_insert 直接产出 triggers；plan_update 二次规划才产出）
    for t in (plan or {}).get('triggers') or []:
        cmd = (t or {}).get('command') or {}
        if cmd:
            out.add(cmd.get('source') or _datasource.DEFAULT_SOURCE)
    return out or {_datasource.DEFAULT_SOURCE}


def declare_trigger_sources(base_sources, triggers):
    """触发链触及源的原子性声明（update 专用：其 triggers 在事务内二次规划后才产出，
    顶层 ``_sources_of`` 已不及）——触发源超出已声明源集时发 ``non_atomic_write``
    （含全部涉及源），此后顺序执行；会话内不发声明（跨源写由既有 fail-closed 拒绝）。
    对齐 nodejs-store ``declareTriggerSources``。"""
    uniq = set(base_sources)
    for t in triggers or ():
        cmd = (t or {}).get('command') or {}
        if cmd:
            uniq.add(cmd.get('source') or _datasource.DEFAULT_SOURCE)
    if len(uniq) > 1 and _datasource.current_session() is None:
        _warn_multi_source(uniq)
    return uniq


def _warn_multi_source(sources):
    """多源写：无法原子 → 程序化声明 nonAtomic（允许顺序执行，禁止静默）"""
    listed = sorted(sources)
    _emit_feedback({
        'type': 'non_atomic_write',
        'code': 'nonAtomic',
        'layer': 'crud',
        'message': ('本次写调用跨 %d 个数据源（%s）：无法原子，按顺序执行（非原子）'
                    % (len(listed), ', '.join(listed))),
        'hint': '把写操作收敛到单源；或在 store.session() 内执行以便跨源写被拒（fail-closed）',
        'sources': listed,
    })


async def run_atomic(sources, fn):
    """顶层 API 调用的原子包络：无会话 + 单一源（SQL 或 Mongo）→ 包事务；否则原样执行

    - 会话内：事务边界由会话统一管理，直接执行（不嵌套）；
    - 单一 SQL 源：包事务（原子）；
    - 多源：无法原子 → 程序化声明 ``nonAtomic``（反馈通道），再按顺序原样执行；
    - 单一 Mongo 源：按探测结果包 session 事务或降级声明（见 ``run_in_transaction``）；
    - 未配置源：按原样执行；
    - sources 由调用方从「规划结果」中提取（``_sources_of``），命令源与事务源一致。
    """
    if _datasource.current_session() is not None:
        return await fn()
    uniq = {s or _datasource.DEFAULT_SOURCE for s in sources}
    if len(uniq) == 1:
        source = next(iter(uniq))
        if _datasource.has_connection(source) and (
                _datasource.is_sql_source(source) or _datasource.is_mongo_source(source)):
            return await _datasource.run_in_transaction(source, fn)
    elif len(uniq) > 1:
        _warn_multi_source(uniq)
    return await fn()


def _substitute(value, resolver):
    """深度替换命令中的占位符（命中 resolver 返回非字符串时替换）"""
    if isinstance(value, str):
        return resolver(value)
    if isinstance(value, list):
        return [_substitute(v, resolver) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, resolver) for k, v in value.items()}
    return value


def resolve_placeholders(command, ids=None, steps=None):
    """Host 契约：把命令中的占位符替换为执行结果

      - ``{{phase1.ids}}``   → 两阶段查询第一步取回的 id 数组（整值替换）
      - ``{{step.<N>._id}}`` → mutation 第 N 步执行结果的 _id

    未命中的占位符原样保留（便于定位 core 与 Host 的契约漂移）。
    Node 侧 ``nodejs-store/src/crud.js`` 的 ``resolvePlaceholders`` 为同语义实现，
    两侧共测 ``rust-store/fixtures/host/placeholders.json``。
    """
    steps = steps or []

    def resolver(s):
        if _PHASE1_IDS.match(s):
            return ids if ids is not None else s
        m = _STEP_PH.match(s)
        if m:
            idx = int(m.group(1))
            if idx < len(steps):
                return steps[idx]
        return s

    return _substitute(command, resolver)
