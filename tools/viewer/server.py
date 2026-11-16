"""revit-wy 数模对照工作台（浏览器 + 本地服务）

为什么以浏览器为主载体（D 方案）
--------------------------------
翻模的真实瓶颈是**「改规则 → 看识别对不对」的循环速度**，不是界面好不好看。
浏览器方案把这个循环压到「改一个数 → 秒级看到结果」，且：

  · 零插件依赖：不装 Revit 也能审图、调规则、出对照单
  · 零前端构建：单文件 HTML + Canvas，不用 npm/node，离线环境可用
  · 复用同一后端：和 MCP / CLI 用的是同一套 mapping / ir / diffing，不会出现"两套逻辑两套结果"
  · 可远程：同事在自己电脑上开浏览器就能看你的对照单

Revit 内的停靠面板保留，但只做 Revit 原生的事（选中/清理/打开视图），
不再重复实现可视化——避免两套 UI 两处维护。

接口一览
--------
  GET  /                    工作台页面
  GET  /api/preview         图层 + 图元（Canvas 渲染用）
  GET  /api/ir              构件清单 + 识别结果几何
  GET  /api/samples         可用样例图
  GET  /api/rules           当前建模规则
  GET  /api/revit           探测 Revit 插件是否可用（浏览器→本服务→插件，避免 CORS）
  POST /api/load            切换图纸/专业
  POST /api/confirm         下发图层归属（写别名库）
  POST /api/rules           热改建模规则
  POST /api/build           写入 Revit（dry_run 默认 true，落模型需 confirm）
  POST /api/select          让 Revit 选中/缩放到指定构件
  POST /api/export          导出对照单（md/csv/json）
  POST /api/snapshot        保存当前 IR 作为基线（供差异对比）
"""
from __future__ import annotations

import json
import sys
import threading
import traceback
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from revit_wy import config as cfg                                  # noqa: E402
from revit_wy.bridge import RevitBridge, probe                      # noqa: E402
from revit_wy.diffing import compare                                # noqa: E402
from revit_wy.dwg import extract, layer_thickness_from_name         # noqa: E402
from revit_wy.ir import assign_host_walls, build as build_ir        # noqa: E402
from revit_wy.mapping import LayerMapper, load_aliases, save_aliases  # noqa: E402
from revit_wy.preview import build_preview, preview_ir              # noqa: E402
from revit_wy.report import rules_summary, write_review             # noqa: E402

ALIAS_FILE = cfg.USER_CONFIG_DIR / "aliases.json"
SNAPSHOT_FILE = cfg.USER_CONFIG_DIR / "snapshot.ir.json"
HTML = Path(__file__).resolve().parent / "index.html"

# 允许写入的构件类型（防止前端传奇怪的值污染别名库）
VALID_COMPONENTS = {"wall", "column", "beam", "door", "window", "slab",
                    "grid", "isolate", "ignore", "manual_review"}


@dataclass
class Session:
    dxf: str = ""
    profile: str = "arch"
    mapper: Any = None
    rules: Dict[str, Any] = field(default_factory=dict)
    preview: Dict[str, Any] = field(default_factory=dict)
    model: Any = None
    ir: Dict[str, Any] = field(default_factory=dict)
    # 上次成功写入 Revit 的 IR 快照，用于差异对比
    baseline: Dict[str, Any] | None = None
    revit_host: str = "127.0.0.1"
    revit_port: int = 48880
    last_build: Dict[str, Any] = field(default_factory=dict)

    # ---------------------------------------------------------- 构建
    def rebuild(self) -> Dict[str, Any]:
        """重算识别结果。映射或规则变化后调用。"""
        if not self.dxf:
            return {}
        prims = extract(self.dxf, self.mapper)
        lt: Dict[str, float] = {}
        for layer in {p.layer for p in prims.get("wall", [])}:
            th = layer_thickness_from_name(layer, self.mapper.match(layer).extras)
            if th:
                lt[layer] = th
        model = build_ir(prims, self.rules, profile=self.profile,
                         source=self.dxf, walls_layer_thickness=lt)
        assign_host_walls(model)
        self.model = model
        self.ir = {
            "counts": model.counts(),
            "preview": preview_ir(model),
            # 逐构件带 id，供前端"点构件 → 回 Revit 选中"
            "items": {
                "walls": [w.to_dict() for w in model.walls],
                "columns": [c.to_dict() for c in model.columns],
                "beams": [b.to_dict() for b in model.beams],
                "slabs": [s.to_dict() for s in model.slabs],
                "grids": [g.to_dict() for g in model.grids],
                "doors": [d.to_dict() for d in model.doors],
                "windows": [w.to_dict() for w in model.windows],
            },
        }
        return self.ir

    def load(self, dxf: str, profile: str) -> Dict[str, Any]:
        self.dxf, self.profile = dxf, profile
        self.mapper = LayerMapper(cfg.load_mapping(profile), load_aliases(ALIAS_FILE))
        self.rules = cfg.load_rules()
        self.preview = build_preview(dxf, self.mapper)
        self.rebuild()
        return {"counts": self.ir["counts"], "layers": len(self.preview["layers"])}

    def reload_after_change(self) -> Dict[str, Any]:
        """映射/规则变化后的统一刷新入口。"""
        self.rebuild()
        self.refresh_layers()
        return {"counts": self.ir.get("counts", {})}

    def refresh_layers(self) -> None:
        """映射变化后，把 preview 里的图层判定刷新到最新。"""
        for b in self.preview.get("layers", []):
            r = self.mapper.match(b["name"])
            b.update({
                "component": r.component, "name_cn": r.name_cn,
                "action": r.action, "confidence": r.confidence,
                "matched_by": r.matched_by, "reason": r.reason,
            })

    def diff_vs_baseline(self) -> Dict[str, Any]:
        if not self.baseline or self.model is None:
            return {}
        return compare(self.baseline, self.model.to_dict()).to_dict()


S = Session()


# ====================================================================== 动作
def act_confirm(decisions: Dict[str, str]) -> Dict[str, Any]:
    if S.mapper is None:
        return {"ok": False, "error": "尚未加载图纸"}
    applied, rejected = {}, {}
    for layer, comp in (decisions or {}).items():
        if comp not in VALID_COMPONENTS:
            rejected[layer] = f"非法归属 {comp!r}"
            continue
        S.mapper.learn(layer, comp)
        applied[layer] = comp
    if applied:
        save_aliases(S.mapper.export_aliases(), ALIAS_FILE)
        S.rebuild()
        S.refresh_layers()
    return {"ok": True, "applied": applied, "rejected": rejected,
            "counts": S.ir.get("counts", {})}


def act_rules(overrides: Dict[str, Any]) -> Dict[str, Any]:
    S.rules = cfg.deep_merge(S.rules, overrides or {})
    S.rebuild()
    S.refresh_layers()
    return {"ok": True, "counts": S.ir.get("counts", {}),
            "summary": rules_summary(S.rules)}


def act_revit_probe() -> Dict[str, Any]:
    p = probe(S.revit_host, S.revit_port, timeout=2.5)
    return {"ok": True, "revit": p}


def act_build(dry_run: bool, confirm: bool, steps=None,
              replace_existing: bool = False) -> Dict[str, Any]:
    if S.model is None:
        return {"ok": False, "error": "尚未构建 IR"}

    # 已有构件数：不连 Revit 就只能是"不知道"，绝不能假装是 0
    existing, reachable, probe_err = 0, True, ""
    try:
        with RevitBridge(S.revit_host, S.revit_port) as b:
            existing = int(b.status().get("revit_wy_tagged", 0) or 0)
    except Exception as ex:
        reachable, probe_err = False, str(ex)

    if dry_run:
        check = [
            "标高将按缺省创建或复用同名",
            "墙类型按厚度匹配，缺失则复制基准类型",
            "门窗按邻近墙找宿主，失败则跳过并记录",
        ]
        if not reachable:
            check.append(f"⚠ 连不上 Revit：{probe_err}")
        if existing and not replace_existing:
            check.append(f"⚠ 模型里已有 {existing} 个上次翻模构件，本次会叠加两份")
        return {
            "ok": True, "dry_run": True, "will_create": S.model.counts(),
            "would_delete": existing if replace_existing else 0,
            "existing_tagged": existing, "revit_reachable": reachable,
            "check": check,
            "note": "确认后调用 build(dry_run=false, confirm=true"
                    + (", replace_existing=true)" if replace_existing else ")"),
        }
    if not confirm:
        return {"ok": False,
                "error": "安全闸未开：必须同时传 dry_run=false 与 confirm=true"}

    ir = S.model.to_dict()
    deleted = 0
    try:
        with RevitBridge(S.revit_host, S.revit_port) as b:
            if replace_existing and existing:
                deleted = int((b.delete_tagged() or {}).get("deleted", 0))
            res = b.build_ir(ir, steps=steps, dry_run=False, auto_save=True)
    except Exception as ex:
        msg = str(ex)
        if deleted:
            msg = f"已删除 {deleted} 个旧构件，但写入失败：{ex}"
        return {"ok": False, "error": msg, "deleted": deleted,
                "hint": "确认 Revit 已打开项目，且功能区 revit-wy 显示运行中"}

    if res.get("ok"):
        # 写入成功即存为基线，后续改图能直接看出差异
        S.baseline = ir
        try:
            SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
            SNAPSHOT_FILE.write_text(
                json.dumps(ir, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    S.last_build = res
    return {"ok": bool(res.get("ok")), "result": res,
            "deleted": deleted, "diff": S.diff_vs_baseline()}


def act_select(ids: list, kinds: list | None = None) -> Dict[str, Any]:
    """让 Revit 选中构件。ids 是 IR 里的构件 id。"""
    if not ids:
        return {"ok": False, "error": "未提供构件 id"}
    try:
        with RevitBridge(S.revit_host, S.revit_port) as b:
            res = b.call("select_by_tags", {"ids": ids, "kinds": kinds or []})
        return {"ok": True, "result": res}
    except Exception as ex:
        return {"ok": False, "error": str(ex)}


def act_export(fmt: str = "md") -> Dict[str, Any]:
    if S.mapper is None:
        return {"ok": False, "error": "尚未加载图纸"}
    from revit_wy.dwg import scan
    out = Path(S.dxf).with_suffix("") / "review"
    try:
        info = scan(S.dxf)
        paths = write_review(info, S.mapper, out, rules_summary(S.rules))
        return {"ok": True, "files": {k: str(v) for k, v in paths.items()},
                "dir": str(out)}
    except Exception as ex:
        return {"ok": False, "error": str(ex)}


def act_snapshot() -> Dict[str, Any]:
    if S.model is None:
        return {"ok": False, "error": "尚未构建 IR"}
    S.baseline = S.model.to_dict()
    SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_FILE.write_text(json.dumps(S.baseline, ensure_ascii=False),
                             encoding="utf-8")
    return {"ok": True, "path": str(SNAPSHOT_FILE),
            "counts": S.model.counts()}


def act_diff() -> Dict[str, Any]:
    if not S.baseline:
        try:
            if SNAPSHOT_FILE.exists():
                S.baseline = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
        except Exception:
            S.baseline = None
    if not S.baseline:
        return {"ok": False, "error": "还没有基线。写入 Revit 成功或点「保存基线」后再试"}
    return {"ok": True, "diff": S.diff_vs_baseline()}


# ====================================================================== HTTP
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: bytes, mime: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        try:
            body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        except Exception as ex:
            body = json.dumps({"ok": False, "error": f"序列化失败: {ex}"},
                              ensure_ascii=False).encode("utf-8")
        self._send(code, body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                return self._send(200, HTML.read_bytes(), "text/html; charset=utf-8")

            if u.path == "/api/preview":
                if not S.preview:
                    dxf = q.get("dxf", [""])[0]
                    if not dxf:
                        return self._json({"ok": False, "error": "缺少 dxf 参数"})
                    S.load(dxf, q.get("profile", ["arch"])[0])
                return self._json({"ok": True, **S.preview})

            if u.path == "/api/ir":
                return self._json({"ok": True, **S.ir})

            if u.path == "/api/rules":
                return self._json({"ok": True, "rules": S.rules,
                                   "summary": rules_summary(S.rules)})

            if u.path == "/api/revit":
                return self._json(act_revit_probe())

            if u.path == "/api/diff":
                return self._json(act_diff())

            if u.path == "/api/samples":
                files = sorted(str(p) for p in (ROOT / "samples").glob("*.dxf"))
                return self._json({"ok": True, "samples": files,
                                   "cwd": str(ROOT)})

            self._json({"ok": False, "error": "not found"}, 404)
        except Exception as ex:
            traceback.print_exc()
            self._json({"ok": False, "error": f"{type(ex).__name__}: {ex}"}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            p = json.loads(self.rfile.read(n) or b"{}")
        except Exception as ex:
            return self._json({"ok": False, "error": f"JSON 解析失败: {ex}"}, 400)

        try:
            if u.path == "/api/load":
                r = S.load(p["dxf"], p.get("profile", "arch"))
                return self._json({"ok": True, **r})

            if u.path == "/api/confirm":
                return self._json(act_confirm(p.get("decisions", {})))

            if u.path == "/api/rules":
                return self._json(act_rules(p.get("overrides", {})))

            if u.path == "/api/build":
                return self._json(act_build(
                    dry_run=p.get("dry_run", True),
                    confirm=p.get("confirm", False),
                    replace_existing=p.get("replace_existing", False),
                    steps=p.get("steps")))

            if u.path == "/api/select":
                return self._json(act_select(p.get("ids", []), p.get("kinds")))

            if u.path == "/api/export":
                return self._json(act_export(p.get("fmt", "md")))

            if u.path == "/api/snapshot":
                return self._json(act_snapshot())

            self._json({"ok": False, "error": "not found"}, 404)
        except Exception as ex:
            traceback.print_exc()
            self._json({"ok": False, "error": f"{type(ex).__name__}: {ex}"}, 500)


def serve(host: str = "0.0.0.0", port: int = 8770, dxf: str = "",
          profile: str = "arch") -> None:
    if dxf:
        try:
            r = S.load(dxf, profile)
            print(f"已加载 {dxf} → {r['counts']}", flush=True)
        except Exception as ex:
            print(f"预加载失败（可在页面里再选）: {ex}", flush=True)
    if SNAPSHOT_FILE.exists():
        try:
            S.baseline = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
            print(f"已载入基线快照 {SNAPSHOT_FILE.name}，可做差异对比", flush=True)
        except Exception:
            pass

    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    print(f"\n  revit-wy 工作台  →  http://{host}:{port}/\n"
          f"  样例：{', '.join(p.name for p in sorted((ROOT/'samples').glob('*.dxf')))}\n"
          f"  Ctrl+C 停止\n", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="revit-wy 数模对照工作台（浏览器载体）")
    ap.add_argument("dxf", nargs="?", default="")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--profile", default="arch", choices=["arch", "struct", "mep"])
    ap.add_argument("--revit-port", type=int, default=48880)
    a = ap.parse_args()
    S.revit_port = a.revit_port
    serve(a.host, a.port, a.dxf, a.profile)
