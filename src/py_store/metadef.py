"""meta-store 定义持久化与版本化（宿主层；core 无 IO 铁律）

设计见 doc/execution/2026/10/meta-store定义控制面-01（A1/A2/A3）。三条契约：
  - 定义落库为内建 schema ``__schemaDef``（tenant/env/name/version/defn 五要素）；
  - 同名同形幂等不新增行，异形 version+1；
  - 版本历史可列（append-only），``rollbackTo`` 以历史 defn **追加新版本**（跨进程经 ``restore_defs`` 对协议面可见）。

对齐 ``nodejs-store/src/metadef.js``（双宿主流库行内容逐字节一致由对拍脚本守护）。
存储是 IO，故落宿主层，core 零改动。
"""

import json
from contextlib import contextmanager

from . import permission as _permission
from .schema import _to_core_defn
from .schema import has as _schema_has
from .schema import register as _schema_register
from .workflow import register as _register_workflow

# 内建定义表名（``__`` 前缀为内建保留名，对齐 workflow 的 name.startswith('__') 校验）
_SCHEMA_DEF = '__schemaDef'
_WORKFLOW_DEF = '__workflowDef'
_FEEDBACK = '__feedback'
# 读取投影（双端一致；对拍比较用）
_DEF_FIELDS = '_id, tenant, env, name, version, defn, status, createdBy'

# 定义类型（kind）→ 内建表名 / 注册函数（缺省 schema，保既有调用零变更）
_TABLES = {'schema': _SCHEMA_DEF, 'workflow': _WORKFLOW_DEF}
# 注册函数：(defn, internal) → 注册到对应注册表；internal 仅供系统重建（restore）使用
_REGISTRARS = {
    'schema': lambda defn, internal=False: _schema_register(defn, {'internal': True} if internal else None),
    'workflow': lambda defn, internal=False: _register_workflow(defn, {'internal': True} if internal else None),
}


def _kind_of(opts):
    """解析 kind → ``(kind, table)``；未知 kind 显式 Err（不兜底）"""
    kind = (opts or {}).get('kind') or 'schema'
    table = _TABLES.get(kind)
    if not table:
        raise MetaDefError(f'metadef: 未知定义类型 {kind!r}')
    return kind, table


def _def_model(name, id_prefix):
    """内建定义表 schema（write 显式空名单：普通角色禁写，防篡改定义审计）

    不声明 indexes：内建表随宿主注册表进入 `ddl.generate()`，而场景 harness 会对全部
    注册表执行其中的 CREATE INDEX（建表仅限业务表）——为内建表加索引会令其对未建的
    内建表建索引而报错。版本唯一性改由行自然键 ``_id``（见 ``def_id``，D2）在**存储层**
    保证：并发写同版本必触发唯一键冲突，按 §4.4 显式上抛（不重试、不吞）。
    """
    return {
        'name': name,
        'system': True,
        'collection': name,
        'idPrefix': id_prefix,
        'write': [],
        'fields': {
            '_id': {'type': 'string'},
            'tenant': {'type': 'string'},
            'env': {'type': 'string'},
            'name': {'type': 'string'},
            'version': {'type': 'int'},
            'defn': {'type': 'object'},
            'status': {'type': 'string'},
            'createdBy': {'type': 'string'},
        },
    }


_SCHEMA_DEF_MODEL = _def_model(_SCHEMA_DEF, 'sdef')
_WORKFLOW_DEF_MODEL = _def_model(_WORKFLOW_DEF, 'wdef')

# 内建反馈事件表（05）：承载结构化降级/拦截事件（write 空名单——普通角色禁写，事件审计）
_FEEDBACK_MODEL = {
    'name': _FEEDBACK,
    'system': True,
    'collection': _FEEDBACK,
    'idPrefix': 'fdbk',
    'write': [],
    'fields': {
        '_id': {'type': 'string'},
        'type': {'type': 'string'},
        'code': {'type': 'string'},
        'layer': {'type': 'string'},
        'message': {'type': 'string'},
        'hint': {'type': 'string'},
        'tenant': {'type': 'string'},
        'env': {'type': 'string'},
        'now': {'type': 'number'},
    },
}


def ensure_builtins():
    """注册内建 ``__schemaDef``/``__workflowDef``/``__feedback``（幂等；core 对重复三元组显式报错，故先 has 守卫）"""
    if not _schema_has(_SCHEMA_DEF):
        _schema_register(_SCHEMA_DEF_MODEL)
    if not _schema_has(_WORKFLOW_DEF):
        _schema_register(_WORKFLOW_DEF_MODEL)
    if not _schema_has(_FEEDBACK):
        _schema_register(_FEEDBACK_MODEL)


class MetaDefError(ValueError):
    """metadef 契约失败（defn.name 缺失 / 版本不存在 / 唯一冲突）"""


# ─── 纯逻辑（双端逐字节等价；parity 锚） ───────────────────────

def stable_stringify(value):
    """稳定序列化（键序无关）：与 node ``_stableStringify`` 逐字节等价"""
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def same_defn(a, b):
    """两 defn 是否同形（深比较；键序无关）"""
    return stable_stringify(a) == stable_stringify(b)


def next_version(rows):
    """由既有行推下一个版本号（无行 → 1）"""
    return (max(r['version'] for r in rows) + 1) if rows else 1


def build_def_row(defn, opts, version):
    """构造待插入行（纯函数，便于双端对拍）。键序与 node ``buildDefRow`` 一致"""
    opts = opts or {}
    return {
        'tenant': opts.get('tenant'),
        'env': opts.get('env'),
        'name': defn['name'],
        'version': version,
        'defn': defn,
        'status': 'active',
        'createdBy': opts.get('actor') or '',
    }


def def_id(tenant, env, name, version):
    """定义行自然键 ``_id`` = ``(tenant, env, name, version)``（D2；对齐 node ``defId``）。

    作为存储层主键，同 ``(tenant,env,name,version)`` 二次写入必触发唯一键冲突
    （SQLite UNIQUE / Mongo E11000），使「读最新行 +1」的并发窗口在存储层收口 ——
    并发写同版本时后到者显式报错（控制面映射 409 CONFLICT），不产重复 version。
    分隔符用 US（``\\u001f``）：schema name 不含该控制字符，拼接无歧义。
    """
    t = '' if tenant is None else str(tenant)
    e = '' if env is None else str(env)
    return f'{t}\u001f{e}\u001f{name}\u001f{version}'


@contextmanager
def _internal_ctx():
    """干净 internal 上下文（{internal: True}，丢弃触发者 roles）

    内建表 write 显式空名单仅放行 internal/admin；写入身份与触发者权限解耦，
    对齐 ``workflow._internal_ctx`` 的 __workflowRun 写入约束。
    """
    token = _permission._ctx.set({'internal': True})
    try:
        yield
    finally:
        _permission._ctx.reset(token)


# ─── IO（经传入的 store 面，避免包内循环依赖） ─────────────────

async def list_defs(store, opts):
    """列定义行（按 version desc；name 缺省列全部）"""
    opts = opts or {}
    _, table = _kind_of(opts)
    condition = {'tenant': opts.get('tenant'), 'env': opts.get('env')}
    if opts.get('name') is not None:
        condition['name'] = opts['name']
    gql = f'{table}($condition:@c0, $sort:@s0){{{_DEF_FIELDS}}}'
    return await store.query(gql, {'c0': condition, 's0': {'version': -1}})


async def load_defs(store, opts):
    """各 name 的**最新 active** 行（每 name 取 version 最大者；version desc 后首见即最新）"""
    opts = opts or {}
    _, table = _kind_of(opts)
    gql = f'{table}($condition:@c0, $sort:@s0){{{_DEF_FIELDS}}}'
    rows = await store.query(gql, {
        'c0': {'tenant': opts.get('tenant'), 'env': opts.get('env'), 'status': 'active'},
        's0': {'version': -1},
    })
    out = []
    seen = set()
    for r in rows:
        if r.get('name') in seen:
            continue
        seen.add(r['name'])
        out.append(r)
    return out


async def persist_def(store, defn, opts):
    """持久化定义：同名同形返回原行不插入；异形插入 version+1"""
    if not isinstance(defn, dict) or not defn.get('name'):
        raise MetaDefError('metadef: defn.name 必填')
    opts = opts or {}
    _, table = _kind_of(opts)
    name = defn['name']
    rows = await list_defs(store, {'tenant': opts.get('tenant'), 'env': opts.get('env'),
                                   'name': name, 'kind': opts.get('kind')})
    latest = rows[0] if rows else None
    core_defn = _to_core_defn(defn)  # 函数值剔除（纯 JSON 入库，A3 前提）
    if latest is not None and same_defn(latest.get('defn'), core_defn):
        return latest
    version = next_version(rows)
    row = build_def_row(core_defn, opts, version)
    # 自然键 `_id`（D2）：同版本并发写必冲突 → 存储层保证 version 唯一
    row['_id'] = def_id(opts.get('tenant'), opts.get('env'), core_defn['name'], version)
    with _internal_ctx():
        return await store.insert(table, row)


# 已重建进注册表的定义自然键（进程级；同名同版本只注册一次，避免每次 reload 全量覆盖）
_applied: set = set()


async def restore_defs(store, opts):
    """从持久化定义重建注册表（D1 闭环桥）：``load_defs`` → 逐条 ``register(defn, internal)``。

    网关 reload 在重装配前调用本函数，使「控制面 publish（写库）」与「协议面可见（注册）」
    经 reload 衔接。已注册过的同版本跳过（幂等）；版本变化时以新 defn 覆盖注册。
    注册走 internal 上下文：属系统重建动作，不受业务定义层门禁（MetaPolicy）影响。
    返回 ``{'total': 库内最新 active 行数, 'applied': 本次新注册数}``。
    """
    opts = opts or {}
    kind, _ = _kind_of(opts)
    rows = await load_defs(store, opts)
    applied = 0
    for r in rows:
        key = f"{kind}\u001f{def_id(opts.get('tenant'), opts.get('env'), r['name'], r['version'])}"
        if key in _applied:
            continue
        _REGISTRARS[kind](r['defn'], True)
        _applied.add(key)
        applied += 1
    return {'total': len(rows), 'applied': applied}


async def rollback_to(store, opts):
    """回滚到历史版本（追加式）：取历史行 defn → 作为新版本再发布 → 本进程 register → 返回落库行。

    D21 跨进程闭环：回滚不再「原地重注册」，而是复用 ``persist_def`` 把历史 defn 落成一条
    **新版本行**（同名同形幂等 → 返回当前最新行）。``load_defs`` 取「最新 active」故必然返回
    该行，网关 ``restore_defs`` hydrate 即按回滚后的 defn 装配 —— 回滚对协议面可见。
    历史保持 append-only：目标历史行不被改写。
    """
    opts = opts or {}
    kind, _ = _kind_of(opts)
    rows = await list_defs(store, {'tenant': opts.get('tenant'), 'env': opts.get('env'),
                                   'name': opts.get('name'), 'kind': opts.get('kind')})
    version = opts.get('version')
    row = next((r for r in rows if r.get('version') == version), None)
    if row is None:
        raise MetaDefError(f"metadef: 版本不存在 {opts.get('name')}@{version}")
    # 追加式回滚：以历史 defn 走 persist 语义（异形 → version+1；同形 → 返回当前最新）
    persisted = await persist_def(store, row['defn'], opts)
    # 系统重建动作：workflow 走 internal；schema 维持既有调用形态（False = 无 ctx，门禁行为零变更）
    _REGISTRARS[kind](row['defn'], kind == 'workflow')
    return persisted


# 模块导入即自举内建定义表（幂等；零配置）
ensure_builtins()
