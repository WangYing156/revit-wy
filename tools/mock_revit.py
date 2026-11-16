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


METHODS = {
    "ping": handle_ping,
    "status": handle_status,
    "prepare_project": handle_prepare_project,
    "build_ir": handle_build_ir,
    "delete_tagged": handle_delete_tagged,
    "list_types": handle_list_types,
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
