<#
.SYNOPSIS
  revit-wy 一键安装（Python 环境 + 插件部署 + MCP 配置生成）

.EXAMPLE
  .\scripts\install.ps1 -Workspace "D:\revit-wy"
#>
param(
    [string]$Workspace = (Split-Path -Parent $PSScriptRoot),
    [string]$Python = "python",
    [switch]$SkipPlugin,
    [switch]$SkipMcp
)

$ErrorActionPreference = "Stop"

function Step($msg) { Write-Host "`n== $msg ==" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "  OK  $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "  !!  $msg" -ForegroundColor Yellow }

Step "1/4 检查 Python"
& $Python -c "import sys; assert sys.version_info >= (3,9), 'need 3.9+'; print(sys.version)"
if ($LASTEXITCODE -ne 0) { Write-Host "需要 Python 3.9+" -ForegroundColor Red; exit 1 }
Ok "Python 可用"

Step "2/4 安装 Python 依赖"
$venv = Join-Path $Workspace ".venv"
if (-not (Test-Path $venv)) { & $Python -m venv $venv }
$py = Join-Path $venv "Scripts\python.exe"
& $py -m pip install --upgrade pip | Out-Null
& $py -m pip install -e $Workspace
if ($LASTEXITCODE -ne 0) { Write-Host "依赖安装失败" -ForegroundColor Red; exit 1 }
Ok "已安装到 $venv"

Step "3/4 部署 Revit 插件"
if ($SkipPlugin) {
    Warn "已跳过（-SkipPlugin）"
} else {
    & (Join-Path $PSScriptRoot "build-addin.ps1") -Deploy
    if ($LASTEXITCODE -ne 0) { Warn "插件构建/部署失败，其余部分继续" }
}

Step "4/4 生成 MCP 配置"
if ($SkipMcp) {
    Warn "已跳过（-SkipMcp）"
} else {
    $cursorDir = Join-Path $env:USERPROFILE ".cursor"
    New-Item -ItemType Directory -Force -Path $cursorDir | Out-Null
    $cursorFile = Join-Path $cursorDir "mcp.json"
    $cfg = @{
        mcpServers = @{
            "revit-wy" = @{
                command = $py
                args = @("-m", "revit_wy.mcp_server", "--profile", "arch")
                env = @{ PYTHONPATH = (Join-Path $Workspace "src") }
            }
        }
    }
    if (Test-Path $cursorFile) {
        Copy-Item $cursorFile "$cursorFile.bak" -Force
        Warn "已有 $cursorFile，已备份为 mcp.json.bak，请手工合并"
        $cfg | ConvertTo-Json -Depth 8 | Set-Content "$cursorFile.revit-wy.json" -Encoding UTF8
        Ok "新配置写入 $cursorFile.revit-wy.json"
    } else {
        $cfg | ConvertTo-Json -Depth 8 | Set-Content $cursorFile -Encoding UTF8
        Ok "已写入 $cursorFile"
    }

    $wbDir = Join-Path $env:USERPROFILE ".workbuddy"
    New-Item -ItemType Directory -Force -Path $wbDir | Out-Null
    $wbCfg = @{
        mcpServers = @{
            "revit-wy" = @{
                type = "stdio"
                command = $py
                args = @("-m", "revit_wy.mcp_server", "--profile", "arch")
                env = @{ PYTHONPATH = (Join-Path $Workspace "src") }
            }
        }
    }
    $wbFile = Join-Path $wbDir "mcp.json"
    if (-not (Test-Path $wbFile)) {
        $wbCfg | ConvertTo-Json -Depth 8 | Set-Content $wbFile -Encoding UTF8
        Ok "已写入 $wbFile（记得在 WorkBuddy 里点 Trust）"
    } else {
        $wbCfg | ConvertTo-Json -Depth 8 | Set-Content "$wbFile.revit-wy.json" -Encoding UTF8
        Warn "已有 $wbFile，新配置在 mcp.json.revit-wy.json，请手工合并"
    }
}

Write-Host "`n安装完成。下一步：" -ForegroundColor Cyan
Write-Host "  1) 重启 Revit 2022，功能区出现 revit-wy"
Write-Host "  2) 运行  .venv\Scripts\python -m revit_wy.cli doctor"
Write-Host "  3) 在 Cursor / WorkBuddy 里让它调用 rwy_doctor"
