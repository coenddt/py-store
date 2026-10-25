"""fields 键空间完全开放（原初设计修复回归）：字段名叫 ``type`` 的合法 schema 全链路

镜像 ``nodejs-store/tests/field-name-type.test.js``。

背景：core 4.8.x 的 RESERVED_KEYS 保留键检查把 field 级契约键误当字段名保留字，
``fields: {'type': {'type': 'string'}}`` 注册即抛 ERR_NAME_CONFLICT；core 4.9.0 移除
保留键检查后全链路可用。本文件是用户验收锚点（注册 + insert + GQL + update + remove）。

运行：PYTHONPATH=src LOCAL_CORE=1 python -m pytest tests/test_field_name_type.py -q
"""

import asyncio

from py_store import datasource as _ds
from py_store import init, store
from py_store import schema as _sc
from py_store.memory import connect


def _run(coro):
    return asyncio.run(coro)


def test_field_named_type_full_chain():
    _sc.register({
        'name': 'TypeFieldMsg', 'collection': 'type_field_msgs', 'idPrefix': 'tfm_', 'timestamps': False,
        'fields': {
            '_id': {'type': 'string'},
            'type': {'type': 'string', 'default': 'text'},
            'content': {'type': 'string'},
        },
        'relations': {},
    })

    async def _main():
        # insert：缺省 type → default 'text' 生效
        a = await store.insert('TypeFieldMsg', {'content': 'hello'})
        assert a['type'] == 'text', 'default 应生效'

        # insert：显式 type 保真
        b = await store.insert('TypeFieldMsg', {'type': 'image', 'content': 'pic'})
        assert b['type'] == 'image'

        # GQL 查询：投影含 type 字段 + $condition 过滤（GQL 参数只接受 $var:@key 引用形态）
        rows = await store.query(
            'TypeFieldMsg($condition:@c){ _id type content }',
            {'c': {'type': 'image'}},
        )
        assert rows == [{'_id': b['_id'], 'type': 'image', 'content': 'pic'}]

        # update：type 字段可写
        await store.update('TypeFieldMsg', {'_id': b['_id']}, {'type': 'video'})
        after = await store.query('TypeFieldMsg{ _id type }')
        assert next(r for r in after if r['_id'] == b['_id'])['type'] == 'video'

        # remove
        await store.remove('TypeFieldMsg', {'_id': a['_id']})
        assert await store.count('TypeFieldMsg', {}) == 1

    try:
        _run(init(connect({'store': {}})))
        _run(_main())
    finally:
        _ds.set_connections({})
