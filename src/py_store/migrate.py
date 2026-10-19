"""迁移编排器（C3/A4）：diff 两版定义 → 方言 SQL → 事务执行 → ``__schemaMigration`` 版本行

七步编排（双端一致，执行文档 04 §4.2）：
  ① ``old_defs`` 显式传入者优先（显式 > 探测）；None → 经 ``driver``（原生驱动实例，同
     ``sync_schema`` 惯例）introspect 拉当前物理结构（``schema_from_rows`` 转 defn）；
  ② 逐 name ``ddl.diff_defs(old, new)``；
  ③ errors 非空 → 收集 skipped 并抛 ``MigrationUnsupported``（附固定分步建议；不自动执行、不自动回滚）；
  ④ 白名单内 ``generate_migration`` 产方言 SQL 文本（不执行）；
  ⑤ 幂等闸（先于 diff）：``__schemaMigration`` 同 (tenant,env,backend,name) 最新行 hash 相同
     → skipped 'unchanged'——introspect 反推类型粒度可粗于用户声明，重跑不进 diff；
  ⑥ 单表 SQL 与版本行在同一事务（``run_in_transaction``）内执行/写入——失败整体回滚、无孤儿版本行；
  ⑦ 汇总 ``{'applied': [...], 'skipped': [...]}``。

边界（执行文档 04 §5 步骤说明）：
  - 同批次多表不做跨表事务（每表独立事务 + 版本行）；部分成功按 skipped/applied 对账重跑；
  - 破坏性变更被拦后不提供自动回滚——``MigrationUnsupported`` 只给分步建议；
  - 版本表缺失时按探测补建（DDL 由 core ``ddl.generate`` 产出，宿主不拼 SQL）；
  - diff 在**物理命名空间**进行：introspect→schema_from_rows 的键是物理列名（snake_case），
    且物理名不可逆（core::naming 单向翻译）——new 侧经 ``ddl._pname`` 翻到物理名对齐；
    反推粒度（SQLite INTEGER→number）经 ``_align_types`` 归一（物理层确无差异）。

对齐 ``nodejs-store/src/migrate.js``（接线偏差见执行账本 tmp/execution-status-store家族扩展-04.md）。
"""

import hashlib
import time

from . import ddl as _ddl
from . import introspect as _introspect
from .schema import core as _core
from .metadef import (
    MetaDefError,
    _MIGRATION,
    _internal_ctx,
    stable_stringify,
)
from . import crud as _crud
from .datasource import execute_raw, run_in_transaction

#: 白名单外变更的固定分步建议（契约逐字；MigrationUnsupported 消息必须原文携带）
_UNSUPPORTED_HINT = '白名单外变更请分两版走：先加新列并回填，确认切换后再于下一版删除旧列'

_NUMBERS = {'int', 'long', 'float', 'double', 'number'}


class MigrationUnsupported(MetaDefError):
    """白名单外变更（diff errors 非空）：只给分步建议，不自动执行、不自动回滚"""

    def __init__(self, message, skipped=None):
        super().__init__(message)
        self.skipped = skipped or []


def migration_id(tenant, env, backend, name, version):
    """版本行自然键：``def_id`` 惯例追加 backend 段（tenant␟env␟backend␟name␟version，US 分隔）"""
    seg = lambda v: '' if v is None else str(v)  # noqa: E731
    return f'{seg(tenant)}\u001f{seg(env)}\u001f{seg(backend)}\u001f{seg(name)}\u001f{seg(version)}'


def _hash_of(defn):
    """当前版 defn 的稳定 hash：stable_stringify（键序无关）→ SHA-1 hex"""
    return hashlib.sha1(stable_stringify(defn).encode('utf-8')).hexdigest()


def _strip_builtin_fields(old_defn, new_defn):
    """内建物理列剔除：introspect→schema_from_rows 的 defn 带 ``__present`` 哨兵与 timestamps
    物理列（宿主 DDL 的 snake_case：``created_at``/``updated_at``；兼容 camelCase）——不剔则
    diff 误报「删除字段不支持」。schema_from_rows 推断 ``timestamps: False``，剔除条件看用户侧声明。"""
    if not old_defn or not isinstance(old_defn, dict):
        return old_defn
    drop = ['__present']
    if new_defn and new_defn.get('timestamps'):
        drop += ['created_at', 'updated_at', 'createdAt', 'updatedAt']
    fields = dict(old_defn.get('fields') or {})
    for k in drop:
        fields.pop(k, None)
    return {**old_defn, 'fields': fields}


# 版本表存在性探测 SQL（表名为常量非用户输入，内联无注入面；宿主只读 SELECT，
# 同 ``introspect/<backend>.py`` 既有惯例——introspect 需原生驱动实例，连接视图不暴露）
_PROBE_SQL = {
    'sqlite': f"SELECT name FROM sqlite_master WHERE type = 'table' AND name = '{_MIGRATION}'",
    'mysql': ('SELECT table_name AS name FROM information_schema.tables'
              f" WHERE table_schema = DATABASE() AND table_name = '{_MIGRATION}'"),
    'postgres': ('SELECT table_name AS name FROM information_schema.tables'
                 f" WHERE table_name = '{_MIGRATION}' AND table_schema = current_schema()"),
}


async def _has_migration_table(backend, source):
    out = await execute_raw(source, _PROBE_SQL[backend], None, False)
    return bool(out.get('rows'))


def _physical_defn(backend, defn):
    """用户 defn → 物理命名空间拷贝（顶层 fields 键与 indexes keys 经 ``_pname`` 按后端翻译）。

    ``_pname`` 对 snake_case 输入幂等，``_id``/``__`` 保留名原样（generate_migration 内部
    再次翻译无害）。"""
    fields = {_ddl._pname(backend, k): v for k, v in (defn.get('fields') or {}).items()}
    indexes = [
        {**ix, 'keys': {_ddl._pname(backend, k): v for k, v in ((ix or {}).get('keys') or {}).items()}}
        for ix in (defn.get('indexes') or [])
    ]
    return {**defn, 'fields': fields, 'indexes': indexes}


def _align_types(backend, old_defn, phys_new):
    """反推粒度对齐：introspect 反推类型粒度可粗于用户声明（SQLite 动态类型 → ``number``，
    承载 int/long/float/double 全域）——old 侧字段为 ``number`` 时把 new 侧同名数值字段归一为
    ``number`` 比对（物理层确无差异；MySQL/PG 反推保真不受影响）。仅同名两侧都有时归一；
    新加列保留用户类型（DDL 仍按声明产列型）。"""
    old_fields = (old_defn or {}).get('fields') or {}
    fields = dict(phys_new.get('fields') or {})
    for k, v in fields.items():
        o = old_fields.get(k)
        ot = (o.get('type') if isinstance(o, dict) else o) if o is not None else None
        nt = v.get('type') if isinstance(v, dict) else v
        if ot == 'number' and nt in _NUMBERS:
            fields[k] = {**v, 'type': 'number'} if isinstance(v, dict) else 'number'
    return {**phys_new, 'fields': fields}


async def migrate(*, backend, source, defs, old_defs=None, driver=None,
                  tenant=None, env=None):
    """迁移编排（A4）。参数与返回对齐 node ``migrate``：

    :param backend: ``'sqlite' | 'mysql' | 'postgres'``
    :param source: 数据源名（DDL 与版本表同源同事务）
    :param defs: 目标定义 ``{name: defn}``（与 ``persist_def`` 的 defn 同构）
    :param old_defs: 旧版定义 ``{name: defn}``；None → introspect 探测（需 ``driver``）
    :param driver: 原生驱动实例（introspect 用；连接视图不暴露原生 handle，同 ``sync_schema`` 惯例）
    :param tenant: 租户段（None → 空串段，与 metadef 既有 None 处理一致）
    :param env: 环境段（None → 空串段）
    :returns: ``{'applied': [{'name','from','to','version'}], 'skipped': [{'name','reason'}]}``
    """
    if not backend or not source or not isinstance(defs, dict):
        raise MetaDefError('migrate: backend/source/defs 必填（defs 为 {name: defn} 对象）')
    # 版本表保障：有 driver 时一次完整 introspect（两用：版本表探测 + old_defs=None 转 defn）；
    # 无 driver 时退化为 execute_raw 只读探测。缺失才建（core 产 DDL；CREATE TABLE 非
    # IF NOT EXISTS，幂等由探测保证）。ddl.generate 返回语句串，按分号空行拆条执行。
    probe = None
    if driver is not None:
        probe = await _introspect.run(backend, driver, None)
        has_version_table = any(t.get('name') == _MIGRATION for t in probe.get('tables') or [])
    else:
        has_version_table = await _has_migration_table(backend, source)
    if not has_version_table:
        ensure = _ddl.generate(backend, [_MIGRATION])
        stmts = _split_statements(ensure)

        async def _ensure():
            for sql in stmts:
                if sql.strip():
                    await execute_raw(source, sql, None, True)
        await run_in_transaction(source, _ensure)
    # ① 旧版定义：old_defs 显式传入者优先；None → 复用本次探测行转 defn（需 driver）。
    # schema_from_rows 的 name 是物理表名（如 `notes`），与 defs 的 schema 名（如 `Note`）
    # 不同键——按 name 与 collection 双索引，查找时以 defs 侧 collection 兜底对齐
    old = old_defs
    if old is None:
        if probe is None:
            raise MetaDefError(
                'migrate: old_defs 为 None 时需传 driver（introspect 需原生驱动实例，同 sync_schema 惯例）；'
                '或显式传入 old_defs 跳过探测')
        old = {}
        for d in _core.schema_from_rows(probe, backend):
            old[d['name']] = d
            if d.get('collection') and d['collection'] != d['name']:
                old[d['collection']] = d
    applied = []
    skipped = []
    for name, defn in defs.items():
        old_defn = _strip_builtin_fields(
            old.get(name) or (old.get(defn.get('collection')) if defn.get('collection') else None), defn)
        # ⑤ 幂等闸（先于 diff）：hash 相同 → skipped unchanged，不 diff（反推粒度不齐防误报）
        hash_ = _hash_of(defn)
        seg = {'tenant': '' if tenant is None else tenant,
               'env': '' if env is None else env, 'backend': backend, 'name': name}
        rows = await _crud.query(
            f'{_MIGRATION}($condition:@c0, $sort:@s0){{_id, version, hash}}',
            {'c0': seg, 's0': {'version': -1}}, {'source': source})
        if rows and rows[0].get('hash') == hash_:
            skipped.append({'name': name, 'reason': 'unchanged'})
            continue
        # ②③ diff（物理命名空间）：白名单外 → 收集 skipped 并抛 MigrationUnsupported（固定分步建议）
        phys_new = _align_types(backend, old_defn, _physical_defn(backend, defn))
        plan = _ddl.diff_defs(old_defn, phys_new)
        if plan['errors']:
            skipped.append({'name': name, 'reason': plan['errors']})
            raise MigrationUnsupported(
                f'MIGRATION_UNSUPPORTED: {"；".join(plan["errors"])}（{_UNSUPPORTED_HINT}）', skipped)
        # ④ 方言 SQL（纯文本）
        stmts = _ddl.generate_migration(backend, old_defn, phys_new)
        # version = 该 name 历史最大 version+1（复用 metadef next_version 语义）
        version = (max(r['version'] for r in rows) if rows else 0) + 1
        # ⑥ SQL 与版本行同事务：失败整体回滚、无孤儿版本行
        row = {
            '_id': migration_id(tenant, env, backend, name, version),
            **seg,
            'hash': hash_,
            'version': version,
            'appliedAt': int(time.time() * 1000),
            'sqls': stmts,
        }

        async def _apply():
            for sql in stmts:
                await execute_raw(source, sql, None, True)
            with _internal_ctx():
                await _crud.insert(_MIGRATION, row, {'source': source})
        await run_in_transaction(source, _apply)
        applied.append({'name': name, 'from': rows[0]['version'] if rows else 0,
                        'to': version, 'version': version})
    # ⑦ 汇总
    return {'applied': applied, 'skipped': skipped}


def _split_statements(sql_text):
    """``ddl.generate`` 的语句串 → 语句列表（双端一致按块拆分；node 侧 ``split('\\n\\n')``）"""
    return sql_text.split('\n\n') if isinstance(sql_text, str) else list(sql_text)
