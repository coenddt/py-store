"""框架统一入口（Python）：init → 逐条 register；协议面由接入方另行挂载。

注意：``init`` / ``store`` 用函数内惰性导入——``__init__.py`` 末尾导入本模块，
若此处顶层 ``from . import init, store`` 会在包尚未完成初始化时取到未定义名，
故延迟到调用期解析。
"""

from typing import Any, Iterable


async def create_app(*, datasource: Any, schemas: Iterable[dict] = (), ctx: Any = None):
    from . import init, store  # noqa: PLC0415 —— 延迟导入，规避包内循环导入

    if datasource is None:
        raise ValueError("ERR_BOOTSTRAP:缺 datasource")
    await init(datasource)
    for defn in schemas:
        store.register(defn, ctx)
    return {"store": store}
