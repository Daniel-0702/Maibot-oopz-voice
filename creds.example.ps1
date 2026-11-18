# 复制为 creds.ps1（已被 .gitignore 忽略），填入自己的凭据。
# 不要把 creds.ps1 提交到 GitHub。

# ── OOPZ 四件套（推荐）──────────────────────────────           #——
# $env:OOPZ_DEVICE_ID   = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"                 #——
# $env:OOPZ_PERSON_UID  = "xxxxxxxxxxxxxxxxxxxxxxxxxx"                               #这
# $env:OOPZ_JWT_TOKEN   = "eyJhbGciOi..."                                                            #里
#$env:OOPZ_PRIVATE_KEY = @"                                                                                  #是
# -----BEGIN PRIVATE KEY-----                                                                                       #凭
# ...                                                                                                                                      #证
# -----END PRIVATE KEY-----                                                                                          #——    
# "@                                                                                                                                   #——   

# ── 或者：账号密码（不推荐，可能触发风控）──────────
# $env:OOPZ_LOGIN_PHONE    = "13800000000"
# $env:OOPZ_LOGIN_PASSWORD = "your-password"

# ── 小米 MiMo（TTS + ASR）─────────────────────────
# $env:MIMO_API_KEY = "sk-..."
# 音色/流式等设置请改 start_bridge.ps1 顶部的 $MimoTtsVoice / $MimoTtsStream

# ── 备选：SiliconFlow ASR + edge-tts ──────────────
# $env:ASR_PROVIDER = "openai"
# $env:ASR_API_KEY  = "sk-..."
# $env:TTS_PROVIDER = "edge"
