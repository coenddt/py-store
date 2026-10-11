"""适配器门面（py 侧；对齐执行文档 03 §4.3）。

把「拉取 → 规范化」的结果落到主链路——**必须**经既有 CRUD
（``store.insert_many`` → core 计划 → 宿主执行），禁绕过 core 直连驱动写库。
"""

from __future__ import annotations

from .http import HttpAdapterError as HttpAdapterError
from .http import create_http_adapter as create_http_adapter

__all__ = ['HttpAdapterError', 'create_http_adapter', 'pull_into']


async def pull_into(adapter, schema_name, store, params=None, route_override=None):
    """拉取 → 落主链路：调既有 CRUD（由 core 计划决定落点），不自行写库。

    :returns: ``{'pulled': int, 'written': Any}``；``pulled == 0`` 时 ``written`` 为
        ``None`` 且不调用写路径。
    """
    if not isinstance(adapter, dict) or not callable(adapter.get('fetch')):
        raise TypeError("pull_into 需要 adapter['fetch']")
    if not schema_name:
        raise TypeError('pull_into 需要 schema_name')
    if store is None or not callable(getattr(store, 'insert_many', None)):
        raise TypeError('pull_into 需要 store.insert_many')

    docs = await adapter['fetch'](params or {})
    pulled = len(docs)
    if pulled == 0:
        return {'pulled': 0, 'written': None}
    written = await store.insert_many(schema_name, docs, route_override)
    return {'pulled': pulled, 'written': written}
