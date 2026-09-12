"""
schema 同步：introspect → core.schema_from_rows → core.merge_schema(overlay) → register

纯编排（无 SQL 拼装、无 schema 推断）：Host 只负责「取物理结构行」与「注册结果」，
映射与合并全在 core（铁律 1/6）。SQL 后端只 pull 结构，**绝不写 DDL 回库**。
对齐 ``nodejs-store/src/sync.js``。
"""

from . import introspect
from .schema import core as _core
from .schema import register as _register


async def sync_schema(backend, driver, introspect_options=None, overlay=None,
                      datasource=None, namespace=None, register_defs=True):
    """
    同步一个数据源的物理结构到 Registry。

    :param backend: ``'mysql' | 'postgres' | 'sqlite'``
    :param driver: 驱动实例（建议只读账号）
    :param introspect_options: 透传给 introspection（如 PG 的 ``{'schema': 'public'}``）
    :param overlay: 本地 overlay schemaJSON（权限/计算列/覆盖）
    :param datasource: 绑定到该 schema 的数据源名（写入每个 def）
    :param namespace: 连接内的库/schema 名（写入每个 def；缺省 = 连接默认）
    :param register_defs: 是否直接注册（False 时仅返回 defs）
    :returns: 合并后的 schemaJSON 列表
    """
    rows = await introspect.run(backend, driver, introspect_options)
    defs = _core.schema_from_rows(rows, backend)
    if overlay:
        defs = _core.merge_schema(defs, overlay)
    if datasource:
        defs = [dict(d, datasource=datasource) for d in defs]
    if namespace:
        defs = [dict(d, namespace=namespace) for d in defs]
    if register_defs:
        for d in defs:
            _register(d)
    return defs
