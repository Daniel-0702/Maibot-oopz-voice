# 一台电脑全包：目标机部署指南

> **所有组件都跑在装着 MaiBot 的那台电脑上**（下称"目标机"）。
> 本页每一条命令都在目标机上执行，不涉及你现在问我问题的那台电脑。
>
> 目标机配置较低（2 核 4 线程、老 A 卡）也没关系：本机只跑 Chromium + 桥接，识别和合成都走云端。

---

## 0. 先把文件弄到目标机

1. 在本机工作区找到 `oopz-maibot-voice.zip`；
2. 用 U 盘 / 微信 / 网盘拷到目标机；
3. 解压到 **纯英文、无空格** 的路径，推荐：

```
C:\oopz-voice
```

解压后应该是：

```
C:\oopz-voice\
├─ bridge.py
├─ pipeline.py
├─ agora_ai.html
├─ requirements.txt
├─ start_bridge.ps1
├─ run.bat
├─ README.md
├─ docs\
│  ├─ single-pc.md      ← 本页
│  └─ two-machine.md
├─ maibot_plugin\
│  ├─ _manifest.json
│  ├─ plugin.py
│  └─ requirements.txt
└─ tools\
   ├─ doctor.ps1
   └─ install_plugin.ps1
```

> 不要放在中文路径或带空格的路径下（例如 `C:\Users\张三\桌面\新建文件夹`），Playwright 和 PowerShell 都可能出问题。

---

## 1. 先体检（30 秒）

在目标机上，右键「Windows PowerShell」→ 打开，然后：

```powershell
cd C:\oopz-voice
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\doctor.ps1
```

它会在同目录生成 `doctor-report.txt`，并把结果打印出来。**把这个文件发回给助手**，它会告诉我们：

- 目标机有没有可用的 Python；
- MaiBot 装在哪、插件目录在哪；
- `aiohttp / numpy / edge-tts / playwright / oopz_sdk / websockets` 缺哪些；
- 8765 端口是否空闲；
- ASR、edge-tts、OOPZ、Agora 的网络通不通；
- CPU / 内存 / 显卡。

如果脚本报「无法加载文件，因为在此系统上禁止运行脚本」，先执行一次：

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

---

## 2. 装 Python 环境

### 情况 A：体检报告里找到了 MaiBot 自带运行时

一键版 MaiBot 自带 Python 3.12，路径形如：

```
D:\MaiBot\MaiBot OneKey\resources\runtime\python\python.exe
```

用它建一个独立 venv（不会污染 MaiBot）：

```powershell
cd C:\oopz-voice
& "D:\MaiBot\MaiBot OneKey\resources\runtime\python\python.exe" -m venv .venv
.\.venv\Scripts\python -m pip install -U pip
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m playwright install chromium
```

### 情况 B：没有 Python

1. 去 <https://www.python.org/downloads/windows/> 下载 **Python 3.12（64 位）**；
2. 安装时勾选 **Add python.exe to PATH**；
3. 重新打开 PowerShell，确认 `python --version` 输出 3.12；
4. 再执行：

```powershell
cd C:\oopz-voice
python -m venv .venv
.\.venv\Scripts\python -m pip install -U pip
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m playwright install chromium
```

### 验证

```powershell
.\.venv\Scripts\python -c "import aiohttp, numpy, edge_tts, playwright, oopz_sdk; print('deps ok')"
.\.venv\Scripts\python -m playwright --version
```

两条都正常输出才继续。`playwright install chromium` 约 150MB，网慢就挂代理重试。

---

## 3. 填 OOPZ 凭证

**一定用专门的小号**。两种方式选一种：

### 方式一：手机号 + 密码（简单）

在 `C:\oopz-voice\start_bridge.ps1` 顶部找到：

```powershell
$OopzPhone    = ""
$OopzPassword = ""
```

填上小号的手机号和密码。SDK 会自动登录并续期。

### 方式二：四件套（更稳，适合密码登录被风控）

```powershell
$OopzDeviceId   = "..."
$OopzPersonUid  = "..."
$OopzJwtToken   = "..."
$OopzPrivateKey = "-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----"
```

提取方法见 SDK 文档的「账号密码登录提取凭据」：<https://tangqingfeng7.github.io/Oopzbot-SDK/recipes/password-login/>

> 四件套里的私钥换行写成字面量 `\n`，脚本会原样传给程序，程序自己还原。

### 不知道语音频道 ID？

OOPZ 客户端界面只显示频道名，不显示 ID，这是正常的。两个办法：

**办法一（推荐）：直接填频道名**，`bridge.py` 会自动按名字在域里找语音频道：

```powershell
$ChannelId = "开黑房"
```

**办法二：把域和频道列出来，复制 ID**：

```powershell
cd C:\oopz-voice
.\.venv\Scripts\python tools\list_channels.py

# 如果系统里没有设置 OOPZ 环境变量，直接把小号手机号密码带上：
.\.venv\Scripts\python tools\list_channels.py --phone 138xxxxxxxx --password "你的密码"
```

输出形如：

```
[域] 我的服务器   area=6ad0261bc4eb41e882692531981a0972
  [分组] 语音频道
    [语音] 开黑房   id=8f3c2a1d4b5e46e7a9c0d1e2f3a4b5c6
    [语音] 挂机房   id=...
  [分组] 文字频道
    [文字] 公告   id=...
```

脚本最后会直接打印可以粘贴的 `$AreaId` / `$ChannelId` 两行。

> 列不出来通常是：小号还没被拉进那个域，或者凭证过期。

---

## 4. 第一次启动：只验音频（回环）

打开 `C:\oopz-voice\start_bridge.ps1`，改这几行：

```powershell
$DefaultMode = "loopback"
$AreaId      = "你的域ID"
$ChannelId   = "开黑房"              # 可以写频道名，也可以写 tools\list_channels.py 查到的 ID
$ChannelName = "开黑房"
```

保存，然后**双击 `run.bat`**。

第一次启动会慢一些（Chromium 冷启动 + OOPZ 登录），通常 10~30 秒。看到这些日志就对了：

```
本地音频服务: http://127.0.0.1:8765/
浏览器页面已就绪
已进房 area=... channel=... rtc_uid=...
按 Ctrl+C 结束
```

用手机或另一台设备进同一个 OOPZ 语音频道，说一句话：

- 约 0.4 秒后听到自己的回声 → **"听"和"说"都通了**；
- 没回声，先看日志里有没有 `[cap] attached`（没有就是没订阅到别人）。

再验证发声：把 `$DefaultMode` 改成 `"tone"`，重启，频道里应该每 3 秒"滴"一声。

> 回环模式下机器人会把你说的原样播回去，这是测试，不是 bug。

---

## 5. 装 MaiBot 插件（同一台机器）

在目标机上执行：

```powershell
cd C:\oopz-voice
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\install_plugin.ps1
```

它会自动找到 MaiBot 的 `plugins` 目录，把插件复制成 `plugins\oopz_voice\`。

找不到就手动指定 MaiBot 根目录（体检报告里有）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\install_plugin.ps1 `
  -MaiBotRoot "D:\MaiBot\MaiBot OneKey\resources\modules\MaiBot"
```

然后：

1. **重启 MaiBot**；
2. 打开 MaiBot WebUI → 插件 → 应该能看到「OOPZ 语音桥」；
3. 点开配置，「语音桥」分组：

| 字段 | 值 |
|---|---|
| `bridge_url` | `ws://127.0.0.1:8765/maibot` |
| `token` | 留空（同一台机器不需要） |
| `account_id` | `oopz-voice` |
| `scope` | `voice` |
| `channel_id` | 你的语音频道 ID（可留空） |
| `channel_name` | 开黑房 |

4. 保存，等 2 秒。

验证插件连上了：

```powershell
Invoke-RestMethod "http://127.0.0.1:8765/health"
```

要看到 `maibot_connected : True`。

如果是 `False`，看 MaiBot 的日志有没有：

```
已连接语音桥 ws://127.0.0.1:8765/maibot
```

没有就是插件没加载或 URL/token 不对。

---

## 6. 开 AI 对话

### 6.1 拿小米 MiMo 的 API Key

现在 TTS 和 ASR 都默认走 **小米 MiMo**：

- TTS：`MiMo-V2.5-TTS` 系列，内置冰糖/茉莉/苏打/白桦等音色，官方标注**限时免费**；
- ASR：`MiMo-V2.5-ASR`，中英双语、方言、强噪音、多人对话都能识，平台侧也在免费活动里（以计费页为准）。

申请步骤：

1. 打开 <https://platform.xiaomimimo.com/>，用小米账号登录；
2. 进「API Key」页面新建一个 key（注意：语音 key 和聊天模型 key 是分开的）；
3. 复制 `sk-...`。

### 6.2 改配置

`start_bridge.ps1` 顶部：

```powershell
$DefaultMode = "ai"

$TtsProvider   = "mimo"
$AsrProvider   = "mimo"
$MimoApiKey    = "sk-..."          # 填你的 MiMo key
$MimoTtsVoice  = "冰糖"            # 也可换 茉莉/苏打/白桦/Mia/Chloe/Milo/Dean
$MimoTtsStyle  = "自然、口语化，像在语音频道里和朋友聊天"
$MimoAsrLang   = "zh"              # auto / zh / en
```

保存，双击 `run.bat`。首次会看到：

```
语音管线就绪：ASR=mimo  TTS=mimo  MiMo=on
MiMo TTS: model=mimo-v2.5-tts voice=冰糖 stream=True
```

### 6.3 想换回别家（备选）

- **ASR 换硅基流动**：`$AsrProvider = "openai"`、`$AsrApiKey = "sk-..."`、`$AsrBaseUrl = "https://api.siliconflow.cn/v1"`、`$AsrModel = "FunAudioLLM/SenseVoiceSmall"`；
- **TTS 换 edge-tts**（免费、无需 key）：`$TtsProvider = "edge"`，`$TtsVoice = "zh-CN-XiaoxiaoNeural"`。

### 6.4 期望日志

对着麦克风说一句：

```
[ASR 420ms] uid=... -> 今天开黑吗
[MaiBot] 开啊，等我两分钟
[TTS 首句 380ms]
TTS 播放完成: id=1 1.80s ok=True
```

同时 MaiBot 日志：

```
语音输入已注入: 今天开黑吗
```

> MiMo TTS 支持行内标签，例如 `(开心)来啦来啦！`、`(悄悄话)别告诉别人`，MaiBot 回复里带这些标签会直接生效。
> 如果首句迟迟不出声，把 `$MimoTtsStream` 保持默认（流式），它走 24k PCM 边收边播，比整段合成快 0.3~0.8 秒。

### 6.4 延迟预期

| 阶段 | 预期 |
|---|---|
| VAD 断句 | 0.56s |
| 云端 ASR | 0.3~0.7s |
| MaiBot 生成首条回复 | 0.8~2.5s |
| TTS 首句 | 0.2~0.5s（MiMo 流式） |
| **合计** | **约 2~3.5s** |

MaiBot 那段是最大头。想压到 1.5 秒左右，需要给语音开快速通道（直连模型），可以下一步做。

---

## 7. 日常使用

启动顺序：

1. 先开 MaiBot（OneKey 或 `bot.py`），等它完全起来；
2. 再双击 `C:\oopz-voice\run.bat`；
3. 控制台窗口不要关——关掉 = 机器人退出语音频道。

想开机自启：

- `Win + R` → `shell:startup` → 把 `run.bat` 的快捷方式放进去；
- 或者用「任务计划程序」创建「登录时」触发的任务，程序填 `C:\oopz-voice\run.bat`。

> 机器人账号的语音是从这台电脑的 Chromium 发出去的，所以**这台电脑要一直开着**，但不需要开 OOPZ 客户端。

---

## 8. 低配机的注意事项

- **可以在这台机器上开 OOPZ 客户端**：桥用的 Chromium 是无头的、不经过系统扬声器，和客户端不冲突；
- 但客户端要登录**你自己的号**，桥登录**小号**——同一个号会被顶下线；
- 用耳机，别用外放：否则你的麦克风会把机器人/别人的声音收进去，形成回声；
- 不用时关掉 MaiBot WebUI 页面，浏览器标签也吃资源；
- Chromium 已经加了 `--disable-gpu`，老 A 卡不会参与渲染；
- 如果听到断续：先看任务管理器 CPU 是否 100%，关掉别的程序；再考虑把 `HALF_DUPLEX` 保持 `1`；
- 内存建议 ≥ 8GB：Chromium 约 300~500MB，MaiBot 视插件 300MB~1GB。

---

## 9. 排错表（目标机）

| 症状 | 原因 | 处理 |
|---|---|---|
| `找不到 Python: ...\.venv\Scripts\python.exe` | 还没建 venv | 回到 §2 |
| `playwright install chromium` 卡住/失败 | 网络 | 挂代理重试；或手动下载 Chromium 放到 `%LOCALAPPDATA%\ms-playwright` |
| `voice browser init timeout` | Chromium 起不来/被杀软拦 | 看 Playwright 是否装好；临时关杀软；`playwright install chromium` 重跑 |
| 登录 OOPZ 失败 / 要验证码 | 小号风控 | 换四件套；或用手机登录一次小号再试 |
| `enter_channel` 报错 / `supplierSign` 为空 | 频道不是语音频道 / 小号不在该域 / token 过期 | 核对域 ID、频道 ID、小号是否在域里 |
| `在域 ... 里找不到名字含「xx」的语音频道` | 频道名写错，或那不是语音频道 | 跑 `tools\list_channels.py`，按输出里的名字/ID 填 |
| 找不到频道 ID | OOPZ 界面不显示 ID | 直接填频道名，或跑 `tools\list_channels.py` |
| `Address already in use` | 8765 被占 | `start_bridge.ps1` 里 `$Port = 8766`，插件地址同步改 |
| 插件列表里没有「OOPZ 语音桥」 | 目录名/版本不对 | 确认是 `plugins\oopz_voice\plugin.py`；MaiBot 要 1.3.x |
| `/health` 里 `maibot_connected=False` | 插件没连上 | 看 MaiBot 日志；确认 `bridge_url` 是 `127.0.0.1:8765` |
| MaiBot 日志 `Host 丢弃了语音消息` | `adapter_policy.toml` 拦截 | 给 `oopz_voice` 加 allow，或删掉该文件（默认全放行） |
| `[ASR]` 有、`[MaiBot]` 没有 | 注入被拦 | 同上 |
| `[MaiBot]` 有、没声音 | MiMo key/网络，或 edge 网络 | 看 bridge 日志 `MiMo TTS HTTP 4xx/5xx`；临时 `$TtsProvider = "edge"` 对比 |
| ASR HTTP 401 | key / base URL 错 | 核对 `$AsrApiKey`、`$AsrBaseUrl` |
| 频道里只有机器人 | 没别人 publish | 正常；让别人先进频道说话 |
| 机器人不说话但日志正常 | 身份帧失败 | 看 `身份帧: {'ok': False}`；等 Agora 信令起来后重试 |

---

## 10. 更新与备份

- 代码更新：覆盖 `bridge.py / pipeline.py / agora_ai.html / maibot_plugin\*`，`start_bridge.ps1` 里的配置**不会丢**（配置在脚本里，别覆盖它）；
- 插件更新：重跑 `tools\install_plugin.ps1`；
- 备份：整个 `C:\oopz-voice`（不含 `.venv`）打包即可，约 100KB。
