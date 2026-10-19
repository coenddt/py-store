"""资源凭据命名池（C2/A3）单测：双池共存互不串写 + 旧形状向后兼容 + 互斥/未知池校验。

对应执行文档 03 §4.4：s3 双凭据用 local provider 双根目录等价替代（离线可跑）；
元数据经 configure({"store": _FakeStore()}) 注入假 store（不依赖真库）。

运行：$env:PYTHONPATH='py-store/src'; python -m pytest py-store/tests/test_resource_pools.py -q
"""

import asyncio
import os
import tempfile

import pytest

from py_store import resource
from py_store.resource import configure, create_provider, pool_names
from test_resource import _FakeStore


class _SafeFakeStore(_FakeStore):
    """修共享 fixture 的 remove 缺陷（Resource 行无 resourceId 键会 KeyError），
    本文件用例 remove 时会遍历到 Resource 行，故覆盖；不动共享 fixture 本身。"""

    async def remove(self, schema, cond):
        for i in range(len(self.rows) - 1, -1, -1):
            r = self.rows[i]
            hit = (r.get('resourceId') == cond.get('resourceId')) if 'resourceId' in cond \
                else (r.get('_id') == cond.get('_id'))
            if r['_schema'] == schema and hit:
                del self.rows[i]
        return {'deletedCount': 1}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) \
        if False else asyncio.run(coro)


def _pool_cfg(tmp, subs=('a', 'b')):
    """local provider 双根目录 = 双凭据池的离线等价物"""
    return {'pools': {
        'sys': {'providers': [{'kind': 'local', 'options': {'baseDir': os.path.join(tmp, subs[0])}}]},
        'biz': {'providers': [{'kind': 'local', 'options': {'baseDir': os.path.join(tmp, subs[1])}}]},
    }}


@pytest.fixture(autouse=True)
def _reset():
    yield
    configure({})


def test_dual_pools_isolated():
    """用例1：双池共存——put(pool=...) 各落各的根目录，互不串写"""
    tmp = tempfile.mkdtemp()
    configure({'store': _SafeFakeStore(), **_pool_cfg(tmp)})
    assert pool_names() == ['sys', 'biz']

    r_sys = _run(resource.put(bytes=b'sys-doc', pool='sys'))
    r_biz = _run(resource.put(bytes=b'biz-doc', pool='biz'))
    # 同一内容同 sha1 → 同 resourceId，但两池各自持有副本文件
    # （local provider 文件路径 = baseDir + resource_content_path(sha1)，与 put 同一 key 函数）
    from py_store.core import native
    key_sys = native.resource_content_path(r_sys['sha1'])
    key_biz = native.resource_content_path(r_biz['sha1'])
    a_root, b_root = os.path.join(tmp, 'a'), os.path.join(tmp, 'b')
    assert os.path.exists(os.path.join(a_root, key_sys))
    assert not os.path.exists(os.path.join(a_root, key_biz))
    assert os.path.exists(os.path.join(b_root, key_biz))
    assert not os.path.exists(os.path.join(b_root, key_sys))
    # open 按池读取：sys 池读回的是 sys 写入的字节
    got = _run(resource.open(r_sys['resourceId'], pool='sys'))
    assert got['bytes'] == b'sys-doc'
    # biz 池没有该副本 → 无可读副本报 ERR_RESOURCE_NOT_FOUND（禁跨池串读）
    with pytest.raises(FileNotFoundError):
        _run(resource.open(r_sys['resourceId'], pool='biz'))
    # remove 按池清副本：sys 池清理不影响 biz 池文件
    assert _run(resource.remove(r_sys['resourceId'], pool='sys'))['removed'] == ['local']
    assert not os.path.exists(os.path.join(a_root, key_sys))
    assert os.path.exists(os.path.join(b_root, key_biz))


def test_legacy_shape_backward_compatible():
    """用例2：旧形状完全向后兼容——等价默认池 "default"，put/open/remove/url 行为不变"""
    tmp = tempfile.mkdtemp()
    r = configure({'store': _SafeFakeStore(),
                   'providers': [{'kind': 'local', 'options': {'baseDir': os.path.join(tmp, 'only')}}]})
    # 返回结构视图：默认池键恒 "default"；pool_names 恒 ['default']
    assert list(r.keys()) == ['default']
    assert pool_names() == ['default']
    got = _run(resource.put(bytes=b'legacy', file_name='x.txt', mime='text/plain'))
    assert got['locations'][0]['status'] == 'ok'
    opened = _run(resource.open(got['resourceId']))
    assert opened['bytes'] == b'legacy'
    assert opened['fileName'] == 'x.txt'
    # pool=None 与不传等价（= "default"）
    opened2 = _run(resource.open(got['resourceId'], pool=None))
    assert opened2['bytes'] == b'legacy'


def test_mutual_exclusive_and_unknown_pool():
    """用例3：providers 与 pools 同现抛 ValueError；未知池名抛 ValueError（禁回落默认池）"""
    tmp = tempfile.mkdtemp()
    with pytest.raises(ValueError, match='互斥'):
        configure({'providers': [{'kind': 'local', 'options': {'baseDir': tmp}}],
                   'pools': {'sys': {'providers': []}}})
    configure({'store': _SafeFakeStore(), **_pool_cfg(tmp)})
    with pytest.raises(ValueError, match='未知资源池'):
        _run(resource.put(bytes=b'x', pool='nope'))
    with pytest.raises(ValueError, match='未知资源池'):
        _run(resource.open('deadbeef', pool='nope'))
    with pytest.raises(ValueError, match='未知资源池'):
        _run(resource.remove('deadbeef', pool='nope'))
    with pytest.raises(ValueError, match='未知资源池'):
        _run(resource.url('local://k', pool='nope'))


def test_create_provider_public():
    """用例4：create_provider 公开可达，返回形状稳定（kind + 四方法）"""
    tmp = tempfile.mkdtemp()
    p = create_provider('local', {'baseDir': tmp})
    assert p['kind'] == 'local'
    for m in ('put', 'get', 'remove', 'exists'):
        assert callable(p[m])
    with pytest.raises(ValueError, match='未知资源 provider'):
        create_provider('nope')
