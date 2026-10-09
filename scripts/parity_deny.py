"""双端拒绝文案对拍（A6）—— 输出一行：坏 defn 的拒绝文案（异常消息原样）。

与 ``nodejs-store/scripts/parity-deny.js`` 输出逐字节比对；依据 core 原文，禁改文案对齐。
脚本自举 ``src`` 到 sys.path，保证在仓库内任意 cwd 直接可跑。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from py_store import workflow

BAD_DEFN = {
    "name": "parityBad",
    "steps": [{"op": "query", "as": "x", "gql": "NoSuchModel($condition:@c){ _id }", "params": {"c": {}}}],
}

try:
    workflow.register(BAD_DEFN)
    sys.stdout.write("OK\n")          # 不应发生；出现即测试判失败
except Exception as e:
    sys.stdout.write(str(e) + "\n")
