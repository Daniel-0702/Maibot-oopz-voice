# OOPZ 语音桥 · 目标机环境体检
#
# 在"要跑全部组件"的那台电脑上运行：
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\doctor.ps1
#
# 结果同时打印到屏幕并写入脚本同目录的 doctor-report.txt。
# 不需要管理员权限。

$ErrorActionPreference = "SilentlyContinue"
$ProgressPreference = "SilentlyContinue"

$script:lines = New-Object System.Collections.Generic.List[string]

function W([string]$text = "") {
    Write-Host $text
    $script:lines.Add($text)
}

function Section([string]$title) {
    W ""
    W ("=" * 62)
    W "  $title"
    W ("=" * 62)
}

function Try-Cmd([string]$exe, [string[]]$cmdArgs) {
    try {
        $out = & $exe @cmdArgs 2>$null
        if ($LASTEXITCODE -eq 0) { return ($out | Out-String).Trim() }
    } catch { }
    return $null
}

Section "基础信息"
W ("采集时间   : " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
W ("计算机名   : " + $env:COMPUTERNAME)
W ("当前用户   : " + $env:USERNAME)
$os = Get-CimInstance Win32_OperatingSystem
if ($os) {
    W ("操作系统   : " + $os.Caption + "  (build " + $os.BuildNumber + ")")
    W ("系统架构   : " + $os.OSArchitecture)
    W ("内存       : " + [math]::Round($os.TotalVisibleMemorySize / 1MB, 1) + " GB")
}
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
if ($cpu) {
    W ("CPU        : " + $cpu.Name.Trim() + "  (" + $cpu.NumberOfCores + " 核 " + $cpu.NumberOfLogicalProcessors + " 线程)")
}
$gpus = Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name
if ($gpus) { W ("显卡       : " + ($gpus -join " / ")) }

$sysDrive = ($env:SystemDrive + "\")
$disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$($env:SystemDrive)'"
if ($disk) { W ("系统盘可用 : " + [math]::Round($disk.FreeSpace / 1GB, 1) + " GB / " + [math]::Round($disk.Size / 1GB, 1) + " GB") }

Section "Python"
$pythonPaths = New-Object System.Collections.Generic.List[string]

# 1) PATH 里的 python
$wherePy = Try-Cmd "where.exe" @("python.exe")
if ($wherePy) {
    foreach ($line in ($wherePy -split "`r?`n")) {
        $t = $line.Trim()
        if ($t) { $pythonPaths.Add($t) }
    }
}

# 2) py 启动器
$pyList = Try-Cmd "py.exe" @("-0p")
if ($pyList) {
    W "py -0p 输出:"
    foreach ($line in ($pyList -split "`r?`n")) { if ($line.Trim()) { W ("  " + $line.Trim()) } }
    foreach ($m in [regex]::Matches($pyList, "([A-Za-z]:\\[^\r\n]*?python\.exe)")) {
        $pythonPaths.Add($m.Groups[1].Value.Trim())
    }
}

# 3) 常见安装位置
foreach ($p in @(
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe",
    "$env:ProgramFiles\Python312\python.exe",
    "$env:ProgramFiles\Python311\python.exe",
    "C:\Python312\python.exe",
    "C:\Python311\python.exe"
)) {
    if (Test-Path $p) { $pythonPaths.Add($p) }
}

$pythonPathsNow = @($pythonPaths | Select-Object -Unique)
if ($pythonPathsNow.Count -eq 0) {
    W "没有找到 python.exe（这不一定是问题：下面会检查 MaiBot 自带的运行时）"
} else {
    foreach ($p in $pythonPathsNow) {
        $ver = Try-Cmd $p @("-c", "import sys;print(sys.version.split()[0])")
        if ($ver) { W ("python     : " + $ver + "   " + $p) }
    }
}

Section "MaiBot"
$pluginsDirs = New-Object System.Collections.Generic.List[string]
$maiBotRoots = New-Object System.Collections.Generic.List[string]

# 正在运行的 MaiBot / python 进程能直接暴露安装路径
$running = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match "MaiBot|python" -and $_.CommandLine -match "MaiBot|bot\.py"
}
foreach ($proc in $running) {
    W ("运行中     : " + $proc.Name + "  " + $proc.CommandLine)
}

# 卸载信息里的安装目录
foreach ($key in @(
    "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*",
    "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*",
    "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*"
)) {
    Get-ItemProperty $key | Where-Object { $_.DisplayName -match "MaiBot" } | ForEach-Object {
        if ($_.InstallLocation) { $maiBotRoots.Add($_.InstallLocation) }
        W ("已安装     : " + $_.DisplayName + "  " + $_.InstallLocation)
    }
}

# 各盘根目录下名字带 MaiBot 的目录
foreach ($drive in (Get-PSDrive -PSProvider FileSystem | Where-Object { $_.Root -match "^[A-Za-z]:\\$" })) {
    Get-ChildItem $drive.Root -Directory | Where-Object { $_.Name -match "MaiBot" } | ForEach-Object {
        $maiBotRoots.Add($_.FullName)
    }
    Get-ChildItem (Join-Path $drive.Root "") -Directory -Filter "MaiBot OneKey" | ForEach-Object {
        $maiBotRoots.Add($_.FullName)
    }
}

foreach ($candidate in @("$env:LOCALAPPDATA\Programs\MaiBot", "$env:APPDATA\MaiBot", "$env:LOCALAPPDATA\MaiBot")) {
    if (Test-Path $candidate) { $maiBotRoots.Add($candidate) }
}

$maiBotRoots = $maiBotRoots | Select-Object -Unique
if (-not $maiBotRoots -or $maiBotRoots.Count -eq 0) {
    W "没有自动找到 MaiBot 目录。请在 MaiBot 里看一眼安装位置，或运行 MaiBot 后再跑一次本脚本。"
} else {
    foreach ($root in $maiBotRoots) {
        # 安装目录可能直接是核心目录，也可能是 OneKey 外壳（多一层）
        $layouts = @($root)
        Get-ChildItem $root -Directory -ErrorAction SilentlyContinue | ForEach-Object {
            $layouts += $_.FullName
        }

        foreach ($layout in ($layouts | Select-Object -Unique)) {
            $found = $false
            $onekeyCore = Join-Path $layout "resources\modules\MaiBot"
            if (Test-Path (Join-Path $onekeyCore "plugins")) {
                $pluginsDirs.Add((Join-Path $onekeyCore "plugins"))
                $found = $true
            }
            if (Test-Path (Join-Path $layout "plugins")) {
                $pluginsDirs.Add((Join-Path $layout "plugins"))
                $found = $true
            }
            foreach ($runtimePy in @(
                (Join-Path $layout "resources\runtime\python\python.exe"),
                (Join-Path $layout "runtime\python\python.exe")
            )) {
                if (Test-Path $runtimePy) {
                    $ver = Try-Cmd $runtimePy @("-c", "import sys;print(sys.version.split()[0])")
                    W ("MaiBot 运行时 Python: " + $ver + "   " + $runtimePy)
                    $pythonPaths.Add($runtimePy)
                    $found = $true
                }
            }
            if ($found) { W ("MaiBot 目录: " + $layout) }
        }
    }
}

$pluginsDirs = $pluginsDirs | Select-Object -Unique
if ($pluginsDirs.Count -gt 0) {
    foreach ($d in $pluginsDirs) {
        W ("插件目录   : " + $d)
        $existing = Get-ChildItem $d -Directory | Select-Object -ExpandProperty Name
        if ($existing) { W ("  已有插件 : " + ($existing -join ", ")) }
    }
} else {
    W "没有找到 plugins 目录。"
}

Section "依赖包"
$pythonPaths = @($pythonPaths | Select-Object -Unique)
if ($pythonPaths.Count -gt 0) {
    $probe = "import importlib.util as u; mods=['aiohttp','numpy','edge_tts','playwright','oopz_sdk','websockets'];" +
             "print(' '.join((m + ('=OK' if u.find_spec(m) else '=NO')) for m in mods))"
    foreach ($p in $pythonPaths) {
        $out = Try-Cmd $p @("-c", $probe)
        if ($out) { W ("  " + $p + "`n    " + $out) }
    }
}

Section "语音配置（环境变量）"
$mimoKey = $env:MIMO_API_KEY
$asrKey = $env:ASR_API_KEY
$asrProvider = $env:ASR_PROVIDER
$ttsProvider = $env:TTS_PROVIDER
if (-not $asrProvider) { $asrProvider = "mimo（默认）" }
if (-not $ttsProvider) { $ttsProvider = "mimo（默认）" }
W ("MIMO_API_KEY   : " + ($(if ($mimoKey) { "已设置，长度 " + $mimoKey.Length } else { "未设置" })))
W ("ASR_API_KEY    : " + ($(if ($asrKey) { "已设置，长度 " + $asrKey.Length } else { "未设置（只有切到 openai ASR 才需要）" })))
W ("ASR_PROVIDER   : " + $asrProvider)
W ("TTS_PROVIDER   : " + $ttsProvider)

Section "端口与网络"
$port = Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue
if ($port) {
    W ("8765 被占用 : " + (($port | ForEach-Object { $_.LocalAddress + " " + $_.State }) -join "; "))
} else {
    W "8765 空闲   : 可以给语音桥用"
}

foreach ($target in @(
    @("pypi.org", 443, "pip 源"),
    @("cdn.playwright.dev", 443, "Playwright Chromium 下载"),
    @("api.xiaomimimo.com", 443, "小米 MiMo 语音 API"),
    @("api.siliconflow.cn", 443, "ASR 备选(硅基流动)"),
    @("speech.platform.bing.com", 443, "edge-tts 备选"),
    @("gateway.oopz.cn", 443, "OOPZ API"),
    @("ws.oopz.cn", 443, "OOPZ WebSocket"),
    @("download.agora.io", 443, "Agora Web SDK")
)) {
    $ok = Test-NetConnection -ComputerName $target[0] -Port $target[1] -InformationLevel Quiet -WarningAction SilentlyContinue
    W ("  " + $target[2] + " : " + ($(if ($ok) { "可达" } else { "不可达" })) + "  (" + $target[0] + ":" + $target[1] + ")")
}

Section "结论提示"
W "把本文件（doctor-report.txt）发回给助手即可。"
W "如果 Python 为空、但找到了 OneKey 运行时 python.exe，可以直接用它的 venv。"
W "如果 8765 被占用，启动语音桥时用 --port 8766，插件地址同步改。"
W "如果 ASR / edge-tts 不可达，先解决网络（代理/换服务）。"

$outFile = Join-Path $PSScriptRoot "doctor-report.txt"
[System.IO.File]::WriteAllText($outFile, ($script:lines -join "`r`n"), (New-Object System.Text.UTF8Encoding($false)))
Write-Host ""
Write-Host ("报告已写入: " + $outFile) -ForegroundColor Green
