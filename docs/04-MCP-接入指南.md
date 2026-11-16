# MCP 接入指南（Cursor / WorkBuddy）

> 对应需求：③MCP 客户端为 Cursor 与 WorkBuddy

---

## 一、为什么 MCP Server 是独立进程

Revit 插件跑在 `Revit.exe` 进程内。如果让它直接做 stdio MCP 服务：

- Revit 的 stdio 被客户端占用 → Revit 自己的命令行/日志没地方去
- MCP 是同步阻塞语义 → 建模时 Revit UI 直接假死
- 客户端一断开，Revit 可能连带崩

所以标准做法是 **三段式**（revit-mcp 社区、本方案一致）：

```
MCP 客户端 ──stdin/stdout──► Python MCP Server ──TCP──► Revit 插件
```

Python 进程是"翻译官"：对客户端讲 MCP，对 Revit 讲 NDJSON。

---

## 二、Cursor 配置

### 2.1 项目级（推荐，配置随项目走）

`.cursor/mcp.json`：

```json
{
  "mcpServers": {
    "revit-wy": {
      "command": "python",
      "args": ["-m", "revit_wy.mcp_server", "--profile", "arch"],
      "env": { "PYTHONPATH": "${workspaceFolder}/src" }
    }
  }
}
```

### 2.2 全局

`~/.cursor/mcp.json`（Windows：`%USERPROFILE%\.cursor\mcp.json`），内容同上，
但 `PYTHONPATH` 要写**绝对路径**。

### 2.3 Windows 特别注意

Cursor 启动 MCP 服务时不继承你的 PowerShell PATH，所以 `command` 建议写绝对路径：

```json
"command": "C:\\Users\\你\\AppData\\Local\\Programs\\Python\\311\\python.exe"
```

或者直接用虚拟环境里的：

```json
"command": "D:\\revit-wy\\.venv\\Scripts\\python.exe"
```

用 venv 的 python 更好——依赖隔离，不会因为全局装了别的东西把 `ezdxf` 版本搞乱。

---

## 三、WorkBuddy 配置

### 3.1 配置文件

```
%USERPROFILE%\.workbuddy\mcp.json
```

```json
{
  "mcpServers": {
    "revit-wy": {
      "type": "stdio",
      "command": "D:\\revit-wy\\.venv\\Scripts\\python.exe",
      "args": ["-m", "revit_wy.mcp_server", "--profile", "arch"],
      "env": { "PYTHONPATH": "D:\\revit-wy\\src" },
      "defer_loading": false
    }
  }
}
```

### 3.2 两个必踩的坑

| 坑 | 说明 |
|---|---|
| **改了 `.mcp.json`（带点）** | 那个文件是程序自动生成的，会被覆盖。要改不带点的 `mcp.json` |
| **忘了点 Trust** | 写了配置但没点信任，工具列表里根本不出现，会让人误以为配置错了 |

步骤：桌面版 → 侧边栏「Experts · Skills · Connectors」→ Connectors → Custom connectors →
Add MCP → 粘贴配置 → Save → **在 MCP Server Management 里点 Trust** → 重启 WorkBuddy。

### 3.3 Web 版限制

WorkBuddy 网页版没有自定义连接器入口，**必须用桌面版**。

---

## 四、工具清单与推荐调用顺序

### 4.1 12 个工具

| # | 工具 | 输入 | 输出要点 |
|---|---|---|---|
| 1 | `rwy_doctor` | host/port | Python/ezdxf/配置/Revit 连通性 |
| 2 | `rwy_scan_drawing` | path | 单位、图幅、图层清单、图块、异常提示 |
| 3 | `rwy_review_mapping` | path, profile | **确认单 4 件套** + `need_review` + `can_auto_build` |
| 4 | `rwy_confirm_mapping` | decisions{} | 生效项 / 被拒项 / 别名数 |
| 5 | `rwy_get_rules` | — | 规则摘要（表格形式便于人读） |
| 6 | `rwy_set_rules` | overrides{} | 本次会话生效的规则摘要 |
| 7 | `rwy_save_rules` | path? | 持久化路径 |
| 8 | `rwy_prepare_build` | path, profile | IR 文件 + 构件统计 + **质量指标** |
| 9 | `rwy_build` | dry_run, confirm | 创建结果 / 校验清单 |
| 10 | `rwy_status` | host/port | Revit 文档、标高、构件数、tag 数 |
| 11 | `rwy_dwg2dxf` | folder | 批量转换结果 |
| 12 | `rwy_diff` | next_ir_path | 增量构件数 |

### 4.2 标准对话脚本（可直接抄给 AI）

**第一轮：体检**
> 帮我翻模 D:\drawings\arch\F1.dxf，先做环境自检和图纸体检。

AI 会依次调 `rwy_doctor` → `rwy_scan_drawing`。

**第二轮：数模对照**
> 生成数模对照确认单，把需要我确认的图层列出来。

AI 调 `rwy_review_mapping`，然后**你应该打开生成的 md 看一眼**（AI 的摘要可能不完整）。

**第三轮：确认**
> 图层「墙体-240」和「图层1」都是墙，「墙线2」也是墙，「门窗线」忽略，其余按你判断。

AI 调 `rwy_confirm_mapping`。

**第四轮：干跑**
> 建立一个模型清单给我看，先不要写进 Revit。

AI 调 `rwy_prepare_build` → 返回质量指标 → 如果 `unpaired_lines` 很高，让它调规则重跑。

**第五轮：写入**
> 确认无误，写入 Revit，层高按 3000，门窗用默认尺寸。

AI 调 `rwy_build(dry_run=false, confirm=true)`。

**改图后**
> Revit 里先清理上次翻模的构件，然后重新翻一遍。

AI 调 `rwy_status` 确认 → 提示你在 Revit 点「清理上次翻模」（或后续版本加 `delete_tagged` 工具暴露）。

### 4.3 安全闸设计

`rwy_build` 的三重保护，防止 AI 误触发：

```
rwy_build()                              → dry_run=true，只返回"将要创建什么"
rwy_build(dry_run=false)                 → ok=false，"安全闸未开"
rwy_build(dry_run=false, confirm=true)   → 真正写入
```

同时 `rwy_prepare_build` 永远不写模型——它只产出 IR 文件，你可以先看 JSON 再决定。

---

## 五、会话状态说明

MCP 进程内保留了本次会话的中间状态（`_S`）：

| 键 | 内容 | 生命周期 |
|---|---|---|
| `mapper` | 当前映射器（含别名） | 进程内 |
| `rules` | 当前规则（含临时覆盖） | 进程内 |
| `ir` | 最近一次构建的 IR | 进程内，供 `rwy_build` 消费 |

**这意味着 `rwy_build` 必须紧跟 `rwy_prepare_build`**，中间不要重启客户端。
如果重启了，重新调一次 `rwy_prepare_build` 即可（IR 也在磁盘上：`work/<图纸名>/model.ir.json`）。

---

## 六、多专业并存

同一台机器上同时用建筑和结构两套规则：

**方案 A：配置两个 server**（推荐，AI 自己选）

```json
{
  "mcpServers": {
    "revit-wy-arch": {
      "command": "python",
      "args": ["-m", "revit_wy.mcp_server", "--profile", "arch"]
    },
    "revit-wy-struct": {
      "command": "python",
      "args": ["-m", "revit_wy.mcp_server", "--profile", "struct"]
    }
  }
}
```

**方案 B：一个 server，调用时传 profile**

`rwy_review_mapping(path, profile="struct")`、`rwy_prepare_build(path, profile="struct")`。

> 注意：`rwy_confirm_mapping` 使用的是当前 profile 的映射器。混用时显式切 profile 更安全。

---

## 七、不用 AI 时的等价命令

所有 MCP 能力都有 CLI 等价物，方便排查"是 AI 的问题还是工具的问题"：

| MCP 工具 | CLI |
|---|---|
| `rwy_doctor` | `revit-wy doctor` |
| `rwy_scan_drawing` | `revit-wy scan a.dxf` |
| `rwy_review_mapping` | `revit-wy review a.dxf -p arch` |
| `rwy_confirm_mapping` | `revit-wy confirm --set "层=wall"` 或 `--review x.csv` |
| `rwy_prepare_build` | `revit-wy build a.dxf --dry-run` |
| `rwy_build` | `revit-wy build a.dxf --send` |
| `rwy_status` | `revit-wy status` |
| `rwy_dwg2dxf` | `revit-wy dwg2dxf <目录>` |

**排错原则**：先跑 CLI。CLI 通 → 问题在 MCP 配置；CLI 不通 → 问题在工具链。
