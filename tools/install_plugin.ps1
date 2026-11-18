# 把 oopz_voice 插件装进这台电脑上的 MaiBot
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\install_plugin.ps1
#
# 自动找不到时，手动指定 MaiBot 根目录（包含 bot.py 或 resources 的那一层）：
#   ... -File .\tools\install_plugin.ps1 -MaiBotRoot "D:\MaiBot\MaiBot OneKey\resources\modules\MaiBot"
#
# 只看会复制到哪里、不真的复制：
#   ... -File .\tools\install_plugin.ps1 -DryRun

param(
    [string]$MaiBotRoot = "",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$source = Join-Path $PSScriptRoot "..\maibot_plugin"
$source = (Resolve-Path $source).Path


function Test-MaiBotPluginsDir([string]$dir) {
    if ([string]::IsNullOrWhiteSpace($dir) -or -not (Test-Path $dir)) { return $false }
    $parent = Split-Path $dir -Parent
    # 路径里带 MaiBot，或者旁边有 bot.py / src\plugin_runtime（源码部署）
    if ($dir -match 'MaiBot' -or $parent -match 'MaiBot') { return $true }
    if (Test-Path (Join-Path $parent "bot.py")) { return $true }
    if (Test-Path (Join-Path $parent "src\plugin_runtime")) { return $true }
    return $false
}

function Get-PluginsDir([string]$root) {
    if ([string]::IsNullOrWhiteSpace($root)) { return $null }
    $root = ([string]$root).Trim().TrimEnd('\')
    if (-not (Test-Path $root)) { return $null }

    # 用户直接给了 plugins 目录
    if ((Split-Path $root -Leaf) -eq "plugins" -and (Test-MaiBotPluginsDir $root)) { return $root }

    # 核心目录本身 / OneKey 外壳（多一层）
    $layouts = New-Object System.Collections.Generic.List[string]
    $layouts.Add($root)
    foreach ($child in (Get-ChildItem $root -Directory -ErrorAction SilentlyContinue)) {
        if ($child -and $child.FullName) { $layouts.Add([string]$child.FullName) }
    }

    foreach ($layout in $layouts) {
        $layoutText = [string]$layout
        if ([string]::IsNullOrWhiteSpace($layoutText)) { continue }
        $onekey = Join-Path $layoutText "resources\modules\MaiBot"
        $onekeyPlugins = Join-Path $onekey "plugins"
        if ((Test-Path $onekeyPlugins) -and (Test-MaiBotPluginsDir $onekeyPlugins)) { return $onekeyPlugins }
        # OneKey 数据目录：<layout>\modules\MaiBot\plugins
        $dataPlugins = Join-Path $layoutText "modules\MaiBot\plugins"
        if ((Test-Path $dataPlugins) -and (Test-MaiBotPluginsDir $dataPlugins)) { return $dataPlugins }
        $directPlugins = Join-Path $layoutText "plugins"
        if ((Test-Path $directPlugins) -and (Test-MaiBotPluginsDir $directPlugins)) { return $directPlugins }
    }
    return $null
}

function Find-MaiBotRoots {
    $roots = New-Object System.Collections.Generic.List[string]

    # 1) 正在运行的 MaiBot / python bot.py
    foreach ($proc in (Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)) {
        $cmd = [string]$proc.CommandLine
        $exe = [string]$proc.ExecutablePath
        if ($cmd -match '([A-Za-z]:\\[^"]*?)\\bot\.py') {
            $roots.Add($matches[1])
        }
        if ($exe -and $exe -match 'MaiBot') {
            $roots.Add((Split-Path $exe -Parent))
        }
    }

    # 2) 卸载信息
    foreach ($key in @(
        "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*",
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*",
        "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*"
    )) {
        Get-ItemProperty $key -ErrorAction SilentlyContinue |
            Where-Object { $_.DisplayName -match "MaiBot" -and $_.InstallLocation } |
            ForEach-Object { $roots.Add($_.InstallLocation) }
    }

    # 3) 常见位置：桌面 / 下载 / 文档 / 应用目录 / OneKey 的 AppData 数据目录
    $bases = @(
        (Join-Path $env:USERPROFILE "Desktop"),
        (Join-Path $env:USERPROFILE "Downloads"),
        (Join-Path $env:USERPROFILE "Documents"),
        (Join-Path $env:LOCALAPPDATA "Programs"),
        (Join-Path $env:LOCALAPPDATA "MaiBotOneKeyDesktop"),
        (Join-Path $env:APPDATA "MaiBotOneKeyDesktop"),
        $env:LOCALAPPDATA,
        $env:APPDATA,
        $env:ProgramFiles,
        ${env:ProgramFiles(x86)}
    )
    foreach ($base in $bases) {
        if (-not $base -or -not (Test-Path $base)) { continue }
        $roots.Add($base)
        Get-ChildItem $base -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match "MaiBot" } |
            ForEach-Object { $roots.Add($_.FullName) }
    }

    # OneKey 数据目录：%APPDATA%\MaiBotOneKeyDesktop\<hash>\modules\MaiBot
    foreach ($onekeyBase in @(
        (Join-Path $env:APPDATA "MaiBotOneKeyDesktop"),
        (Join-Path $env:LOCALAPPDATA "MaiBotOneKeyDesktop")
    )) {
        if ($onekeyBase -and (Test-Path $onekeyBase)) {
            Get-ChildItem $onekeyBase -Directory -ErrorAction SilentlyContinue |
                ForEach-Object { $roots.Add($_.FullName) }
        }
    }

    # 4) 各盘根目录
    foreach ($drive in (Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue |
                        Where-Object { $_.Root -match "^[A-Za-z]:\\$" })) {
        $roots.Add($drive.Root)
        Get-ChildItem $drive.Root -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match "MaiBot" } |
            ForEach-Object { $roots.Add($_.FullName) }
    }

    return @($roots | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Unique)
}

$candidates = @()
if ($MaiBotRoot) {
    $dir = Get-PluginsDir $MaiBotRoot
    if (-not $dir) {
        throw "在 $MaiBotRoot 下找不到 plugins 目录。请指向包含 bot.py 或 resources 的那一层。"
    }
    $candidates = @($dir)
} else {
    foreach ($root in @(Find-MaiBotRoots)) {
        $rootText = [string]$root
        if ([string]::IsNullOrWhiteSpace($rootText)) { continue }
        try {
            $dir = Get-PluginsDir $rootText
        } catch {
            continue
        }
        if ($dir) { $candidates += $dir }
    }
    $candidates = @($candidates | Select-Object -Unique)
}

if (-not $candidates -or $candidates.Count -eq 0) {
    Write-Host "没有自动找到 MaiBot 的 plugins 目录。" -ForegroundColor Red
    Write-Host ""
    Write-Host "请先启动一次 MaiBot，或者手动找一下 bot.py 所在目录：" -ForegroundColor Yellow
    Write-Host '  Get-ChildItem $env:USERPROFILE -Recurse -Filter bot.py -Depth 5 -ErrorAction SilentlyContinue | Select-Object -First 10 FullName'
    Write-Host ""
    Write-Host "然后指定 MaiBot 根目录再运行：" -ForegroundColor Yellow
    Write-Host '  ... -File .\tools\install_plugin.ps1 -MaiBotRoot "<包含 bot.py 的目录>"'
    exit 1
}

if ($candidates.Count -gt 1) {
    # OneKey 真正加载的是 AppData 里的数据目录，优先用它
    $preferred = $candidates | Where-Object { $_ -match '\\AppData\\' } | Select-Object -First 1
    if (-not $preferred) { $preferred = $candidates[0] }
    Write-Host ("找到多个插件目录，自动使用: " + $preferred) -ForegroundColor Yellow
    foreach ($c in $candidates) {
        if ($c -ne $preferred) { Write-Host ("  其他候选: " + $c) -ForegroundColor DarkGray }
    }
    $candidates = @($preferred)
}

$pluginsDir = $candidates[0]
$dest = Join-Path $pluginsDir "oopz_voice"

Write-Host ("源目录 : " + $source)
Write-Host ("目标   : " + $dest)

if ($DryRun) {
    Write-Host "DryRun：没有真的复制。" -ForegroundColor Cyan
    exit 0
}

if (Test-Path $dest) {
    Write-Host "目标已存在，覆盖更新（旧文件会被替换）。" -ForegroundColor Yellow
}

New-Item -ItemType Directory -Force -Path $dest | Out-Null
Copy-Item (Join-Path $source "plugin.py") $dest -Force
Copy-Item (Join-Path $source "_manifest.json") $dest -Force
Copy-Item (Join-Path $source "requirements.txt") $dest -Force
Get-ChildItem $dest -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force

Write-Host ""
Write-Host "插件已安装。" -ForegroundColor Green
Write-Host "下一步：" -ForegroundColor Cyan
Write-Host "  1. 重启 MaiBot"
Write-Host "  2. 打开 WebUI -> 插件 -> OOPZ 语音桥"
Write-Host "  3. bridge_url 保持 ws://127.0.0.1:8765/maibot，token 留空"
Write-Host "  4. Invoke-RestMethod http://127.0.0.1:8765/health 里 maibot_connected=True"
