"""模拟 Revit 插件端：实现 revit-wy/1 协议，用于在没有 Windows/Revit 的机器上
验证 Python 侧到插件的整条链路（协议一致性、参数校验、干跑逻辑）。

它不生成真实几何，只做“协议级仿真”：校验 IR 字段、统计构件、按真实插件的错误码回包。
因此可以放在 CI 里跑，保证 Python 侧改动不会悄悄破坏与 C# 插件的契约。

    python tools/mock_revit.py --port 48880
"""
from __future__ import annotations

import argparse
import json
import socket
import socketserver
import threading
from typing import Any, Dict

PROTOCOL = "revit-wy/1"

REQUIRED_IR_FIELDS = ["schema", "levels", "walls", "columns", "beams",
                      "doors", "windows", "slabs", "grids"]

# 各构件的必填字段（与 addin/RevitWy.Plugin/Model/IrModel.cs 对应）
REQUIRED_ITEM_FIELDS = {
    "levels": ["name", "elevation_mm"],
    "grids": ["name", "start", "end"],
    "walls": ["id", "start", "end", "thickness_mm", "base_level"],
    "columns": ["id", "center", "b_mm", "h_mm"],
    "beams": ["id", "start", "end", "b_mm", "h_mm"],
    "doors": ["id", "center", "width_mm", "height_mm"],
    "windows": ["id", "center", "width_mm", "height_mm"],
    "slabs": ["id", "boundary", "thickness_mm"],
}


class State:
    def __init__(self):
        self.document = "MockProject.rvt"
        self.levels: Dict[str, float] = {}
        self.counts = {"walls": 0, "columns": 0, "beams": 0,
                       "doors": 0, "windows": 0, "slabs": 0, "grids": 0}
        self.last_build: Dict[str, Any] = {}
        self.builds = 0


STATE = State()


def validate_ir(ir: Dict[str, Any]) -> list[str]:
    errs = []
    for f in REQUIRED_IR_FIELDS:
        if f not in ir:
            errs.append(f"IR 缺少字段 {f}")
    for key, fields in REQUIRED_ITEM_FIELDS.items():
        for i, item in enumerate(ir.get(key) or []):
            for f in fields:
                if f not in item:
                    errs.append(f"{key}[{i}] 缺少字段 {f}")
    return errs


def handle_ping(p):
    return {"pong": True, "revit_version": "2022", "revit_build": "mock",
            "product": "Autodesk Revit 2022 (mock)",
            "document": STATE.document, "document_path": "/mock/" + STATE.document,
            "is_family": False, "plugin": "revit-wy 0.1.0 (mock)", "protocol": PROTOCOL}


def handle_status(p):
    return {"document": STATE.document, "document_path": "/mock/" + STATE.document,
            "is_modified": STATE.builds > 0, "revit_version": "2022",
            "units": "feet (internal) / mm (IR)",
            "counts": {"levels": len(STATE.levels), "grids": STATE.counts["grids"],
                       "walls": STATE.counts["walls"], "columns": STATE.counts["columns"],
                       "beams": STATE.counts["beams"], "doors": STATE.counts["doors"],
                       "windows": STATE.counts["windows"], "slabs": STATE.counts["slabs"]},
            "revit_wy_tagged": sum(STATE.counts.values()),
            "levels_detail": [{"name": k, "elevation_mm": v}
                              for k, v in sorted(STATE.levels.items(), key=lambda t: t[1])]}


def handle_prepare_project(p):
    for lv in p.get("levels") or []:
        STATE.levels[lv["name"]] = lv.get("elevation_mm", 0)
    return {"ok": True, "document": STATE.document,
            "warnings": ["mock: 未真实设置单位"] if p.get("units_mm") else []}


def handle_build_ir(p):
    ir = p.get("ir")
    if not ir:
        raise ValueError("缺少 ir 参数")
    errs = validate_ir(ir)
    if errs:
        raise ValueError("IR 校验失败: " + "; ".join(errs[:5]))

    dry = bool(p.get("dry_run"))
    created = {k: 0 for k in STATE.counts}
    created["levels"] = 0
    warnings, errors = [], []

    if not dry:
        for lv in ir.get("levels") or []:
            if lv["name"] not in STATE.levels:
                STATE.levels[lv["name"]] = lv["elevation_mm"]
                created["levels"] += 1

        level_names = set(STATE.levels) | {l["name"] for l in ir.get("levels") or []}
        wall_ids = set()

        for w in ir.get("walls") or []:
            if w.get("base_level") not in level_names:
                errors.append(f"墙 {w['id']} 的标高 {w.get('base_level')} 不存在")
                continue
            if w["thickness_mm"] <= 0:
                errors.append(f"墙 {w['id']} 厚度非法")
                continue
            created["walls"] += 1
            wall_ids.add(w["id"])

        for c in ir.get("columns") or []:
            if min(c["b_mm"], c["h_mm"]) <= 0:
                errors.append(f"柱 {c['id']} 尺寸非法")
                continue
            created["columns"] += 1

        for b in ir.get("beams") or []:
            created["beams"] += 1

        for key, label in (("doors", "doors"), ("windows", "windows")):
            for d in ir.get(key) or []:
                host = d.get("host_wall_id")
                if host and host not in wall_ids:
                    warnings.append(f"{d['id']} 指定的宿主墙不在本次生成范围内，改用就近搜索")
                created[label] += 1

        for s in ir.get("slabs") or []:
            if len(s.get("boundary") or []) < 3:
                errors.append(f"楼板 {s['id']} 轮廓点不足")
                continue
            created["slabs"] += 1

        for g in ir.get("grids") or []:
            created["grids"] += 1

        for k in ("walls", "columns", "beams", "doors", "windows", "slabs", "grids"):
            STATE.counts[k] += created[k]
        STATE.builds += 1

    result = {
        "ok": len(errors) == 0,
        "dry_run": dry,
        "elapsed_s": 0.05,
        "created": created,
        "skipped": {},
        "warnings": warnings,
        "errors": errors,
        "document": STATE.document,
        "mode": "mock",
    }
    STATE.last_build = result
    return result


def handle_select_by_tags(p):
    """模拟按 IR 构件 id 选中。真实插件按 Comments 标记匹配。"""
    ids = p.get("ids") or []
    kinds = p.get("kinds") or []
    if not ids:
        raise ValueError("ids 为空")
    known = {k: v for k, v in STATE.counts.items() if v > 0}
    if not kinds:
        kinds = list(known)
    matched = [i for i in ids if not kinds or True]
    if STATE.counts.get("walls", 0) == 0 and STATE.counts.get("columns", 0) == 0:
        return {"selected": 0, "scanned": 0,
                "message": "当前模拟项目中还没有翻模构件（先调 build_ir）"}
    return {"selected": len(matched), "scanned": sum(STATE.counts.values()) + 20}


def handle_delete_tagged(p):
    n = sum(STATE.counts.values())
    for k in STATE.counts:
        STATE.counts[k] = 0
    return {"deleted": n}


def handle_list_types(p):
    cat = (p.get("category") or "wall").lower()
    if cat == "wall":
        return [{"name": f"常规 - {t}mm", "width_mm": t} for t in (100, 120, 150, 200, 240, 300)]
    if cat == "door":
        return [{"family": "门", "type": "单扇 - 与墙齐", "active": True},
                {"family": "门", "type": "双扇 - 与墙齐", "active": True}]
    if cat == "window":
        return [{"family": "窗", "type": "固定窗", "active": True},
                {"family": "窗", "type": "推拉窗", "active": True}]
    if cat == "column":
        return [{"family": "混凝土矩形柱", "type": "400x400", "active": True},
                {"family": "混凝土矩形柱", "type": "500x500", "active": True}]
    return [{"family": "混凝土矩形梁", "type": "200x500", "active": True},
            {"family": "混凝土矩形梁", "type": "300x600", "active": True}]


def _mock_audit_facts(p):
    """仿真一个"典型的不太干净的模型"，让 audit 规则在无 Revit 时也能测。"""
    views = [
        {"id": 101, "name": "3F-建-平面", "view_type": "FloorPlan", "is_template": False,
         "is_placed": True, "is_empty": False, "element_count": 40, "has_annotations": True},
        {"id": 102, "name": "副本 1", "view_type": "FloorPlan", "is_template": False,
         "is_placed": False, "is_empty": False, "element_count": 12, "has_annotations": True},
        {"id": 103, "name": "平面图 2", "view_type": "FloorPlan", "is_template": False,
         "is_placed": True, "is_empty": True, "element_count": 0, "has_annotations": False},
        {"id": 104, "name": "标高 1", "view_type": "ThreeD", "is_template": False,
         "is_placed": False, "is_empty": False, "element_count": 33, "has_annotations": False},
    ]
    elements, eid = [], 1000
    for kind, cat, fam, tn, n in [("walls", "墙", "基本墙", "常规 - 200mm", 20),
                                  ("columns", "结构柱", "混凝土矩形柱", "400x400", 8),
                                  ("beams", "结构框架", "混凝土矩形梁", "300x600", 6),
                                  ("doors", "门", "单扇-与墙齐", "900x2100", 4)]:
        for i in range(n):
            eid += 1
            elements.append({"id": eid, "kind": kind, "category": cat, "family": fam,
                             "type_name": tn,
                             "has_mark": (i % 3 != 0),          # 三分之一缺标记
                             "has_comments": (i % 5 != 0)})     # 五分之一缺注释
    types = [
        {"id": 2001, "category": "墙", "family": "基本墙", "name": "常规 - 200mm", "instance_count": 20},
        {"id": 2002, "category": "墙", "family": "基本墙", "name": "常规 (200)", "instance_count": 0},
        {"id": 2003, "category": "结构柱", "family": "混凝土矩形柱", "name": "400x400", "instance_count": 8},
    ]
    return {"schema": "1.0", "revit_version": "2022", "document": STATE.document,
            "document_path": "/mock/" + STATE.document, "is_workshared": False,
            "element_count": len(elements), "views": views, "elements": elements,
            "types": types, "warnings": ["mock: 数据为协议级仿真，不代表真实模型"]}


def handle_audit_model(p):
    return _mock_audit_facts(p)


def handle_fix_issues(p):
    ids = p.get("ids") or []
    if not ids:
        raise KeyError("ids 为空：为避免误改，必须显式给出要修正的元素 id")
    if not p.get("apply"):
        return {"ok": True, "dry_run": True, "would_change": len(ids),
                "skipped": [], "sample": [{"id": i, "from": None} for i in ids[:5]],
                "note": "干跑：未修改。确认后传 apply=true"}
    return {"ok": True, "changed": len(ids), "skipped": [], "errors": []}


def handle_export_audit(p):
    import os
    import tempfile
    d = p.get("out_dir") or os.path.join(tempfile.gettempdir(), "revit-wy-audit")
    return {"ok": True, "json": os.path.join(d, "model.json"),
            "csv": os.path.join(d, "model_elements.csv"), "out_dir": d,
            "views": 4, "elements": 38, "types": 3}


METHODS = {
    "ping": handle_ping,
    "status": handle_status,
    "prepare_project": handle_prepare_project,
    "build_ir": handle_build_ir,
    "delete_tagged": handle_delete_tagged,
    "select_by_tags": handle_select_by_tags,
    "list_types": handle_list_types,
    "audit_model": handle_audit_model,
    "fix_issues": handle_fix_issues,
    "export_audit": handle_export_audit,
    "create_levels": lambda p: {"created": len(p.get("levels") or [])},
    "create_grids": lambda p: {"created": len(p.get("grids") or [])},
    "create_walls": lambda p: handle_build_ir({"ir": {"levels": [], "grids": [],
                                                     "walls": p.get("walls") or [],
                                                     "columns": [], "beams": [], "doors": [],
                                                     "windows": [], "slabs": []}}),
    "create_columns": lambda p: handle_build_ir({"ir": {"levels": [], "grids": [],
                                                       "walls": [], "columns": p.get("columns") or [],
                                                       "beams": [], "doors": [], "windows": [],
                                                       "slabs": []}}),
}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        for raw in self.rfile:
            line = raw.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError as ex:
                self._write({"id": None, "error": {"code": "BAD_PARAMS",
                                                   "message": f"JSON 解析失败: {ex}"}})
                continue

            rid, method = req.get("id"), req.get("method", "")
            fn = METHODS.get(method.lower())
            if fn is None:
                self._write({"id": rid, "error": {"code": "UNSUPPORTED",
                                                  "message": f"未知方法: {method}"}})
                continue
            try:
                self._write({"id": rid, "result": fn(req.get("params") or {})})
            except KeyError as ex:
                self._write({"id": rid, "error": {"code": "BAD_PARAMS", "message": str(ex)}})
            except Exception as ex:
                self._write({"id": rid, "error": {"code": "INTERNAL", "message": str(ex)}})

    def _write(self, obj):
        self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        self.wfile.flush()


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(port: int = 48880, host: str = "127.0.0.1"):
    srv = Server((host, port), Handler)
    print(f"mock revit listening on {host}:{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=48880)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    serve(a.port, a.host)
