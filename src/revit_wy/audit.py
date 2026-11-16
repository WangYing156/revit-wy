"""模型体检：把 Revit 里的原始事实判成"问题清单 + 可执行建议"。

为什么判定放在 Python 而不是 C# 插件
------------------------------------
1. **调规则不用重编译 DLL**。设计院的命名规范、必填参数清单每家都不一样，
   今天改一条规则就要重新编译部署插件的方案，没人愿意用。
2. **可以在没有 Revit 的机器上单测**。规则是这个项目里最容易写错、
   又最容易被信任的部分，必须有测试兜住。
3. **插件只负责捞事实**。Revit 那边只做 FilteredElementCollector 能做的事，
   不做判断——判断逻辑越少，跨 Revit 版本（2022→2026）的风险越小。

三层结构
--------
    facts（插件产出） → 规则（本模块 scoring 段） → 报告（issues + 建议 + 减负估算）

规则分四类，**按风险递增排列**，也按这个顺序展示给用户：
    A. 视图卫生（无用视图 / 空视图 / 未放置 / 命名）
    B. 参数完整（缺标记 / 缺注释）
    C. 族与命名规范
    D. 重复与未使用类型

安全边界
--------
`fix()` 只允许改白名单参数（comments / mark / description），且只处理显式 id。
几何、类型、视图结构一律不碰——这三件事的风险量级完全不同，
混在一个"一键修复"里迟早出事。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set

from .effort import load_baseline

# --------------------------------------------------------------------- 规则表
# 每条规则：id / 级别 / 面向谁 / 一句话说明 / 建议动作
RULES: Dict[str, Dict[str, str]] = {
    "VIEW-001": {"level": "warn", "scope": "view", "title": "未放置在图纸上的视图",
                 "fix": "确认是否还需要；不需要的删除，需要的放到图纸上"},
    "VIEW-002": {"level": "warn", "scope": "view", "title": "空视图（无任何元素）",
                 "fix": "删除或确认是否漏画"},
    "VIEW-003": {"level": "info", "scope": "view", "title": "视图命名不符合规范",
                 "fix": "按「楼层-专业-类型」重命名，例如 3F-建-平面"},
    "VIEW-004": {"level": "warn", "scope": "view", "title": "存在孤立标注（有标注无模型）",
                 "fix": "多为删模型留标注，建议清理"},
    "PARAM-001": {"level": "warn", "scope": "element", "title": "构件缺「标记」",
                  "fix": "批量填写标记，便于出图与算量对账"},
    "PARAM-002": {"level": "info", "scope": "element", "title": "构件缺「注释」",
                  "fix": "可批量补注释（如来源图层），便于追溯"},
    "FAMILY-001": {"level": "warn", "scope": "type", "title": "族/类型命名不合规",
                   "fix": "按「专业_构件_尺寸」重命名，或重载标准族"},
    "FAMILY-002": {"level": "info", "scope": "type", "title": "类型未被任何构件使用",
                   "fix": "确认后可清理，减小模型体积"},
}

# 视图名规范：允许 楼层-专业-类型 或包含中文关键字；不合规的判定见 _view_name_ok
VIEW_NAME_ALLOWED_HINTS = (
    "平面", "立面", "剖面", "三维", "详图", "总图", "系统", "轴测",
    "平面图", "布置图", "FLOOR", "PLAN", "SECTION", "ELEVATION", "3D",
)

# 族命名允许的字符集（中文 + 字母数字 + 少量分隔符）
_FAMILY_BAD_CHARS = set(" \t\r\n/\\:*?\"<>|()（）【】[]{}#@!$%^&*+=~`，。；：、")


@dataclass
class Issue:
    rule: str
    title: str
    level: str                      # warn / info
    scope: str                      # view / element / type
    target_id: int
    target_name: str = ""
    detail: str = ""
    fix: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"rule": self.rule, "title": self.title, "level": self.level,
                "scope": self.scope, "id": self.target_id,
                "name": self.target_name, "detail": self.detail, "fix": self.fix}


@dataclass
class AuditReport:
    document: str = ""
    revit_version: str = ""
    score: int = 100
    issues: List[Issue] = field(default_factory=list)
    by_rule: Dict[str, int] = field(default_factory=dict)
    by_scope: Dict[str, int] = field(default_factory=dict)
    counts: Dict[str, int] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    effort: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "document": self.document, "revit_version": self.revit_version,
            "score": self.score, "counts": self.counts,
            "by_rule": self.by_rule, "by_scope": self.by_scope,
            "issues": [i.as_dict() for i in self.issues],
            "warnings": self.warnings, "effort": self.effort,
            "fixable": self.fixable_summary(),
        }

    def fixable_summary(self) -> Dict[str, Any]:
        """能一键批量修的有多少（只统计白名单参数）。"""
        ids = [i.target_id for i in self.issues if i.rule in ("PARAM-001", "PARAM-002")]
        return {"auto_fixable_ids": sorted(set(ids)),
                "auto_fixable_count": len(set(ids)),
                "params": ["mark", "comments"]}

    def markdown(self) -> str:
        L = [f"# 模型体检报告 · {self.document}",
             "",
             f"- Revit 版本：{self.revit_version or '未知'}",
             f"- 健康分：**{self.score} / 100**",
             f"- 问题总数：{len(self.issues)}"
             f"（警告 {sum(1 for i in self.issues if i.level == 'warn')}"
             f" / 提示 {sum(1 for i in self.issues if i.level == 'info')}）",
             "", "## 一、按规则汇总", "",
             "| 规则 | 级别 | 问题 | 数量 | 建议动作 |", "|---|---|---|---:|---|"]
        for rid, n in sorted(self.by_rule.items(), key=lambda t: -t[1]):
            r = RULES.get(rid, {})
            L.append(f"| {rid} | {r.get('level','')} | {r.get('title', rid)} | {n} | {r.get('fix','')} |")
        if not self.by_rule:
            L.append("| — | — | 未发现问题 | 0 | — |")

        L += ["", "## 二、明细（最多 200 条）", ""]
        for i in self.issues[:200]:
            L.append(f"- `{i.rule}` **{i.title}** — {i.target_name or i.target_id}"
                     + (f"：{i.detail}" if i.detail else ""))
        if len(self.issues) > 200:
            L.append(f"- …（其余 {len(self.issues) - 200} 条见 JSON）")

        fs = self.fixable_summary()
        L += ["", "## 三、可批量修正", "",
              f"可一键修正的构件：**{fs['auto_fixable_count']} 个**"
              f"（只涉及 {', '.join(fs['params'])} 参数）", "",
              "> 只改参数，不改几何、不改类型、不删视图。",
              "> 命令：`revit-wy audit fix --param mark --value 待补` 或 MCP `rwy_fix_issues`。"]
        if self.effort:
            L += ["", "## 四、这次巡检省了多少人工", ""]
            L += [f"- {k}：{v}" for k, v in self.effort.items()]
        if self.warnings:
            L += ["", "## 五、采集警告", ""] + [f"- {w}" for w in self.warnings]
        return "\n".join(L) + "\n"


# --------------------------------------------------------------------- 判定
def _view_name_ok(name: str) -> bool:
    if not name:
        return False
    up = name.upper()
    return any(h.upper() in up for h in VIEW_NAME_ALLOWED_HINTS)


def _family_name_ok(name: str) -> bool:
    if not name:
        return False
    if len(name) > 60:
        return False
    return not (set(name) & _FAMILY_BAD_CHARS)


def _level_of(rule: str) -> str:
    return RULES.get(rule, {}).get("level", "info")


def _issue(rule: str, target_id: int, name: str = "", detail: str = "") -> Issue:
    r = RULES.get(rule, {})
    return Issue(rule=rule, title=r.get("title", rule), level=r.get("level", "info"),
                 scope=r.get("scope", "element"), target_id=target_id,
                 target_name=name, detail=detail, fix=r.get("fix", ""))


def analyze(facts: Dict[str, Any],
            strict_view_names: bool = True,
            require_mark: Optional[Iterable[str]] = None,
            ignore_rules: Optional[Iterable[str]] = None) -> AuditReport:
    """facts 来自插件 `audit_model`；本函数只做判定，不改任何东西。

    `require_mark`：哪些构件类别必须有「标记」，默认墙/柱/梁/门/窗/板。
    传空集合表示不检查。
    """
    need_mark: Set[str] = set(require_mark if require_mark is not None else
                              ("walls", "columns", "beams", "doors", "windows", "slabs"))
    skip = set(ignore_rules or ())

    rep = AuditReport(document=str(facts.get("document") or ""),
                      revit_version=str(facts.get("revit_version") or ""),
                      warnings=list(facts.get("warnings") or []))

    views = facts.get("views") or []
    elements = facts.get("elements") or []
    types = facts.get("types") or []
    rep.counts = {"views": len(views), "elements": len(elements), "types": len(types)}

    # ---------------- A. 视图卫生
    for v in views:
        if v.get("is_template"):
            continue
        vid = int(v.get("id") or 0)
        name = str(v.get("name") or "")
        vtype = str(v.get("view_type") or "")
        # 三维/明细表/图例这类视图本来就不用放图纸，不判"未放置"
        skip_unplaced = vtype in ("ThreeD", "Legend", "Schedule", "DrawingSheet",
                                  "ProjectBrowser", "Internal", "Report")
        if strict_view_names and not _view_name_ok(name):
            if "VIEW-003" not in skip:
                rep.issues.append(_issue("VIEW-003", vid, name, f"类型 {vtype}"))
        if v.get("is_empty"):
            if "VIEW-002" not in skip:
                rep.issues.append(_issue("VIEW-002", vid, name, f"类型 {vtype}"))
        elif not skip_unplaced and not v.get("is_placed"):
            if "VIEW-001" not in skip:
                rep.issues.append(_issue("VIEW-001", vid, name, f"类型 {vtype}"))
        # 空视图已经由 VIEW-002 报过，不再重复报"只剩标注"
        if (not v.get("is_empty")) and v.get("has_annotations") \
                and int(v.get("element_count") or 0) <= 3:
            if "VIEW-004" not in skip:
                rep.issues.append(_issue("VIEW-004", vid, name,
                                         f"仅 {v.get('element_count')} 个元素，疑似只剩标注"))

    # ---------------- B. 参数完整
    for e in elements:
        eid = int(e.get("id") or 0)
        kind = str(e.get("kind") or "other")
        name = f"{e.get('family','')} {e.get('type_name','')}".strip() or str(e.get("category") or "")
        hm = e.get("has_mark")
        hc = e.get("has_comments")
        # has_mark 为 None 表示该类别没有这个参数，跳过（不误报）
        if hm is False and kind in need_mark:
            if "PARAM-001" not in skip:
                rep.issues.append(_issue("PARAM-001", eid, name, f"类别 {e.get('category','')}"))
        if hc is False:
            if "PARAM-002" not in skip:
                rep.issues.append(_issue("PARAM-002", eid, name, f"类别 {e.get('category','')}"))

    # ---------------- C. 族与命名
    seen_type_key: Dict[tuple, List[Dict[str, Any]]] = {}
    for t in types:
        tid = int(t.get("id") or 0)
        fam = str(t.get("family") or "")
        nm = str(t.get("name") or "")
        if not _family_name_ok(nm) or not _family_name_ok(fam):
            if "FAMILY-001" not in skip:
                rep.issues.append(_issue("FAMILY-001", tid, f"{fam} : {nm}",
                                         "含空格/括号/特殊符号或过长"))
        if int(t.get("instance_count") or 0) == 0:
            if "FAMILY-002" not in skip:
                rep.issues.append(_issue("FAMILY-002", tid, f"{fam} : {nm}", "未被使用"))
        seen_type_key.setdefault((fam, nm), []).append(t)

    # ---------------- 统计与评分
    for i in rep.issues:
        rep.by_rule[i.rule] = rep.by_rule.get(i.rule, 0) + 1
        rep.by_scope[i.scope] = rep.by_scope.get(i.scope, 0) + 1

    rep.score = _score(rep, len(elements), len(views))
    rep.effort = _effort(rep, len(elements), len(views))
    return rep


def _score(rep: AuditReport, n_elem: int, n_view: int) -> int:
    """健康分 0-100。按"每 100 个对象的问题密度"扣分，避免大模型必然低分。

    权重：警告 1.0，提示 0.3；视图问题与构件问题分开计密度。
    """
    import math

    w_warn = sum(1 for i in rep.issues if i.level == "warn")
    w_info = sum(1 for i in rep.issues if i.level == "info")
    penalty = w_warn * 1.0 + w_info * 0.3

    # 以"每 100 个对象多少个问题"为密度单位，再用指数曲线转成分数：
    #     score = 100 · exp(−密度 / 12)
    # 为什么用曲线而不是线性扣分：线性会让中等问题的模型直接掉到 0 分，
    # 分数就失去区分度了。指数曲线的性质是——
    #     密度 0    → 100 分（干净）
    #     密度 4    →  72 分（少量遗漏）
    #     密度 20   →  19 分（需要治理）
    #     密度 57   →   1 分（三分之一构件缺参数，确实很差）
    # 既能反映差距，又不会把所有不完美都压成同一个 0。
    # 分母下限 10：小模型（刚起步）也要有区分度，不能因为基数小就一动归零。
    denom = max(10.0, (n_elem + n_view) / 100.0)
    density = penalty / denom
    return max(0, min(100, round(100 * math.exp(-density / 12.0))))


def _effort(rep: AuditReport, n_elem: int, n_view: int) -> Dict[str, Any]:
    """这次巡检替你省了多少人工核对。

    诚实起见：只算"逐条人工核对"的部分，不算"修复"的时间（修复靠人判断）。
    """
    base, _ = load_baseline()
    elem_check = float(base.get("audit_seconds_per_element", 4))
    view_check = float(base.get("audit_seconds_per_view", 20))
    manual = n_elem * elem_check + n_view * view_check
    auto = max(1.0, 0.02 * (n_elem + n_view))       # 机器侧基本可忽略，仍列出
    return {
        "人工逐条核对": f"{manual / 60:.1f} 分钟" if manual < 3600 else f"{manual/3600:.2f} 小时",
        "自动巡检": f"{auto:.1f} 秒",
        "净节省": f"{(manual - auto) / 60:.1f} 分钟",
        "口径": "只算核对，不含修复；基线见 config/effort_baseline.yaml",
    }


# --------------------------------------------------------------------- 渲染
def render_html(rep: AuditReport) -> str:
    """单文件 HTML，可直接发微信/邮件给领导看。零依赖。"""
    import html as _h
    warn = sum(1 for i in rep.issues if i.level == "warn")
    info = len(rep.issues) - warn
    color = "#3ecf8e" if rep.score >= 80 else ("#f5a623" if rep.score >= 60 else "#f05a5a")
    rows = []
    for rid, n in sorted(rep.by_rule.items(), key=lambda t: -t[1]):
        r = RULES.get(rid, {})
        rows.append(f"""<tr><td class=mono>{rid}</td>
        <td><span class="tag {r.get('level','info')}">{r.get('level','')}</span></td>
        <td>{_h.escape(r.get('title', rid))}</td><td class=num>{n}</td>
        <td class=dim>{_h.escape(r.get('fix',''))}</td></tr>""")
    detail = "".join(
        f"<tr><td class=mono>{i.rule}</td><td>{_h.escape(i.title)}</td>"
        f"<td>{_h.escape(i.target_name or str(i.target_id))}</td>"
        f"<td class=dim>{_h.escape(i.detail)}</td></tr>"
        for i in rep.issues[:300])
    fs = rep.fixable_summary()
    return f"""<!DOCTYPE html><html lang=zh-CN><head><meta charset=utf-8>
<title>模型体检报告 · {_h.escape(rep.document)}</title><style>
:root{{--bg:#0f1319;--card:#171d26;--line:#28303d;--text:#e6ebf2;--dim:#8b95a5}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);
font:14px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;padding:32px 24px}}
.wrap{{max-width:1000px;margin:0 auto}}h1{{font-size:22px;margin:0 0 4px}}
h2{{font-size:15px;margin:30px 0 12px;padding-bottom:7px;border-bottom:1px solid var(--line)}}
.sub{{color:var(--dim);font-size:12.5px;margin-bottom:22px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:9px;padding:18px 20px}}
.hero{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px}}
@media(max-width:760px){{.hero{{grid-template-columns:1fr}}}}
.big{{font-size:30px;font-weight:700;line-height:1.15;color:{color}}}
.k{{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.6px}}
table{{width:100%;border-collapse:collapse;font-size:12.5px}}
th{{text-align:left;color:var(--dim);font-size:11px;padding:7px 9px;
border-bottom:1px solid var(--line);text-transform:uppercase}}
td{{padding:7px 9px;border-bottom:1px solid #1e2530;vertical-align:top}}
.mono{{font-family:ui-monospace,Consolas,monospace;font-size:11.5px}}
.num{{text-align:right;font-variant-numeric:tabular-nums}}.dim{{color:var(--dim)}}
.tag{{font-size:10.5px;padding:1px 7px;border-radius:9px}}
.tag.warn{{background:rgba(245,166,35,.14);color:#f5a623}}
.tag.info{{background:rgba(77,163,255,.14);color:#4da3ff}}
ul{{margin:6px 0;padding-left:20px}}li{{margin:3px 0;font-size:13px}}
</style></head><body><div class=wrap>
<h1>模型体检报告 · {_h.escape(rep.document)}</h1>
<div class=sub>Revit {_h.escape(rep.revit_version or '未知')} ｜
视图 {rep.counts.get('views',0)} · 构件 {rep.counts.get('elements',0)} · 类型 {rep.counts.get('types',0)}</div>
<div class=hero>
  <div class=card><div class=k>健康分</div><div class=big>{rep.score}</div><div class=dim>满分 100</div></div>
  <div class=card><div class=k>发现的问题</div><div class=big>{len(rep.issues)}</div>
    <div class=dim>警告 {warn} · 提示 {info}</div></div>
  <div class=card><div class=k>可一键批量修正</div><div class=big>{fs['auto_fixable_count']}</div>
    <div class=dim>仅 {', '.join(fs['params'])} 参数</div></div>
</div>
<h2>一、按规则汇总</h2><div class=card style="padding:6px 10px"><table>
<thead><tr><th>规则</th><th>级别</th><th>问题</th><th class=num>数量</th><th>建议动作</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan=5 class=dim>未发现问题</td></tr>'}</tbody></table></div>
<h2>二、明细（前 300 条）</h2><div class=card style="padding:6px 10px"><table>
<thead><tr><th>规则</th><th>问题</th><th>对象</th><th>说明</th></tr></thead>
<tbody>{detail or '<tr><td colspan=4 class=dim>无</td></tr>'}</tbody></table></div>
<h2>三、效率账</h2><div class=card><ul>
{''.join(f'<li>{_h.escape(k)}：<b>{_h.escape(str(v))}</b></li>' for k, v in rep.effort.items())}
</ul></div>
<h2>四、安全边界</h2><div class=card><ul>
<li>本次巡检<b>只读</b>：没有修改你的模型。</li>
<li>批量修正只动 <b>标记 / 注释 / 说明</b> 三个参数，且必须显式给出元素 id。</li>
<li>几何、类型、视图结构<b>不在</b>批量修正范围内——风险量级不同，需要单独确认。</li>
</ul></div>
</div></body></html>"""
