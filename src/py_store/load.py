"""目录语义装载（宿主薄 IO 层）—— 共享分层与多落点择优 06

职责切分（判据唯一在 core）：
  - IO 在本模块：读 store.config.json、递归 walk 定义目录、读 JSON 文件；
  - 纯判决在 core：``schema.get_core().plan_load``（db 归属 / 路径→落点 / 主从 / 查重）。
本模块**不含**任何落点/主从判据（禁双端漂移；总纲 §5 + 06 §4.1）。

对标 nodejs-store/src/load.js（双端同名同义实现）。
"""

import json
import os

from . import schema

__all__ = ['collect_files', 'load_defs', 'plan_load', 'read_config']


def read_config(path_or_obj, base_dir=None):
    """读 store.config.json（路径字符串 或 已解析对象）；返回 ``(config, base_dir)``"""
    if isinstance(path_or_obj, dict):
        return path_or_obj, base_dir or os.getcwd()
    if not isinstance(path_or_obj, str) or not path_or_obj:
        raise TypeError('ERR:LOAD config 须为 store.config.json 路径或对象')
    abs_path = os.path.abspath(path_or_obj)
    try:
        with open(abs_path, 'r', encoding='utf-8') as fp:
            parsed = json.load(fp)
    except Exception as e:
        raise RuntimeError(f'ERR:LOAD 读取 store.config.json 失败: {abs_path}: {e}') from e
    return parsed, base_dir or os.path.dirname(abs_path)


def collect_files(roots):
    """递归收集定义文件（IO）：跳过 ``_`` 前缀文件/目录，仅 ``.json``。

    返回 ``[{'rel', 'defn'}]``，``rel`` = 相对该定义根的路径（POSIX 分隔符）。
    """
    out: list[dict] = []
    for root in roots or []:
        abs_root = os.path.abspath(root)
        if not os.path.exists(abs_root):
            raise RuntimeError(f'ERR:LOAD 定义根不存在: {root}')
        _walk(abs_root, abs_root, out)
    return out


def _walk(dir_path, root, out):
    entries = sorted(os.listdir(dir_path))  # 目录项名排序（确定性）
    for name in entries:
        if name.startswith('_'):
            continue  # IO 层约定：忽略 `_` 前缀
        full = os.path.join(dir_path, name)
        if os.path.isdir(full):
            _walk(full, root, out)
        elif os.path.isfile(full) and name.lower().endswith('.json'):
            rel = os.path.relpath(full, root).replace(os.sep, '/')
            try:
                with open(full, 'r', encoding='utf-8') as fp:
                    defn = json.load(fp)
            except Exception as e:
                raise RuntimeError(f'ERR:LOAD 解析定义失败: {rel}: {e}') from e
            out.append({'rel': rel, 'defn': defn})


def plan_load(config, files):
    """纯规划（转调 core；判决唯一在 core）"""
    return schema.get_core().plan_load(config, files)


def load_defs(config=None, ctx=None, base_dir=None):
    """运行期装载入口：读配置 → 收集定义 → core 纯规划 → 带定位批量注册。

    返回装载项（主在前、其后从），每项为
    ``{'defn': dict, 'location': {'source', 'database', 'schema'}}``。
    """
    if not config:
        raise RuntimeError('ERR:LOAD load_defs 缺 config（store.config.json 路径或对象）')
    cfg, base = read_config(config, base_dir)
    roots = [os.path.abspath(os.path.join(base, r)) for r in (cfg.get('defs') or [])]
    files = collect_files(roots)
    items = plan_load(cfg, files)
    schema.register_batch(items, ctx)
    return items
