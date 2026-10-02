"""场景 harness 显式加载器 —— 规避 sys.modules 同名模块冲突

``example/<场景>/impl/`` 下的 harness 均以 ``import harness`` 同名加载（内部又
``import checks`` 同名依赖）。两个场景测试文件同进程批跑时，先收集的模块占住
``sys.modules['harness']``，后收集的 import **静默命中缓存**——其场景测试实际
执行的是先收集场景的 harness 与 cases（场景对拍形同虚设），且两套同名 checks/
fns/probes 互相覆盖，批跑时序不稳（曾以 seed 撞共享库残留数据的 DuplicateKeyError
呈现）。

本加载器以**场景唯一模块名**显式加载 harness 与其依赖（checks / fns / probes）：
依赖在 harness 执行期间以标准名进驻 sys.modules（harness.py 的 ``import checks``
按名命中），执行完即卸下标准名——两场景互不覆盖，收集顺序无关。
"""

import importlib.util
import sys
from pathlib import Path

_DEPS = ('checks', 'fns', 'probes')


def load_scenario_harness(unique_name: str, impl_dir: Path):
    """以 ``unique_name`` 显式加载 ``<impl_dir>/harness.py``，返回 harness 模块

    依赖（``checks`` / ``fns`` / ``probes``，存在才加载）同样以唯一名执行，
    并在 harness 执行期间临时占用标准名供其 ``import checks`` 命中；harness
    加载完成后即卸下标准名（harness 的 globals 已持有引用，不受影响）。
    """
    # py-store/src（harness.py 内部虽自行 insert，但以测试侧为准先行注入）
    src = impl_dir.parent.parent.parent / 'src'
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))

    loaded = {}
    try:
        for dep in _DEPS:
            dep_path = impl_dir / f'{dep}.py'
            if not dep_path.exists():
                continue
            spec = importlib.util.spec_from_file_location(f'{unique_name}_{dep}', dep_path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            loaded[dep] = mod
        for dep, mod in loaded.items():
            sys.modules[dep] = mod  # harness.py 的 `import checks` 按标准名命中

        spec = importlib.util.spec_from_file_location(unique_name, impl_dir / 'harness.py')
        harness = importlib.util.module_from_spec(spec)
        sys.modules[unique_name] = harness
        spec.loader.exec_module(harness)
    finally:
        for dep in loaded:
            sys.modules.pop(dep, None)  # 标准名用完即卸，防跨场景覆盖
    return harness
