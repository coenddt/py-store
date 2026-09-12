"""introspection 分派：按后端名把驱动交对应模块，产出统一的规范化行 JSON。

统一输出（交 core ``schema_from_rows``）：
  ``{tables:[{name}], columns:[{table,name,type,notnull,pk}],
     fks:[{table,column,refTable,refColumn}], indexes:[{table,name,columns,unique}]}``
"""

from . import mysql, postgres, sqlite

_BACKENDS = {'mysql': mysql, 'postgres': postgres, 'sqlite': sqlite}


async def run(backend, driver, options=None):
    """按后端名执行 introspection（统一 await 返回）"""
    mod = _BACKENDS.get(backend)
    if mod is None:
        raise ValueError(f'未知 introspection 后端: {backend}（支持 mysql/postgres/sqlite）')
    return await mod.introspect(driver, options)


__all__ = ['mysql', 'postgres', 'run', 'sqlite']
