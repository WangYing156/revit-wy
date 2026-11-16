# revit-wy ｜ AutoCAD → Revit 2022 一键翻模

把 DWG 施工图转成 Revit 构件：**图纸清洗 → 图层映射（数模对照确认）→ 规则建模 → 构件全自动生成**，
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

---

## 快速开始

```powershell
# 1) 安装（Python 环境 + 插件部署 + MCP 配置）
.\scripts\install.ps1

# 2) 重启 Revit 2022 → 功能区出现 "revit-wy" → 点「连接状态」
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

在 Cursor / WorkBuddy 里就是一句话：「帮我翻模 D:\drawings\arch\F1.dxf」。

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
│   ├── mcp_server.py              MCP 服务（12 个工具）
│   └── cli.py                     命令行
├── addin/RevitWy.Plugin/          Revit 2022 C# 插件（net48）
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
│   └── viewer/                    数模对照可视化预览（Canvas，零依赖）
├── tests/                         33 个测试
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
| [docs/03-Revit-2022-插件开发说明.md](docs/03-Revit-2022-插件开发说明.md) | 线程模型、事务策略、Revit 2022 专有坑 |
| [docs/04-MCP-接入指南.md](docs/04-MCP-接入指南.md) | Cursor / WorkBuddy 配置、对话脚本 |
| [docs/05-踩坑与排错.md](docs/05-踩坑与排错.md) | 错误码、排错决策树、已修问题清单 |
| [docs/06-可视化界面选型与实现.md](docs/06-可视化界面选型与实现.md) | 四种载体对比、WebView2 为何不用、面板实现 |

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

C# 插件需在 Windows 上编译：

```powershell
.\scripts\build-addin.ps1              # 用本机 Revit 2022 API
.\scripts\build-addin.ps1 -UseNuget    # 无 Revit 机器（CI）
.\scripts\build-addin.ps1 -Deploy      # 编译并部署到 %APPDATA%
```

---

## 已知边界

| 不做 / 不支持 | 说明 |
|---|---|
| 直读 DWG | ezdxf 只支持 DXF，需先转格式 |
| 弧形墙/斜梁真实曲面 | 弧离散为 24 段线段（可配） |
| 钢筋、机电建模 | 二期；机电目前只识别隔离 |
| 出图（视图/标注/明细表） | 一期只建模 |
| 单张图多楼层自动分层 | 需图框解析，二期 |

---

## 许可

内部项目，未开源。
