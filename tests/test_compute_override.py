"""fields ∩ computes 同名（计算列覆盖字段）—— local 载体端到端

语义（对齐 nodejs-store）：
  - **查询期**：computes 拥有同名键的输出所有权 —— 输出的是计算列求值结果；
  - **写入期**：字段仍在 fields 中，`$inc` 等原生增量更新照常按同名键落库。

本用例固化三件事：
  1. `fields.favoriteCount` + `computes.favoriteCount`（asyncFn）注册成功、不抛
     `ERR_NAME_CONFLICT`；
  2. 查询输出计算列值（DB 原值 7、实时计数 3 → 输出 3）；
  3. `$inc favoriteCount` 后磁盘快照中的 DB 原值递增（7 → 8）。

运行：PYTHONPATH=src LOCAL_CORE=1 python -m pytest tests/test_compute_override.py -q
"""

import asyncio

from py_store import datasource as _ds
from py_store import init, store
from py_store import schema as _sc
from py_store.local import connect as connect_local
from py_store.local.store import read_snapshot


def _run(coro):
    return asyncio.run(coro)


async def _live_favorite_count(items, ctx=None):
    """实时计数 3：与 DB 存储值 7 不同，用于区分「计算列输出」与「字段存储值」。"""
    for it in items:
        it['favoriteCount'] = 3


def _register():
    _sc.register({
        'name': 'FavoriteListing',
        'collection': 'favorite_listings',
        'idPrefix': 'fl_',
        'timestamps': False,
        'fields': {
            '_id': {'type': 'string'},
            'title': {'type': 'string'},
            # 与 computes 同名：保留类型声明，供写入校验 / DDL / $inc 使用
            'favoriteCount': {'type': 'int'},
        },
        'relations': {},
        'computes': {
            'favoriteCount': {
                'type': 'int',
                'asyncFn': _live_favorite_count,
                'depends': [],
            },
        },
    })


def test_fields_compute_same_name_local(tmp_path):
    _register()
    rows = []
    snapshot = {}

    try:
        _run(init(connect_local({'dir': str(tmp_path)})))

        async def _main():
            await store.insert('FavoriteListing', {
                '_id': 'fl_1', 'title': 'a', 'favoriteCount': 7,
            })
            got = await store.query(
                'FavoriteListing($sort:@s0){ _id favoriteCount }',
                {'s0': {'_id': 1}},
            )
            await store.update(
                'FavoriteListing', {'_id': 'fl_1'}, {'$inc': {'favoriteCount': 1}},
            )
            return got

        rows = _run(_main())
        snapshot = read_snapshot(str(tmp_path))
    finally:
        _ds.set_connections({})

    # 查询输出计算列值（DB 原值 7 被覆盖）
    assert rows == [{'_id': 'fl_1', 'favoriteCount': 3}], '查询期计算列覆盖同名字段'

    # 写入仍按 fields 同名键落库：$inc 在原值上递增（物理集合名经命名归一为 camelCase）
    stored = snapshot.get('favoriteListings') or []
    assert len(stored) == 1, f'本地集合应有 1 条文档：{snapshot}'
    assert stored[0]['favoriteCount'] == 8, '$inc 按 fields 同名键落库（7 → 8）'
