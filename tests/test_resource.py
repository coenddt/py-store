"""资源能力（宿主旁路 B 档）单测：假 store + 真 local provider + 内存 provider。

覆盖：put fan-out 2 副本 + sha1 内容寻址；open 跳过 failed 副本降级到 memory；url 纯拼接（外部直返）。

运行：$env:PYTHONPATH='py-store/src'; python -m pytest py-store/tests/test_resource.py -q
（本地需让 `import rust_store_py` 命中含 resource_content_path 的新构建，见执行文档 02）
"""

import asyncio
import re
import tempfile

import pytest

from py_store import feedback, resource


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


class _FakeStore:
    def __init__(self):
        self.rows = []

    async def exists(self, schema, cond):
        return any(r['_schema'] == schema and r.get('_id') == cond.get('_id') for r in self.rows)

    async def insert(self, schema, data):
        self.rows.append({'_schema': schema, **data})
        return data

    async def insert_many(self, schema, docs):
        for d in docs:
            self.rows.append({'_schema': schema, **d})
        return docs

    async def query(self, gql, params):
        rid = params['c0']['resourceId']
        return [r for r in self.rows if r['_schema'] == 'ResourceLocation' and r.get('resourceId') == rid]

    async def query_one(self, gql, params):
        return next((r for r in self.rows
                     if r['_schema'] == 'Resource' and r.get('_id') == params['c0'].get('_id')), None)

    async def remove(self, schema, cond):
        for i in range(len(self.rows) - 1, -1, -1):
            r = self.rows[i]
            hit = (r['resourceId'] == cond['resourceId']) if 'resourceId' in cond else (r.get('_id') == cond.get('_id'))
            if r['_schema'] == schema and hit:
                del self.rows[i]
        return {'deletedCount': 1}


class _GenericStore:
    """通用 fake store：按物理字段名匹配（供字段映射用例）。"""

    def __init__(self, meta_missing=False):
        self.rows = []
        self.meta_missing = meta_missing

    @staticmethod
    def _match(r, cond):
        return all(r.get(k) == v for k, v in cond.items())

    async def exists(self, schema, cond):
        return any(r['_schema'] == schema and self._match(r, cond) for r in self.rows)

    async def insert(self, schema, data):
        self.rows.append({'_schema': schema, **data})
        return data

    async def insert_many(self, schema, docs):
        for d in docs:
            self.rows.append({'_schema': schema, **d})
        return docs

    @staticmethod
    def _schema_of(gql):
        return gql.split('(')[0].strip()

    async def query(self, gql, params):
        schema = self._schema_of(gql)
        return [r for r in self.rows if r['_schema'] == schema and self._match(r, params['c0'])]

    async def query_one(self, gql, params):
        if self.meta_missing:
            return None
        schema = self._schema_of(gql)
        return next((r for r in self.rows if r['_schema'] == schema and self._match(r, params['c0'])), None)

    async def remove(self, schema, cond):
        for i in range(len(self.rows) - 1, -1, -1):
            r = self.rows[i]
            if r['_schema'] == schema and self._match(r, cond):
                del self.rows[i]
        return {'deletedCount': 1}


def _run(coro):
    return asyncio.run(coro)


def test_put_fanout_open_degrade_url():
    async def scenario():
        store = _FakeStore()
        resource.register_provider('memory', _MemoryProvider)
        with tempfile.TemporaryDirectory() as tmp:
            resource.configure({
                'store': store,
                'providers': [{'kind': 'local', 'options': {'baseDir': tmp}}, {'kind': 'memory'}],
                'url': {'baseUrl': 'https://cdn', 'pathTemplate': '/{contentPath}'},
            })

            out = await resource.put(bytes=b'hello', file_name='a.txt', mime='text/plain')
            assert len(out['locations']) == 2
            assert re.match(r'^[0-9a-f]{40}$', out['resourceId'])

            for r in store.rows:
                if r['_schema'] == 'ResourceLocation' and r['backend'] == 'local':
                    r['status'] = 'failed'

            got = await resource.open(out['resourceId'])
            assert got['backend'] == 'memory'
            assert got['bytes'] == b'hello'

            sha1 = out['sha1']
            assert await resource.url(out['resourceId']) == f'https://cdn/objects/{sha1[:2]}/{sha1}'
            assert await resource.url('https://x/y.png') == 'https://x/y.png'

    _run(scenario())


class _BrokenGetProvider:
    @staticmethod
    def create(options=None):
        async def put(key, data, opts=None):
            return None

        async def get(key, opts=None):
            raise RuntimeError('disk boom')

        async def remove(key, opts=None):
            return None

        async def exists(key, opts=None):
            return False

        return {'kind': 'badget', 'put': put, 'get': get, 'remove': remove, 'exists': exists}


class _BrokenPutProvider:
    @staticmethod
    def create(options=None):
        async def put(key, data, opts=None):
            raise RuntimeError('boom')

        async def get(key, opts=None):
            raise RuntimeError('boom')

        async def remove(key, opts=None):
            return None

        async def exists(key, opts=None):
            return False

        return {'kind': 'broken', 'put': put, 'get': get, 'remove': remove, 'exists': exists}


def test_open_degrade_on_provider_failure():
    async def scenario():
        store = _FakeStore()
        events = []
        prev = feedback.get_sink()
        feedback.set_sink(events.append)
        try:
            resource.register_provider('badget', _BrokenGetProvider)
            resource.register_provider('memory', _MemoryProvider)
            resource.configure({'store': store,
                                'providers': [{'kind': 'badget'}, {'kind': 'memory'}], 'url': {}})
            out = await resource.put(bytes=b'hello')
            got = await resource.open(out['resourceId'])
            assert got['backend'] == 'memory'
            assert got['bytes'] == b'hello'
            assert any(e['code'] == 'resourceLocationDegraded' for e in events)
        finally:
            feedback.set_sink(prev)

    _run(scenario())


def test_open_missing_resource_raises_prefixed_error():
    """零副本行 → 抛 FileNotFoundError 且带 ERR_RESOURCE_NOT_FOUND: 稳定前缀（spec/03 判定顺序第 4 层）。"""
    async def scenario():
        store = _FakeStore()
        resource.register_provider('memory', _MemoryProvider)
        resource.configure({'store': store, 'providers': [{'kind': 'memory'}], 'url': {}})

        rid = '0' * 40
        prefix = 'ERR_RESOURCE_NOT_FOUND:'
        with pytest.raises(FileNotFoundError) as ei:
            await resource.open(rid)
        assert str(ei.value).startswith(prefix), str(ei.value)
        assert str(ei.value)[len(prefix):] == f'资源不存在或无可读副本: {rid}'

    _run(scenario())


def test_open_provider_failure_reraises_without_prefix():
    """有副本行但 provider 读取失败 → 原样重抛、不带前缀（保持 500 语义，禁伪装成「不存在」）。"""
    async def scenario():
        store = _FakeStore()
        prev = feedback.get_sink()
        feedback.set_sink(lambda e: None)
        try:
            resource.register_provider('badget', _BrokenGetProvider)
            resource.configure({'store': store, 'providers': [{'kind': 'badget'}], 'url': {}})
            store.rows.append({'_schema': 'ResourceLocation', 'resourceId': 'a' * 40,
                               'backend': 'badget', 'key': 'k', 'status': 'ok', 'priority': 0})

            with pytest.raises(RuntimeError) as ei:
                await resource.open('a' * 40)
            assert str(ei.value) == 'disk boom'
            assert not str(ei.value).startswith('ERR_RESOURCE_NOT_FOUND:')
        finally:
            feedback.set_sink(prev)

    _run(scenario())


def test_partial_write_failure_persists_failed_and_feedback():
    async def scenario():
        store = _FakeStore()
        events = []
        prev = feedback.get_sink()
        feedback.set_sink(events.append)
        try:
            resource.register_provider('broken', _BrokenPutProvider)
            resource.register_provider('memory', _MemoryProvider)
            resource.configure({'store': store,
                                'providers': [{'kind': 'memory'}, {'kind': 'broken'}], 'url': {}})
            out = await resource.put(bytes=b'x')
            by_backend = {loc['backend']: loc for loc in out['locations']}
            assert by_backend['broken']['status'] == 'failed'
            assert by_backend['memory']['status'] == 'ok'
            assert any(e['code'] == 'resourceLocationWriteFailed' for e in events)

            got = await resource.open(out['resourceId'])
            assert got['backend'] == 'memory'
        finally:
            feedback.set_sink(prev)

    _run(scenario())


def test_fields_mapping_full_chain():
    """字段映射：自定义物理字段名全链路（put/open/remove/binding/跳过列）。"""
    async def scenario():
        store = _GenericStore()
        resource.register_provider('memmap', _MemoryProvider)
        resource.configure({
            'store': store,
            'schema': {'resource': 'Asset', 'location': 'AssetLoc', 'binding': 'AssetBind'},
            'fields': {
                'resource': {'sha1': 'contentHash', 'fileName': 'name', 'mime': 'contentType',
                             'size': None, 'kind': None},
                'location': {'resourceId': 'assetId', 'backend': 'store', 'key': 'objectKey',
                             'status': None, 'priority': None},
                'binding': {'resourceId': 'assetId', 'businessTable': 'entity', 'businessId': 'entityId',
                            'userId': None},
            },
            'providers': [{'kind': 'memmap'}],
        })

        out = await resource.put(bytes=b'data', file_name='n.bin', mime='application/x-bin', kind='img',
                                 bind={'businessTable': 'T', 'businessId': 7, 'userId': 9})

        res = next(r for r in store.rows if r['_schema'] == 'Asset')
        assert res == {'_schema': 'Asset', '_id': out['resourceId'], 'contentHash': out['sha1'],
                       'name': 'n.bin', 'contentType': 'application/x-bin'}
        loc = next(r for r in store.rows if r['_schema'] == 'AssetLoc')
        assert loc['assetId'] == out['resourceId']
        assert loc['store'] == 'memmap'
        assert out['sha1'] in loc['objectKey']
        assert 'status' not in loc and 'priority' not in loc          # 跳过列不落库
        bind = next(r for r in store.rows if r['_schema'] == 'AssetBind')
        assert bind == {'_schema': 'AssetBind', 'assetId': out['resourceId'], 'entity': 'T', 'entityId': '7'}
        assert 'userId' not in bind                                    # 可选角色 null → 跳过

        got = await resource.open(out['resourceId'])
        assert got['bytes'] == b'data'
        assert got['fileName'] == 'n.bin'
        assert got['mime'] == 'application/x-bin'
        assert got['backend'] == 'memmap'
        assert got['key'] == loc['objectKey']

        await resource.remove(out['resourceId'])
        assert len([r for r in store.rows if r['_schema'] in ('Asset', 'AssetLoc')]) == 0

    _run(scenario())


def test_fields_invalid_raises_no_write():
    """必填/未知 → configure 抛错且零写库。"""
    store = _GenericStore()
    base = {'store': store, 'providers': [{'kind': 'memmap'}]}
    with pytest.raises(ValueError):
        resource.configure({**base, 'fields': {'resource': {'sha1': None}}})
    with pytest.raises(ValueError):
        resource.configure({**base, 'fields': {'location': {'backend': ''}}})
    with pytest.raises(ValueError):
        resource.configure({**base, 'fields': {'binding': {'businessId': 1}}})
    with pytest.raises(ValueError):
        resource.configure({**base, 'fields': {'bogus': {}}})
    with pytest.raises(ValueError):
        resource.configure({**base, 'fields': {'resource': {'bogus': 'x'}}})
    assert len(store.rows) == 0


def test_fields_meta_missing_feedback():
    """meta 缺失 → 仍出字节 + resource_meta_missing 反馈。"""
    async def scenario():
        store = _GenericStore(meta_missing=True)
        events = []
        prev = feedback.get_sink()
        feedback.set_sink(events.append)
        try:
            resource.register_provider('memmiss2', _MemoryProvider)
            resource.configure({'store': store, 'providers': [{'kind': 'memmiss2'}]})
            out = await resource.put(bytes=b'z', file_name='z.bin', mime='text/plain')
            got = await resource.open(out['resourceId'])
            assert got['bytes'] == b'z'
            assert got['fileName'] is None                             # meta 缺失 → None（调用方兜底）
            assert got['mime'] is None
            assert any(e['code'] == 'resourceMetaMissing' for e in events)
        finally:
            feedback.set_sink(prev)

    _run(scenario())
