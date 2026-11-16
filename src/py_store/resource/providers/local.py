"""本地目录 provider。"""

import asyncio
import os


def create(options=None):
    """构造本地目录 provider（``options['baseDir']`` 为根目录，缺省 ``.resource-store``）。"""
    options = options or {}
    root = os.path.realpath(
        os.path.abspath(options.get("baseDir") or ".resource-store")
    )

    def full(key):
        """key → 落盘绝对路径（realpath 规范化；逃逸出 baseDir 显式拒绝）。"""
        # realpath + 包含性校验：``..`` 或符号链接逃逸出 baseDir 时显式拒绝（禁静默失守）；
        # 合法 key 的落盘目标不变（realpath 仅规范化路径/解析链接）
        p = os.path.realpath(os.path.join(root, key))
        if os.path.commonpath([p, root]) != root:
            raise ValueError(f"local provider: key escapes baseDir: {key!r}")
        return p

    async def put(key, data, opts=None):
        """写入对象（建目录 + 落盘，IO 经线程池）。"""
        p = full(key)
        await asyncio.to_thread(os.makedirs, os.path.dirname(p), exist_ok=True)
        await asyncio.to_thread(_write, p, data)

    async def get(key, opts=None):
        """读取对象字节。"""
        return await asyncio.to_thread(_read, full(key))

    async def remove(key, opts=None):
        """删除对象（不存在视为已删，不报错）。"""
        return await asyncio.to_thread(_rm, full(key))

    async def exists(key, opts=None):
        """判断对象是否存在。"""
        return os.path.exists(full(key))

    return {
        "kind": options.get("kind", "local"),
        "put": put,
        "get": get,
        "remove": remove,
        "exists": exists,
    }


def _write(p, data):
    """同步写入文件（二进制）。"""
    with open(p, "wb") as f:
        f.write(data)


def _read(p):
    """同步读取文件全部字节。"""
    with open(p, "rb") as f:
        return f.read()


def _rm(p):
    """同步删除文件（``FileNotFoundError`` 视为已删）。"""
    try:
        os.remove(p)
    except FileNotFoundError:
        pass
