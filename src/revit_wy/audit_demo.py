"""内置仿真模型事实，用于无 Revit 时演示体检报告（revit-wy audit model --demo）。

刻意造一个"典型的不干净模型"：有副本视图、空视图、三分之一构件缺标记。
这样团队第一次打开报告就能看到"它到底会挑出什么问题"，
而不是面对一个全绿的、看不出价值的空报告。
"""
from __future__ import annotations

from typing import Any, Dict


def demo_facts() -> Dict[str, Any]:
    views = [
        {"id": 101, "name": "3F-建-平面", "view_type": "FloorPlan", "is_template": False,
         "is_placed": True, "is_empty": False, "element_count": 40, "has_annotations": True},
        {"id": 102, "name": "副本 1", "view_type": "FloorPlan", "is_template": False,
         "is_placed": False, "is_empty": False, "element_count": 12, "has_annotations": True},
        {"id": 103, "name": "平面图 2", "view_type": "FloorPlan", "is_template": False,
         "is_placed": True, "is_empty": True, "element_count": 0, "has_annotations": False},
        {"id": 104, "name": "仅标注视图", "view_type": "FloorPlan", "is_template": False,
         "is_placed": True, "is_empty": False, "element_count": 2, "has_annotations": True},
        {"id": 105, "name": "标高 1", "view_type": "ThreeD", "is_template": False,
         "is_placed": False, "is_empty": False, "element_count": 33, "has_annotations": False},
    ]
    elements, eid = [], 1000
    for kind, cat, fam, tn, n in [("walls", "墙", "基本墙", "常规 - 200mm", 120),
                                  ("columns", "结构柱", "混凝土矩形柱", "400x400", 40),
                                  ("beams", "结构框架", "混凝土矩形梁", "300x600", 60),
                                  ("doors", "门", "单扇-与墙齐", "900x2100", 30)]:
        for i in range(n):
            eid += 1
            elements.append({
                "id": eid, "kind": kind, "category": cat, "family": fam,
                "type_name": tn,
                "has_mark": (i % 3 != 0),        # 约三分之一缺标记
                "has_comments": (i % 5 != 0),    # 约五分之一缺注释
            })
    types = [
        {"id": 2001, "category": "墙", "family": "基本墙", "name": "常规 - 200mm", "instance_count": 120},
        {"id": 2002, "category": "墙", "family": "基本墙", "name": "常规 (200)", "instance_count": 0},
        {"id": 2003, "category": "结构柱", "family": "混凝土矩形柱", "name": "400x400", "instance_count": 40},
        {"id": 2004, "category": "门", "family": "单扇-与墙齐", "name": "900x2100", "instance_count": 30},
    ]
    return {
        "schema": "1.0", "revit_version": "2022（仿真）",
        "document": "示例项目.rvt（仿真数据）",
        "document_path": "", "is_workshared": False,
        "element_count": len(elements), "views": views, "elements": elements,
        "types": types,
        "warnings": ["这是内置仿真数据，用于演示报告结构；真实结果请在 Revit 里跑 audit model"],
    }
