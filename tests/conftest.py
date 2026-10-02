"""pytest 共享夹具 —— 进程级全局状态的跨文件隔离

py-store 的 schema 注册表（core 注册表 + Host 镜像）是**进程级单例**，而 pytest
收集阶段会 import 全部测试模块——任何模块级 ``schema.register``（副作用注册）
都会进入并集注册表，跨文件互相污染。已证实的两种串扰形态：

  1. describe 摘要污染：``store.ask`` 的 system prompt 摘要涵盖**全部**已注册
     schema，其它文件注册的字段名（如 ``userId``）触雷 test_ask T1 的
     「受信物零暴露」断言；
  2. 注册表脏态传染：某文件用例内的重复注册（RegDup）残留进程注册表，后续
     文件 ``store.init`` 遍历建索引时 ``KeyError: Schema 未注册``。

隔离方案（模块作用域，模块内行为完全不变）：

  - 收集期：所有文件把模块级注册改写为约定入口 ``_register_test_schemas()``
    （**只定义不执行**，import 零副作用）；
  - 每模块首个用例前：清空注册表 → 恢复内建 schema（``workflow.ensure_builtin``，
    幂等自举 ``__workflowRun``）→ 重放本模块的 ``_register_test_schemas()``
    （无该约定的文件自行在用例/夹具内注册，同样从干净注册表起步）；
  - 模块结束：再清空，不泄漏给后续模块。

连接（``set_db`` / ``init``）不在本夹具管辖内：各文件用例自足设置，且先执行
文件的用例先于后续文件的任何 setup 完成，无跨文件读序。
"""

import pytest

from py_store import schema as _sc
from py_store import workflow as _wf


@pytest.fixture(scope='module', autouse=True)
def _isolated_schema_registry(request):
    """模块级注册表隔离（schema + workflow 注册表；进入清场 + 重放本模块注册 + 退出清场）

    workflow 注册表同属进程级全局：场景 harness（manager-transaction 等）注册的
    工作流定义会与 test_workflow 的同名异形定义撞车（同名异形显式 Err 语义）。
    """
    _sc.clear_schemas()
    _wf.clear_workflows()
    _wf.ensure_builtin()  # 内建 __workflowRun 随注册表重建恢复（幂等自举）
    register_own = getattr(request.module, '_register_test_schemas', None)
    if register_own is not None:
        register_own()
    yield
    _sc.clear_schemas()
    _wf.clear_workflows()
