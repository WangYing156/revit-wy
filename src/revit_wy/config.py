"""配置加载：YAML 读取、深合并、路径解析。"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Dict

import yaml

PKG_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = PKG_ROOT / "config"

# 用户级配置目录，允许覆盖内置默认值
USER_CONFIG_DIR = Path(
    os.environ.get("REVIT_WY_HOME", Path.home() / ".revit-wy")
)


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并，override 优先；list 整体替换（规则集不希望被逐项拼接）。"""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_yaml(path: str | Path) -> Dict[str, Any]:
    p = Path(path)
    if not p.is_absolute():
        cand = [Path.cwd() / p, CONFIG_DIR / p, PKG_ROOT / p]
        for c in cand:
            if c.exists():
                p = c
                break
    if not p.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"配置文件顶层必须是映射: {p}")
    return data


def load_mapping(profile: str = "arch", custom: str | Path | None = None) -> Dict[str, Any]:
    """加载图层映射规则集。

    若同目录存在 <profile>_confirmed.yaml（数模对照确认后固化），会叠加到内置规则之上，
    这样"确认一次"就对后续所有图纸持久生效，而不只依赖别名库。
    """
    base = load_yaml(CONFIG_DIR / "layer_mapping" / f"{profile}.yaml")
    confirmed = CONFIG_DIR / "layer_mapping" / f"{profile}_confirmed.yaml"
    if confirmed.exists():
        base = deep_merge(base, load_yaml(confirmed))
    if custom:
        base = deep_merge(base, load_yaml(custom))
    return base


def load_rules(custom: str | Path | None = None) -> Dict[str, Any]:
    """加载 BIM 建模规则（内置默认 + 用户覆盖）。"""
    base = load_yaml(CONFIG_DIR / "bim_rules" / "default.yaml")
    user_default = USER_CONFIG_DIR / "rules" / "default.yaml"
    if user_default.exists():
        base = deep_merge(base, load_yaml(user_default))
    if custom:
        base = deep_merge(base, load_yaml(custom))
    return base


def save_yaml(data: Dict[str, Any], path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, width=120)
    return p
