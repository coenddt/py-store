"""本地磁盘数据源的文件 IO（唯一落盘边界）

布局：``<dir>/<物理集合名>.json``，内容为该集合的**文档数组**。

  - 读：整个目录快照 ``{"<集合名>": [文档…]}``（目录不存在 → ``{}``）。
    非数组 / 非法 JSON 文件一律**抛错**（禁静默当空集合，见执行文档 §7）。
  - 写：只回写 ``changed`` 里的集合，先写 ``.tmp`` 再 ``os.replace``（原子替换），
    避免中途崩溃留下半截 JSON。
  - 串行化：同一目录的写操作经 ``with_dir_lock`` 互斥（**仅覆盖单进程**；
    跨进程并发不在 v1 保证范围，见执行文档 §8-1）。

对齐 ``nodejs-store/src/local/store.js``。
"""

from __future__ import annotations

import json
import os
import threading

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(root) -> threading.Lock:
    key = str(root)
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def read_snapshot(root) -> dict:
    """读取整个目录快照 ``{集合名: [文档…]}``。

    目录不存在 → ``{}``；非数组 JSON / 非法 JSON 一律抛错（禁静默当空集合）。
    """
    root = os.fspath(root)
    if not os.path.isdir(root):
        return {}
    out = {}
    for name in sorted(os.listdir(root)):
        # `.` 前缀（含 `.tmp` 中间文件）与后缀非 `.json` 的一律跳过
        if not name.endswith('.json') or name.startswith('.'):
            continue
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        with open(path, encoding='utf-8') as fh:
            docs = json.load(fh)
        if not isinstance(docs, list):
            raise ValueError(f'本地集合文件非法（须为文档数组）: {name}')
        out[name[:-5]] = docs
    return out


def write_collections(root, changed, collections) -> None:
    """只回写 ``changed`` 内的集合；临时文件 + ``os.replace`` 原子替换。"""
    root = os.fspath(root)
    names = list(changed)
    if not names:
        return
    os.makedirs(root, exist_ok=True)
    for name in names:
        path = os.path.join(root, f'{name}.json')
        tmp = os.path.join(root, f'.{name}.json.tmp')
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(collections.get(name) or [], fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)


def with_dir_lock(root, fn):
    """同一目录的写操作互斥（避免同一进程内并发写互相覆盖）。"""
    with _lock_for(root):
        return fn()