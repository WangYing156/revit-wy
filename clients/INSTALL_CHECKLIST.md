# ---------------------------------------------------------------------------
# Windows 部署清单（PowerShell 使用）
# 本文件只是对照表，实际安装请运行 scripts/install.ps1 或 scripts/build-addin.ps1
# ---------------------------------------------------------------------------

## 1. Revit 插件
目标目录（单用户，无需管理员）：
    %APPDATA%\Autodesk\Revit\Addins\2022\
        RevitWy.Plugin.addin
        revit-wy\
            RevitWy.Plugin.dll
            Newtonsoft.Json.dll
            config.json

全机器部署（需管理员）：
    C:\Program Files\Autodesk\Revit 2022\AddIns\

## 2. Python 侧
    python -m venv .venv
    .venv\Scripts\pip install -e .

用户级配置与状态目录（自动创建）：
    %USERPROFILE%\.revit-wy\
        aliases.json            # 用户确认过的图层别名（跨项目复用）
        rules\default.yaml      # 用户自定义建模规则覆盖
        work\<图纸名>\           # 每次运行的清洗结果、IR、对照单

## 3. MCP 客户端
Cursor（项目级，推荐）：
    <工程目录>\.cursor\mcp.json      ← 内容见 clients/cursor_mcp.json
Cursor（全局）：
    %USERPROFILE%\.cursor\mcp.json

WorkBuddy：
    %USERPROFILE%\.workbuddy\mcp.json ← 内容见 clients/workbuddy_mcp.json
    注意：写入后必须在 WorkBuddy「连接器」面板点击 Trust，否则工具不可调用。

## 4. 端口
默认 127.0.0.1:48880
改端口需同时改三处：
    addin\RevitWy.Plugin\config.json  (插件监听)
    clients\*.json 里 rwy_build 的默认 port 参数（或调用时显式传参）
    Python 侧 --port / rwy_doctor(port=)

## 5. 检查
    1) Revit 里出现 "revit-wy" 功能区 → 点「连接状态」，应显示“运行中”
    2) 命令行：python -m revit_wy.cli doctor  → revit_bridge.connected = true
    3) 在 Cursor 里问：调用 rwy_doctor
