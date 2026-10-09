"""A6 双端作用域对拍（R2 03 §5 步骤 6）—— py 侧驱动测试。

  1. 两侧脚本内嵌的 ``FIXTURE_JSON`` 文本**逐字一致**（禁各写一份用例定义）；
  2. ``python scripts/parity_scope.py`` 输出单行合法 JSON（py 自身合法，不依赖 node 仓），
     且快照有判别力（档位 / rbac 判决 / 非法覆盖拒绝文案均随视图变化——防「空对拍」假绿）；
  3. py 与 node 两侧输出**逐字节相等**（node 仓缺失 / 本机无 node → 显式 skip，禁静默通过）。

注意：子进程按宿主环境加载 rust core（开发期需 ``LOCAL_CORE=1``；默认 site-packages 绑定为
旧版、无 ``with_policy``，脚本会以 ``ERR:`` 前缀显式失守——不静默降级）。
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PY_SCRIPT = ROOT / "scripts" / "parity_scope.py"
NODE_SCRIPT = ROOT.parent / "nodejs-store" / "scripts" / "parity-scope.js"


def _run_py():
    return subprocess.run(
        [sys.executable, str(PY_SCRIPT)], capture_output=True, text=True, encoding="utf-8", check=True
    ).stdout


def _run_node():
    return subprocess.run(
        ["node", str(NODE_SCRIPT)], capture_output=True, text=True, encoding="utf-8", check=True
    ).stdout


def _fixture_text(path, pattern):
    m = re.search(pattern, path.read_text(encoding="utf-8"))
    assert m, f"{path.name} 的 FIXTURE_JSON 提取失败"
    return m.group(1)


def _snapshot(parsed, view_name):
    for v in parsed["views"]:
        if v["view"] == view_name:
            return v["snapshot"]
    raise AssertionError(f"fixture 视图缺失: {view_name}")


def test_fixture_identical():
    if not NODE_SCRIPT.exists():
        pytest.skip("相邻 nodejs-store 仓缺失，跳过 fixture 一致性断言")
    py_fx = _fixture_text(PY_SCRIPT, r'FIXTURE_JSON = """([\s\S]*?)"""')
    node_fx = _fixture_text(NODE_SCRIPT, r"const FIXTURE_JSON = `([\s\S]*?)`;")
    assert py_fx == node_fx, "两侧 fixture 文本不一致（禁各写一份用例定义）"


def test_py_output_valid_and_discriminating():
    out = _run_py()
    assert not out.startswith("ERR:"), f"py 侧不得以错误输出结束（开发期须 LOCAL_CORE=1）: {out}"
    lines = out.strip().split("\n")
    assert len(lines) == 1, "py 侧必须输出单行 JSON"
    parsed = json.loads(lines[0])

    # 视图集合与 fixture 对齐
    assert [v["view"] for v in parsed["views"]] == [
        "standard", "text2query", "rbacGranted", "rbacDenied", "locked",
    ]

    # 判别力①：档位随视图变化
    assert _snapshot(parsed, "standard")["profile"] == "standard"
    assert _snapshot(parsed, "text2query")["profile"] == "text2query"

    # 判别力②：rbac 判决随视图变化（同一 ctx）
    assert _snapshot(parsed, "rbacGranted")["rbac:ScopePost:read:viewer"] == {"ok": True, "value": True}
    assert _snapshot(parsed, "rbacDenied")["rbac:ScopePost:read:viewer"] == {"ok": True, "value": False}

    # 判别力③：三开关视图（requireContext + unconfigured closed）确实拦截
    locked = _snapshot(parsed, "locked")
    assert locked["plan:condition:viewer"]["ok"] is False, "closed 策略下无授权模型须被拦"
    assert locked["plan:condition:viewer"]["error"].startswith("ERR_"), locked["plan:condition:viewer"]

    # 判别力④：plan 为规范化 JSON 文本（非空命令）
    plan = json.loads(_snapshot(parsed, "standard")["plan:condition:viewer"]["value"])
    assert plan["mode"] in ("find", "aggregate") and plan["commands"], plan

    # 判别力⑤：非法覆盖逐条被拒，文案原文非空
    assert [b["view"] for b in parsed["badViews"]] == [
        "unknownKey", "badProfile", "unknownRoleRule", "unknownMetaPolicy", "notObject",
    ]
    for b in parsed["badViews"]:
        assert b["ok"] is False, f"{b['view']} 应被拒（禁静默派生）"
        assert b["error"], f"{b['view']} 拒绝文案不得为空"

    # 域外零变更：作用域前后 base 快照逐字段一致
    assert parsed["base"] == parsed["baseAfter"], "各视图退出后 base 须零变更"


def test_parity_with_node():
    if not NODE_SCRIPT.exists():
        pytest.skip("相邻 nodejs-store 仓缺失，跳过对拍")
    if shutil.which("node") is None:
        pytest.skip("本机无 node，跳过对拍")
    py_out = _run_py().strip()
    node_out = _run_node().strip()
    assert not py_out.startswith("ERR:"), f"py 侧不得以错误输出对拍: {py_out}"
    assert not node_out.startswith("ERR:"), f"node 侧不得以错误输出对拍: {node_out}"
    assert py_out == node_out, (
        f"双端快照不一致（py {len(py_out)} 字节 / node {len(node_out)} 字节）"
    )
