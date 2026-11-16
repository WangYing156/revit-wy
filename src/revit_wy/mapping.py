"""图层 → 构件的规范映射引擎。

设计要点
--------
1. 多层匹配：exact / glob / contains / regex / keyword / fuzzy，命中即停但累加置信度。
2. 名称归一化：全角转半角、去前后缀、去数字与分隔符，兼容 "A-WALL-200"、"墙体 200"、"Q-200"。
3. 学习别名：用户在一次“数模对照”确认中认可的结果，写入别名库，下次直接高分命中。
4. 每个结果都带 confidence + matched_by + reason，供人工快速判断。
"""
from __future__ import annotations

import fnmatch
import re
import unicodedata
from dataclasses import dataclass, field, asdict
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Optional

# 置信度权重：越精确越高
WEIGHTS = {
    "alias": 1.00,
    "exact": 0.98,
    "glob": 0.90,
    "contains": 0.80,
    "regex": 0.78,
    "keyword": 0.72,
    "fuzzy": 0.60,
}

HOUSEKEEPING_TOKENS = (
    "a-", "s-", "m-", "e-", "p-", "x-", "_", "-", " ", ".", "$", "0$",
)


def normalize(name: str) -> str:
    """归一化图层名：全角→半角、大写、去 Autodesk 常见修饰。"""
    if not name:
        return ""
    s = unicodedata.normalize("NFKC", name).strip()
    s = s.upper()
    # AutoCAD 常见前缀修饰：$0$A$C..., |图层|视图
    s = re.sub(r"^\$0\$", "", s)
    s = re.sub(r"^[^|]*\|", "", s)
    s = re.sub(r"\s{2,}", " ", s)
    # Xref 前缀 "墙体|A-WALL" 已处理；去掉纯数字后缀便于匹配 A-WALL-200
    return s


def core_name(name: str) -> str:
    """去掉厚度/编号后缀后的核心名，用于 fuzzy 匹配。"""
    s = normalize(name)
    s = re.sub(r"[-_ ]?\d{2,4}\s*$", "", s)          # -200 / _2400
    s = re.sub(r"[^A-Z\u4e00-\u9fa5]+", "", s)        # 只留字母与中文
    return s


@dataclass
class MatchResult:
    layer: str
    component: str
    name_cn: str
    revit_category: Optional[str]
    action: str                     # build | isolate | ignore | manual_review
    confidence: float
    matched_by: str
    reason: str
    rule_index: int = -1
    extras: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


class LayerMapper:
    def __init__(self, mapping: Dict[str, Any], aliases: Optional[Dict[str, str]] = None):
        self.mapping = mapping
        self.rules: List[Dict[str, Any]] = mapping.get("rules", [])
        # 按 priority 降序，让高优先级规则先匹配
        self.rules = sorted(
            enumerate(self.rules), key=lambda t: -int(t[1].get("priority", 50))
        )
        self.auto_accept = float(mapping.get("auto_accept", 0.85))
        self.manual_review = float(mapping.get("manual_review", 0.55))
        self.fallback = mapping.get("fallback_component", "manual_review")
        # 别名库: 归一化图层名 -> component
        self.aliases: Dict[str, str] = {
            normalize(k): v for k, v in (aliases or {}).items()
        }
        self._component_index: Dict[str, Dict[str, Any]] = {}
        for _, r in self.rules:
            self._component_index.setdefault(r.get("component", ""), r)

    # ---------------- 单条规则匹配 ----------------
    def _match_rule(self, layer: str, norm: str, core: str, rule: Dict[str, Any]):
        """返回 (score, matched_by, detail) 或 None。"""
        comp = rule.get("component", "")

        for pat in rule.get("exact", []) or []:
            if norm == normalize(pat):
                return WEIGHTS["exact"], "exact", f"精确匹配 {pat}"

        for pat in rule.get("glob", []) or []:
            if fnmatch.fnmatch(norm, normalize(pat)):
                return WEIGHTS["glob"], "glob", f"通配匹配 {pat}"

        for pat in rule.get("contains", []) or []:
            p = normalize(pat)
            if p and p in norm:
                return WEIGHTS["contains"], "contains", f"包含 “{pat}”"

        for pat in rule.get("regex", []) or []:
            try:
                if re.search(pat, norm):
                    return WEIGHTS["regex"], "regex", f"正则匹配 {pat}"
            except re.error:
                continue

        for kw in rule.get("keyword", []) or []:
            k = normalize(kw)
            if k and (k in norm or k in core):
                return WEIGHTS["keyword"], "keyword", f"关键词 “{kw}”"

        # fuzzy：与规则内任一候选词做相似度
        best, best_word = 0.0, ""
        for group in ("exact", "glob", "contains", "keyword"):
            for pat in rule.get(group, []) or []:
                cand = core_name(pat.replace("*", ""))
                if not cand:
                    continue
                ratio = SequenceMatcher(None, core, cand).ratio()
                if ratio > best:
                    best, best_word = ratio, pat
        if best >= 0.78:
            return (
                WEIGHTS["fuzzy"] * best / 0.78 * 0.98,
                "fuzzy",
                f"相似 “{best_word}” ({best:.2f})",
            )
        return None

    # ---------------- 对外接口 ----------------
    def match(self, layer: str) -> MatchResult:
        norm, core = normalize(layer), core_name(layer)

        # ---- 0) 别名短路 ----
        # 别名是人工确认过的事实，比任何规则推断都可信，必须无条件优先。
        # 早期实现只在"规则也命中"时才算别名，导致 isolate/ignore 这类
        # 非构件归属永远无法生效（它们没有对应的 component 规则）。
        alias_comp = self.aliases.get(norm)
        if alias_comp:
            rule = self._component_index.get(alias_comp)
            if alias_comp in ("isolate", "ignore"):
                return MatchResult(
                    layer=layer, component=alias_comp,
                    name_cn={"isolate": "隔离", "ignore": "忽略"}.get(alias_comp, alias_comp),
                    revit_category=None, action=alias_comp,
                    confidence=1.0, matched_by="alias",
                    reason="已确认别名（人工指定）",
                )
            return MatchResult(
                layer=layer,
                component=alias_comp,
                name_cn=(rule or {}).get("name_cn", "") or alias_comp,
                revit_category=(rule or {}).get("revit_category"),
                action="build",
                confidence=1.0,
                matched_by="alias",
                reason="已确认别名（人工指定）",
                extras={k: (rule or {})[k] for k in
                        ("thickness_suffix", "pair", "block_glob",
                         "grid_name_source", "structural", "shape")
                        if rule and k in rule},
            )
        if alias_comp == "manual_review":
            return MatchResult(
                layer=layer, component="manual_review", name_cn="待确认",
                revit_category=None, action="manual_review",
                confidence=0.0, matched_by="alias", reason="已确认需要人工判断",
            )

        best: Optional[MatchResult] = None

        for idx, rule in self.rules:
            got = self._match_rule(layer, norm, core, rule)
            if not got:
                continue
            score, by, detail = got
            action = rule.get("action", "build")
            if action == "build" and score < self.manual_review:
                action = "manual_review"
            cand = MatchResult(
                layer=layer,
                component=rule.get("component", ""),
                name_cn=rule.get("name_cn", ""),
                revit_category=rule.get("revit_category"),
                action=action,
                confidence=round(min(score, 1.0), 3),
                matched_by=by,
                reason=detail,
                rule_index=idx,
                extras={
                    k: rule[k]
                    for k in ("thickness_suffix", "pair", "block_glob",
                              "grid_name_source", "structural", "shape")
                    if k in rule
                },
            )
            if best is None or cand.confidence > best.confidence:
                best = cand
                if cand.confidence >= self.auto_accept and by in ("alias", "exact"):
                    break

        if best is None:
            return MatchResult(
                layer=layer,
                component=self.fallback,
                name_cn="未识别",
                revit_category=None,
                action="manual_review" if self.fallback == "manual_review" else self.fallback,
                confidence=0.0,
                matched_by="none",
                reason="未命中任何规则，需人工指定",
            )

        # 降到 manual_review 区间时，把 action 改为需确认
        if best.action == "build" and best.confidence < self.auto_accept:
            best.action = "manual_review"
            best.reason += "（置信度不足，建议确认）"
        return best

    def match_many(self, layers: Iterable[str]) -> List[MatchResult]:
        return [self.match(x) for x in layers]

    # ---------------- 别名学习 ----------------
    def learn(self, layer: str, component: str) -> None:
        """用户确认后写入别名，后续同名图层直接 alias 命中。"""
        self.aliases[normalize(layer)] = component

    def export_aliases(self) -> Dict[str, str]:
        return dict(self.aliases)


def load_aliases(path) -> Dict[str, str]:
    from pathlib import Path
    import json
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_aliases(aliases: Dict[str, str], path) -> None:
    from pathlib import Path
    import json
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(aliases, ensure_ascii=False, indent=2), encoding="utf-8")

# ------------------------------------------------------------------ 门窗消歧
# 实际图纸里「门窗」常画在同一图层，靠块名/图层名区分。
# 判定顺序：块名 > 图层名 > 默认（door）。
WINDOW_PAT = re.compile(
    r"(?i)(窗|WINDOW|WIND|^WIN|\bLC\b|飘窗|幕墙|百叶|\\bC\d+|^C[-_]?\d)"
)
DOOR_PAT = re.compile(
    r"(?i)(门|DOOR|DOR|^M[-_]?\d|卷帘|防火门|推拉门|平开门|转门)"
)


def opening_kind(block_name: str = "", layer: str = "") -> str:
    """返回 'door' | 'window'。"""
    for probe in (block_name or "", layer or ""):
        if not probe:
            continue
        if WINDOW_PAT.search(probe):
            return "window"
        if DOOR_PAT.search(probe):
            return "door"
    return "door"
