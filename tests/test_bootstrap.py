"""bootstrap.create_app 装配门禁 —— D8 低覆盖补测（bootstrap.py 0% → 全路径）

对标 design：框架统一入口「注册（或按 config 目录装载）→ init → 绑 fns →
校验覆盖 → 反馈落库接线」。用例：

  1. 缺 datasource → ``ValueError``（ERR_BOOTSTRAP 前缀，禁静默）；
  2. schemas + fns 正常装配（memory 驱动）→ 返回 ``{'store'}``，fns 绑定生效；
  3. computes 声明 fnRef 但未注入实现 → ``assert_fns_covered`` 抛 ``ERR_FN_MISSING``
     （A3：缺实现即抛，进程不启动）；
  4. feedback=True 分支：``set_feedback_meta`` / ``enable_feedback_table`` 被调用
     （monkeypatch 记录调用——enable 为进程级接线，测试不真接线防污染其他用例）；
  5. config 目录装载分支：tmp_path 构造 ``store.config.json`` + ``defs/``，
     走 ``load_defs`` → ``register_batch`` → ``init``（core 纯规划判决）。

运行：$env:LOCAL_CORE='1'; $env:PYTHONPATH='src'; python -m pytest tests/test_bootstrap.py -q
"""

import asyncio
import json

import pytest

from py_store import schema as _sc
from py_store.bootstrap import create_app
from py_store.memory import connect


def _register_test_schemas():
    """conftest 模块夹具重放（清场后恢复本模块 schema；BsApp* 前缀隔离命名空间）"""
    _sc.register(
        {
            "name": "BsAppItem",
            "collection": "bs_app_items",
            "idPrefix": "bsi_",
            "timestamps": False,
            "fields": {"title": {"type": "string"}},
            "relations": {},
            "computes": {},
        }
    )


def _defn_with_fn():
    """带回调计算列的定义（fnRef = BsApp.shout，实现由 fns 注入）"""
    return {
        "name": "BsAppOrder",
        "collection": "bs_app_orders",
        "idPrefix": "bso_",
        "timestamps": False,
        "fields": {"a": {"type": "int"}, "b": {"type": "int"}},
        "relations": {},
        "computes": {"shout": {"type": "string", "fn": True, "fnRef": "BsApp.shout"}},
    }


def _run(coro):
    return asyncio.run(coro)


# ── 1. 缺 datasource ──────────────────────────────────────────


def test_missing_datasource_raises():
    with pytest.raises(ValueError, match="ERR_BOOTSTRAP"):
        _run(create_app(datasource=None))


# ── 2. 正常装配（schemas + fns） ─────────────────────────────


def test_ok_assembles_store_and_binds_fns():
    calls = []

    async def impl(item, ctx=None):
        calls.append(item)
        return (item or {}).get("a", 0) + (item or {}).get("b", 0)

    out = _run(
        create_app(
            datasource=connect({"database": "bs_db"}),
            schemas=[_defn_with_fn()],
            fns={"BsApp.shout": impl},
            feedback=False,
        )
    )
    assert "store" in out and out["store"] is not None
    assert calls == []  # 仅绑定，未执行


# ── 3. 缺 fns 实现（A3） ──────────────────────────────────────


def test_missing_fn_impl_raises():
    # 独立 fnRef：set_fn 为进程级注册（用例 2 已绑 BsApp.shout），不得复用同名 ref
    defn = {
        "name": "BsAppMute",
        "collection": "bs_app_mutes",
        "idPrefix": "bsm_",
        "timestamps": False,
        "fields": {"a": {"type": "int"}},
        "relations": {},
        "computes": {"mute": {"type": "string", "fn": True, "fnRef": "BsApp.mute"}},
    }
    with pytest.raises(RuntimeError, match="ERR_FN_MISSING"):
        _run(
            create_app(
                datasource=connect({"database": "bs_db"}),
                schemas=[defn],
                fns=None,
                feedback=False,
            )
        )


# ── 4. feedback=True 接线分支 ─────────────────────────────────


def test_feedback_branch_wires_meta_and_table(monkeypatch):
    from py_store import feedback as _feedback_mod

    calls = []
    monkeypatch.setattr(
        _feedback_mod, "set_meta", lambda meta: calls.append(("set_meta", meta))
    )
    monkeypatch.setattr(
        _feedback_mod,
        "enable_feedback_table",
        lambda store: calls.append(("enable",)) or None,
    )
    out = _run(
        create_app(
            datasource=connect({"database": "bs_db"}),
            schemas=[
                {
                    "name": "BsAppPlain",
                    "collection": "bs_app_plain",
                    "idPrefix": "bsp_",
                    "timestamps": False,
                    "fields": {"t": {"type": "string"}},
                    "relations": {},
                    "computes": {},
                }
            ],
            tenant="t1",
            env="dev",
            feedback=True,
        )
    )
    assert "store" in out
    assert ("set_meta", {"tenant": "t1", "env": "dev"}) in calls, calls
    assert ("enable",) in calls, calls


def test_feedback_false_skips_wiring(monkeypatch):
    from py_store import feedback as _feedback_mod

    calls = []
    monkeypatch.setattr(
        _feedback_mod,
        "enable_feedback_table",
        lambda store: calls.append(("enable",)) or None,
    )
    _run(
        create_app(
            datasource=connect({"database": "bs_db"}),
            schemas=[],  # 空 schemas：仍覆盖「逐条 register」分支（零次循环）
            feedback=False,
        )
    )
    assert calls == []  # feedback=False 不接线


# ── 5. config 目录装载分支 ────────────────────────────────────


def test_config_dir_load_branch(tmp_path):
    # 落点由目录语义承载（06 §5.2）：<defs-root>/<db>/ 首段 = database，defn 零落点
    defn = {
        "name": "BsAppCfg",
        "collection": "bs_app_cfg",
        "idPrefix": "bsc_",
        "timestamps": False,
        "fields": {"t": {"type": "string"}},
        "relations": {},
        "computes": {},
    }
    defs_dir = tmp_path / "defs" / "bs_db"
    defs_dir.mkdir(parents=True)
    (defs_dir / "bs_app_cfg.json").write_text(
        json.dumps(defn, ensure_ascii=False), encoding="utf-8"
    )
    config = tmp_path / "store.config.json"
    config.write_text(
        json.dumps(
            {
                "sources": {"default": {"kind": "mongodb", "databases": ["bs_db"]}},
                "defs": ["defs"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    out = _run(
        create_app(
            datasource=connect({"database": "bs_db"}),
            config=str(config),
            feedback=False,
        )
    )
    assert "store" in out
    assert _sc.get("BsAppCfg") is not None  # 装载即注册
