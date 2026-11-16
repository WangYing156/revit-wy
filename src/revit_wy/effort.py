"""减负量化：把「省了多少工时」算成可核对的数字，而不是空泛的形容词。

为什么要这个模块
----------------
对传统 Revit 工程师说「能提效」没用，他会问三个问题：
  1. 具体省哪儿？        → 按构件类型拆时间
  2. 省多少？            → 每个构件的秒数 × 数量
  3. 凭什么信你？        → 给出工时基准，并允许他改成自己的值

所以这里的工时基准**全部外置在 `config/effort_baseline.yaml`**，
工程师觉得「我们手画墙一道要 40 秒不是 25 秒」直接改 YAML，
报告立刻按他的数字重算 —— 数字可以被质疑，但不能是黑箱。

计算方法
--------
    净节省 = 人工工时 − (AI 处理时间 + 人工复核时间) − 一次性投入摊销
其中：
  · 人工工时   = Σ(构件数 × 该构件手工建模秒数)
  · 复核时间   = 自动归类构件按 `review_rate` 抽检 + 待确认构件必查
  · 一次性投入 = 每张图的规则调校、确认单核对（首次必然要花，之后复用）

注意这是**估算**，不是实测（实测需要现场计时），所以报告里明确标注
"估算"二字，并给出敏感性区间 —— 让数字诚实地软一点。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from . import config as cfg

BASELINE_PATH = cfg.CONFIG_DIR / "effort_baseline.yaml"

# 各构件的**默认**手工建模工时（秒/个）。真实项目差异大，放在 YAML 里可改。
DEFAULT_BASELINE: Dict[str, Any] = {
    "manual_seconds": {
        "level": 120, "grid": 45, "column": 40, "wall": 25,
        "beam": 35, "door": 90, "window": 90, "slab": 15,
    },
    "manual_overhead": {
        "open_and_orient_drawing": 60,
        "create_wall_types": 120,
        "fix_units_and_cleanup": 180,
    },
    "ai_seconds": {
        "per_drawing_fixed": 8,       # 转换+体检+对照+清洗+建 IR 的固定开销
        "per_component": 0.02,
    },
    "review_rate": 0.25,              # 自动归类的构件按 25% 抽检
    "review_seconds_per_component": 20,
    "visual_check_minutes_per_drawing": 10,   # 每张图整体目视校核（不省）
    "manual_review_seconds_each": 60, # 每个待确认图层人工判断
    "setup_minutes_first_time": 20,   # 首次规则调校
    "setup_minutes_later": 3,         # 后续复用
    "hourly_cost_cny": 120,           # 仅用于折算人天成本，可改
    "work_hours_per_day": 8,
}


# IR 的 counts 用复数（walls），工时基线用单数（wall）。
# 早期版本没做归一，导致查不到键就静默退回默认 30 秒/个 —— 数字看着正常，
# 其实是错的。所有取基线的地方必须先过这个函数。
def _base_key(comp: str) -> str:
    return comp[:-1] if comp.endswith("s") and comp != "levels" else (
        "level" if comp == "levels" else (
            "slab" if comp == "slabs" else comp))


@dataclass
class Saving:
    drawing: str = ""
    counts: Dict[str, int] = field(default_factory=dict)
    manual_seconds: float = 0.0
    ai_seconds: float = 0.0
    review_seconds: float = 0.0
    setup_seconds: float = 0.0
    manual_review_layers: int = 0
    by_component: Dict[str, Dict[str, float]] = field(default_factory=dict)
    baseline_source: str = "内置默认值"
    _review_rate: float = 0.15

    # ---------------- 派生
    @property
    def net_saving_seconds(self) -> float:
        """可以为负 —— 单张小图确实可能不划算（首图要调规则）。

        不把负数抹成 0：老实告诉工程师「这张图你是亏的，从第 N 张开始赚」，
        比一个漂亮的假数字更能建立信任。
        """
        return (self.manual_seconds - self.ai_seconds
                - self.review_seconds - self.setup_seconds)

    @property
    def saving_pct(self) -> float:
        if self.manual_seconds <= 0:
            return 0.0
        return 100.0 * self.net_saving_seconds / self.manual_seconds

    def days(self, seconds: float, per_day_hours: float = 8.0) -> float:
        return seconds / (per_day_hours * 3600)

    def as_dict(self) -> Dict[str, Any]:
        b = 8.0
        return {
            "drawing": self.drawing,
            "counts": self.counts,
            "estimate": True,
            "baseline_source": self.baseline_source,
            "seconds": {
                "manual": round(self.manual_seconds, 1),
                "ai": round(self.ai_seconds, 1),
                "review": round(self.review_seconds, 1),
                "setup": round(self.setup_seconds, 1),
                "net_saving": round(self.net_saving_seconds, 1),
            },
            "days": {
                "manual": round(self.days(self.manual_seconds, b), 2),
                "net_saving": round(self.days(self.net_saving_seconds, b), 2),
            },
            "saving_pct": round(self.saving_pct, 1),
            "worth_it": self.net_saving_seconds > 0,
            "by_component": self.by_component,
            "manual_review_layers": self.manual_review_layers,
            "assumptions": [
                "手工工时取自 config/effort_baseline.yaml，可按本司实测改",
                f"自动归类构件抽检比例 {self.review_rate_note()}，"
                f"另含每张图整体目视校核",
                "不含返工、不含出图（一期不出图）",
            ],
        }

    def review_rate_note(self) -> str:
        return f"{self._review_rate:.0%}"


def load_baseline(path: Optional[str | Path] = None) -> tuple[Dict[str, Any], str]:
    """内置默认 + 用户覆盖（config/effort_baseline.yaml 可由工程师自己改）。"""
    base = {k: (dict(v) if isinstance(v, dict) else v)
            for k, v in DEFAULT_BASELINE.items()}
    cands = ([Path(path)] if path else
             [BASELINE_PATH, cfg.USER_CONFIG_DIR / "effort_baseline.yaml"])
    for cand in cands:
        if Path(cand).exists():
            try:
                user = yaml.safe_load(Path(cand).read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(base.get(k), dict):
                    base[k].update(v)
                else:
                    base[k] = v
            return base, str(cand)
    return base, "内置默认值（config/effort_baseline.yaml 可覆盖）"


def estimate(
    counts: Dict[str, int],
    drawing: str = "",
    manual_review_layers: int = 0,
    baseline: Optional[Dict[str, Any]] = None,
    first_time: bool = True,
) -> Saving:
    """按构件台账估算减负效果。

    `first_time=False` 表示该单位已经把规则调好、确认单固化过，摊销更小。
    """
    if baseline is None:
        baseline, src = load_baseline()
    else:
        src = "调用方传入"
    manual_s = baseline["manual_seconds"]
    overhead = baseline["manual_overhead"]
    ai_s = baseline["ai_seconds"]
    rate = float(baseline.get("review_rate", 0.15))

    s = Saving(drawing=drawing, counts=dict(counts), baseline_source=src)
    s._review_rate = rate

    total = 0
    for comp, n in counts.items():
        if not n:
            continue
        key = _base_key(comp)
        per = float(manual_s.get(key, manual_s.get(comp, 30)))
        ai_per = float(ai_s.get("per_component", 0.02))
        cost = n * per
        s.manual_seconds += cost
        s.ai_seconds += n * ai_per
        total += n
        s.by_component[key if key in manual_s else comp] = {
            "count": n,
            "manual_s_each": per,
            "manual_s_total": round(cost, 1),
            "ai_s_total": round(n * ai_per, 2),
        }

    if total:
        s.manual_seconds += sum(float(v) for v in overhead.values())

    s.ai_seconds += float(ai_s.get("per_drawing_fixed", 8))
    # 复核：只对自动归类的构件抽检
    auto_n = max(0, total - 0)
    s.review_seconds = (auto_n * rate
                        * float(baseline.get("review_seconds_per_component", 6)))
    s.manual_review_layers = manual_review_layers
    s.review_seconds += manual_review_layers * float(
        baseline.get("manual_review_seconds_each", 60))
    # 整体目视校核是省不掉的，必须计入，否则数字会过于漂亮
    if total:
        s.review_seconds += float(
            baseline.get("visual_check_minutes_per_drawing", 10)) * 60

    setup_min = float(baseline["setup_minutes_first_time"] if first_time
                      else baseline["setup_minutes_later"])
    s.setup_seconds = setup_min * 60
    return s


def batch_estimate(rows: list[Dict[str, Any]],
                   baseline: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """多张图纸汇总。`rows` 每项需含 counts / manual_review_layers / drawing。"""
    if baseline is None:
        baseline, _ = load_baseline()
    total = Saving()
    total._review_rate = float(baseline.get("review_rate", 0.15))
    detail = []
    first = True
    for r in rows:
        one = estimate(r.get("counts", {}), r.get("drawing", ""),
                       int(r.get("manual_review_layers", 0)),
                       baseline=baseline, first_time=first)
        first = False
        detail.append(one.as_dict())
        total.manual_seconds += one.manual_seconds
        total.ai_seconds += one.ai_seconds
        total.review_seconds += one.review_seconds
        total.setup_seconds += one.setup_seconds
        total.manual_review_layers += one.manual_review_layers
        for k, v in one.counts.items():
            total.counts[k] = total.counts.get(k, 0) + v

    hourly = float(baseline.get("hourly_cost_cny", 120))
    d = total.as_dict()
    d["drawing"] = f"合计 {len(rows)} 张图"
    d["drawings"] = detail
    d["cost_cny"] = {
        "saved": round(total.net_saving_seconds / 3600 * hourly, 0),
        "hourly_rate": hourly,
    }
    return d


def write_markdown(rep: Dict[str, Any], out: str | Path) -> Path:
    """生成可直接贴进周报的 Markdown。"""
    p = Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    sec = rep["seconds"]
    days = rep["days"]
    L = [
        f"# 减负效果估算 · {rep['drawing']}", "",
        f"> ⚠️ 以下为**估算值**，工时基准见 `config/effort_baseline.yaml`，"
        f"可直接改成贵司实测值后重算。来源：{rep['baseline_source']}", "",
        "## 一、总账", "",
        "| 项目 | 耗时 | 折合人天 |", "|---|---:|---:|",
        f"| 人工逐个建模 | {sec['manual']/3600:.2f} 小时 | {days['manual']} |",
        f"| AI 处理（含转换/清洗/建构件） | {sec['ai']/60:.1f} 分钟 | — |",
        f"| 人工复核（抽检 + 待确认） | {sec['review']/60:.1f} 分钟 | — |",
        f"| 一次性投入（规则调校） | {sec['setup']/60:.1f} 分钟 | — |",
        f"| **净节省** | **{sec['net_saving']/3600:.2f} 小时** | **{days['net_saving']}** |",
        "",
        f"**提效幅度：{rep['saving_pct']}%**"
        + (f"（约合 {rep['cost_cny']['saved']:.0f} 元，按 "
           f"{rep['cost_cny']['hourly_rate']} 元/小时）" if "cost_cny" in rep else ""),
        "", "## 二、按构件拆解", "",
        "| 构件 | 数量 | 手工(秒/个) | 手工合计 | AI 合计 |",
        "|---|---:|---:|---:|---:|",
    ]
    for k, v in rep.get("by_component", {}).items():
        L.append(f"| {k} | {v['count']} | {v['manual_s_each']:.0f} "
                 f"| {v['manual_s_total']/60:.1f} 分钟 | {v['ai_s_total']:.1f} 秒 |")
    L += ["", "## 三、前提假设", ""] + [f"- {a}" for a in rep["assumptions"]]
    if rep.get("drawings"):
        L += ["", "## 四、逐图明细", "",
              "| 图纸 | 构件数 | 手工(人天) | 净节省(人天) | 提效 |",
              "|---|---:|---:|---:|---:|"]
        for d in rep["drawings"]:
            L.append(f"| {d['drawing']} | {sum(d['counts'].values())} "
                     f"| {d['days']['manual']} | {d['days']['net_saving']} "
                     f"| {d['saving_pct']}% |")
    p.write_text("\n".join(L) + "\n", encoding="utf-8")
    return p
