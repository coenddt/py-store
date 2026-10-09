"""双端作用域对拍（A6 子集）—— 输出一行规范化 JSON：视图覆盖 × 输入的逐字段快照。

与 ``nodejs-store/scripts/parity-scope.js`` 输出**逐字节**比对（两侧共用同一份
``FIXTURE_JSON``，由 ``tests/test_parity_scope.py`` / ``tests/parity-scope.test.js``
断言文本一致）。覆盖：``plan_query`` 命令 JSON、``profile()``、``can_read``、``rbac_can``、
非法视图覆盖的拒绝文案（原文，禁改文案对齐）。

对拍失真防线（fail-loud，禁静默）：
  - 数字：两侧只允许整数（Python ``1.0`` 与 JS ``1`` 的表示差异不是语义差异）；
  - 键序：两侧统一按键排序 + 紧凑分隔符；
  - 脚本自举 ``src`` 到 sys.path，仓内任意 cwd 直接可跑（同 parity_deny.py）。

环境：开发期须 ``LOCAL_CORE=1``（默认 site-packages 绑定为旧版、无 ``with_policy``）；
缺失时输出 ``ERR:`` 前缀（显式失守，不静默降级）。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from py_store import permission, schema, store
from py_store.scope import with_scope

FIXTURE_JSON = """
{
  "schemas": [
    {
      "name": "ScopePost",
      "collection": "scope_posts",
      "timestamps": false,
      "fields": { "title": { "type": "string" }, "status": { "type": "string" } },
      "relations": {}
    },
    {
      "name": "ScopeAudit",
      "collection": "scope_audits",
      "timestamps": false,
      "read": ["viewer"],
      "fields": { "title": { "type": "string" } },
      "relations": {}
    }
  ],
  "views": [
    { "name": "standard", "overrides": {} },
    { "name": "text2query", "overrides": { "profile": "text2query" } },
    {
      "name": "rbacGranted",
      "overrides": {
        "rbac": {
          "mode": "enforce",
          "roles": { "viewer": {} },
          "grants": [{ "role": "viewer", "model": "ScopePost", "actions": ["read"] }]
        }
      }
    },
    {
      "name": "rbacDenied",
      "overrides": { "rbac": { "mode": "enforce", "roles": { "viewer": {} }, "grants": [] } }
    },
    {
      "name": "locked",
      "overrides": {
        "requireContext": true,
        "roleRules": { "unconfigured": "closed" },
        "metaPolicy": { "closed": true, "roles": ["admin"] }
      }
    }
  ],
  "badViews": [
    { "name": "unknownKey", "overrides": { "noSuchKey": 1 } },
    { "name": "badProfile", "overrides": { "profile": "noSuchProfile" } },
    { "name": "unknownRoleRule", "overrides": { "roleRules": { "noSuchRule": ["x"] } } },
    { "name": "unknownMetaPolicy", "overrides": { "metaPolicy": { "noSuchKey": true } } },
    { "name": "notObject", "overrides": "not-an-object" }
  ],
  "contexts": [
    { "name": "viewer", "value": { "userId": "u1", "roles": ["viewer"] } },
    { "name": "other", "value": { "userId": "u2", "roles": ["other"] } },
    { "name": "none", "value": null }
  ],
  "queries": [
    {
      "name": "condition",
      "gql": "ScopePost($condition:@c){ _id title }",
      "params": { "c": { "status": "open" } }
    },
    {
      "name": "sortLimit",
      "gql": "ScopePost($sort:@s, $limit:@l){ _id title }",
      "params": { "s": { "title": 1 }, "l": 2 }
    },
    { "name": "auditAll", "gql": "ScopeAudit{ _id title }", "params": {} }
  ],
  "reads": [
    { "model": "ScopePost", "ctx": "viewer" },
    { "model": "ScopeAudit", "ctx": "viewer" },
    { "model": "ScopeAudit", "ctx": "other" },
    { "model": "ScopeAudit", "ctx": "none" }
  ],
  "rbac": [
    { "model": "ScopePost", "action": "read", "ctx": "viewer" },
    { "model": "ScopeAudit", "action": "read", "ctx": "viewer" },
    { "model": "ScopePost", "action": "read", "ctx": "none" }
  ]
}
"""


def _canon(v):
    """规范化 JSON：键排序 + 紧凑分隔符（与 node 侧 canonical 同序同形）"""
    return json.dumps(v, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _outcome(fn):
    """成功 → ``{'ok': True, 'value': ...}``；异常 → ``{'ok': False, 'error': 原文}``"""
    try:
        return {"ok": True, "value": fn()}
    except Exception as e:  # 对拍用途：文案原样输出（禁改文案对齐）
        return {"ok": False, "error": str(e)}


def _ctx(fx, name):
    for c in fx["contexts"]:
        if c["name"] == name:
            return c["value"]
    raise KeyError(f"fixture 未声明 ctx: {name}")


def _snapshot(fx):
    """当前 core（作用域内 = 视图；作用域外 = base）上的观测快照"""
    row = {"profile": schema.get_profile()}
    for r in fx["reads"]:
        row[f"read:{r['model']}:{r['ctx']}"] = _outcome(
            lambda r=r: permission.can_read_schema(r["model"], _ctx(fx, r["ctx"]))
        )
    for r in fx["rbac"]:
        row[f"rbac:{r['model']}:{r['action']}:{r['ctx']}"] = _outcome(
            lambda r=r: store.rbac_can(r["model"], r["action"], _ctx(fx, r["ctx"]))
        )
    for q in fx["queries"]:
        for c in fx["contexts"]:
            row[f"plan:{q['name']}:{c['name']}"] = _outcome(
                lambda q=q, c=c: _canon(
                    schema.get_core().plan_query(q["gql"], q["params"], c["value"], None)
                )
            )
    return row


def _derive(view_def):
    """派生视图覆盖；成功返回占位串（不应发生，出现即测试判失败）"""
    schema.core.with_policy(view_def["overrides"])
    return "DERIVED-OK"


def main():
    if not hasattr(schema.core, "with_policy"):
        sys.stdout.write("ERR: 当前 core 绑定无 with_policy（开发期须 LOCAL_CORE=1）\n")
        return
    fx = json.loads(FIXTURE_JSON)
    for defn in fx["schemas"]:
        schema.register(defn)

    out = {"base": _snapshot(fx)}
    out["views"] = []
    for v in fx["views"]:
        view = schema.core.with_policy(v["overrides"])
        with with_scope(view):
            out["views"].append({"view": v["name"], "snapshot": _snapshot(fx)})
    out["badViews"] = [
        {"view": b["name"], **_outcome(lambda b=b: _derive(b))} for b in fx["badViews"]
    ]
    out["baseAfter"] = _snapshot(fx)

    sys.stdout.write(_canon(out) + "\n")


if __name__ == "__main__":
    main()
