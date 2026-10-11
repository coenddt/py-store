"""本地磁盘 × 内存 双载体对拍（A2）—— py 侧驱动测试

镜像 ``nodejs-store/tests/parity-local-memory.test.js``；覆盖执行文档 01 §5 步骤 10：
  - 同一批文档经 local 与 memory 两载体读回**逐文档一致**；
  - memory 写后可读回（A2 后半句）。

约定：
  - 只走**真实 CRUD**（``init`` + ``store.insert`` / ``store.query``），**不得**直连 store/handle
    绕过 core 求值；
  - local 与 memory 用**同一份 schema、同一批文档**，各自写入后读回，逐文档相等；
  - 注入独立 memory 分区 dict，隔离进程内默认分区（不污染其它用例）。

运行：PYTHONPATH=src LOCAL_CORE=1 python -m pytest tests/test_parity_local_memory.py -q
"""

import asyncio

from py_store import datasource as _ds
from py_store import init, store
from py_store import schema as _sc
from py_store.local import connect as connect_local
from py_store.local.store import read_snapshot
from py_store.memory import connect as connect_memory


def _run(coro):
    return asyncio.run(coro)


def _register_test_schemas():
    _sc.register({
        'name': 'PmUser', 'collection': 'pm_users', 'idPrefix': 'pm_', 'timestamps': False,
        'fields': {
            'name': {'type': 'string'}, 'age': {'type': 'int'}, 'tag': {'type': 'string'},
        },
        'relations': {},
    })


# 同一批文档（显式 _id、无时钟、含 null 字段）：local 与 memory 逐文档对拍
DOCS = [
    {'_id': 'pm_1', 'name': 'Ada', 'age': 36, 'tag': 'a'},
    {'_id': 'pm_2', 'name': 'Bob', 'age': 20, 'tag': 'b'},
    {'_id': 'pm_3', 'name': None, 'age': 45, 'tag': None},
]

GQL = 'PmUser($sort:@s0){ _id name age tag }'
PARAMS = {'s0': {'_id': 1}}


def test_parity_local_memory(tmp_path):
    # ── local 载体：真实 CRUD 写入 + 读回 ──
    try:
        _run(init(connect_local({'dir': str(tmp_path)})))

        async def _main():
            for d in DOCS:
                await store.insert('PmUser', d)
            return await store.query(GQL, PARAMS)

        local_rows = _run(_main())
        # 落盘确为物理名分区（证明写入走 local 载体、非内存缓存）
        assert len(read_snapshot(tmp_path)['pmUsers']) == len(DOCS)
    finally:
        _ds.set_connections({})

    # ── memory 载体：注入独立分区，同批写入 + 读回 ──
    mem_store = {}
    try:
        _run(init(connect_memory({'store': mem_store})))

        async def _main():
            for d in DOCS:
                await store.insert('PmUser', d)
            return await store.query(GQL, PARAMS)

        memory_rows = _run(_main())
        # memory 写后可读回（A2 后半句）
        assert len(memory_rows) == len(DOCS), 'memory 写后应可读回'
        # 进程内容器确有对应分区（证明写入落到 memory 载体）
        assert len(mem_store['pmUsers']) == len(DOCS)
    finally:
        _ds.set_connections({})

    # ── 双链路对拍：逐文档相等 ──
    assert len(memory_rows) == len(local_rows), '两载体读回行数应一致'
    for i, row in enumerate(local_rows):
        assert memory_rows[i] == row, f'第 {i} 篇文档两载体读回应一致'
    assert memory_rows == local_rows
