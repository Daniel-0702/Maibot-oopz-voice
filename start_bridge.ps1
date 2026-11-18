# OOPZ 语音桥 · 一键启动（全部组件都在这台电脑上）
#
# 改下面这一段，然后双击 run.bat，或者：
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\start_bridge.ps1
#
# 参数会原样透传，例如：
#   .\start_bridge.ps1 -Mode ai

param(
    [string]$Mode = "",
    [string]$Area = "",
    [string]$Channel = "",
    [switch]$Verbose
)

# ============================ 只需要改这里 ============================
$DefaultMode   = "loopback"    # loopback 回音测试 / tone 蜂鸣 / ai 正式对话
$AreaId        = ""            # 你的 OOPZ 域 ID 或域名（必填，不要提交真实值）
$ChannelId     = ""            # 语音频道 ID 或频道名（必填）
$ChannelName   = "语音频道"

$Port          = 8765
$ListenHost    = "127.0.0.1"         # 全部在本机跑就保持 127.0.0.1
$Token         = ""                  # 本机跑留空；跨机器才需要

$PythonExe     = ""                  # 留空 = 用 .venv\Scripts\python.exe

# OOPZ 凭证：留空表示沿用系统里已经设置好的环境变量
$OopzDeviceId  = ""
$OopzPersonUid = ""
$OopzJwtToken  = ""
$OopzPrivateKey = ""                 # 换行写成 \n
$OopzPhone     = ""
$OopzPassword  = ""

# 语音模型（ai 模式用）
$AsrProvider   = "mimo"              # mimo（小米，限时免费）或 openai（硅基流动/OpenAI）
$TtsProvider   = "mimo"              # mimo 或 edge
$MimoApiKey    = ""                  # 小米 MiMo 开放平台 API Key；不要提交真实值
$MimoBaseUrl   = "https://api.xiaomimimo.com/v1"
$MimoTtsVoice  = "冰糖"              # 冰糖/茉莉/苏打/白桦/Mia/Chloe/Milo/Dean
$MimoTtsStyle  = "自然口语"              # 越短首包越快；留空则不加风格提示
$MimoTtsStream = "1"                 # 1=流式 PCM（首包快），0=整段 MP3
$MimoAsrLang   = "zh"                # auto / zh / en
$AsrApiKey     = ""                  # 仅 AsrProvider=openai 时需要
$AsrBaseUrl    = "https://api.siliconflow.cn/v1"
$AsrModel      = "FunAudioLLM/SenseVoiceSmall"
$TtsVoice      = "zh-CN-XiaoxiaoNeural"
$VadFloor      = "0.012"
$HalfDuplex    = "1"
# ====================================================================

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$root = $PSScriptRoot
Set-Location $root

if (-not $Mode) { $Mode = $DefaultMode }
if (-not $Area) { $Area = $AreaId }
if (-not $Channel) { $Channel = $ChannelId }

if (-not $PythonExe) {
    $PythonExe = Join-Path $root ".venv\Scripts\python.exe"
}

if (-not (Test-Path $PythonExe)) {
    Write-Host "找不到 Python: $PythonExe" -ForegroundColor Red
    Write-Host "先在语音桥目录执行：" -ForegroundColor Yellow
    Write-Host '  & "<MaiBot 自带的 python.exe>" -m venv .venv'
    Write-Host '  .\.venv\Scripts\python -m pip install -r requirements.txt'
    Write-Host '  .\.venv\Scripts\python -m playwright install chromium'
    exit 1
}

if (-not $Area -or -not $Channel) {
    Write-Host "还没填 OOPZ 的域 ID / 语音频道 ID。" -ForegroundColor Red
    Write-Host "用记事本打开 start_bridge.ps1，把顶部 `$AreaId / `$ChannelId 填上。" -ForegroundColor Yellow
    exit 1
}

if ($Mode -eq "ai") {
    if ($TtsProvider -eq "mimo" -and -not $MimoApiKey -and -not $env:MIMO_API_KEY) {
        Write-Host "TtsProvider=mimo 需要 MimoApiKey（小米 MiMo 开放平台）。" -ForegroundColor Red
        exit 1
    }
    if ($AsrProvider -eq "mimo" -and -not $MimoApiKey -and -not $env:MIMO_API_KEY) {
        Write-Host "AsrProvider=mimo 需要 MimoApiKey（小米 MiMo 开放平台）。" -ForegroundColor Red
        exit 1
    }
    if ($AsrProvider -eq "openai" -and -not $AsrApiKey -and -not $env:ASR_API_KEY) {
        Write-Host "AsrProvider=openai 需要 AsrApiKey。" -ForegroundColor Red
        exit 1
    }
}

# 凭证（留空则不覆盖系统环境变量）
if ($OopzDeviceId)   { $env:OOPZ_DEVICE_ID   = $OopzDeviceId }
if ($OopzPersonUid)  { $env:OOPZ_PERSON_UID  = $OopzPersonUid }
if ($OopzJwtToken)   { $env:OOPZ_JWT_TOKEN   = $OopzJwtToken }
if ($OopzPrivateKey) { $env:OOPZ_PRIVATE_KEY = $OopzPrivateKey }
if ($OopzPhone)      { $env:OOPZ_LOGIN_PHONE = $OopzPhone }
if ($OopzPassword)   { $env:OOPZ_LOGIN_PASSWORD = $OopzPassword }

if ($AsrProvider)  { $env:ASR_PROVIDER   = $AsrProvider }
if ($TtsProvider)  { $env:TTS_PROVIDER   = $TtsProvider }
if ($MimoApiKey)   { $env:MIMO_API_KEY   = $MimoApiKey }
if ($MimoBaseUrl)  { $env:MIMO_BASE_URL  = $MimoBaseUrl }
if ($MimoTtsVoice) { $env:MIMO_TTS_VOICE = $MimoTtsVoice }
if ($MimoTtsStyle) { $env:MIMO_TTS_STYLE = $MimoTtsStyle }
if ($MimoTtsStream){ $env:MIMO_TTS_STREAM = $MimoTtsStream }
if ($MimoAsrLang)  { $env:MIMO_ASR_LANGUAGE = $MimoAsrLang }

if ($AsrApiKey)      { $env:ASR_API_KEY   = $AsrApiKey }
if ($AsrBaseUrl)     { $env:ASR_BASE_URL  = $AsrBaseUrl }
if ($AsrModel)       { $env:ASR_MODEL     = $AsrModel }
if ($TtsVoice)       { $env:TTS_VOICE     = $TtsVoice }
if ($VadFloor)       { $env:VAD_FLOOR     = $VadFloor }
if ($HalfDuplex)     { $env:HALF_DUPLEX   = $HalfDuplex }

$argsList = @(
    "bridge.py",
    "--area", $Area,
    "--channel", $Channel,
    "--channel-name", $ChannelName,
    "--mode", $Mode,
    "--port", "$Port"
)

if ($ListenHost -and $ListenHost -ne "127.0.0.1") {
    $argsList += @("--host", $ListenHost)
}
if ($Token) { $argsList += @("--token", $Token) }
if ($Verbose) { $argsList += "--verbose" }

Write-Host ""
Write-Host "启动语音桥" -ForegroundColor Cyan
Write-Host ("  模式   : " + $Mode)
Write-Host ("  频道   : " + $Area + " / " + $Channel + " (" + $ChannelName + ")")
Write-Host ("  端口   : " + $Port)
Write-Host ("  Python : " + $PythonExe)
if ($Mode -eq "ai") {
    Write-Host ("  语音   : ASR=" + $AsrProvider + "  TTS=" + $TtsProvider)
}
Write-Host ""

& $PythonExe @argsList
$code = $LASTEXITCODE

Write-Host ""
if ($code -eq 0) {
    Write-Host "语音桥已退出。" -ForegroundColor Yellow
} else {
    Write-Host "语音桥异常退出（exit code $code），把上面的日志发回给助手。" -ForegroundColor Red
}
