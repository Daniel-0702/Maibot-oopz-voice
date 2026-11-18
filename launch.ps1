# OOPZ 语音桥 · PowerShell 启动器
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File C:\oopz-voice\launch.ps1
#   powershell -ExecutionPolicy Bypass -File C:\oopz-voice\launch.ps1 -Mode loopback
#
# 它会：
#   1) 自动加载同目录 creds.ps1（如果存在）
#   2) 如果还没凭据，加载一次 PowerShell 配置文件（凭据放在 $PROFILE 里的情况）
#   3) 调用 start_bridge.ps1（不改动里面的域/频道/API Key）

param(
    [string]$Mode = ""
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$root = $PSScriptRoot
Set-Location $root

# 1) creds.ps1：把 oopz-login 打印的四段 $env:OOPZ_* 粘进去
$creds = Join-Path $root "creds.ps1"
if (Test-Path $creds) {
    . $creds
}

# 2) 凭据放在 PowerShell 配置文件里的情况
if (-not $env:OOPZ_DEVICE_ID) {
    foreach ($profilePath in @($PROFILE.CurrentUserAllHosts, $PROFILE.CurrentUserCurrentHost)) {
        if ($profilePath -and (Test-Path $profilePath)) {
            . $profilePath
            if ($env:OOPZ_DEVICE_ID) { break }
        }
    }
}

# 3) 仍然没有：给一次性设置说明
if (-not $env:OOPZ_DEVICE_ID) {
    Write-Host "没有找到 OOPZ 凭据（OOPZ_DEVICE_ID）。" -ForegroundColor Red
    Write-Host ""
    Write-Host "只需做一次：" -ForegroundColor Yellow
    Write-Host '  C:\oopz-voice\.venv\Scripts\oopz-login.exe --phone 你的手机号 --headful --print-env powershell'
    Write-Host '  把打印的四段 $env:OOPZ_* 粘贴到 C:\oopz-voice\creds.ps1，保存。'
    Write-Host '  然后重新运行本脚本。'
    exit 1
}

$startArgs = @()
if ($Mode) {
    $startArgs += @("-Mode", $Mode)
}

& (Join-Path $root "start_bridge.ps1") @startArgs
