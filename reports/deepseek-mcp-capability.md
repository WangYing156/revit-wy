# DeepSeek × MCP × Revit 能力边界报告

- 模型：`offline-selfcheck`
- 时间：2026-11-16T06:40:55
- 通过率：**100.0%**（9/9）

## 各层级通过率

| 层级 | 名称 | 通过 | 通过率 |
|---|---|---|---:|
| L0 | L0 只读感知 | 3/3 | 100.0% |
| L1 | L1 单步操作 | 3/3 | 100.0% |
| L2 | L2 多步编排 | 1/1 | 100.0% |
| L3 | L3 异常自愈 | 1/1 | 100.0% |
| L4 | L4 规则推理 | 1/1 | 100.0% |

## 明细

| 编号 | 任务 | 结果 | 工具 | 轮次 | 耗时 |
|---|---|---|---|---:|---:|
| L0-1 | 环境自检 | 通过 | rwy_doctor | 1 | 0.002s |
| L0-2 | 图纸体检 | 通过 | rwy_scan_drawing | 1 | 0.046s |
| L0-3 | 查询已有构件 | 通过 | rwy_status | 1 | 0.001s |
| L1-1 | 生成数模对照单 | 通过 | rwy_review_mapping | 1 | 0.067s |
| L1-2 | 修改建模规则 | 通过 | rwy_set_rules | 1 | 0.0s |
| L1-3 | 干跑不落模型 | 通过 | rwy_prepare_build, rwy_build | 2 | 0.15s |
| L2-1 | 完整翻模流程 | 通过 | rwy_review_mapping, rwy_prepare_build, rwy_build | 3 | 0.239s |
| L3-1 | 错误自愈 | 通过 | rwy_build, rwy_build | 2 | 0.138s |
| L4-1 | 不规范图层推断 | 通过 | rwy_confirm_mapping | 1 | 0.0s |
