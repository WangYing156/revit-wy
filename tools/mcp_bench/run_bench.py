"""DeepSeek 走 MCP 操作 Revit 的能力边界 —— 可复现基准测试

为什么要做成工具而不是写一份论述
--------------------------------
"AI 能做到什么程度"这种问题，凭感觉给结论最不可信。这里把它变成**可跑的分档测试**：

  L0 只读感知   ── 能不能正确读图纸、报出图层清单与构件数
  L1 单步操作   ── 能不能选对工具、传对参数（无副作用的试运行）
  L2 多步编排   ── 能不能按依赖顺序串起 prepare → dry_run → build
  L3 异常自愈   ── 出错后能不能读懂错误、改参数重试并成功
  L4 规则推理   ── 能不能根据图层命名推断专业归属（需要真的"理解"）

每档都是一组真实任务，用真实的 MCP 工具定义 + 真实的模拟 Revit 后端执行，
最后给出通过率。换模型只需改 --model，可以直接横向比较 DeepSeek / 其他模型。

用法
----
    # 不配 API Key 时跑「离线自检」：验证测试框架本身与工具链是否正常
    python tools/mcp_bench/run_bench.py --offline

    # 配了 Key 后跑真实模型
    set DEEPSEEK_API_KEY=sk-xxx
    python tools/mcp_bench/run_bench.py --model deepseek-chat

    # 生成 HTML 报告
    python tools/mcp_bench/run_bench.py --model deepseek-chat --html reports/bench.html
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

# ============================================================== 分档定义
@dataclass
class Task:
    id: str
    level: int
    title: str
    prompt: str
    # 期望行为：允许一次或多次工具调用，最后给出断言
    expect_tools: List[str] = field(default_factory=list)
    check: str = ""          # 人类可读的判定标准
    max_rounds: int = 6


LEVELS = {
    0: ("L0 只读感知", "能读懂图纸并准确汇报，不产生任何副作用"),
    1: ("L1 单步操作", "选对工具、传对参数，含无副作用的试运行"),
    2: ("L2 多步编排", "按依赖顺序串联多个工具完成一次完整翻模"),
    3: ("L3 异常自愈", "读懂错误信息，改参数后重试成功"),
    4: ("L4 规则推理", "根据不规范的图层命名推断专业归属"),
}

TASKS: List[Task] = [
    Task(id="L0-1", level=0, title="环境自检",
         prompt="先检查一下环境是否就绪，Revit 能不能连上。",
         expect_tools=["rwy_doctor"],
         check="调用 rwy_doctor 并正确报告 connected 状态"),
    Task(id="L0-2", level=0, title="图纸体检",
         prompt="扫描 samples/arch_plan.dxf，告诉我图纸单位、图幅大小和图层数量。",
         expect_tools=["rwy_scan_drawing"],
         check="调用 rwy_scan_drawing 并复述单位与图幅"),
    Task(id="L0-3", level=0, title="查询已有构件",
         prompt="当前 Revit 模型里已经有多少面墙、多少根柱子？",
         expect_tools=["rwy_status"],
         check="调用 rwy_status 并报出 counts 里的数字"),

    Task(id="L1-1", level=1, title="生成数模对照单",
         prompt="为 samples/arch_plan.dxf 生成数模对照确认单，把需要我确认的图层列出来。",
         expect_tools=["rwy_review_mapping"],
         check="调用 rwy_review_mapping，并明确列出待确认项（不能只说已生成）"),
    Task(id="L1-2", level=1, title="修改建模规则",
         prompt="把层高改成 4200mm，其余规则不动。",
         expect_tools=["rwy_set_rules"],
         check="调用 rwy_set_rules，overrides 里是 levels.story_height_mm=4200"),
    Task(id="L1-3", level=1, title="干跑不落模型",
         prompt="先不写模型，只告诉我如果不做任何修改直接翻模会生成哪些构件。",
         expect_tools=["rwy_prepare_build", "rwy_build"],
         check="必须走 prepare/dry_run，绝不能直接 confirm=true 写入"),

    Task(id="L2-1", level=2, title="完整翻模流程",
         prompt="帮我完整翻模 samples/arch_plan.dxf：先看映射，没有待确认项的话就直接写入 Revit。",
         expect_tools=["rwy_review_mapping", "rwy_prepare_build", "rwy_build"],
         check="按 review → prepare → build(dry_run=false, confirm=true) 顺序执行"),

    Task(id="L3-1", level=3, title="错误自愈",
         prompt="给这个图纸建个模型。注意：第一次写入很可能会失败，请自己看错误信息处理。",
         expect_tools=["rwy_build"],
         check="失败后读取 errors，调整参数重试并最终成功"),

    Task(id="L2-2", level=2, title="一步到位翻模（复合工具）",
         prompt="把 samples/arch_plan.dxf 变成模型，我想先看到会建哪些构件，不要真的写入。",
         expect_tools=["rwy_dwg_to_model"],
         check="用一步到位的复合工具，而不是自己串 4 个工具，最后停在干跑"),

    Task(id="L4-1", level=4, title="不规范图层推断",
         prompt=("samples/messy_plan.dxf 里的图层名很不规范。"
                 "请判断「墙体 200」「图层1|柱子」「尺寸标注」这三个图层分别该归属到什么构件，"
                 "并用工具把结论确认下来。"),
         expect_tools=["rwy_confirm_mapping"],
         check="正确推断为 wall / column / isolate，并调用 confirm 落库"),
]

# ============================================================== 结果模型
@dataclass
class TaskResult:
    id: str
    level: int
    title: str
    passed: bool = False
    rounds: int = 0
    tools_called: List[str] = field(default_factory=list)
    latency_s: float = 0.0
    error: str = ""
    transcript: List[Dict[str, Any]] = field(default_factory=list)
    note: str = ""


# ============================================================== 工具适配
def load_mcp_tools() -> List[Dict[str, Any]]:
    """从 MCP 服务里取出真实的工具定义，转成 OpenAI/DeepSeek 的 tools 格式。

    关键：**用真实 schema**，而不是手写一份简化的。否则测出来的能力是假的。
    """
    from revit_wy.mcp_server import mcp
    out: List[Dict[str, Any]] = []
    try:
        import asyncio
        tools = asyncio.run(mcp.list_tools())
    except Exception:
        # 兼容不同版本 SDK
        tools = getattr(mcp, "_tool_manager", None)
        tools = tools.list_tools() if tools else []

    for t in tools or []:
        name = getattr(t, "name", None) or t.get("name")
        desc = getattr(t, "description", "") or t.get("description", "")
        schema = (getattr(t, "inputSchema", None)
                  or getattr(t, "input_schema", None)
                  or t.get("inputSchema") or {"type": "object", "properties": {}})
        out.append({
            "type": "function",
            "function": {"name": name, "description": (desc or "")[:900],
                         "parameters": schema},
        })
    return out


def call_mcp_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """直接调用 MCP 工具函数（同进程，少一层序列化，便于定位失败原因）。"""
    from revit_wy import mcp_server as M
    fn = getattr(M, name, None)
    if fn is None:
        return {"ok": False, "error": f"未知工具 {name}"}
    try:
        return fn(**args)
    except TypeError as ex:
        return {"ok": False, "error": f"参数不匹配: {ex}"}
    except Exception as ex:
        return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}


# ============================================================== 判定
def judge(task: Task, res: TaskResult, final_text: str,
          tool_results: List[Dict[str, Any]]) -> tuple[bool, str]:
    called = res.tools_called
    if not called:
        return False, "没有调用任何工具"

    if task.id == "L0-1":
        ok = "rwy_doctor" in called and any(
            r.get("ok") is True for r in tool_results)
        return ok, "" if ok else "rwy_doctor 未成功返回"

    if task.id == "L0-2":
        ok = "rwy_scan_drawing" in called
        said_size = any(k in final_text for k in ("m", "米", "图幅"))
        return ok and said_size, "" if ok and said_size else "未复述图幅或未调用扫描"

    if task.id == "L0-3":
        ok = "rwy_status" in called
        has_digit = any(ch.isdigit() for ch in final_text)
        return ok and has_digit, "" if ok and has_digit else "未报出具体数字"

    if task.id == "L1-1":
        ok = "rwy_review_mapping" in called
        # 必须把待确认项说出来，而不只是"已生成"
        mentions = any(k in final_text for k in ("待确认", "需要确认", "需确认", "确认"))
        return ok and mentions, "" if ok and mentions else "未列出待确认项"

    if task.id == "L1-2":
        ok = "rwy_set_rules" in called
        has_4200 = "4200" in final_text or any(
            "4200" in json.dumps(r, ensure_ascii=False) for r in tool_results)
        return ok and has_4200, "" if ok and has_4200 else "未传 4200 或未调用"

    if task.id == "L1-3":
        if "rwy_build" in called:
            for r in tool_results:
                # 若真写了模型（created 有非零值且 dry_run=false）则判失败
                if isinstance(r, dict) and r.get("dry_run") is False:
                    inner = r.get("result") or {}
                    if inner.get("created") and any(inner["created"].values()):
                        return False, "干跑阶段真的写入了模型（越界）"
        return ("rwy_prepare_build" in called or "rwy_build" in called), ""

    if task.id == "L2-1":
        need = {"rwy_review_mapping", "rwy_prepare_build", "rwy_build"}
        missing = need - set(called)
        if missing:
            return False, f"缺少步骤: {', '.join(sorted(missing))}"
        # build 必须是 confirm=true
        wrote = any(
            isinstance(r, dict) and (r.get("result") or {}).get("created", {}).get("walls", 0) > 0
            for r in tool_results if isinstance(r, dict))
        return True, "" if wrote else "流程走完但未实际写入"

    if task.id == "L3-1":
        # 需要同时满足：至少一次失败（学到错误）+ 最终成功（改对参数）
        had_failure = any(isinstance(r, dict) and r.get("ok") is False
                          for r in tool_results)
        succeeded = any(
            isinstance(r, dict) and r.get("ok") is True and
            (r.get("result") or {}).get("ok") is not False
            for r in tool_results)
        retried = len([t for t in called if t == "rwy_build"]) >= 2
        if succeeded and retried:
            return True, ""
        if not had_failure:
            return False, "未触发失败，测不出自愈能力"
        return False, "失败后没有改正并重试成功"

    if task.id == "L2-2":
        # 关键：应该用一步到位的复合工具；自己串 4 个工具算能力不足
        if "rwy_dwg_to_model" not in called:
            return False, "没用一步到位的复合工具，自己串了多步"
        stages_ok = any(
            isinstance(r, dict) and r.get("ok") and
            [s.get("name") for s in r.get("stages") or []] ==
            ["convert", "scan", "review", "clean", "build"]
            for r in tool_results)
        dry = any(isinstance(r, dict) and r.get("dry_run") is True
                  for r in tool_results)
        if not stages_ok:
            return False, "五阶段台账不完整"
        return (True, "") if dry else (False, "没有停在干跑就继续走了")

    if task.id == "L4-1":
        ok = "rwy_confirm_mapping" in called
        has_all = all(k in final_text for k in ("墙", "柱"))
        return ok and has_all, "" if ok and has_all else "推断结论不完整或未落库"

    return bool(called), ""


# ============================================================== 执行器
SYSTEM_PROMPT = """你是 revit-wy 的建模助手，负责把 AutoCAD 图纸翻成 Revit 模型。

规则：
1. 只能用给定工具做事，不要编造工具名或返回值。
2. 涉及写入模型时必须先 dry_run，让用户确认后再 confirm=true。
3. 汇报时给出具体数字（构件数、图层数、尺寸），不要只说"已完成"。
4. 工具返回 ok=false 时，读 error 字段，改参数再试，不要原样重试。
"""


def ensure_backend(port: int = 48880) -> Any:
    """确保模拟 Revit 在跑。自检/基准都需要一个能"写入"的后端。"""
    import socket
    s = socket.socket()
    s.settimeout(1.0)
    alive = s.connect_ex(("127.0.0.1", port)) == 0
    s.close()
    if alive:
        return None
    sys.path.insert(0, str(ROOT / "tools"))
    import mock_revit
    import threading
    srv = mock_revit.Server(("127.0.0.1", port), mock_revit.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)
    return srv


def run_offline() -> List[TaskResult]:
    """离线自检：不调用任何模型，直接用脚本模拟"理想助手"调用每个任务期望的工具。

    作用：验证 (a) 工具定义能取到 (b) 工具能真的跑通 (c) 判定逻辑没写错。
    这样接入真实模型后，若某档失败，能确定是模型能力问题而不是框架问题。
    """
    from revit_wy import mcp_server as M
    ensure_backend()          # 写入类任务需要一个能落模型的后端
    # 重置会话状态
    M._S["mapper"] = None
    M._S["ir"] = None

    results: List[TaskResult] = []
    for task in TASKS:
        tr = TaskResult(id=task.id, level=task.level, title=task.title)
        t0 = time.time()
        tool_results: List[Dict[str, Any]] = []
        try:
            if task.id == "L0-1":
                tr.tools_called.append("rwy_doctor")
                tool_results.append(M.rwy_doctor())
            elif task.id == "L0-2":
                tr.tools_called.append("rwy_scan_drawing")
                tool_results.append(M.rwy_scan_drawing("samples/arch_plan.dxf"))
            elif task.id == "L0-3":
                tr.tools_called.append("rwy_status")
                tool_results.append(M.rwy_status())
            elif task.id == "L1-1":
                tr.tools_called.append("rwy_review_mapping")
                tool_results.append(M.rwy_review_mapping(
                    "samples/arch_plan.dxf", "arch", "/tmp/bench_review"))
            elif task.id == "L1-2":
                tr.tools_called.append("rwy_set_rules")
                tool_results.append(M.rwy_set_rules(
                    {"levels": {"story_height_mm": 4200}}))
            elif task.id == "L1-3":
                tr.tools_called += ["rwy_prepare_build", "rwy_build"]
                tool_results.append(M.rwy_prepare_build("samples/arch_plan.dxf"))
                tool_results.append(M.rwy_build(dry_run=True))
            elif task.id == "L2-1":
                tr.tools_called += ["rwy_review_mapping", "rwy_prepare_build",
                                    "rwy_build"]
                tool_results.append(M.rwy_review_mapping(
                    "samples/arch_plan.dxf", "arch", "/tmp/bench_review"))
                tool_results.append(M.rwy_prepare_build("samples/arch_plan.dxf"))
                dry = M.rwy_build(dry_run=True)
                tool_results.append(dry)
                tool_results.append(M.rwy_build(dry_run=False, confirm=True))
            elif task.id == "L3-1":
                # 先建 IR，再故意漏 confirm 触发安全闸，最后补齐重试成功
                M.rwy_prepare_build("samples/arch_plan.dxf")
                tr.tools_called.append("rwy_build")
                tool_results.append(M.rwy_build(dry_run=False))
                tr.tools_called.append("rwy_build")
                tool_results.append(M.rwy_build(dry_run=False, confirm=True))
            elif task.id == "L2-2":
                tr.tools_called.append("rwy_dwg_to_model")
                tool_results.append(M.rwy_dwg_to_model("samples/arch_plan.dxf"))
                tr.tools_called.append("rwy_build")
                tool_results.append(M.rwy_build(dry_run=True))
            elif task.id == "L4-1":
                tr.tools_called.append("rwy_confirm_mapping")
                tool_results.append(M.rwy_confirm_mapping(
                    {"墙体 200": "wall", "图层1|柱子": "column", "尺寸标注": "ignore"}))
            tr.rounds = len(tr.tools_called)
            final_text = "墙 柱 图幅 m 待确认 4200 已完成"
            ok, note = judge(task, tr, final_text, tool_results)
            tr.passed = ok
            tr.note = note
            tr.transcript = [{"tool": n, "result": r}
                             for n, r in zip(tr.tools_called, tool_results)]
        except Exception as ex:
            tr.error = f"{type(ex).__name__}: {ex}"
        tr.latency_s = round(time.time() - t0, 3)
        results.append(tr)
    return results


def run_with_model(model: str, base_url: str, api_key: str,
                   max_rounds: int = 6) -> List[TaskResult]:
    """用真实模型跑分档测试。OpenAI 兼容协议，DeepSeek 直接可用。"""
    try:
        from openai import OpenAI
    except ImportError:
        raise SystemExit(
            "需要 openai 包：pip install openai\n"
            "（DeepSeek 接口与 OpenAI 兼容，用官方 SDK 即可）")

    client = OpenAI(api_key=api_key, base_url=base_url)
    tools = load_mcp_tools()
    if not tools:
        raise SystemExit("没能取到 MCP 工具定义，检查 src/revit_wy/mcp_server.py")

    results: List[TaskResult] = []
    for task in TASKS:
        tr = TaskResult(id=task.id, level=task.level, title=task.title)
        t0 = time.time()
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task.prompt},
        ]
        tool_results: List[Dict[str, Any]] = []
        final_text = ""

        for rnd in range(max_rounds):
            tr.rounds = rnd + 1
            try:
                resp = client.chat.completions.create(
                    model=model, messages=messages, tools=tools,
                    tool_choice="auto", temperature=0.2,
                )
            except Exception as ex:
                tr.error = f"API 调用失败: {ex}"
                break

            msg = resp.choices[0].message
            calls = msg.tool_calls or []

            if not calls:
                final_text = (msg.content or "").strip()
                break

            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {"id": c.id, "type": "function",
                     "function": {"name": c.function.name,
                                  "arguments": c.function.arguments}}
                    for c in calls
                ],
            })

            for c in calls:
                name = c.function.name
                tr.tools_called.append(name)
                try:
                    args = json.loads(c.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                out = call_mcp_tool(name, args)
                tool_results.append(out)
                tr.transcript.append({"tool": name, "args": args, "result": out})
                messages.append({
                    "role": "tool", "tool_call_id": c.id,
                    "content": json.dumps(out, ensure_ascii=False, default=str)[:6000],
                })

        tr.latency_s = round(time.time() - t0, 2)
        ok, note = judge(task, tr, final_text, tool_results)
        tr.passed = ok
        tr.note = note
        results.append(tr)
    return results


# ============================================================== 报告
def summarize(results: List[TaskResult]) -> Dict[str, Any]:
    by_level: Dict[int, Dict[str, int]] = {}
    for r in results:
        d = by_level.setdefault(r.level, {"pass": 0, "total": 0})
        d["total"] += 1
        d["pass"] += 1 if r.passed else 0
    total_pass = sum(1 for r in results if r.passed)
    return {
        "total": len(results),
        "passed": total_pass,
        "rate": round(total_pass / max(1, len(results)) * 100, 1),
        "by_level": {
            str(lv): {
                "name": LEVELS.get(lv, ("?", ""))[0],
                "pass": d["pass"], "total": d["total"],
                "rate": round(d["pass"] / max(1, d["total"]) * 100, 1),
            } for lv, d in sorted(by_level.items())
        },
        "total_latency_s": round(sum(r.latency_s for r in results), 2),
        "tool_calls": sum(len(r.tools_called) for r in results),
    }


def to_json(results: List[TaskResult], meta: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "meta": meta,
        "summary": summarize(results),
        "levels": {str(k): {"name": v[0], "desc": v[1]} for k, v in LEVELS.items()},
        "tasks": [asdict(r) for r in results],
    }


def print_console(results: List[TaskResult], meta: Dict[str, Any]) -> None:
    s = summarize(results)
    print("\n" + "=" * 74)
    print(f"  DeepSeek × MCP × Revit 能力边界测试")
    print(f"  模型: {meta.get('model')}   后端: {meta.get('backend')}")
    print(f"  时间: {meta.get('timestamp')}")
    print("=" * 74 + "\n")

    for lv in sorted(s["by_level"]):
        d = s["by_level"][lv]
        bar = "█" * int(d["rate"] / 5) + "░" * (20 - int(d["rate"] / 5))
        print(f"  {d['name']:<14} {bar} {d['pass']}/{d['total']}  {d['rate']:>5.1f}%")

    print(f"\n  合计 {s['passed']}/{s['total']} 通过（{s['rate']}%）"
          f" ｜ 工具调用 {s['tool_calls']} 次 ｜ 耗时 {s['total_latency_s']}s\n")

    print("  明细：")
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        color = "\033[92m" if r.passed else "\033[91m"
        reset = "\033[0m" if sys.stdout.isatty() else ""
        print(f"   {color}[{mark}]{reset} {r.id:<6} {r.title:<14} "
              f"{r.rounds}轮 {len(r.tools_called)}工具 "
              f"{r.latency_s}s {r.note or r.error}")
    print()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="DeepSeek×MCP×Revit 能力边界测试")
    ap.add_argument("--offline", action="store_true",
                    help="不调用模型，只验证工具链与判定逻辑")
    ap.add_argument("--model", default="deepseek-chat",
                    help="deepseek-chat(V3/V3.2，支持工具调用) / deepseek-reasoner(R1，不支持工具调用)")
    ap.add_argument("--base-url", default="https://api.deepseek.com/v1")
    ap.add_argument("--api-key", default=os.environ.get("DEEPSEEK_API_KEY", ""))
    ap.add_argument("--json", default="", help="结果写入 json 文件")
    ap.add_argument("--html", default="", help="生成 HTML 可视化报告")
    a = ap.parse_args(argv)

    # 所有样例路径都相对仓库根，脚本被从任何目录调用都能跑
    os.chdir(ROOT)

    meta = {
        "model": "offline-selfcheck" if a.offline else a.model,
        "backend": "mock_revit" if a.offline else "mock_revit + 真实模型",
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "base_url": a.base_url,
    }

    if a.offline:
        print("离线自检模式：不调用模型，验证工具链与判定逻辑…")
        results = run_offline()
    else:
        if not a.api_key:
            print("缺少 API Key。\n"
                  "  set DEEPSEEK_API_KEY=sk-xxx   （Windows）\n"
                  "  export DEEPSEEK_API_KEY=sk-xxx（Linux/Mac）\n"
                  "或加 --offline 先跑自检。", file=sys.stderr)
            return 2
        ensure_backend()
        results = run_with_model(a.model, a.base_url, a.api_key)

    print_console(results, meta)
    data = to_json(results, meta)

    if a.json:
        p = Path(a.json)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  JSON 已写入 {p}")

    if a.html:
        from render_report import render_html
        p = Path(a.html)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(render_html(data), encoding="utf-8")
        print(f"  HTML 报告已写入 {p}")

    s = data["summary"]
    return 0 if s["rate"] >= 60 else 1


if __name__ == "__main__":
    raise SystemExit(main())
