"""degraded 模块门禁 —— D8 低覆盖补测（with_request 作用域 + end 跨上下文回退）

对标 degraded.js 语义：

  1. ``with_request`` 同步 fn → ``{result, status}``，容器随作用域建立/清理；
  2. ``with_request`` 异步 fn → await 后同上；
  3. fn 抛错 → 异常原样传播、容器随 finally 清理（``_als.run`` 同语义）；
  4. ``end`` 跨上下文 reset 抛 ``ValueError`` → 保持当前容器原样（不掩盖、不抛出），
     用 ``asyncio.create_task`` 子 task 的独立 context 构造跨上下文 token。

运行：$env:LOCAL_CORE='1'; $env:PYTHONPATH='src'; python -m pytest tests/test_degraded.py -q
"""

import asyncio

import pytest

from py_store import degraded


def _run(coro):
    return asyncio.run(coro)


# ── 1/2. with_request：同步 / 异步 fn ─────────────────────────


def test_with_request_sync_fn():
    out = _run(degraded.with_request(lambda: "ok"))
    assert out["result"] == "ok"
    assert out["status"] == {"degraded": False, "events": []}


def test_with_request_async_fn():
    async def fn():
        degraded.record({"type": "t"})
        return 7

    out = _run(degraded.with_request(fn))
    assert out["result"] == 7
    assert out["status"]["degraded"] is True
    assert [e["type"] for e in out["status"]["events"]] == ["t"]


# ── 3. fn 抛错：原样传播 + 容器清理 ───────────────────────────


def test_with_request_propagates_and_cleans():
    def boom():
        degraded.record({"type": "before_fail"})
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        _run(degraded.with_request(boom))
    # 容器已随 finally 清理：作用域外 status 视为空
    assert degraded.status() == {"degraded": False, "events": []}


# ── 4. end：跨上下文 reset 失败保持原样 ───────────────────────


def test_end_cross_context_reset_keeps_bucket():
    holder = {}

    async def inner():
        holder["token"] = degraded.begin()
        degraded.record({"type": "in_task"})

    async def main():
        t = asyncio.create_task(inner())  # 子 task 独立 context：token 归属其 context
        await t
        # 父 context 里 reset 子 context 的 token → ValueError → 被吞、容器原样
        degraded.end(holder["token"])

    _run(main())
    assert degraded.status() == {"degraded": False, "events": []}


# ── 5. emit_plan / vector_degraded（monkeypatch emit 收集广播事件） ──


def test_emit_plan_forwards_and_defaults_type(monkeypatch):
    seen = []
    monkeypatch.setattr(degraded, "emit", lambda evt: seen.append(evt))
    degraded.emit_plan({"degraded": [{"type": "a", "x": 1}, {"x": 2}]}, "fb")
    degraded.emit_plan(None, "fb")  # 空计划零事件
    assert [e["type"] for e in seen] == ["a", "fb"]
    assert seen[0]["x"] == 1 and seen[1]["x"] == 2


def test_vector_degraded_shape(monkeypatch):
    seen = []
    monkeypatch.setattr(degraded, "emit", lambda evt: seen.append(evt))
    degraded.vector_degraded(
        backend="sqlite", field="emb", metric="cosine",
        message="vec missing", hint="create index",
    )
    assert seen == [
        {
            "type": "vector_degraded",
            "code": "vectorDegraded",
            "layer": "host",
            "backend": "sqlite",
            "field": "emb",
            "metric": "cosine",
            "message": "vec missing",
            "hint": "create index",
        }
    ]
