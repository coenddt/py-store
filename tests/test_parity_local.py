"""本地磁盘数据源 双端对拍（A6）—— py 侧驱动测试。

  1. 两侧脚本内嵌的 FIXTURE_JSON 文本**逐字一致**（禁各写一份场景定义）；
  2. `python scripts/parity_local.py` 输出单行合法 JSON、无 `ERR:`（py 自身合法，不依赖 node 仓）；
  3. py 与 node 两侧输出**逐字节相等**（node 仓缺失 / 本机无 node → 显式 skip，禁静默通过）。

对拍失败时断言 message 打出双方原文（对齐 test_parity_deny.py 范式，便于逐场景定位）。
注意：子进程按宿主环境加载 rust core（开发期需 LOCAL_CORE=1；发版后走正式依赖）。
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PY_SCRIPT = ROOT / "scripts" / "parity_local.py"
NODE_SCRIPT = ROOT.parent / "nodejs-store" / "scripts" / "parity-local.js"


def test_fixture_identical():
    if not NODE_SCRIPT.exists():
        pytest.skip("相邻 nodejs-store 仓缺失，跳过 fixture 一致性断言")
    py_text = PY_SCRIPT.read_text(encoding="utf-8")
    node_text = NODE_SCRIPT.read_text(encoding="utf-8")
    py_m = re.search(r'FIXTURE_JSON = """([\s\S]*?)"""', py_text)
    node_m = re.search(r"const FIXTURE_JSON = `([\s\S]*?)`;", node_text)
    assert py_m, "py 侧 FIXTURE_JSON 提取失败"
    assert node_m, "node 侧 FIXTURE_JSON 提取失败"
    assert py_m.group(1) == node_m.group(1), "两侧 fixture 文本不一致（禁各写一份场景定义）"


def test_py_output_valid_standalone():
    out = subprocess.run(
        [sys.executable, str(PY_SCRIPT)], capture_output=True, text=True, check=True
    ).stdout
    assert not out.startswith("ERR:"), f"py 侧不得以错误输出结束: {out}"
    lines = out.strip().split("\n")
    assert len(lines) == 1, "py 侧必须输出单行 JSON"
    parsed = json.loads(lines[0])
    assert isinstance(parsed, list) and parsed, "py 侧输出必须是非空 JSON 数组"


def test_parity_with_node():
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
    assert not py_out.startswith("ERR:"), f"py 侧不得以错误输出对拍: {py_out}"
    assert not node_out.startswith("ERR:"), f"node 侧不得以错误输出对拍: {node_out}"
    assert py_out == node_out, f"\npy  ={py_out}\nnode={node_out}"
