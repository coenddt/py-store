"""redis_io 单元测试（与 nodejs-store/src/cache/redis-io.js 同构的整量载体）。

覆盖基线 D8 点名的低覆盖分支：client 契约校验、scan/get 失败显式 Err（禁静默当空
快照，A9）、非法 JSON 显式 Err、键分区过滤、save 只回写变更集合、同步/异步 client
兼容（_maybe_await 两分支）。client 为内存假实现（禁新增 Redis 依赖，同生产契约）。
"""

import asyncio
import json

import pytest

from py_store.cache.redis_io import RedisIoError, create_redis_io


def _run(coro):
    return asyncio.run(coro)


class _FakeClient:
    """契约假实现：scan(pattern)->list[str] / get(key)->str|None / set(key,value)"""

    def __init__(self, data=None, *, async_mode=False, fail=None, bad_scan=None):
        self.data = dict(data or {})
        self.async_mode = async_mode
        self.fail = dict(fail or {})  # op -> Exception
        self.bad_scan = bad_scan  # scan 返回值替换（测非数组分支）
        self.sets = []

    def scan(self, pattern):
        if self.async_mode:
            return self._async(self._scan(pattern))
        return self._scan(pattern)

    def _scan(self, pattern):
        if "scan" in self.fail:
            raise self.fail["scan"]
        if self.bad_scan is not None:
            return self.bad_scan
        # 极简 glob：仅支持前缀 * 尾部通配
        if pattern.endswith("*"):
            return [k for k in self.data if k.startswith(pattern[:-1])]
        return [k for k in self.data if k == pattern]

    def get(self, key):
        if self.async_mode:
            return self._async(self._get(key))
        return self._get(key)

    def _get(self, key):
        if "get" in self.fail:
            raise self.fail["get"]
        return self.data.get(key)

    def set(self, key, value):
        if self.async_mode:
            return self._async(self._set(key, value))
        return self._set(key, value)

    def _set(self, key, value):
        if "set" in self.fail:
            raise self.fail["set"]
        self.data[key] = value
        self.sets.append((key, value))

    @staticmethod
    async def _async(value):
        return value


def _io(client, **kw):
    return create_redis_io(client=client, **kw)


class _BadScanOnly:
    def scan(self, pattern):
        return []


class _BadGetOnly:
    def scan(self, pattern):
        return []

    def get(self, key):
        return None


# ---------- 工厂契约 ----------


@pytest.mark.parametrize(
    "client",
    [None, object(), _BadScanOnly(), _BadGetOnly()],
    ids=["none", "no-methods", "missing-get", "missing-set"],
)
def test_create_requires_client_with_scan_get_set(client):
    with pytest.raises(RedisIoError, match="需要注入 client") as ei:
        _io(client)
    assert ei.value.op == "init"


# ---------- load：整量取回 ----------


def test_load_sync_client_returns_collections():
    c = _FakeClient(
        {"store/main/orders": "[{\"_id\":\"a\"}]", "store/main/users": "[]"}
    )
    out = _run(_io(c)["load"]("main"))
    assert out == {"orders": [{"_id": "a"}], "users": []}


def test_load_async_client_same_result():
    c = _FakeClient({"store/orders": "[]"}, async_mode=True)
    out = _run(_io(c)["load"]())
    assert out == {"orders": []}


def test_load_scan_failure_raises_explicit():
    c = _FakeClient(fail={"scan": RuntimeError("conn refused")})
    with pytest.raises(RedisIoError, match="SCAN 失败") as ei:
        _run(_io(c)["load"]("main"))
    assert ei.value.op == "scan"
    assert ei.value.database == "main"


def test_load_scan_non_array_raises():
    c = _FakeClient(bad_scan="not-a-list")
    with pytest.raises(RedisIoError, match="返回值非数组"):
        _run(_io(c)["load"]())


def test_load_get_failure_raises():
    c = _FakeClient({"store/orders": "[]"}, fail={"get": RuntimeError("boom")})
    with pytest.raises(RedisIoError, match="GET 失败") as ei:
        _run(_io(c)["load"]())
    assert ei.value.op == "get"


def test_load_invalid_json_raises():
    c = _FakeClient({"store/orders": "{oops"})
    with pytest.raises(RedisIoError, match="非法 JSON") as ei:
        _run(_io(c)["load"]())
    assert ei.value.op == "parse"


def test_load_skips_expired_keys():
    # raw None（键已失效）跳过；非解析失败
    c = _FakeClient({"store/orders": "[]", "store/gone": None, "store/empty": None})
    c.data["store/gone"] = None  # get 显式 None
    out = _run(_io(c)["load"]())
    assert out == {"orders": []}


@pytest.mark.parametrize(
    "scan_keys, db, want",
    [
        # 正常：键 = base/database/collection（有 database）
        (["store/main/orders"], "main", {"orders": []}),
        # 非 str 键跳过（client 越界返回 bytes 等）
        (["store/main/orders", b"store/main/x"], "main", {"orders": []}),
        # 键不在 base 前缀内（防 client 越界）跳过
        (["other/orders"], "main", {}),
        # 同 base 前缀但不属本库分区跳过
        (["store/other/orders"], "main", {}),
        # 嵌套分区（db='a' 不命中 'a/b/c'）跳过
        (["store/a/b/c"], "a", {}),
        # rel 为空（恰为 db 目录本身）跳过
        (["store/main/"], "main", {}),
        # database 为空：键 = base/collection
        (["store/orders"], None, {"orders": []}),
    ],
)
def test_load_partition_filtering(scan_keys, db, want):
    # 用 bad_scan 直接提供键列表（模拟 client 越界返回，覆盖载体内全部过滤分支）
    c = _FakeClient(bad_scan=scan_keys)
    for k in scan_keys:
        if isinstance(k, str):
            c.data[k] = "[]"
    out = _run(_io(c)["load"](db))
    assert out == want


# ---------- save：只回写变更 ----------


def test_save_writes_only_changed_collections():
    c = _FakeClient({})
    colls = {"orders": [{"_id": "b"}], "users": None}
    # changed 是 set：save 按 list(changed) 遍历，顺序不定 → 断言用键值映射（无序）
    _run(_io(c)["save"]("main", {"orders", "users"}, colls))
    assert dict(c.sets) == {
        "store/main/orders": "[{\"_id\": \"b\"}]",
        "store/main/users": "[]",  # value None ⇒ 空数组（与 local 同规）
    }


def test_save_set_failure_raises():
    c = _FakeClient(fail={"set": RuntimeError("readonly")})
    with pytest.raises(RedisIoError, match="SET 失败") as ei:
        _run(_io(c)["save"]("main", ["orders"], {"orders": []}))
    assert ei.value.op == "set"


def test_save_unserializable_value_raises():
    c = _FakeClient({})
    with pytest.raises(RedisIoError, match="序列化失败") as ei:
        _run(_io(c)["save"]("main", ["orders"], {"orders": {1, 2}}))  # set 不可序列化
    assert ei.value.op == "stringify"


def test_key_database_empty_omits_segment():
    # _key 的 database 空分支：base/collection（不带 /database/）
    c = _FakeClient({})
    _run(_io(c, prefix="p")["save"](None, ["c"], {"c": []}))
    assert c.sets[0][0] == "p/c"
