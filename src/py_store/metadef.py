"""meta-store 定义持久化与版本化（宿主层；core 无 IO 铁律）

设计见 doc/execution/2026/10/meta-store定义控制面-01（A1/A2/A3）。三条契约：
  - 定义落库为内建 schema ``__schemaDef``（tenant/env/name/version/defn 五要素）；
  - 同名同形幂等不新增行，异形 version+1；
  - 版本历史可列，``rollbackTo`` 按历史 defn 重新 register。

对齐 ``nodejs-store/src/metadef.js``（双宿主流库行内容逐字节一致由对拍脚本守护）。
存储是 IO，故落宿主层，core 零改动。
"""

import json
from contextlib import contextmanager

from . import permission as _permission
from .schema import _to_core_defn
from .schema import has as _schema_has
from .schema import register as _schema_register

# 内建定义表名（``__`` 前缀为内建保留名，对齐 workflow 的 name.startswith('__') 校验）
_SCHEMA_DEF = '__schemaDef'
_WORKFLOW_DEF = '__workflowDef'
# 读取投影（双端一致；对拍比较用）
_DEF_FIELDS = '_id, tenant, env, name, version, defn, status, createdBy'


def _def_model(name, id_prefix):
    """内建定义表 schema（write 显式空名单：普通角色禁写，防篡改定义审计）

    不声明 indexes：内建表随宿主注册表进入 `ddl.generate()`，而场景 harness 会对全部
    注册表执行其中的 CREATE INDEX（建表仅限业务表）——为内建表加索引会令其对未建的
    内建表建索引而报错。版本唯一性由控制面「读最新行 + 1」保证（§4.3）；一旦存储层
    报唯一键冲突按 §4.4 显式上抛（不重试、不吞）。
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


def ensure_builtins():
    """注册内建 ``__schemaDef``/``__workflowDef``（幂等；core 对重复三元组显式报错，故先 has 守卫）"""
    if not _schema_has(_SCHEMA_DEF):
        _schema_register(_SCHEMA_DEF_MODEL)
    if not _schema_has(_WORKFLOW_DEF):
        _schema_register(_WORKFLOW_DEF_MODEL)


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
    condition = {'tenant': opts.get('tenant'), 'env': opts.get('env')}
    if opts.get('name') is not None:
        condition['name'] = opts['name']
    gql = f'__schemaDef($condition:@c0, $sort:@s0){{{_DEF_FIELDS}}}'
    return await store.query(gql, {'c0': condition, 's0': {'version': -1}})


async def load_defs(store, opts):
    """各 name 的**最新 active** 行（每 name 取 version 最大者；version desc 后首见即最新）"""
    opts = opts or {}
    gql = f'__schemaDef($condition:@c0, $sort:@s0){{{_DEF_FIELDS}}}'
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
    name = defn['name']
    rows = await list_defs(store, {'tenant': opts.get('tenant'), 'env': opts.get('env'),
                                   'name': name})
    latest = rows[0] if rows else None
    core_defn = _to_core_defn(defn)  # 函数值剔除（纯 JSON 入库，A3 前提）
    if latest is not None and same_defn(latest.get('defn'), core_defn):
        return latest
    row = build_def_row(core_defn, opts, next_version(rows))
    with _internal_ctx():
        return await store.insert(_SCHEMA_DEF, row)


async def rollback_to(store, opts):
    """回滚到历史版本：取历史行 → 重新 register(row.defn) → 返回该行"""
    opts = opts or {}
    rows = await list_defs(store, {'tenant': opts.get('tenant'), 'env': opts.get('env'),
                                   'name': opts.get('name')})
    version = opts.get('version')
    row = next((r for r in rows if r.get('version') == version), None)
    if row is None:
        raise MetaDefError(f"metadef: 版本不存在 {opts.get('name')}@{version}")
    _schema_register(row['defn'])
    return row


# 模块导入即自举内建定义表（幂等；零配置）
ensure_builtins()
