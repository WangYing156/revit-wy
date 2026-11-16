"""revit-wy 数模对照可视化预览（本地 HTTP + Canvas）

为什么先做这个而不是直接嵌进 Revit
--------------------------------
"改图纸图层 → 看一眼识别对不对 → 再调规则"这个循环，一天要跑几十次。
如果每次都要开 Revit、等插件加载、等 API 响应，循环成本高一个数量级。
这个预览服务只依赖 Python + 浏览器：改完 yaml 刷新页面即可看到结果，迭代速度快 10 倍。

设计要点
--------
* 零前端构建：单文件 HTML + Canvas，不用 npm/node，设计院离线环境友好。
* 数据接口只有 4 个 JSON，前端无需框架。
* 不轮询：改规则后手动刷新或点"重新加载"。
* 可留空给后续嵌到 Revit 的 WebView2 里（同一套 HTML 双用）。
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from revit_wy import config as cfg                      # noqa: E402
from revit_wy.dwg import extract, layer_thickness_from_name  # noqa: E402
from revit_wy.ir import assign_host_walls, build as build_ir  # noqa: E402
from revit_wy.mapping import LayerMapper, load_aliases, save_aliases  # noqa: E402
from revit_wy.preview import build_preview, preview_ir  # noqa: E402

ALIAS_FILE = cfg.USER_CONFIG_DIR / "aliases.json"
HTML = Path(__file__).resolve().parent / "index.html"


class ViewerState:
    """单用户本地服务，状态放内存即可。"""

    def __init__(self):
        self.dxf: str = ""
        self.profile: str = "arch"
        self.mapper: LayerMapper | None = None
        self.preview: dict = {}
        self.ir: dict = {}
        self.rules: dict = {}

    def load(self, dxf: str, profile: str) -> dict:
        self.dxf, self.profile = dxf, profile
        self.mapper = LayerMapper(cfg.load_mapping(profile), load_aliases(ALIAS_FILE))
        self.rules = cfg.load_rules()
        self.preview = build_preview(dxf, self.mapper)

        prims = extract(dxf, self.mapper)
        lt = {}
        for layer in {p.layer for p in prims.get("wall", [])}:
            th = layer_thickness_from_name(layer, self.mapper.match(layer).extras)
            if th:
                lt[layer] = th
        model = build_ir(prims, self.rules, profile=profile, source=dxf,
                         walls_layer_thickness=lt)
        assign_host_walls(model)
        self.model = model
        self.ir = {"counts": model.counts(), "preview": preview_ir(model)}
        return {"ok": True, "counts": model.counts()}

    def confirm(self, decisions: dict) -> dict:
        if self.mapper is None:
            return {"ok": False, "error": "尚未加载图纸"}
        applied = {}
        for layer, comp in (decisions or {}).items():
            self.mapper.learn(layer, comp)
            applied[layer] = comp
        save_aliases(self.mapper.export_aliases(), ALIAS_FILE)
        # 映射变了，重算识别结果
        prims = extract(self.dxf, self.mapper)
        lt = {}
        for layer in {p.layer for p in prims.get("wall", [])}:
            th = layer_thickness_from_name(layer, self.mapper.match(layer).extras)
            if th:
                lt[layer] = th
        model = build_ir(prims, self.rules, profile=self.profile, source=self.dxf,
                         walls_layer_thickness=lt)
        assign_host_walls(model)
        self.model = model
        self.ir = {"counts": model.counts(), "preview": preview_ir(model)}
        # 刷新图层的 action/confidence
        for b in self.preview["layers"]:
            r = self.mapper.match(b["name"])
            b.update({
                "component": r.component, "name_cn": r.name_cn, "action": r.action,
                "confidence": r.confidence, "matched_by": r.matched_by,
                "reason": r.reason,
            })
        return {"ok": True, "applied": applied, "counts": model.counts()}

    def set_rules(self, overrides: dict) -> dict:
        self.rules = cfg.deep_merge(self.rules, overrides or {})
        return self.load(self.dxf, self.profile)


STATE = ViewerState()

# 彩色配色：建模层用暖色，隔离/忽略用灰，方便一眼分辨
MIME = {"html": "text/html; charset=utf-8", "json": "application/json; charset=utf-8"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # 静音

    def _send(self, code: int, body: bytes, mime: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            return self._send(200, HTML.read_bytes(), MIME["html"])
        if u.path == "/api/preview":
            if not STATE.preview:
                dxf = q.get("dxf", [""])[0]
                if not dxf:
                    return self._json({"ok": False, "error": "缺少 dxf 参数"})
                try:
                    STATE.load(dxf, q.get("profile", ["arch"])[0])
                except Exception as ex:
                    return self._json({"ok": False, "error": f"{type(ex).__name__}: {ex}"})
            return self._json({"ok": True, **STATE.preview})
        if u.path == "/api/ir":
            return self._json({"ok": True, **STATE.ir})
        if u.path == "/api/samples":
            samples = sorted(str(p) for p in (ROOT / "samples").glob("*.dxf"))
            return self._json({"ok": True, "samples": samples})
        if u.path == "/api/rules":
            return self._json({"ok": True, "rules": STATE.rules})
        self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception as ex:
            return self._json({"ok": False, "error": f"JSON 解析失败: {ex}"}, 400)

        try:
            if u.path == "/api/load":
                return self._json({"ok": True, **STATE.load(
                    payload["dxf"], payload.get("profile", "arch"))})
            if u.path == "/api/confirm":
                return self._json(STATE.confirm(payload.get("decisions", {})))
            if u.path == "/api/rules":
                return self._json({"ok": True, **STATE.set_rules(payload.get("overrides", {}))})
        except Exception as ex:
            return self._json({"ok": False, "error": f"{type(ex).__name__}: {ex}"}, 500)
        self._json({"ok": False, "error": "not found"}, 404)


def serve(host: str = "0.0.0.0", port: int = 8770, dxf: str = "", profile: str = "arch"):
    if dxf:
        try:
            STATE.load(dxf, profile)
            print(f"已加载 {dxf} → {STATE.ir['counts']}")
        except Exception as ex:
            print(f"预加载失败（可在页面里再选）: {ex}")

    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"\n  数模对照预览  →  http://{host}:{port}/")
    print(f"  样例图：{', '.join(p.name for p in sorted((ROOT/'samples').glob('*.dxf')))}")
    print("  Ctrl+C 停止\n", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="revit-wy 数模对照可视化预览")
    ap.add_argument("dxf", nargs="?", default="")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--profile", default="arch", choices=["arch", "struct", "mep"])
    a = ap.parse_args()
    serve(a.host, a.port, a.dxf, a.profile)
