"""本地目录 provider。"""

import asyncio
import os


def create(options=None):
    options = options or {}
    root = os.path.realpath(
        os.path.abspath(options.get("baseDir") or ".resource-store")
    )

    def full(key):
        # realpath + 包含性校验：``..`` 或符号链接逃逸出 baseDir 时显式拒绝（禁静默失守）；
        # 合法 key 的落盘目标不变（realpath 仅规范化路径/解析链接）
        p = os.path.realpath(os.path.join(root, key))
        if os.path.commonpath([p, root]) != root:
            raise ValueError(f"local provider: key escapes baseDir: {key!r}")
        return p

    async def put(key, data, opts=None):
        p = full(key)
        await asyncio.to_thread(os.makedirs, os.path.dirname(p), exist_ok=True)
        await asyncio.to_thread(_write, p, data)

    async def get(key, opts=None):
        return await asyncio.to_thread(_read, full(key))

    async def remove(key, opts=None):
        return await asyncio.to_thread(_rm, full(key))

    async def exists(key, opts=None):
        return os.path.exists(full(key))

    return {
        "kind": options.get("kind", "local"),
        "put": put,
        "get": get,
        "remove": remove,
        "exists": exists,
    }


def _write(p, data):
    with open(p, "wb") as f:
        f.write(data)


def _read(p):
    with open(p, "rb") as f:
        return f.read()


def _rm(p):
    try:
        os.remove(p)
    except FileNotFoundError:
        pass
