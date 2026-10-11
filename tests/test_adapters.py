"""第三方接口适配器 E2E（py 侧）—— A7 前半 / A9

镜像 ``nodejs-store/tests/adapters.test.js``：
  - A7 前半：本地 HTTP stub → ``pull_into`` → 落主链路 → 既有 GQL 查询命中；
  - A9：非 2xx / 响应体非 JSON / ``pick`` 非 list ⇒ ``HttpAdapterError``（禁静默）；
    成功 0 条 ⇒ ``{'pulled': 0, 'written': None}`` 且不调用写路径。

运行：``cd py-store; PYTHONPATH=src LOCAL_CORE=1 python -m pytest tests/test_adapters.py -q``
"""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from py_store import datasource as _ds
from py_store import init, store
from py_store import schema as _sc
from py_store.adapters import HttpAdapterError, create_http_adapter, pull_into
from py_store.memory import connect


def _run(coro):
    return asyncio.run(coro)


def _register_test_schemas():
    """conftest 模块夹具重放入口（清场后恢复本模块 schema）。"""
    _sc.register({
        'name': 'AdpPyUser', 'collection': 'adp_py_users', 'idPrefix': 'adpp_', 'timestamps': False,
        'fields': {'name': {'type': 'string'}}, 'relations': {},
    })


# ─── 本地 HTTP stub（监听 127.0.0.1 随机端口，模块结束关闭） ──────────────

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静音访问日志
        pass

    def _json(self, status, obj):
        body = json.dumps(obj).encode('utf-8')
        self.send_response(status)
        self.send_header('content-type', 'application/json')
        self.send_header('content-length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/pull':
            self._json(200, [{'name': 'Ada'}, {'name': 'Bob'}])
        elif self.path == '/boom':
            self._json(500, {'error': 'boom'})
        elif self.path == '/notjson':
            body = b'<html>not json</html>'
            self.send_response(200)
            self.send_header('content-type', 'text/plain')
            self.send_header('content-length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == '/scalar':
            self._json(200, {'total': 2})  # 对象而非数组
        elif self.path == '/empty':
            self._json(200, [])
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture(scope='module')
def stub_base_url():
    server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_address[1]}'
    finally:
        server.shutdown()
        server.server_close()


# ─── A7 前半：stub → pull_into → 落主链路 → GQL 命中 ────────────────────

def test_adapters_a7_pull_into_query(stub_base_url):
    try:
        _run(init(connect({'store': {}})))

        async def _main():
            adapter = create_http_adapter({'base_url': f'{stub_base_url}/pull'})
            out = await pull_into(adapter, 'AdpPyUser', store)
            assert out['pulled'] == 2

            rows = await store.query('AdpPyUser{ _id name }')
            assert len(rows) == 2
            assert sorted(r['name'] for r in rows) == ['Ada', 'Bob']
            assert await store.count('AdpPyUser', {}) == 2

        _run(_main())
    finally:
        _ds.set_connections({})


# ─── A9：失败显式 Err（禁静默） ───────────────────────────────────────

def test_adapters_a9_non_2xx(stub_base_url):
    adapter = create_http_adapter({'base_url': f'{stub_base_url}/boom'})
    with pytest.raises(HttpAdapterError) as ei:
        _run(adapter['fetch']())
    assert ei.value.status == 500
    assert ei.value.url.endswith('/boom')


def test_adapters_a9_not_json(stub_base_url):
    adapter = create_http_adapter({'base_url': f'{stub_base_url}/notjson'})
    with pytest.raises(HttpAdapterError):
        _run(adapter['fetch']())


def test_adapters_a9_pick_not_list(stub_base_url):
    adapter = create_http_adapter(
        {'base_url': f'{stub_base_url}/scalar', 'pick': lambda j: j['total']})
    with pytest.raises(HttpAdapterError):
        _run(adapter['fetch']())


# ─── A9：成功 0 条 ⇒ pulled:0 且不写（合法结果，与失败可区分） ──────────

def test_adapters_a9_empty_no_write(stub_base_url):
    adapter = create_http_adapter({'base_url': f'{stub_base_url}/empty'})
    calls = {'n': 0}

    class _SpyStore:
        async def insert_many(self, *args, **kwargs):
            calls['n'] += 1
            return []

    out = _run(pull_into(adapter, 'AdpPyEmpty', _SpyStore()))
    assert out == {'pulled': 0, 'written': None}
    assert calls['n'] == 0
