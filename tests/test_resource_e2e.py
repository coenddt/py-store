"""资源数据驱动 · 真库 e2e（Python 对拍，SQLite 内存）。

复用 node 侧 example/resource-hub 的 schema.json 与 ddl/sqlite.sql（单一来源），
跑与 node 版等价的断言序列：上传 fan-out → 多副本落库 → 去重 → 打开 → 计算列 URL → 三级级联删除。

运行：$env:PYTHONPATH='py-store/src'; python -m pytest py-store/tests/test_resource_e2e.py -q
（本地需让 `import rust_store_py` 命中含 resource_* 的新构建）
"""

import asyncio
import json
import os
import re
import sqlite3
import tempfile

import pytest

from py_store import executors, init, resource, store
from py_store import schema as sc
from py_store.core import native

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE_EXAMPLE = os.path.join(os.path.dirname(ROOT), 'nodejs-store', 'example', 'resource-hub')

_BASE_URL = 'https://cdn.example.com'


def _load_ddl():
    path = os.path.join(NODE_EXAMPLE, 'ddl', 'sqlite.sql')
    if not os.path.exists(path):
        pytest.skip(f'缺少 node 侧 DDL: {path}（需同时 clone nodejs-store 仓库）')
    with open(path, encoding='utf-8') as f:
        text = f.read()
    body = '\n'.join(line for line in text.split('\n') if not line.strip().startswith('--'))
    return [s.strip() for s in body.split(';') if s.strip()]


def _load_schemas():
    path = os.path.join(NODE_EXAMPLE, 'schema.json')
    if not os.path.exists(path):
        pytest.skip(f'缺少 node 侧 schema: {path}（需同时 clone nodejs-store 仓库）')
    with open(path, encoding='utf-8') as f:
        arr = json.load(f)

    def cover_url(item):
        cid = item.get('coverId') if isinstance(item, dict) else None
        if not cid:
            return None
        return native.resource_compose_url(cid, {'baseUrl': _BASE_URL, 'pathTemplate': '/{contentPath}'})

    for d in arr:
        for _k, c in (d.get('computes') or {}).items():
            if c.get('fn') is True:
                c['fn'] = cover_url
    return arr


class _MemoryProvider:
    @staticmethod
    def create(options=None):
        writes = {}

        async def put(key, data, opts=None):
            writes[key] = bytes(data)

        async def get(key, opts=None):
            if key not in writes:
                raise KeyError('miss')
            return writes[key]

        async def remove(key, opts=None):
            writes.pop(key, None)

        async def exists(key, opts=None):
            return key in writes

        return {'kind': 'memory', 'put': put, 'get': get, 'remove': remove, 'exists': exists}


async def _cascade_by_business(business_table, business_id):
    bindings = await store.query(
        'ResourceBinding($condition: @c0) { _id, resourceId }',
        {'c0': {'businessTable': business_table, 'businessId': str(business_id)}})
    summary = {'total': len(bindings), 'kept': 0, 'removed': 0}
    for b in bindings:
        await store.remove('ResourceBinding', {'_id': b['_id']})
        others = await store.query('ResourceBinding($condition: @c0) { _id }',
                                   {'c0': {'resourceId': b['resourceId']}})
        if others:
            summary['kept'] += 1
            continue
        await resource.remove(b['resourceId'])
        summary['removed'] += 1
    return summary


def test_resource_e2e():
    async def scenario():
        import aiosqlite
        db = await aiosqlite.connect(':memory:')
        try:
            await _run_e2e(db)
        finally:
            await db.close()

    async def _run_e2e(db):
        for stmt in _load_ddl():
            await db.execute(stmt)
        await db.commit()
        for defn in _load_schemas():
            sc.register(defn)
        await init({'default': executors.create_connection('sqlite', db)})

        resource.register_provider('memory', _MemoryProvider)
        resource.configure({
            'providers': [
                {'kind': 'local', 'options': {'baseDir': tempfile.mkdtemp(prefix='res-e2e-')}},
                {'kind': 'memory'},
            ],
            'url': {'baseUrl': _BASE_URL, 'pathTemplate': '/{contentPath}'},
        })

        data = b'hello-resource'
        up = await resource.put(bytes=data, file_name='a.txt', mime='text/plain', kind='file',
                                bind={'businessTable': 'Doc', 'businessId': 'd1', 'userId': 'u1'})
        assert re.match(r'^[0-9a-f]{40}$', up['resourceId'])
        assert len(up['locations']) == 2
        assert all(loc['status'] == 'ok' for loc in up['locations'])

        locs = await store.query('ResourceLocation($condition: @c0) { _id, backend, status }',
                                 {'c0': {'resourceId': up['resourceId']}})
        assert len(locs) == 2

        # A9：唯一索引 —— 重复插入同 (resourceId, backend) 显式报错
        with pytest.raises(sqlite3.IntegrityError):
            await store.insert('ResourceLocation', {'resourceId': up['resourceId'], 'backend': 'local',
                                                    'key': 'dup', 'status': 'ok', 'priority': 0})

        # 内容寻址去重：同内容二次上传不新增 Resource 行
        again = await resource.put(bytes=data, file_name='copy.txt', mime='text/plain')
        assert again['resourceId'] == up['resourceId']

        got = await resource.open(up['resourceId'])
        assert got['backend'] == 'local'
        assert got['bytes'] == b'hello-resource'

        await resource.put(bytes=b'cover-x', file_name='c.png', mime='image/png', kind='image',
                           bind={'businessTable': 'Doc', 'businessId': 'd1', 'userId': 'u1'})

        # A9：ResourceBinding 三元组唯一 —— 重复插入显式报错
        with pytest.raises(sqlite3.IntegrityError):
            await store.insert('ResourceBinding', {'resourceId': up['resourceId'], 'businessTable': 'Doc',
                                                   'businessId': 'd1', 'userId': 'u1'})

        await store.insert('Doc', {'_id': 'd1', 'title': 'D', 'coverId': up['resourceId'], 'createdBy': 'u1'})
        read = await store.query_one('Doc($condition: @c0) { _id, title, coverId, coverUrl }',
                                     {'c0': {'_id': 'd1'}})
        sha1 = up['sha1']
        assert read['coverUrl'] == f'https://cdn.example.com/objects/{sha1[:2]}/{sha1}'

        summary = await _cascade_by_business('Doc', 'd1')
        assert summary['total'] == 2
        assert summary['removed'] == 2
        assert summary['kept'] == 0
        after = await store.query('Resource($condition: @c0) { _id }', {'c0': {}})
        assert len(after) == 0

    asyncio.run(scenario())
