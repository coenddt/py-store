"""本地磁盘数据源 —— 宿主门面（py 侧）

``connect({'dir': …})`` 产出一个**连接描述符**（``dict``，``kind == 'local'``），
供 ``init`` / ``set_connections`` 注册为数据源；命令求值一律经 ``handle`` →
``core.local_eval``（宿主只做 IO 与手柄适配）。

描述符契约（与执行文档 §0 常驻契约卡 / §4.1 对齐）：

  ``{'kind': 'local', 'dir', 'handle', 'open_transaction', 'with_transaction'}``

  - ``handle``：PyMongo Database 兼容（``db[name]``），直连 IO（即时读写磁盘）；
  - ``open_transaction()``：返回 ``{'session', 'handle', 'commit', 'rollback', 'release'}``
    （与 ``py_store/executors/sqlite.py`` 事务句柄同形，三者均为 async 可调用）；
    事务期 ``handle`` 读写**内存快照** ``staged``，``commit()`` 才整目录落盘，
    ``rollback()`` 丢弃 —— 快照隔离天然提供原子性；
  - ``with_transaction(fn)``：便捷包装（与 sqlite 执行器同形）。

★ 描述符必须是 ``dict``（``Mapping``）——``datasource._kind_of`` 按 Mapping 取 ``kind``
（见执行文档 §8-1）。并发写经 ``store.with_dir_lock`` 串行化（**单进程**；跨进程不在
v1 保证范围，见执行文档 §8-1）。对齐 ``nodejs-store/src/local/index.js``。
"""

from __future__ import annotations

import pathlib
from typing import Any

from .handle import create_db
from .store import read_snapshot, with_dir_lock, write_collections

LOCAL_KIND = 'local'


def connect(options: dict | None = None) -> dict:
    """创建本地磁盘数据源连接（描述符）。

    :param options: ``{'dir': <路径>}``（兼容 ``base_dir``）；缺省 ``.store-local``（相对 cwd）
    """
    options = options or {}
    root = pathlib.Path(
        options.get('dir') or options.get('base_dir') or '.store-local'
    ).resolve()

    def _load():
        return read_snapshot(root)

    def _save(changed, collections):
        with_dir_lock(root, lambda: write_collections(root, changed, collections))

    desc = {
        'kind': LOCAL_KIND,
        'dir': str(root),
        'handle': create_db(_load, _save),
    }

    async def open_transaction():
        """事务原语：快照隔离（读内存 ``staged``，commit 才落盘）。"""
        state: dict[str, Any] = {'staged': read_snapshot(root), 'closed': False}

        def _tx_load():
            return state['staged']

        def _tx_save(_changed, collections):
            state['staged'] = collections

        async def commit():
            if state['closed']:
                return
            state['closed'] = True
            staged = state['staged']
            with_dir_lock(root, lambda: write_collections(root, list(staged.keys()), staged))

        async def rollback():
            state['closed'] = True

        async def release():
            state['closed'] = True

        return {
            'session': {'snapshot': lambda: state['staged']},
            'handle': create_db(_tx_load, _tx_save),
            'commit': commit,
            'rollback': rollback,
            'release': release,
        }

    async def with_transaction(fn):
        """便捷包装（与 sqlite 执行器 with_transaction 同形）。"""
        tx = await open_transaction()
        try:
            out = await fn(None, tx)
            await tx['commit']()
            return out
        except BaseException:
            await tx['rollback']()
            raise
        finally:
            await tx['release']()

    desc['open_transaction'] = open_transaction
    desc['with_transaction'] = with_transaction
    return desc


__all__ = ['LOCAL_KIND', 'connect']
