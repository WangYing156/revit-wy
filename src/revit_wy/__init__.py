"""revit-wy: AutoCAD → Revit 2022 一键翻模工具链。

模块划分
--------
config      配置加载与默认值合并
mapping     图层 → 构件 的规范映射引擎（支持不规范命名）
dwg         图纸清洗、图层统计、几何提取（ezdxf）
rules       BIM 建模规则解析
ir          Revit 中立中间表示（Intermediate Representation）
report      “数模对照”报告与确认单
bridge      与 Revit 插件通信（TCP JSON-Lines）
mcp_server  MCP 服务（stdio，供 Cursor / WorkBuddy 调用）
cli         命令行入口
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
