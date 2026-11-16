<#
.SYNOPSIS
  revit-wy 插件构建与部署脚本（Windows / PowerShell 5.1+）

.DESCRIPTION
  支持 Revit 2022 及以上。一次可以编译多个版本，每个版本输出到独立目录，
  部署到对应的 %APPDATA%\Autodesk\Revit\Addins\<版本>。

  版本与运行时：
    2022–2024 → net48
    2025–2026 → net8.0-windows（需要 .NET SDK 8+）
    2027+     → net10.0-windows（需要 .NET SDK 10+，预留）

.PARAMETER RevitVersions
  要编译的版本列表。默认自动探测本机已安装的 Revit（2022 及以上）。
  也可显式指定，例如 -RevitVersions 2022,2024,2025

.PARAMETER UseNuget
  用 NuGet 的 Revit API 包编译。**不需要装 .NET Framework 开发包，也不需要装 Revit**，
  是最省事的路径（尤其是只装了 .NET 8/9/10 SDK 的新机器）。
  net48 目标下还会自动生成 app.config 绑定重定向，避免 Newtonsoft 版本冲突。

.EXAMPLE
  .\scripts\build-addin.ps1                      # 编译本机装了的全部版本
.EXAMPLE
  .\scripts\build-addin.ps1 -Deploy              # 编译并部署到各版本目录
.EXAMPLE
  .\scripts\build-addin.ps1 -RevitVersions 2024 -Deploy
.EXAMPLE
  .\scripts\build-addin.ps1 -UseNuget -RevitVersions 2022,2025
#>
param(
    [int[]]$RevitVersions = @(),
    [switch]$UseNuget,
    [switch]$Deploy,
    [string]$RevitApiDir = "",
    [string]$Configuration = "Release"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$proj = Join-Path $root "addin\RevitWy.Plugin\RevitWy.Plugin.csproj"
$minVersion = 2022

Write-Host "== revit-wy 插件构建 ==" -ForegroundColor Cyan
Write-Host "项目: $proj"

# --- 1. 检查 .NET SDK ---
$dotnetCmd = Get-Command dotnet -ErrorAction SilentlyContinue
if (-not $dotnetCmd) {
    Write-Host "未找到 dotnet SDK。请安装 .NET SDK（net48 用 SDK 6+，net8 用 SDK 8+）。" -ForegroundColor Red
    Write-Host "下载: https://dotnet.microsoft.com/download" -ForegroundColor Yellow
    exit 1
}
$sdkList = & dotnet --list-sdks 2>$null
$sdkMajor = 0
foreach ($line in $sdkList) {
    if ($line -match '^(\d+)\.') { if ([int]$Matches[1] -gt $sdkMajor) { $sdkMajor = [int]$Matches[1] } }
}
Write-Host "SDK 最高主版本: $sdkMajor" -ForegroundColor DarkGray

# --- 1b. net48 引用程序集（MSB3644 的根因）---
# 只要编 2022–2024 就需要它。但走 -UseNuget 时也仍然需要（net48 是目标框架），
# 所以把它单独探一次，提前告诉用户该走哪条路，而不是等 MSBuild 抛英文错误码。
$fwRefDir = $null
$candidates = @(
    "$env:ProgramFiles(x86)\Reference Assemblies\Microsoft\Framework\.NETFramework\v4.8",
    "$env:ProgramFiles\Reference Assemblies\Microsoft\Framework\.NETFramework\v4.8"
)
foreach ($c in $candidates) {
    if ($c -and (Test-Path (Join-Path $c "mscorlib.dll"))) { $fwRefDir = $c; break }
}
if ($fwRefDir) {
    Write-Host "net48 引用程序集: $fwRefDir" -ForegroundColor DarkGray
} else {
    Write-Host "未找到 .NET Framework 4.8 引用程序集（编 Revit 2022-2024 需要）" -ForegroundColor Yellow
}

# --- 2. 决定要编译哪些版本 ---
function Get-InstalledRevitVersions {
    $base = "C:\Program Files\Autodesk"
    $out = @()
    if (Test-Path $base) {
        Get-ChildItem $base -Directory -Filter "Revit *" | ForEach-Object {
            $v = $_.Name -replace '^Revit\s+', ''
            if ($v -match '^\d{4}$' -and [int]$v -ge $minVersion -and
                (Test-Path (Join-Path $_.FullName "RevitAPI.dll"))) {
                $out += [int]$v
            }
        }
    }
    return $out | Sort-Object
}

if ($RevitVersions.Count -eq 0) {
    if ($UseNuget) {
        # 没装 Revit 时也能编译：默认编最新的两条运行时线
        $RevitVersions = @(2022, 2025)
        Write-Host "未指定版本，使用 NuGet 模式默认: $($RevitVersions -join ', ')" -ForegroundColor DarkGray
    } else {
        $RevitVersions = Get-InstalledRevitVersions
        if ($RevitVersions.Count -eq 0) {
            Write-Host "本机没有检测到 Revit $minVersion 及以上（需要 RevitAPI.dll）。" -ForegroundColor Red
            Write-Host "  1) 装 Revit 后再跑本脚本；或" -ForegroundColor Yellow
            Write-Host "  2) 用 -UseNuget（无需装 Revit）：" -ForegroundColor Yellow
            Write-Host "     .\scripts\build-addin.ps1 -UseNuget -RevitVersions 2022,2025" -ForegroundColor Yellow
            exit 1
        }
    }
}
Write-Host "目标版本: $($RevitVersions -join ', ')" -ForegroundColor Green

# --- 3. 逐版本编译 ---
$results = @()
foreach ($v in $RevitVersions) {
    $tf = if ($v -ge 2027) { "net10.0-windows" } elseif ($v -ge 2025) { "net8.0-windows" } else { "net48" }
    $needSdk = if ($tf -like "net10*") { 10 } elseif ($tf -like "net8*") { 8 } else { 6 }

    Write-Host ""
    Write-Host "--- Revit $v  ($tf) ---" -ForegroundColor Cyan

    if ($sdkMajor -lt $needSdk) {
        Write-Host "  跳过：需要 .NET SDK $needSdk+（当前 $sdkMajor）。装好 SDK 后重跑。" -ForegroundColor Yellow
        $results += [pscustomobject]@{ Version = $v; TF = $tf; Ok = $false; Reason = "SDK $needSdk+ 缺失" }
        continue
    }

    if ($tf -eq "net48" -and -not $fwRefDir) {
        Write-Host "  跳过：缺 .NET Framework 4.8 引用程序集（MSB3644）" -ForegroundColor Red
        Write-Host "    三选一，任选其一即可：" -ForegroundColor Yellow
        Write-Host "      1) 装 .NET Framework 4.8 Developer Pack（体积小，只补引用程序集）" -ForegroundColor Yellow
        Write-Host "         https://dotnet.microsoft.com/download/dotnet-framework/net48" -ForegroundColor Yellow
        Write-Host "      2) 装 Visual Studio Build Tools，勾选 \".NET Framework 4.8 targeting pack\"" -ForegroundColor Yellow
        Write-Host "      3) 只编新版 Revit：.\scripts\build-addin.ps1 -RevitVersions 2025 -Deploy" -ForegroundColor Yellow
        $results += [pscustomobject]@{ Version = $v; TF = $tf; Ok = $false; Reason = "缺 net48 引用程序集" }
        continue
    }

    $apiDir = $RevitApiDir
    if (-not $UseNuget -and -not $apiDir) { $apiDir = "C:\Program Files\Autodesk\Revit $v" }

    if (-not $UseNuget) {
        if (-not (Test-Path (Join-Path $apiDir "RevitAPI.dll"))) {
            Write-Host "  跳过：找不到 $apiDir\RevitAPI.dll" -ForegroundColor Yellow
            $results += [pscustomobject]@{ Version = $v; TF = $tf; Ok = $false; Reason = "RevitAPI.dll 缺失" }
            continue
        }
        Write-Host "  API: $apiDir" -ForegroundColor DarkGray
    }

    $argList = @("build", $proj, "-c", $Configuration,
                 "-p:RevitVersion=$v",
                 "-p:UseNugetRevitApi=$($UseNuget.IsPresent.ToString().ToLower())")
    if ($apiDir) { $argList += "-p:RevitApiDir=$apiDir" }
    if ($Deploy) { $argList += "-p:DeployToRevit=true" }

    Write-Host "  dotnet $($argList -join ' ')" -ForegroundColor DarkGray
    & dotnet @argList
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  编译失败 (Revit $v)" -ForegroundColor Red
        $results += [pscustomobject]@{ Version = $v; TF = $tf; Ok = $false; Reason = "编译失败" }
        continue
    }
    $results += [pscustomobject]@{ Version = $v; TF = $tf; Ok = $true; Reason = "" }
}

# --- 4. 部署兜底（不依赖 MSBuild Target）---
if ($Deploy) {
    Write-Host ""
    Write-Host "== 部署 ==" -ForegroundColor Cyan
    foreach ($r in $results) {
        if (-not $r.Ok) { continue }
        $v = $r.Version
        $out = Join-Path $root "addin\RevitWy.Plugin\bin\$v\$Configuration\$($r.TF)"
        $dll = Join-Path $out "RevitWy.Plugin.dll"
        if (-not (Test-Path $dll)) {
            Write-Host "  Revit $v : 找不到 $dll" -ForegroundColor Yellow
            continue
        }
        $addinDir = Join-Path $env:APPDATA "Autodesk\Revit\Addins\$v"
        $deployDir = Join-Path $addinDir "revit-wy"
        New-Item -ItemType Directory -Force -Path $deployDir | Out-Null
        Copy-Item $dll $deployDir -Force
        foreach ($extra in @("Newtonsoft.Json.dll", "config.json", "RevitWy.Plugin.dll.config")) {
            $src = Join-Path $out $extra
            if (-not (Test-Path $src)) { $src = Join-Path (Split-Path $proj) $extra }
            if (Test-Path $src) { Copy-Item $src $deployDir -Force }
        }
        Copy-Item (Join-Path (Split-Path $proj) "RevitWy.Plugin.addin") $addinDir -Force
        Write-Host "  Revit $v → $addinDir" -ForegroundColor Green
    }
    Write-Host "请重启对应版本的 Revit 生效。" -ForegroundColor Yellow
}

# --- 5. 汇总 ---
Write-Host ""
Write-Host "== 结果 ==" -ForegroundColor Cyan
$results | ForEach-Object {
    $mark = if ($_.Ok) { "OK  " } else { "FAIL" }
    $color = if ($_.Ok) { "Green" } else { "Yellow" }
    Write-Host ("  [{0}] Revit {1}  {2}  {3}" -f $mark, $_.Version, $_.TF, $_.Reason) -ForegroundColor $color
}
$okCount = ($results | Where-Object { $_.Ok }).Count
Write-Host "成功 $okCount / $($results.Count)" -ForegroundColor Cyan
if ($okCount -eq 0) { exit 1 }
