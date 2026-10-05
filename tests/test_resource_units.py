"""资源能力补测：provider 注册表 / local / s3 / 门面 open·remove·url·_crud 分支。

与 test_resource.py（行为主链路）互补，覆盖为通过 py-store 覆盖率门禁（语句 ≥90%）所需的边角分支。
"""

import asyncio
import io

import pytest

from py_store import resource
from py_store.resource import providers


class _Store:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.removed = []

    async def exists(self, schema, cond):
        return any(r.get('_schema') == schema and r.get('_id') == cond.get('_id') for r in self.rows)

    async def insert(self, schema, data):
        self.rows.append({'_schema': schema, **data})
        return data

    async def insert_many(self, schema, docs):
        for d in docs:
            self.rows.append({'_schema': schema, **d})
        return docs

    async def query(self, gql, params):
        rid = params['c0'].get('resourceId')
        if rid is None:
            return list(self.rows)
        return [r for r in self.rows if r.get('_schema') == 'ResourceLocation' and r.get('resourceId') == rid]

    async def remove(self, schema, cond):
        self.removed.append((schema, cond))
        return {'deletedCount': 0}


def _provider(kind, *, fail_get=False, fail_put=False, fail_remove=False):
    class _P:
        @staticmethod
        def create(options=None):
            db = {}

            async def put(key, data, opts=None):
                if fail_put:
                    raise RuntimeError('put fail')
                db[key] = bytes(data)

            async def get(key, opts=None):
                if fail_get:
                    raise RuntimeError('get fail')
                return db.get(key)

            async def remove(key, opts=None):
                if fail_remove:
                    raise RuntimeError('rm fail')
                db.pop(key, None)

            async def exists(key, opts=None):
                return key in db

            return {'kind': kind, 'put': put, 'get': get, 'remove': remove, 'exists': exists}

    return _P


class _FakeS3:
    def __init__(self, head_error=None):
        self.objects = {}
        self.head_error = head_error

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):
        return {'Body': io.BytesIO(self.objects[Key])}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)

    def head_object(self, Bucket, Key):
        if self.head_error is not None:
            raise self.head_error
        if Key not in self.objects:
            err = Exception('not found')
            err.response = {'ResponseMetadata': {'HTTPStatusCode': 404}}
            raise err
        return {}


def _run(coro):
    return asyncio.run(coro)


def test_providers_registry_errors():
    with pytest.raises(ValueError):
        resource.register_provider('bad', None)
    with pytest.raises(ValueError):
        providers.create_provider('nope')


def test_crud_fallback_without_store():
    resource.configure({'providers': []})
    from py_store import crud
    assert resource._crud() is crud


def test_put_validation_and_bind():
    async def scenario():
        store = _Store()
        resource.register_provider('memory', _provider('memory'))
        resource.configure({'store': store, 'providers': [{'kind': 'memory'}], 'url': {}})

        with pytest.raises(ValueError):
            await resource.put(bytes=None)

        out = await resource.put(bytes='text-payload', file_name='t.txt', mime='text/plain', kind='file',
                                 bind={'businessTable': 'Doc', 'businessId': 1, 'userId': None})
        assert any(r.get('_schema') == 'ResourceBinding' for r in store.rows)
        assert out['locations'][0]['status'] == 'ok'

    _run(scenario())


def test_open_provider_missing_and_all_degraded():
    async def scenario():
        store = _Store([{'_schema': 'ResourceLocation', '_id': 'l1', 'resourceId': 'r1',
                         'backend': 'ghost', 'key': 'k', 'status': 'ok', 'priority': 0}])
        resource.configure({'store': store, 'providers': []})
        with pytest.raises(FileNotFoundError):
            await resource.open('r1')

        store2 = _Store([{'_schema': 'ResourceLocation', '_id': 'l2', 'resourceId': 'r2',
                          'backend': 'bad', 'key': 'k2', 'status': 'ok', 'priority': 0}])
        resource.register_provider('bad', _provider('bad', fail_get=True))
        resource.configure({'store': store2, 'providers': [{'kind': 'bad'}]})
        with pytest.raises(RuntimeError):
            await resource.open('r2')

        resource.configure({'store': store2, 'providers': [{'kind': 'bad'}]})
        with pytest.raises(FileNotFoundError):
            await resource.open('r3', order=['bad'])

    _run(scenario())


def test_remove_paths():
    async def scenario():
        store = _Store([
            {'_schema': 'ResourceLocation', '_id': 'l1', 'resourceId': 'r', 'backend': 'memory', 'key': 'k1', 'status': 'ok'},
            {'_schema': 'ResourceLocation', '_id': 'l2', 'resourceId': 'r', 'backend': 'badrm', 'key': 'k2', 'status': 'ok'},
            {'_schema': 'ResourceLocation', '_id': 'l3', 'resourceId': 'r', 'backend': 'ghost', 'key': 'k3', 'status': 'ok'},
        ])
        resource.register_provider('memory', _provider('memory'))
        resource.register_provider('badrm', _provider('badrm', fail_remove=True))
        resource.configure({'store': store, 'providers': [{'kind': 'memory'}, {'kind': 'badrm'}]})

        out = await resource.remove('r')
        assert out['removed'] == ['memory']
        assert any(s == 'ResourceLocation' for s, _ in store.removed)
        assert any(s == 'Resource' for s, _ in store.removed)

    _run(scenario())


def test_url_vars_and_sign():
    async def scenario():
        async def sign(ref, opts):
            return {'sig': 'abc'}

        resource.configure({'store': _Store(), 'providers': [],
                            'url': {'baseUrl': 'https://c', 'pathTemplate': '/{key}', 'vars': {'a': '1'}},
                            'sign': sign})
        out = await resource.url('ab12cd', vars={'b': '2'})
        assert out.startswith('https://c/ab12cd')
        assert 'sig=abc' in out
        assert await resource.url('https://x/y.png') == 'https://x/y.png'

    _run(scenario())


def test_local_provider(tmp_path):
    async def scenario():
        p = providers.create_provider('local', {'baseDir': str(tmp_path)})
        await p['put']('a/b.bin', b'hi')
        assert await p['exists']('a/b.bin') is True
        assert await p['get']('a/b.bin') == b'hi'
        await p['remove']('a/b.bin')
        assert await p['exists']('a/b.bin') is False
        await p['remove']('missing.bin')  # 缺文件静默
        with pytest.raises(FileNotFoundError):
            await p['get']('missing.bin')

    _run(scenario())


def test_s3_provider():
    pytest.importorskip('boto3')

    with pytest.raises(ValueError):
        providers.create_provider('s3', {})

    async def roundtrip(p):
        await p['put']('k1', b'v1', {'mime': 'text/plain'})
        assert await p['exists']('k1') is True
        assert await p['get']('k1') == b'v1'
        await p['remove']('k1')
        assert await p['exists']('k1') is False

    fake = _FakeS3()
    p = providers.create_provider('s3', {'bucket': 'b', 'prefix': 'pre', 'client': fake})
    _run(roundtrip(p))

    fake2 = _FakeS3()
    p2 = providers.create_provider('s3', {'bucket': 'b', 'prefix': 'pre', 'client': fake2})
    _run(p2['put']('z', b'1'))
    assert 'pre/z' in fake2.objects

    err = RuntimeError('boom')
    err.response = {'ResponseMetadata': {'HTTPStatusCode': 500}, 'Error': {'Code': 'InternalError'}}
    p_err = providers.create_provider('s3', {'bucket': 'b', 'client': _FakeS3(head_error=err)})
    with pytest.raises(RuntimeError):
        _run(p_err['exists']('k'))

    # 构造真实 client（不触网）：覆盖 boto3 导入与 _boto_config
    providers.create_provider('s3', {'bucket': 'b', 'region': 'us-east-1'})
    providers.create_provider('s3', {'bucket': 'b', 'endpoint': 'http://127.0.0.1:9000',
                                     'forcePathStyle': True,
                                     'credentials': {'accessKeyId': 'x', 'secretAccessKey': 'y'}})
