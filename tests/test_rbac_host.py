"""RBAC 宿主包装端到端冒烟（A6）：set_rbac → 越权 insert → PermissionError(403) → 清除恢复

判决唯一在 core；RBAC 拒绝发生在 plan 阶段（core 产出命令之前），
故本测试无需 DB 连接即可验证门面透传与策略生命周期。
运行：python -m pytest tests/test_rbac_host.py -q（前置：rust-store/core-py 的 .pyd 已构建）
"""

import asyncio

import pytest

from py_store import permission as perm

_POLICY = {
    "mode": "enforce",
    "roles": {"viewer": {}, "editor": {}},
    "grants": [
        {
            "role": "viewer",
            "model": "Post",
            "actions": ["read"],
            "readFields": ["title"],
        },
        {"role": "editor", "model": "Post", "actions": ["read", "insert"]},
        {"role": "editor", "model": "Comment", "actions": ["read"], "ownerOnly": True},
    ],
}
_CTX_VIEWER = {"userId": "u1", "roles": ["viewer"]}
_CTX_EDITOR = {"userId": "u2", "roles": ["editor"]}


def _register_test_schemas():
    """conftest 模块隔离约定：模块级只定义不执行，由隔离夹具重放"""
    from py_store import store

    store.register(
        {
            "name": "Post",
            "collection": "posts",
            "timestamps": False,
            "fields": {"title": "string", "secret": "string"},
            "relations": {},
        }
    )


def test_set_rbac_and_deny_insert():
    from py_store import store

    store.set_rbac(_POLICY)
    assert store.rbac_enabled() is True
    perm.set_context(_CTX_VIEWER)
    try:
        with pytest.raises(store.PermissionError) as ei:
            asyncio.run(store.insert("Post", {"title": "x"}))
        assert ei.value.status == 403
        assert "RBAC" in str(ei.value), f"错误消息应携带 RBAC 标识: {ei.value}"
    finally:
        perm.set_context(None)
        store.set_rbac(None)


def test_granted_read_and_field_intersect():
    from py_store import store

    store.set_rbac(_POLICY)
    try:
        assert store.rbac_can("Post", "read", _CTX_VIEWER) is True
        assert store.rbac_can("Post", "insert", _CTX_VIEWER) is False
        # viewer readFields=[title] → 与静态可读集（title+secret）取交 = {title}
        assert sorted(store.rbac_readable_fields("Post", _CTX_VIEWER)) == ["title"]
        assert store.rbac_can("Post", "insert", _CTX_EDITOR) is True
    finally:
        store.set_rbac(None)


def test_clear_policy_restores():
    from py_store import store

    store.set_rbac(_POLICY)
    assert store.rbac_can("Post", "insert", _CTX_VIEWER) is False
    store.set_rbac(None)
    assert store.rbac_enabled() is False
    assert store.rbac_can("Post", "insert", _CTX_VIEWER) is True


def test_row_condition_helper():
    from py_store import store

    store.set_rbac(
        {
            "mode": "overlay",
            "roles": {"reader": {}},
            "grants": [
                {"role": "reader", "model": "Post", "actions": ["read"], "ownerOnly": True}
            ],
        }
    )
    try:
        cond = store.rbac_row_condition("Post", "read", {"userId": "u1", "roles": ["reader"]})
        assert cond == {"createdBy": "u1"}, cond
        # overlay 下无匹配角色的角色 → RBAC 不介入 → 无行级条件
        assert store.rbac_row_condition("Post", "read", _CTX_VIEWER) is None
    finally:
        store.set_rbac(None)
