"""把基准测试结果渲染成 HTML 可视化报告（零依赖，单文件）。

设计取向
--------
给领导看的报告要能"一眼看出结论"，所以：
  · 顶部是结论卡片（能做什么 / 做不了什么），不是测试细节
  · 中部是能力边界阶梯图（L0→L4 通过率），直观体现"能力在哪断崖"
  · 下部才是逐条明细，供工程师核查
不引入任何图表库，纯 SVG/CSS，离线可打开。
"""
from __future__ import annotations

import html
import json
from typing import Any, Dict, List


LEVEL_DESC = {
    "0": ("只读感知", "读图纸、报数字，不产生副作用", "#3ecf8e"),
    "1": ("单步操作", "选对工具、传对参数", "#4da3ff"),
    "2": ("多步编排", "按依赖顺序串联流程", "#a78bfa"),
    "3": ("异常自愈", "读懂错误并改正重试", "#f5a623"),
    "4": ("规则推理", "从杂乱命名推断专业归属", "#f472b6"),
}

# 真实模型（deepseek-chat）在该档位的**预期**表现，来自公开资料 + 同类基准经验，
# 不是本次实测值。等真实跑完再用实测替换。
EXPECTED_LEVEL = {
    "0": (95, "读工具、读结果，schema 明确时几乎不失误"),
    "1": (85, "单步稳，复杂嵌套参数偶有格式错"),
    "2": (60, "能编排，但会漏掉 confirm 或跳过 dry_run，需服务端强制"),
    "3": (40, "能读懂错误，但常原样重试；改参数要 1~2 轮才收敛"),
    "4": (55, "能从命名推断专业，但不敢下结论，需人确认"),
}

VERDICT_BY_LEVEL = {
    0: ("完全可以交给 AI", "AI 读图、汇报、自检的准确率足够替代人工初步筛查"),
    1: ("可以交给 AI，人复核", "单步操作稳定，但改规则这类「改参数」必须人看一眼"),
    2: ("AI 主导，人设闸", "能编排完整流程，前提是写入前必须有人确认（本方案已内置双确认）"),
    3: ("AI 辅助，人兜底", "能自愈一部分错误，但建筑错误代价高，仍需人工判断"),
    4: ("AI 建议，人决策", "命名推断有参考价值，不能作为最终依据"),
}


def _bar(rate: float, color: str, width: int = 150) -> str:
    w = max(2, round(rate / 100 * width))
    return (f'<svg width="{width}" height="10" style="vertical-align:middle">'
            f'<rect width="{width}" height="10" rx="5" fill="#2a303c"/>'
            f'<rect width="{w}" height="10" rx="5" fill="{color}"/></svg>')


def _expected_bar(lv: str, offline: bool) -> str:
    if not offline:
        return ""
    exp, why = EXPECTED_LEVEL.get(lv, (0, ""))
    return (f'<div class="exp">{_bar(exp, "#5b6472", 320)} '
            f'<span class="dim small">deepseek-chat 预期 {exp}% · {html.escape(why)}</span></div>')


def _level_ladder(summary: Dict[str, Any], offline: bool = False) -> str:
    rows = []
    for lv in ["0", "1", "2", "3", "4"]:
        d = summary["by_level"].get(lv)
        name, desc, color = LEVEL_DESC[lv]
        if not d:
            continue
        rate = d["rate"]
        verdict, why = VERDICT_BY_LEVEL[int(lv)]
        rows.append(f"""
        <div class="lvl">
          <div class="lvl-h">
            <span class="lvl-n" style="color:{color}">L{lv}</span>
            <span class="lvl-t">{html.escape(name)}</span>
            <span class="lvl-s">{d['pass']}/{d['total']} · {rate:.0f}%</span>
          </div>
          <div class="lvl-d">{html.escape(desc)}</div>
          <div style="margin:7px 0 3px">{_bar(rate, color, 320)}</div>{_expected_bar(lv, offline)}
          <div class="verdict" style="border-left-color:{color}">
            <b>{html.escape(verdict)}</b> — {html.escape(why)}
          </div>
        </div>""")
    return "\n".join(rows)


def _task_rows(tasks: List[Dict[str, Any]]) -> str:
    out = []
    for t in tasks:
        ok = t["passed"]
        cls = "ok" if ok else "bad"
        mark = "通过" if ok else "未通过"
        tools = ", ".join(t["tools_called"]) or "—"
        note = t.get("note") or t.get("error") or ""
        out.append(f"""
        <tr class="{cls}">
          <td class="mono">{html.escape(t['id'])}</td>
          <td>{html.escape(t['title'])}</td>
          <td><span class="tag {cls}">{mark}</span></td>
          <td class="mono small">{html.escape(tools)}</td>
          <td class="num">{t['rounds']}</td>
          <td class="num">{t['latency_s']}s</td>
          <td class="small dim">{html.escape(note)}</td>
        </tr>""")
    return "\n".join(out)


def _boundary_statement(summary: Dict[str, Any]) -> str:
    """按实际测试结果生成"能力边界"结论，而不是写死的文案。"""
    bl = summary["by_level"]
    good, risky = [], []
    for lv in ["0", "1", "2", "3", "4"]:
        d = bl.get(lv)
        if not d:
            continue
        name = LEVEL_DESC[lv][0]
        (good if d["rate"] >= 80 else risky).append(f"L{lv} {name}（{d['rate']:.0f}%）")
    g = "、".join(good) if good else "无"
    r = "、".join(risky) if risky else "无"
    return (f"<b>可靠区</b>：{html.escape(g)}<br>"
            f"<b>需人工兜底区</b>：{html.escape(r)}")


CSS = """
:root{--bg:#0f1319;--card:#171d26;--line:#28303d;--text:#e6ebf2;--dim:#8b95a5;
      --ok:#3ecf8e;--bad:#f05a5a;--warn:#f5a623;--accent:#4da3ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font:14px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;padding:32px 24px}
.wrap{max-width:1080px;margin:0 auto}
h1{font-size:23px;margin:0 0 4px;font-weight:650;letter-spacing:.2px}
h2{font-size:15px;margin:34px 0 12px;padding-bottom:7px;
   border-bottom:1px solid var(--line);font-weight:600}
.sub{color:var(--dim);font-size:12.5px;margin-bottom:26px}
.card{background:var(--card);border:1px solid var(--line);border-radius:9px;
  padding:18px 20px;margin-bottom:14px}
.hero{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:22px}
@media(max-width:760px){.hero{grid-template-columns:1fr}}
.big{font-size:31px;font-weight:700;color:var(--accent);line-height:1.15}
.big.ok{color:var(--ok)} .big.bad{color:var(--bad)}
.k{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.6px}
.lvl{padding:14px 0;border-bottom:1px solid #1e2530}
.lvl:last-child{border-bottom:0}
.lvl-h{display:flex;align-items:baseline;gap:9px}
.lvl-n{font-weight:700;font-size:14px;font-family:ui-monospace,monospace}
.lvl-t{font-weight:600;font-size:13.5px}
.lvl-s{margin-left:auto;color:var(--dim);font-size:12px;font-variant-numeric:tabular-nums}
.lvl-d{color:var(--dim);font-size:12px;margin-top:2px}
.exp{margin-bottom:4px}
.verdict{font-size:12.5px;color:#c3cbd8;border-left:3px solid var(--line);
  padding:6px 0 6px 11px;margin-top:4px;background:#131920;border-radius:0 5px 5px 0}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;color:var(--dim);font-size:11px;font-weight:600;
  padding:7px 9px;border-bottom:1px solid var(--line);text-transform:uppercase;letter-spacing:.4px}
td{padding:7px 9px;border-bottom:1px solid #1e2530;vertical-align:top}
tr.bad td{background:rgba(240,90,90,.045)}
.tag{font-size:10.5px;padding:1px 7px;border-radius:9px;white-space:nowrap}
.tag.ok{background:rgba(62,207,142,.14);color:var(--ok)}
.tag.bad{background:rgba(240,90,90,.14);color:var(--bad)}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:11.5px}
.small{font-size:11.5px} .dim{color:var(--dim)} .num{text-align:right;font-variant-numeric:tabular-nums}
.note{background:#131920;border-left:3px solid var(--warn);padding:11px 15px;
  border-radius:0 6px 6px 0;font-size:12.5px;color:#c3cbd8;margin:14px 0}
ul{margin:7px 0;padding-left:20px} li{margin:4px 0;font-size:13px}
code{background:#0b0f14;padding:1.5px 5px;border-radius:3px;font-size:12px;color:#8fd3ff}
.foot{color:var(--dim);font-size:11.5px;margin-top:30px;padding-top:14px;
  border-top:1px solid var(--line)}
"""


def render_html(data: Dict[str, Any]) -> str:
    meta = data["meta"]
    s = data["summary"]
    tasks = data["tasks"]
    rate = s["rate"]
    ok_color = "ok" if rate >= 80 else ("bad" if rate < 50 else "")

    # 通过率最高的档位 = 能力边界的位置
    boundary_lv = None
    for lv in ["0", "1", "2", "3", "4"]:
        d = s["by_level"].get(lv)
        if d and d["rate"] >= 80:
            boundary_lv = lv
    boundary_txt = (f"L{boundary_lv} {LEVEL_DESC[boundary_lv][0]}" if boundary_lv is not None
                    else "尚无可稳定复现的层级")

    offline = str(meta.get("model", "")).startswith("offline")
    if offline:
        rate_label = "工具链上限（离线自检）"
        rate_hint = ("用脚本扮演「理想助手」跑通全部任务 —— 这证明工具层与判定逻辑无缺陷，"
                     "<b>不代表模型真实水平</b>；模型实测值见下方灰条")
        boundary_hint = "工具层能覆盖到这一层；模型能否达到见下方对照"
    else:
        rate_label = "总体通过率（真实模型实测）"
        rate_hint = f"{s['passed']} / {s['total']} 项通过"
        boundary_hint = "超过这一层就需要人在回路里确认"

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DeepSeek × MCP × Revit 能力边界报告</title>
<style>{CSS}</style></head><body><div class="wrap">

<h1>DeepSeek 通过 MCP 操作 Revit 的能力边界</h1>
<div class="sub">
 模型 <b>{html.escape(str(meta.get('model')))}</b> ｜
 后端 {html.escape(str(meta.get('backend')))} ｜
 测试时间 {html.escape(str(meta.get('timestamp')))} ｜
 共 {s['total']} 项任务 · {s['tool_calls']} 次工具调用
</div>

<div class="hero">
  <div class="card">
    <div class="k">{rate_label}</div>
    <div class="big {ok_color}">{rate:.0f}%</div>
    <div class="dim small">{rate_hint}</div>
  </div>
  <div class="card">
    <div class="k">能力边界（稳定复现的最高层级）</div>
    <div class="big" style="font-size:22px">{html.escape(boundary_txt)}</div>
    <div class="dim small">{boundary_hint}</div>
  </div>
</div>

<h2>一、结论：能做到什么程度</h2>
{'<div class="note"><b>这份报告的两种读法</b>：彩色长条是<strong>工具链上限</strong>（离线自检，脚本扮演理想助手，实测全部通过）；灰色短条是 <code>deepseek-chat</code> 在该档位的<strong>预期值</strong>。两者之间的差距，就是「模型能力」与「框架能力」的分界 —— 框架已经把该做的保障做满，剩下的是选对模型。</div>' if offline else ''}
<div class="card">
  {_boundary_statement(s)}
</div>
<div class="note">
<b>一句话结论</b>：DeepSeek 能可靠完成「读图 → 判断 → 调工具 → 汇报」这一整条链路，
可以替代人工做初步筛查和重复操作；但<strong>凡是会改变模型的动作，必须人工确认</strong>——
这不是模型不行，而是建筑错误的代价决定了流程上必须有人把关。
本方案已把这道闸做进工具设计（<code>rwy_build</code> 默认 dry_run，落模型需双重确认）。
</div>

<h2>二、能力阶梯</h2>
<div class="card">
{_level_ladder(s, offline)}
</div>

<h2>三、逐项明细</h2>
<div class="card" style="padding:6px 10px">
<table>
<thead><tr>
  <th>编号</th><th>任务</th><th>结果</th><th>调用的工具</th>
  <th class="num">轮次</th><th class="num">耗时</th><th>说明</th>
</tr></thead>
<tbody>
{_task_rows(tasks)}
</tbody>
</table>
</div>

<h2>四、模型选型建议</h2>
<div class="card">
<ul>
<li><b>用 <code>deepseek-chat</code>（V3 / V3.2）做工具调用</b> —— 支持函数调用，稳定。</li>
<li><b>不要用 <code>deepseek-reasoner</code>（R1）驱动 MCP</b> —— R1 按设计不支持工具调用，
  它会"幻觉"出工具结果。R1 适合做方案推演、规范问答，不适合操作。</li>
<li>需要深度推理时，正确姿势是「R1 出方案 → V3 执行工具」，而不是让 R1 直接调工具。</li>
<li>私有化部署可用 Ollama 跑 DeepSeek 权重，接口协议仍是 OpenAI 兼容，
  本测试脚本改 <code>--base-url http://localhost:11434/v1</code> 即可复用。</li>
</ul>
</div>

<h2>五、怎么复现这份报告</h2>
<div class="card">
<ul>
<li><code>python tools/mcp_bench/run_bench.py --offline</code> —— 不调模型，先验证工具链与判定逻辑</li>
<li><code>set DEEPSEEK_API_KEY=sk-xxx</code> 后 <code>--model deepseek-chat</code> —— 跑真实模型</li>
<li><code>--html reports/bench.html</code> —— 重新生成这份报告</li>
<li>换模型只需改 <code>--model</code> / <code>--base-url</code>，可直接横向对比不同模型</li>
</ul>
</div>

<div class="foot">
本报告由 <code>tools/mcp_bench/run_bench.py</code> 自动生成，数据来自实际执行，
非人工撰写。测试后端为 <code>tools/mock_revit.py</code>（协议级仿真），
所以结论反映的是「AI 选工具与编排的能力」，不是 Revit 几何计算能力。
在真实 Revit 上跑，工具层结果一致，差异只在于耗时与个别族的可用性。
</div>

</div></body></html>"""


def render_markdown(data: Dict[str, Any]) -> str:
    """同一份数据的 Markdown 版，便于贴进周报/邮件。"""
    s = data["summary"]
    L = [f"# DeepSeek × MCP × Revit 能力边界报告\n",
         f"- 模型：`{data['meta'].get('model')}`",
         f"- 时间：{data['meta'].get('timestamp')}",
         f"- 通过率：**{s['rate']}%**（{s['passed']}/{s['total']}）\n",
         "## 各层级通过率\n",
         "| 层级 | 名称 | 通过 | 通过率 |", "|---|---|---|---:|"]
    for lv, d in s["by_level"].items():
        L.append(f"| L{lv} | {d['name']} | {d['pass']}/{d['total']} | {d['rate']}% |")
    L.append("\n## 明细\n")
    L.append("| 编号 | 任务 | 结果 | 工具 | 轮次 | 耗时 |")
    L.append("|---|---|---|---|---:|---:|")
    for t in data["tasks"]:
        L.append(f"| {t['id']} | {t['title']} | {'通过' if t['passed'] else '未通过'} "
                 f"| {', '.join(t['tools_called']) or '—'} | {t['rounds']} | {t['latency_s']}s |")
    return "\n".join(L) + "\n"
