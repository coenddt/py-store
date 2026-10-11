"""HTTP 拉取适配器（第三方接口 = 供数链路；py 侧，对齐执行文档 03 §4.3）。

与 ``nodejs-store/src/adapters/http.js`` 语义同构：

  - 只有 **pull** 能力，不可被 GQL 直接查询（禁注册为 ``datasource`` kind）；
  - 只做「请求 → 规范化 → 返回 ``list[dict]``」，**禁 filter / 排序 / 聚合**（无下推能力）；
  - 失败一律**显式抛错**（禁静默成空数组）：非 2xx / 响应体非 JSON / ``pick`` 结果非 list
    均抛 ``HttpAdapterError``；成功且 0 条 ⇒ 返回 ``[]``（合法结果，与失败可区分）；
  - 不做重试、不做分页循环。

HTTP 客户端用**标准库** ``urllib.request``（经 ``asyncio.to_thread`` 执行，避免阻塞事件循环），
不新增第三方依赖；允许注入 ``fetch_impl`` 供测试与自定义传输。
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Callable

__all__ = ['HttpAdapterError', 'create_http_adapter']


class HttpAdapterError(Exception):
    """HTTP 适配器错误（携 ``status`` / ``url``）。"""

    def __init__(self, message: str, status: int | None = None, url: str | None = None):
        super().__init__(message)
        self.name = 'HttpAdapterError'
        self.status = status
        self.url = url


def _default_fetch(url, method, headers, body, timeout):
    """标准库同步实现（由调用方经 ``asyncio.to_thread`` 执行）；返回 ``(status, text)``。"""
    data = body.encode('utf-8') if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')


def create_http_adapter(spec: dict) -> dict:
    """创建 HTTP 拉取适配器。

    :param spec: ``{'base_url', 'method'='GET', 'headers', 'path'(params)->str,
        'pick'(json)->list, 'map'(row)->dict, 'fetch_impl', 'timeout_ms'}``
    :returns: ``{'fetch': async (params=None) -> list[dict]}``
    """
    if not spec or not spec.get('base_url'):
        raise TypeError('create_http_adapter 需要 spec.base_url')
    base_url = spec['base_url']
    method = spec.get('method', 'GET')
    headers = spec.get('headers')
    path_fn: Callable | None = spec.get('path')
    pick: Callable | None = spec.get('pick')
    map_fn: Callable | None = spec.get('map')
    fetch_impl: Callable | None = spec.get('fetch_impl')
    timeout_ms = spec.get('timeout_ms') or 0
    fetch = fetch_impl or _default_fetch

    async def fetch_docs(params: dict | None = None) -> list:
        params = params or {}
        url = path_fn(params) if path_fn else base_url
        hdrs = dict(headers or {})
        body = None
        if method == 'POST':
            hdrs.setdefault('content-type', 'application/json')
            body = json.dumps(params)
        timeout = timeout_ms / 1000 if timeout_ms else None

        try:
            status, text = await asyncio.to_thread(fetch, url, method, hdrs, body, timeout)
        except HttpAdapterError:
            raise
        except Exception as e:  # 网络层错误 ⇒ 显式 Err（禁静默）
            raise HttpAdapterError(f'HTTP 请求失败 {url}: {e}', None, url) from e

        if not (200 <= status < 300):
            raise HttpAdapterError(f'HTTP {status} {url}', status, url)

        try:
            payload = json.loads(text)
        except (ValueError, TypeError) as e:
            raise HttpAdapterError(
                f'HTTP 响应非 JSON {url}: {str(text)[:200]}', status, url) from e

        rows = pick(payload) if pick else payload
        if not isinstance(rows, list):
            raise HttpAdapterError(
                f'HTTP 响应结构非数组 {url}: pick 返回 {type(rows).__name__}', status, url)

        return [map_fn(r) for r in rows] if map_fn else rows

    return {'fetch': fetch_docs}
