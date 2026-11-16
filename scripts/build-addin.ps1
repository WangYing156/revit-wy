<#
.SYNOPSIS
  revit-wy 插件构建与部署脚本（Windows / PowerShell 5.1+）

.DESCRIPTION
  两种编译方式：
    A) 本机已装 Revit 2022（推荐设计院离线环境）
         .\scripts\build-addin.ps1
    B) 未装 Revit，走 NuGet 的 Revit API 包（CI / 云主机）
         .\scripts\build-addin.ps1 -UseNuget

.EXAMPLE
  .\scripts\build-addin.ps1 -Deploy
#>
param(
    [switch]$UseNuget,
    [switch]$Deploy,
    [string]$RevitApiDir = "C:\Program Files\Autodesk\Revit 2022",
    [string]$Configuration = "Release"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$proj = Join-Path $root "addin\RevitWy.Plugin\RevitWy.Plugin.csproj"

Write-Host "== revit-wy 插件构建 ==" -ForegroundColor Cyan
Write-Host "项目: $proj"

# --- 1. 检查 .NET SDK ---
$dotnet = Get-Command dotnet -ErrorAction SilentlyContinue
if (-not $dotnet) {
    Write-Host "未找到 dotnet SDK。请安装 .NET SDK 6.0+（可编译 net48 目标）。" -ForegroundColor Red
    Write-Host "下载: https://dotnet.microsoft.com/download" -ForegroundColor Yellow
    exit 1
}

# --- 2. 检查 Revit API ---
if (-not $UseNuget) {
    $api = Join-Path $RevitApiDir "RevitAPI.dll"
    if (-not (Test-Path $api)) {
        Write-Host "未找到 $api" -ForegroundColor Red
        Write-Host "请用 -RevitApiDir 指定 Revit 安装目录，或改用 -UseNuget" -ForegroundColor Yellow
        exit 1
    }
    Write-Host "使用本机 Revit API: $RevitApiDir" -ForegroundColor Green
} else {
    Write-Host "使用 NuGet Revit API (Nice3point)" -ForegroundColor Green
}

# --- 3. 构建 ---
$argsList = @("build", $proj, "-c", $Configuration,
              "-p:UseNugetRevitApi=$($UseNuget.IsPresent.ToString().ToLower())",
              "-p:RevitApiDir=$RevitApiDir")
if ($Deploy) { $argsList += "-p:DeployToRevit=true" }

Write-Host "dotnet $($argsList -join ' ')" -ForegroundColor DarkGray
& dotnet @argsList
if ($LASTEXITCODE -ne 0) {
    Write-Host "构建失败" -ForegroundColor Red
    exit $LASTEXITCODE
}

# --- 4. 手动部署兜底（不依赖 MSBuild Target）---
if ($Deploy) {
    $out = Join-Path $root "addin\RevitWy.Plugin\bin\$Configuration"
    $addinDir = Join-Path $env:APPDATA "Autodesk\Revit\Addins\2022"
    $deployDir = Join-Path $addinDir "revit-wy"
    New-Item -ItemType Directory -Force -Path $deployDir | Out-Null
    Copy-Item (Join-Path $out "RevitWy.Plugin.dll") $deployDir -Force
    $nj = Join-Path $out "Newtonsoft.Json.dll"
    if (Test-Path $nj) { Copy-Item $nj $deployDir -Force }
    Copy-Item (Join-Path $root "addin\RevitWy.Plugin\config.json") $deployDir -Force
    Copy-Item (Join-Path $root "addin\RevitWy.Plugin\RevitWy.Plugin.addin") $addinDir -Force
    Write-Host "已部署到 $addinDir" -ForegroundColor Green
    Write-Host "请重启 Revit 2022" -ForegroundColor Yellow
}
