# OOPZ 语音桥 · MaiBot 语音插件

把 **OOPZ 语音频道** 接进 **MaiBot（麦麦）**：机器人能听见频道里的人说话，经过 ASR → MaiBot → TTS，再用语音回答。

- **听**：订阅 OOPZ 语音房里的 Agora 音轨，按人抓成 16k PCM
- **说**：在房间里维持一条常驻 Agora 音轨，TTS 音频直接推流
- **大脑**：MaiBot 消息网关插件，复用麦麦的人设、记忆和聊天流
- **低延迟**：VAD 断句 + 流式 MiMo TTS（PCM 边收边播）+ 首句优先
- **可回退**：MiMo 不可用时切 SiliconFlow ASR + edge-tts
- **纯本机**：不需要显卡，云端只走 API

> ⚠️ OOPZ 没有官方机器人平台。本项目通过社区逆向协议、以**用户小号**身份接入，随时可能失效。请使用小号，并自行承担平台规则与封号风险。

---

## 架构

```
OOPZ 语音房（Agora RTC）
      ▲                 │
 publish│                │ subscribe
      │                 ▼
┌──────────────────────────────────────┐
│  Headless Chromium · agora_ai.html   │
│  播放：AudioWorklet 环形缓冲 → 音轨   │
│  收音：远端音轨 → 16k PCM worklet     │
└──────────────┬──────────▲────────────┘
               │ WebSocket│ 48k PCM
               ▼          │
┌──────────────────────────────────────┐
│  bridge.py                           │
│  VAD → ASR → MaiBot → TTS            │
└──────────────┬───────────────────────┘
               │ 文本
               ▼
        MaiBot + oopz_voice 插件
```

音频不经过 MaiBot 进程，只有**文本**走两者之间的本地 WebSocket（默认 `ws://127.0.0.1:8765/maibot`）。

---

## 环境要求

| 项目 | 要求 |
|---|---|
| 系统 | Windows 10 / 11 |
| Python | 3.10+，推荐 3.12 |
| OOPZ | 一个专用小号（手机号 + 密码，或 device_id/JWT 四件套） |
| MaiBot | 1.3.0+，已在 1.4.5 上验证 |
| 语音 API | 小米 MiMo（推荐），或 SiliconFlow + edge-tts |
| 显卡 | 不需要 |

---

## 快速开始

### 1. 安装依赖

```powershell
cd C:\oopz-voice
python -m venv .venv
.\.venv\Scripts\python -m pip install -U pip
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m playwright install chromium
```

### 2. 准备 OOPZ 凭据

```powershell
.\.venv\Scripts\oopz-login.exe --phone 你的手机号 --headful --print-env powershell
```

浏览器里登录小号，把打印出来的四段 `$env:OOPZ_*` 保存到 `creds.ps1`。

仓库里提供了模板：

```powershell
Copy-Item .\creds.example.ps1 .\creds.ps1
notepad .\creds.ps1
```

`creds.ps1` 已被 `.gitignore` 忽略，**不要提交**。

### 3. 填域和频道

编辑 `start_bridge.ps1` 顶部：

```powershell
$AreaId    = "你的域 ID 或域名"
$ChannelId = "语音频道 ID 或频道名"
```

不知道 ID 时，先把小号拉进域，然后：

```powershell
.\.venv\Scripts\python tools\list_channels.py
```

会列出域、分组、语音频道和对应 ID。

### 4. 启动

```powershell
powershell -ExecutionPolicy Bypass -File .\launch.ps1
```

`launch.ps1` 会依次加载 `creds.ps1` → PowerShell 配置 → 当前环境变量，然后调用 `start_bridge.ps1`。也可以直接双击 `run.bat`。

运行模式：

| 模式 | 用途 |
|---|---|
| `loopback` | 回音测试：你在频道说话，约 0.4s 后听到自己 |
| `tone` | 蜂鸣：验证机器人能否发声 |
| `ai` | 正式对话：ASR → MaiBot → TTS |

```powershell
powershell -ExecutionPolicy Bypass -File .\launch.ps1 -Mode loopback
```

---

## 安装 MaiBot 插件

推荐用脚本自动查找插件目录：

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\install_plugin.ps1
```

它会找 `plugins` 目录（包括 OneKey 的 `%APPDATA%\MaiBotOneKeyDesktop\...\modules\MaiBot\plugins`），把插件复制成 `oopz_voice`。

也可以手动复制：

```powershell
Copy-Item .\maibot_plugin "<MaiBot 目录>\plugins\oopz_voice" -Recurse -Force
```

重启 MaiBot，在 WebUI → 插件里启用「OOPZ 语音桥」，配置：

```toml
bridge_url = "ws://127.0.0.1:8765/maibot"
token = ""
```

验证：

```powershell
Invoke-RestMethod "http://127.0.0.1:8765/health"
```

`maibot_connected` 为 `True` 即接通。

---

## 配置项

所有语音相关配置都可以用环境变量覆盖（推荐写在 `creds.ps1` 里）：

> `TTS_PROVIDER`、`ASR_PROVIDER`、`MIMO_TTS_VOICE` 等几项在 `start_bridge.ps1` 顶部也有默认值，脚本里的非空值会覆盖环境变量；改这几项时直接改脚本更直观。

| 变量 | 默认 | 说明 |
|---|---|---|
| `TTS_PROVIDER` | `mimo` | `mimo` / `edge` |
| `ASR_PROVIDER` | `mimo` | `mimo` / `openai` |
| `MIMO_API_KEY` | 空 | MiMo 开放平台 Key |
| `MIMO_TTS_VOICE` | `冰糖` | 冰糖/茉莉/苏打/白桦/Mia/Chloe/Milo/Dean |
| `MIMO_TTS_STREAM` | `1` | `1` 流式 PCM，首包更快 |
| `MIMO_TTS_STYLE` | `自然口语` | 越短首包越快，留空则不加风格提示 |
| `MIMO_ASR_LANGUAGE` | `zh` | `auto` / `zh` / `en` |
| `ASR_API_KEY` | 空 | 当 `ASR_PROVIDER=openai` 时使用 |
| `ASR_BASE_URL` | `https://api.siliconflow.cn/v1` | OpenAI 兼容 ASR |
| `ASR_MODEL` | `FunAudioLLM/SenseVoiceSmall` | ASR 模型 |
| `TTS_VOICE` | `zh-CN-XiaoxiaoNeural` | edge-tts 音色 |
| `VAD_SILENCE_MS` | `560` | 尾静音，越小越跟手（可降到 400~460） |
| `TTS_FIRST_MAX` | `0` | `0` 不切第一块；`>0` 按字数切短 |
| `TTS_RATE` | `+0%` | edge-tts 语速 |

### 关于延迟

推荐组合：

| 环节 | 选择 |
|---|---|
| VAD | 400~460ms |
| ASR | 云端流式/短请求（MiMo 或 SenseVoiceSmall） |
| TTS | MiMo 流式 PCM，或 edge-tts |
| 首句 | `TTS_FIRST_MAX=0` 保音质；要更快可设 8~10 |

日志里会打印：

```
[TTS 文本] ...
[TTS 首包 320ms] provider=mimo
[TTS 整句 1800ms] provider=mimo
```

`首包` 才是你听到第一声的时间；`整句` 是整句合成完的时间。

---

## 跨聊天流共享记忆

MaiBot 的长期记忆由 **A_Memorix** 管理，有两种共享方式：

1. **全局共享记忆**（`a_memorix.global_memory_sharing_enabled = true`）：所有聊天流互相检索；
2. **共享记忆组**（`a_memorix.shared_memory_groups`）：只让指定聊天流互相检索。

在 MaiBot WebUI → 配置 → 记忆 / A_Memorix 里设置。共享的是**长期记忆**，不是当前对话上下文。

---

## 常见问题

| 现象 | 处理 |
|---|---|
| `No valid login configuration found` | 当前窗口没有 OOPZ 凭据，检查 `creds.ps1` 或环境变量 |
| `找不到名字含「xxx」的域` | 小号没加入该域，或名字不对；用 `list_channels.py` 查 |
| `OopzPasswordLoginError: 密码错误` | 先用 `oopz-login` 浏览器登录，或改用四件套 |
| 页面已连接又断开 | Chromium 崩溃/内存不足；关掉多余程序，确认 `--disable-gpu` 生效 |
| `/health` 里 `maibot_connected=False` | 插件未启用，或 `bridge_url` 不对 |
| MiMo 返回 402 | 账户余额/免费额度不足，换 edge + SiliconFlow |
| 开头几个字读音怪 | 把 `TTS_FIRST_MAX` 设为 `0`，不要切碎首句 |
| 没有回声 | 频道里只有机器人自己；用另一个账号进频道说话 |

---

## 安全与合规

- **不要提交** `creds.ps1`、`key.txt`、`.venv`、日志；仓库已用 `.gitignore` 忽略；
- 不要把 API Key / JWT / 私钥写进 `start_bridge.ps1` 后提交；
- 如果凭据曾出现在截图、日志或公开仓库，立即去平台**重置/轮换**；
- OOPZ 使用**小号**，不要用主号；
- 本项目与 OOPZ、小米、Agora 均无官方关联。

---

## 目录结构

```
oopz-voice/
├─ bridge.py            # OOPZ 进房、Agora 桥、本地 WS
├─ pipeline.py          # VAD / ASR / MaiBot / TTS
├─ agora_ai.html        # 浏览器侧音轨处理
├─ start_bridge.ps1     # 启动配置（不要填密钥）
├─ launch.ps1           # 启动器（加载 creds.ps1）
├─ run.bat              # 双击入口
├─ creds.example.ps1    # 凭据模板
├─ maibot_plugin/       # MaiBot 插件
├─ tools/               # 体检、列频道、装插件
├─ docs/                # 部署细节
├─ LICENSE
└─ SECURITY.md
```

---

## 致谢

- [oopz-sdk](https://github.com/tangqingfeng7/Oopzbot-SDK)：OOPZ 协议与语音传输
- [MaiBot](https://github.com/Mai-with-u/MaiBot)：聊天机器人与插件框架
- [Agora](https://www.agora.io/)：实时音频
- [MiMo](https://platform.xiaomimimo.com/)：语音识别与合成

## License

MIT
