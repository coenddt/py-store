"""
命令执行（唯一 IO 边界） + 占位符替换 + core 调用包装

全部纯逻辑（GQL 解析、权限、命令规划、结果后处理）都在 Rust core；
本模块只做 Host 三件事里最底层的一件：把 core 产出的 Command JSON
路由到对应数据源连接并执行。不确定性输入由本层供给（now 时钟、newId 随机 ID）。

路由规则见 ``..datasource``：命令自带 ``source`` / ``namespace`` 三元组，按 ``source``
选连接、``namespace`` 定位连接内的库（Mongo 双形态严格校验），
Mongo 走原生驱动，SQL 走 ``translate → exec``。对齐 ``nodejs-store/src/crud/exec.js``。
"""

import re
import time

from .. import datasource as _datasource
from ..executors.mongo import exec_mongo as _exec_mongo
from ..feedback import emit as _emit_feedback
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
    db = _datasource.mongo_db(connection, source, cmd.get('namespace'))
    if db is not None:
        return await _exec_mongo(db, cmd)
    return await _datasource.exec_sql(source, connection, cmd)


async def _exec(cmd):
    """Command JSON → 按命令自带的 ``source`` 路由（不按 collection 反查）"""
    return await _exec_on(cmd.get('source') or _datasource.DEFAULT_SOURCE, cmd)


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
