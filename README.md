# revit-wy ｜ AutoCAD → Revit 2022+ 一键翻模

把 DWG 施工图转成 Revit 构件（**支持 Revit 2022 及以上**：2022–2024 走 .NET 4.8，2025+ 走 .NET 8）：**图纸清洗 → 图层映射（数模对照确认）→ 规则建模 → 构件全自动生成**，
通过 MCP 接入 **Cursor / WorkBuddy**，也能纯命令行跑完。

> 一期范围：建筑平面 + 结构施工图，**只建模不出图**。机电仅做图层识别隔离，为二期预留。

---

## 30 秒了解它解决什么

```
DWG/DXF ──► 清洗分层 ──► 图层→构件映射 ──► 数模对照确认 ──► IR(JSON,mm) ──► Revit 构件
             (8 类语义)   (6 层匹配+别名)    (md/csv 给人工)                    (墙柱梁门窗板轴网)
```

三个关键设计：

1. **不猜**：每个图层给出置信度 + 匹配依据；置信度不足就停下来问人，`--auto` 直接中止。
2. **越用越快**：人工确认写入别名库，同一个院的图纸第二次零确认。
3. **可审计**：中间产物 `model.ir.json` 人能读，出问题时一眼分清是"图没读懂"还是"Revit 没建对"。

还有一件事不翻模也能做——**模型体检**（零风险，先试这个）：

```bat
双击 一键体检.bat                                  :: 开着 Revit 模型时，双击即可出报告
revit-wy audit model --html reports\体检.html      :: 只读巡检，出报告
revit-wy audit model --demo                        :: 没有 Revit 也能看报告长什么样
revit-wy audit fix --ids 1001,1002 --param mark --value 待补   :: 默认干跑
```

查无用/空/未放置视图、缺标记缺注释、族命名不合规、未使用类型，出健康分与整改清单。
详见 [docs/11](docs/11-模型体检与数据治理.md)。

---

## 快速开始

### 最省事：双击

| 文件 | 作用 |
|---|---|
| **`一键启动.bat`** | 装环境 → 编译插件 → 配 MCP → 自检 → 开工作台（首次约 3 分钟） |
| **`一键诊断.bat`** | 出问题双击它，生成 `诊断报告_*.txt` 发给维护者（含"你现在能用到哪一步"） |

**没有 Revit 也能试：**

```powershell
revit-wy demo        # 自动起一个假 Revit，走完全流程，看构件清单/减负账长什么样
```

本项目的清洗、对照、构件清单、差异、可视化、导出都不需要 Revit。
只有"真正把几何建进模型"这一步需要 Revit。

### 命令行

```powershell
# ★ 一条命令：DWG → 构件台账（转换/体检/对照/清洗/建构件，五阶段）
revit-wy dwg 图纸\F1.dwg

# ★ 减负账：把构件台账换算成省了多少工时
revit-wy saving 图纸\F1\F1.ir.json --md 减负报告.md

# 分步（排错用）
.\scripts\install.ps1

# 2) 重启 Revit（2022+）→ 功能区出现 "revit-wy" → 点「连接状态」
#    显示 "运行中 / 127.0.0.1:48880"

# 3) 自检
.venv\Scripts\python -m revit_wy.cli doctor

# 4) DWG 转 DXF（或 AutoCAD 手工另存为 2018 DXF）
.venv\Scripts\revit-wy dwg2dxf D:\drawings\arch

# 5) 生成数模对照单 → 人工确认
.venv\Scripts\revit-wy review D:\drawings\arch\F1.dxf -p arch
#    产物：mapping_review.md / .csv / .json / mapping_confirmed.yaml

# 6) 回灌确认
.venv\Scripts\revit-wy confirm --review D:\drawings\arch\F1\review\mapping_review.csv

# 7) 干跑 → 确认质量
.venv\Scripts\revit-wy build D:\drawings\arch\F1.dxf -p arch --dry-run

# 8) 写入 Revit
.venv\Scripts\revit-wy build D:\drawings\arch\F1.dxf -p arch --send
```

在 Cursor / WorkBuddy 里就是一句话：「帮我翻模 D:\drawings\arch\F1.dwg，先干跑」。

> **改图后重翻**：加 `--replace`，先清理上次翻模的构件再建，
> 避免两份叠加。只删带 `revit-wy` 标记的，手工建的构件不受影响。

---

## 它能帮你省多少

`revit-wy saving` 会算出可核对的工时账（基线在 `config/effort_baseline.yaml`，可改成贵司实测值）：

以一层标准办公楼层（120 墙 / 40 柱 / 60 梁 / 30 门 / 40 窗 / 12 板）为例：

| | 手工 | 用本工具 |
|---|---|---|
| 建模 | 约 4.1 小时 | 自动（约 10 秒） |
| 复核（25% 抽检 + 目视校核） | — | 约 40 分钟 |
| **合计** | **4.1 小时** | **约 0.6 小时** |

**提效约 84%**，且这个数字刻意压过：复核成本全额计入、不算返工、
小图首次使用会如实显示为**负收益**（规则调校是一次性成本）。

---

## 目录结构

```
revit-wy/
├── src/revit_wy/                  Python 侧（图纸理解）
│   ├── config.py                  配置加载与深合并
│   ├── mapping.py                 图层映射引擎（6 层匹配 + 别名）
│   ├── dwg.py                     ezdxf 读取 / 清洗 / 图元抽取
│   ├── geometry.py                双线配对、中心线、轴网、端点吸附
│   ├── ir.py                      IR 构建（Revit 中立中间表示）
│   ├── report.py                  「数模对照」报告 4 件套
│   ├── bridge.py                  TCP NDJSON 客户端
│   ├── autocad.py                 accoreconsole / ODA 批量转 DXF
│   ├── dwg_pipeline.py            ★DWG 优先五阶段流水线（阶段台账 + 失败可解释）
│   ├── diagnose.py                ★环境诊断（结论式报告，可发给支持人员）
│   ├── install_mcp.py             ★MCP 配置写入（合并，不覆盖已有）
│   ├── mock_backend.py            假 Revit 后端（无 Revit 也能全链路试跑）
│   ├── effort.py                  ★减负量化（工时基准可改）
│   ├── diffing.py                 几何签名差异 + 整体平移自动对齐
│   ├── mcp_server.py              MCP 服务（14 个工具）
│   └── cli.py                     命令行（11 个子命令）
├── addin/RevitWy.Plugin/          Revit 2022+ C# 插件（net48 / net8 双线，Compat.cs 收敛版本差异）
│   ├── UI/                         停靠面板（WPF）+ 后端适配层
│   ├── Core/                       TCP 服务 + ExternalEvent 桥
│   ├── Model/                      IR 模型 + 类型三级兜底
│   ├── Builders/                   标高/轴网/柱/墙/梁/门窗/板 + 编排器
│   └── Commands/                   方法分发 + Ribbon
├── config/
│   ├── layer_mapping/             arch / struct / mep 三套映射规则
│   └── bim_rules/default.yaml     内置建模规则默认值
├── clients/                       Cursor / WorkBuddy 配置样例
├── scripts/                       install.ps1 / build-addin.ps1
├── tools/
│   ├── mock_revit.py              协议仿真（无 Revit 也能测链路）
│   ├── mcp_bench/                 ★DeepSeek×MCP 能力边界基准 + HTML 报告
│   └── viewer/                    数模对照可视化预览（Canvas，零依赖）
├── reports/                       能力边界可视化报告（HTML，可直接给领导看）
├── tests/                         78 个测试
├── samples/                       自动生成的样例图纸
└── docs/                          方案与开发文档
```

---

## 可视化界面

**采用「浏览器 + 本地服务」**（D 方案），详见 [docs/06-可视化界面选型与实现.md](docs/06-可视化界面选型与实现.md)。

```powershell
revit-wy panel samples\arch_plan.dxf     # → http://127.0.0.1:8770/
```

浏览器工作台五个页签：

| 页签 | 能力 |
|---|---|
| 图层 | 图层清单 + 归属下拉，改完立即重算 |
| 构件 | 墙/柱/梁/门/窗/板/轴网逐条列出，**点一行 → 图上高亮 + Revit 里选中缩放** |
| 差异 | 与上次写入的模型比对（按几何签名，不是 id），改图重跑前先看影响面 |
| 规则 | 层高/墙厚/板厚/门窗尺寸热改，秒级重算 |
| 导出 | md / csv / json / yaml 四件套 |

> Revit 停靠面板保留，但只做 Revit 原生的事（选中、清理、看状态），
> 不再重复实现可视化——避免两套 UI 两处维护。

> 为什么**不**用 WebView2 嵌网页：Revit 里 WebView2 有 4 条已知崩溃路径
> （关闭 Revit 抛异常、LoadFamily 后永久失效、airspace 空白、DLL 版本冲突）。
> 浏览器载体把它们全部绕开了。

## 文档

| 文档 | 看它能知道什么 |
|---|---|
| [docs/01-方案总览.md](docs/01-方案总览.md) | **推荐方案 / 模块拆解 / 约束与风险 / 执行步骤**（主文档） |
| [docs/02-图层映射与数模对照.md](docs/02-图层映射与数模对照.md) | 映射引擎原理、8 类语义、确认流程 |
| [docs/03-Revit-2022-插件开发说明.md](docs/03-Revit-2022-插件开发说明.md) | 线程模型、事务策略、Revit API 坑 |
| [docs/04-MCP-接入指南.md](docs/04-MCP-接入指南.md) | Cursor / WorkBuddy 配置、对话脚本 |
| [docs/05-踩坑与排错.md](docs/05-踩坑与排错.md) | 错误码、排错决策树、已修问题清单 |
| [docs/06-可视化界面选型与实现.md](docs/06-可视化界面选型与实现.md) | 四种载体对比、WebView2 为何不用、面板实现 |
| **[docs/07-使用说明与能力清单.md](docs/07-使用说明与能力清单.md)** | **怎么用、能做什么、做不到什么、什么时候会停下来问你** |
| **[docs/08-DeepSeek能力边界结论.md](docs/08-DeepSeek能力边界结论.md)** | **DeepSeek 走 MCP 能操作到什么程度（含可视化报告）** |
| [docs/09-补充建议与未考虑到的点.md](docs/09-补充建议与未考虑到的点.md) | 落地一年会踩的坑、值得优先做的三件事 |
| **[docs/10-多版本支持与兼容层.md](docs/10-多版本支持与兼容层.md)** | **支持 2022 以上的做法：多目标编译、Compat 层、按版本部署** |
| **[docs/11-模型体检与数据治理.md](docs/11-模型体检与数据治理.md)** | **零风险切入：巡检无用视图/错漏参数/族命名，出清单、批量补参数** |
| **[docs/12-试点三阶段对照.md](docs/12-试点三阶段对照.md)** | **三阶段试点建议 vs 当前进度：已具备什么、缺什么、要多久** |

---

## 开发

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"

# 测试（不需要 Revit / AutoCAD）
.venv/bin/python -m pytest tests -q

# 启动协议仿真，验证 Python → 插件链路
python tools/mock_revit.py --port 48880

# 生成样例图纸
python -c "import sys; sys.path.insert(0,'tests'); from conftest import make_arch, make_struct, make_messy; \
from pathlib import Path; d=Path('samples'); d.mkdir(exist_ok=True); \
make_arch(d/'arch_plan.dxf'); make_struct(d/'struct_plan.dxf'); make_messy(d/'messy_plan.dxf')"
```

C# 插件需在 Windows 上编译（支持 Revit 2022 及以上）：

```powershell
.\scripts\build-addin.ps1                       # 编译本机装了的全部版本
.\scripts\build-addin.ps1 -Deploy               # 并按版本部署到 %APPDATA%
.\scripts\build-addin.ps1 -RevitVersions 2024 -Deploy
.\scripts\build-addin.ps1 -UseNuget -RevitVersions 2022,2025   # 无 Revit 也能编（CI）
```

版本与运行时：2022–2024 → `net48`；2025–2026 → `net8.0-windows`；2027+ → `net10.0-windows`。
**编错运行时的表现是 Revit 静默跳过插件、连报错都没有**，所以每个版本独立输出目录、独立部署。
详见 [docs/10](docs/10-多版本支持与兼容层.md)。

---

## 已知边界

| 不做 / 不支持 | 说明 |
|---|---|
| 直读 DWG | ezdxf 只支持 DXF，需先转格式 |
| 弧形墙/斜梁真实曲面 | 弧离散为 24 段线段（可配） |
| 钢筋、机电建模 | 二期；机电目前只识别隔离 |
| 出图（视图/标注/明细表） | 一期只建模 |
| 单张图多楼层自动分层 | 需图框解析，二期 |
| Revit 2021 及更早 | 只支持 2022+；更早版本 API 差异大，不做兼容 |
| 专业间碰撞检查 | 各专业分别翻模，需自己在 Revit/Navisworks 里查 |
| 净高分析、能耗、算量 | 不在一期范围 |

---

## 许可

内部项目，未开源。
