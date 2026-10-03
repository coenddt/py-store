"""A6 双端拒绝文案对拍：py 与 node 脚本输出须逐字节一致。

相邻 ``nodejs-store`` 仓缺失（或本机无 node）时显式 skip（不得静默通过）。
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE_SCRIPT = ROOT.parent / "nodejs-store" / "scripts" / "parity-deny.js"
PY_SCRIPT = ROOT / "scripts" / "parity_deny.py"


def test_deny_message_parity_with_node():
    if not NODE_SCRIPT.exists():
        pytest.skip("相邻 nodejs-store 仓缺失，跳过对拍")
    if shutil.which("node") is None:
        pytest.skip("本机无 node，跳过对拍")
    py_out = subprocess.run(
        [sys.executable, str(PY_SCRIPT)], capture_output=True, text=True, check=True
    ).stdout.strip()
    node_out = subprocess.run(
        ["node", str(NODE_SCRIPT)], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert py_out == node_out, f"py={py_out!r} node={node_out!r}"
    assert py_out != "OK"
