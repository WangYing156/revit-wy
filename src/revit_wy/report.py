"""“数模对照”报告生成器。

产出三份东西，分别服务三种读者：
  1. mapping_review.md   —— 给设计师看，逐图层打分 + 待确认项，可直接改
  2. mapping_review.xlsx —— 给建模组批量改（无 Excel 依赖时降级为 .csv）
  3. mapping_confirmed.yaml —— 机器读，确认后回灌 mapping，二次运行无需再问
"""
from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import save_yaml
from .dwg import DrawingInfo, LayerStats
from .ir import _target_revit
from .mapping import LayerMapper, MatchResult, normalize

ACTION_CN = {
    "build": "建模",
    "isolate": "隔离",
    "ignore": "忽略",
    "manual_review": "待确认",
}

CONFIDENCE_BAND = [
    (0.85, "高", "✅"),
    (0.55, "中", "⚠️"),
    (0.0, "低", "❓"),
]


def _band(c: float):
    for th, label, icon in CONFIDENCE_BAND:
        if c >= th:
            return label, icon
    return "低", "❓"


def write_review(
    info: DrawingInfo,
    mapper: LayerMapper,
    out_dir: str | Path,
    rules_summary: Optional[Dict[str, Any]] = None,
    title: str = "数模对照确认单",
) -> Dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    results: List[MatchResult] = [
        mapper.match(l.name) for l in info.layers
    ]
    by_layer = {r.layer: r for r in results}

    paths: Dict[str, Path] = {}
    paths["markdown"] = _write_markdown(info, results, out, rules_summary, title)
    paths["csv"] = _write_csv(info, results, by_layer, out)
    paths["confirmed"] = _write_confirmed(info, results, mapper, out)
    paths["json"] = _write_json(info, results, out)
    return paths


# ------------------------------------------------------------------ markdown
def _write_markdown(info, results, out: Path, rules_summary, title) -> Path:
    p = out / "mapping_review.md"
    ext = info.extent
    c = Counter(r.action for r in results)
    weights = defaultdict(int)
    for r, l in zip(results, info.layers):
        weights[r.action] += l.entity_count

    L: List[str] = []
    L.append(f"# {title}\n")
    L.append(f"> 生成时间：{datetime.now():%Y-%m-%d %H:%M}　"
             f"源图：`{Path(info.path).name}`　"
             f"DXF 版本：{info.dxf_version}\n")
    L.append("## 1. 图纸体检\n")
    L.append("| 项目 | 值 |\n|---|---|")
    L.append(f"| 图纸单位 | {info.insunits}（×{info.unit_to_mm} → mm）|")
    L.append(f"| 图形范围 | X {ext[0]:.0f} ~ {ext[2]:.0f}，Y {ext[1]:.0f} ~ {ext[3]:.0f}（mm）|")
    L.append(f"| 图幅估算 | {(ext[2]-ext[0])/1000:.1f} m × {(ext[3]-ext[1])/1000:.1f} m |")
    L.append(f"| 实体总数 | {info.total_entities} |")
    L.append(f"| 图层数 | {len(info.layers)} |")
    L.append(f"| 图块数 | {len(info.block_names)} |")
    L.append("")
    L.append("**处置汇总**\n")
    L.append("| 处置 | 图层数 | 实体数 | 说明 |\n|---|---:|---:|---|")
    desc = {"build": "提取几何 → 生成构件", "isolate": "冻结隔离，保留可追溯",
            "ignore": "删除（图框/构造线）", "manual_review": "需人工指定归属"}
    for a in ("build", "manual_review", "isolate", "ignore"):
        L.append(f"| {ACTION_CN[a]} | {c.get(a,0)} | {weights.get(a,0)} | {desc[a]} |")
    L.append("")

    L.append("## 2. 图层 → 构件 数模对照表\n")
    L.append("> 排序按实体数降序。**待确认**项必须给出指定后才能全自动建模。\n")
    L.append("| # | 图层名 | 实体数 | 主要图元 | 识别为 | Revit 类别 | 置信度 | 依据 | 处置 |")
    L.append("|---:|---|---:|---|---|---|---:|---|---|")
    for i, (l, r) in enumerate(zip(info.layers, results), 1):
        label, icon = _band(r.confidence)
        kinds = ", ".join(f"{k}×{v}" for k, v in
                          sorted(l.kinds.items(), key=lambda t: -t[1])[:3])
        cat = (r.revit_category or "—").replace("OST_", "")
        flag = "" if l.entity_count else " (空层)"
        L.append(
            f"| {i} | `{l.name}`{flag} | {l.entity_count} | {kinds} | "
            f"{r.name_cn or r.component} | {cat} | {icon} {r.confidence:.2f} {label} | "
            f"{r.matched_by}·{r.reason} | {ACTION_CN[r.action]} |"
        )
    L.append("")

    review = [(l, r) for l, r in zip(info.layers, results) if r.action == "manual_review"]
    if review:
        L.append("## 3. 必须人工确认的图层\n")
        L.append("请在「改为」列填 `wall / column / beam / door / window / slab / "
                 "grid / isolate / ignore`：\n")
        L.append("| 图层名 | 实体数 | 当前猜测 | 置信度 | 改为 |")
        L.append("|---|---:|---|---:|---|")
        for l, r in review:
            L.append(f"| `{l.name}` | {l.entity_count} | {r.name_cn or '未识别'} "
                     f"| {r.confidence:.2f} |  |")
        L.append("")

    L.append("## 4. 建模规则摘要（当前生效）\n")
    if rules_summary:
        L.append("| 规则 | 取值 |\n|---|---|")
        for k, v in rules_summary.items():
            L.append(f"| {k} | {v} |")
        L.append("")

    L.append("## 5. 确认方式\n")
    L.append("三选一，任选其一下发即可：\n")
    L.append("1. **对话确认**（Cursor / WorkBuddy）：")
    L.append("   ```\n   confirm_mapping 把 “墙体-240” 设为 wall，其余保持\n   ```")
    L.append("2. **改表确认**：编辑 `mapping_review.csv` 的 `override_component` 列，再执行")
    L.append("   ```\n   revit-wy confirm --review mapping_review.csv\n   ```")
    L.append("3. **全自动**：仅当「待确认」图层数为 0 时，`--auto` 可直接跑通。\n")
    p.write_text("\n".join(L), encoding="utf-8")
    return p


# ------------------------------------------------------------------ csv
def _write_csv(info, results, by_layer, out: Path) -> Path:
    p = out / "mapping_review.csv"
    cols = ["layer", "entity_count", "component_auto", "name_cn",
            "revit_category", "confidence", "matched_by", "reason",
            "action", "override_component", "note"]
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for l, r in zip(info.layers, results):
            w.writerow([l.name, l.entity_count, r.component, r.name_cn,
                        r.revit_category or "", f"{r.confidence:.3f}",
                        r.matched_by, r.reason, r.action, "", ""])
    return p


def read_overrides(path: str | Path) -> Dict[str, str]:
    """读回确认表里的 override_component。"""
    p = Path(path)
    out: Dict[str, str] = {}
    if not p.exists():
        return out
    if p.suffix.lower() == ".csv":
        with open(p, "r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                ov = (row.get("override_component") or "").strip()
                if ov:
                    out[row["layer"]] = ov
    return out


# ------------------------------------------------------------------ json / yaml
def _write_json(info, results, out: Path) -> Path:
    p = out / "mapping_review.json"
    data = {
        "drawing": {
            "path": info.path, "dxf_version": info.dxf_version,
            "insunits": info.insunits, "unit_to_mm": info.unit_to_mm,
            "extent": info.extent, "total_entities": info.total_entities,
        },
        "layers": [
            {**r.as_dict(), "entity_count": l.entity_count, "kinds": l.kinds}
            for l, r in zip(info.layers, results)
        ],
        "summary": dict(Counter(r.action for r in results)),
    }
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _write_confirmed(info, results, mapper, out: Path) -> Path:
    """生成可回灌的确认映射（把自动结果固化为 glob 规则）。"""
    layers = []
    for l, r in zip(info.layers, results):
        layers.append({
            "layer": l.name,
            "component": r.component,
            "action": r.action,
            "confidence": r.confidence,
            "locked": r.action == "build" and r.confidence >= mapper.auto_accept,
        })
    data = {
        "profile": f"{mapper.mapping.get('profile', 'arch')}_confirmed",
        "generated_from": str(info.path),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "auto_accept": mapper.auto_accept,
        "manual_review": mapper.manual_review,
        "fallback_component": mapper.fallback,
        "rules": [],
        "layer_overrides": layers,
    }
    p = out / "mapping_confirmed.yaml"
    save_yaml(data, p)
    return p


# ------------------------------------------------------------------ 规则摘要
def rules_summary(rules: Dict[str, Any]) -> Dict[str, Any]:
    wall = rules.get("wall", {})
    return {
        "目标 Revit": _target_revit(rules),
        "墙族/类型": f"{wall.get('family','基本墙')} · "
                    f"{wall.get('type_name_template','常规 - {thickness}mm')}",
        "墙默认厚": f"{wall.get('fallback_thickness_mm', 200)} mm",
        "柱族": rules.get("column", {}).get("family", "结构柱"),
        "梁截面默认": f"{rules.get('beam',{}).get('default_b',200)}×"
                    f"{rules.get('beam',{}).get('default_h',500)} mm",
        "门族兜底": rules.get("door", {}).get("fallback_family", "单扇 - 与墙齐"),
        "窗族兜底": rules.get("window", {}).get("fallback_family", "固定窗"),
        "门默认尺寸": f"{rules.get('door',{}).get('default_width_mm',900)}×"
                    f"{rules.get('door',{}).get('default_height_mm',2100)} mm",
        "窗默认尺寸": f"{rules.get('window',{}).get('default_width_mm',1500)}×"
                    f"{rules.get('window',{}).get('default_height_mm',1500)} mm",
        "窗台高": f"{rules.get('window',{}).get('sill_height_mm',900)} mm",
        "板默认厚": f"{rules.get('slab',{}).get('default_thickness_mm',120)} mm",
        "层高": f"{rules.get('levels',{}).get('story_height_mm',3000)} mm",
        "事务模式": rules.get("modeling", {}).get("transaction_mode", "per_element_commit"),
    }
