"""框架统一入口（Python）：init → 逐条 register；协议面由接入方另行挂载。

``fns`` 回调实现来源 = L2 包 ``store-fns-py``（扁平字典 ``{impl_name: impl(item, ctx)}``）：
实现名用 snake_case，由宿主经 ``core::naming`` 归一后与 schema 逻辑 fn_ref 匹配绑定。

注意：``init`` / ``store`` 用函数内惰性导入——``__init__.py`` 末尾导入本模块，
若此处顶层 ``from . import init, store`` 会在包尚未完成初始化时取到未定义名，
故延迟到调用期解析。
"""

from typing import Any, Iterable


async def create_app(*, datasource: Any, schemas: Iterable[dict] = (),
                     fns: dict | None = None, ctx: Any = None,
                     feedback: bool = True, tenant: str = "", env: str = ""):
    from . import init, schema, store  # 延迟导入，规避包内循环导入

    if datasource is None:
        raise ValueError("ERR_BOOTSTRAP:缺 datasource")
    await init(datasource)
    for defn in schemas:
        store.register(defn, ctx)
    for ref, impl in (fns or {}).items():
        schema.set_fn(ref, impl)
    schema.assert_fns_covered(schemas)  # A3：缺实现即抛，进程不启动
    if feedback:
        store.setFeedbackMeta(tenant or "", env or "")   # py 侧签名：(tenant, env)
        store.enableFeedbackTable()                      # 内建 __feedback + sink 落库（幂等）
    # 关闭：await store.flushFeedback()（py 侧为 async）收口在途落库
    return {"store": store}
