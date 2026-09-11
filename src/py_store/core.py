"""
rust-store 原生绑定加载器（唯一原生模块入口）

mongo-store 采用「Rust 单核心 + 双绑定」架构：schema/GQL/权限/计算列/命令规划
全部在 Rust core 实现，本目录 src/py_store/*.py 只是薄 Host 适配层
（驱动 IO + 回调 + 占位符）。

生产环境**只**从 pip 依赖 `rust-store-py` 加载原生扩展。
开发期若需从相邻 rust-store 仓库的调试产物加载，必须显式设置：
  LOCAL_CORE=1 且 NODE_ENV != 'production'
"""

import importlib
import os
import pathlib
import sys

# 相邻 rust-store 仓库的 cargo/maturin 产物目录（含 rust_store_py.pyd）
_DEV_DIST = (
    pathlib.Path(__file__).resolve().parent.parent.parent.parent
    / 'rust-store'
    / 'core-py'
    / 'dist'
)


def _dev_fallback():
    """开发期兜底：仅 LOCAL_CORE=1 且非 production 时启用，避免生产环境从相邻目录加载。"""
    if os.environ.get('LOCAL_CORE') != '1' or os.environ.get('NODE_ENV') == 'production':
        return None
    if str(_DEV_DIST) not in sys.path:
        sys.path.insert(0, str(_DEV_DIST))
    try:
        return importlib.import_module('rust_store_py')
    except Exception as e:  # noqa: BLE001
        return {'__error': f'{_DEV_DIST}: {e}'}


def _load():
    try:
        return importlib.import_module('rust_store_py')
    except ImportError as e:
        fallback = _dev_fallback()
        if fallback is not None and not isinstance(fallback, dict):
            return fallback

        hints = [f'rust_store_py: {e}']
        if isinstance(fallback, dict):
            hints.append(fallback['__error'])
        raise ImportError(
            '无法加载 rust-store 原生核心（rust-store-py 绑定产物）。\n'
            '请安装 pip 依赖 rust-store-py；开发期如需从相邻 rust-store 仓库加载，\n'
            "请设置 LOCAL_CORE=1（且 NODE_ENV != 'production'）并在 rust-store 仓库构建：\n"
            '  python -m maturin develop --manifest-path rust-store/core-py/Cargo.toml\n'
            + '\n'.join(hints)
        )


native = _load()

# Rust core 注册表（全项目共享单例；对齐 nodejs-store/src/schema.js 的 `new native.Registry()`）
core = native.Registry()
