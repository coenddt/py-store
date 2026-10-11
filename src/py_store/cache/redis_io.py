"""Redis 作为「整量求值载体」的 io 实现（04 §4.2，与 ``nodejs-store/src/cache/redis-io.js`` 同构）
—— 不立 ``redis`` 独立 kind，与内存载体同构（经 memory 的 io 注入点接入即可）。

键空间（必须与内存载体一致，§8-3）：
    key = ``<prefix>/<database>/<collection>``；``database`` 为空 ⇒ ``<prefix>/<collection>``。
Redis 只负责**整量取回 / 回写**，**不实现任何查询语义**（filter / 聚合 / 排序）——
求值仍走 ``core.local_eval``（与 memory / local 同路径）。

client 契约（由调用方注入，禁新增 Redis 客户端依赖；同步 / 异步均可，内部兼容 await）：
    scan(pattern) -> list[str]          匹配 pattern 的全部键（游标由调用方内部处理）
    get(key) -> str | None              取单键（值为 JSON 字符串）
    set(key, value) -> Any              写单键

边界（继承内存载体）：不实现失效策略 / TTL（属业务侧）；并发写不做分布式锁（单进程 / 悲观假设）。
"""

from __future__ import annotations

import inspect
import json
from typing import Any


class RedisIoError(Exception):
    """Redis 载体 IO 显式错误（携 ``database`` / ``op``；A9：禁静默当空快照）。"""

    def __init__(self, message: str, *, database: Any = None, op: Any = None, cause: Any = None):
        super().__init__(message)
        self.database = database
        self.op = op
        self.cause = cause


async def _maybe_await(value: Any) -> Any:
    """兼容同步 / 异步 client：是 awaitable 则 await，否则原样返回。"""
    if inspect.isawaitable(value):
        return await value
    return value


def _key(base: str, database: Any, collection: str) -> str:
    """``<prefix>/<database>/<collection>``（``database`` 空 ⇒ ``<prefix>/<collection>``）"""
    return f'{base}/{database}/{collection}' if database else f'{base}/{collection}'


def create_redis_io(*, client: Any, prefix: str = 'store') -> dict:
    """创建「Redis 作载体」的整量 io。

    :param client: 由调用方注入（禁新增依赖），须实现 ``scan`` / ``get`` / ``set``
    :param prefix: 键空间根前缀，默认 ``'store'``
    :returns: ``{'load': (database) -> dict, 'save': (database, changed, collections) -> None}``
    """
    if client is None or not callable(getattr(client, 'scan', None)) \
            or not callable(getattr(client, 'get', None)) \
            or not callable(getattr(client, 'set', None)):
        raise RedisIoError('create_redis_io 需要注入 client（须实现 scan/get/set）', op='init')
    base = '' if prefix == '' else str(prefix)
    base_prefix = f'{base}/' if base else ''

    async def load(database: Any = None) -> dict:
        """整量取回：``{ 集合名: 文档数组 }``；单键解析失败 ⇒ Err（含键名），禁跳过。"""
        db = database
        pattern = f'{base}/{db}/*' if db else f'{base}/*'
        try:
            keys = await _maybe_await(client.scan(pattern))
        except Exception as e:
            raise RedisIoError(f'Redis SCAN 失败（pattern={pattern}）: {e}',
                               database=db, op='scan', cause=e) from e
        if not isinstance(keys, (list, tuple)):
            raise RedisIoError(f'Redis SCAN 返回值非数组（pattern={pattern}）', database=db, op='scan')
        out: dict[str, Any] = {}
        for full_key in keys:
            if not isinstance(full_key, str):
                continue
            if base_prefix and not full_key.startswith(base_prefix):
                continue  # 防 client 越界返回
            rel = full_key[len(base_prefix):] if base_prefix else full_key
            if db:
                db_prefix = f'{db}/'
                if not rel.startswith(db_prefix):
                    continue  # 同前缀但不属本库分区
                rel = rel[len(db_prefix):]
                if '/' in rel:
                    continue  # 如 db='a' 不命中 'a/b/c'
            if rel == '':
                continue
            try:
                raw = await _maybe_await(client.get(full_key))
            except Exception as e:
                raise RedisIoError(f'Redis GET 失败（key={full_key}）: {e}',
                                   database=db, op='get', cause=e) from e
            if raw is None:
                continue  # 键已失效：跳过（非解析失败）
            try:
                out[rel] = json.loads(raw)
            except Exception as e:
                raise RedisIoError(f'Redis 值非法 JSON（key={full_key}）: {e}',
                                   database=db, op='parse', cause=e) from e
        return out

    async def save(database: Any = None, changed: Any = None, collections: Any = None) -> None:
        """只回写 ``changed`` 中的集合（与 local 「只回写变更」同规）。"""
        db = database
        colls = collections or {}
        for name in list(changed or []):
            key = _key(base, db, name)
            value = colls.get(name)
            try:
                payload = json.dumps([] if value is None else value)
            except Exception as e:
                raise RedisIoError(f'序列化失败（key={key}）: {e}',
                                   database=db, op='stringify', cause=e) from e
            try:
                await _maybe_await(client.set(key, payload))
            except Exception as e:
                raise RedisIoError(f'Redis SET 失败（key={key}）: {e}',
                                   database=db, op='set', cause=e) from e

    return {'load': load, 'save': save}


__all__ = ['RedisIoError', 'create_redis_io']
