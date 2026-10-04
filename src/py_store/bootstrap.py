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
                     feedback: bool = True, tenant: str = "", env: str = "",
                     config: Any = None, config_base_dir: str | None = None):
    from . import init, schema, store  # 延迟导入，规避包内循环导入

    if datasource is None:
        raise ValueError("ERR_BOOTSTRAP:缺 datasource")
    # 注册/装载先于 init（保证注册先于建索引）：
    #   - 给 config 时按目录语义装载（core 纯规划 + 带定位批量注册）；
    #   - 否则维持 schemas 逐条 register。
    if config:
        items = store.load_defs(config, ctx, config_base_dir)
        defns = [it['defn'] for it in items]
    else:
        for defn in schemas:
            store.register(defn, ctx)
        defns = list(schemas)
    await init(datasource)
    for ref, impl in (fns or {}).items():
        schema.set_fn(ref, impl)
    schema.assert_fns_covered(defns)  # A3：缺实现即抛，进程不启动
    if feedback:
        store.set_feedback_meta(tenant or "", env or "")   # py 侧签名：(tenant, env)
        store.enable_feedback_table()                      # 内建 __feedback + sink 落库（幂等）
    # 关闭：await store.flush_feedback()（py 侧为 async）收口在途落库
    return {"store": store}
