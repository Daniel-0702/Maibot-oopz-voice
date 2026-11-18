# 两台电脑部署（详细版）

> 适用场景：**语音桥跑在这台机器（PC-A），MaiBot 装在另一台机器（PC-B）**。
> 两台机器在同一个局域网（同一个路由器下）时按 §1~§4 做；不在同一局域网看 §5。

本文里出现的地址：

| 角色 | 示例 | 说明 |
|---|---|---|
| PC-A（语音桥） | `192.168.1.72` | 跑 Chromium + bridge.py + ASR/TTS |
| PC-B（MaiBot） | `192.168.1.50` | 跑 MaiBot 和 `oopz_voice` 插件 |

请用 `ipconfig`（Windows）或 `ip addr`（Linux）换成你自己的地址。

---

## 0. 先看清职责

```
                PC-A（这台，2C4T）                        PC-B（MaiBot 那台）
        ┌──────────────────────────────┐          ┌──────────────────────────────┐
        │  Chromium（无头）             │          │  MaiBot 1.3.3                │
        │   ↕ Agora 语音房              │          │   └ plugins/oopz_voice       │
        │  bridge.py  :8765             │◄──LAN───►│      消息网关 duplex         │
        │   ├ VAD → 云端 ASR            │  ws://   │                              │
        │   ├ MaiBot 网关（WS 服务端）   │          │  只需要出站连接 PC-A          │
        │   └ MiMo TTS → PCM            │          │  不需要 OOPZ 凭证             │
        └──────────────────────────────┘          └──────────────────────────────┘
```

四条原则，先记住：

1. **连接方向是 PC-B 主动连 PC-A**。所以只需要在 PC-A 上开一个入站端口，PC-B 不用开任何端口。
2. **OOPZ 凭证只在 PC-A**；MaiBot 那台完全不需要登录 OOPZ。
3. **PC-A 的 `/maibot` 必须带 token**，因为它监听在局域网上。
4. 语音房里的音频不经过 MaiBot 那台机器：PC-A 自己收、自己发，只有**文字**走局域网。

---

## 1. PC-A：装语音桥

### 1.1 建独立 Python 环境

这台机器的 PATH 里没有 `python`，用 MaiBot 自带的 3.12 建一个 venv（不会污染 MaiBot 的环境）：

```powershell
cd C:\Users\a9199\Documents\deepseek-harness\default-workspace\oopz-maibot-voice

$py = "D:\MaiBot\MaiBot OneKey\resources\runtime\python\python.exe"
& $py -m venv .venv
.\.venv\Scripts\python -m pip install -U pip
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m playwright install chromium
```

> 如果 `playwright install chromium` 下载慢，可以先挂代理，或手动把 Chromium 放进 Playwright 缓存目录。
> 如果这台机器的 `venv` 建不起来（极少数精简版 Python 缺 ensurepip），单独装一个 Python 3.12 再用它建 venv 即可，不影响 MaiBot。

### 1.2 填 OOPZ 凭证

```powershell
$env:OOPZ_DEVICE_ID   = "..."
$env:OOPZ_PERSON_UID  = "..."
$env:OOPZ_JWT_TOKEN   = "..."
$env:OOPZ_PRIVATE_KEY = "-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----"
```

或者用手机号密码：`$env:OOPZ_LOGIN_PHONE` / `$env:OOPZ_LOGIN_PASSWORD`。

### 1.3 生成一个 token

```powershell
$token = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
$token
```

把它记下来，PC-B 的插件配置里要填一模一样的一串。**不要用 123456 这种。**

### 1.4 先在 PC-A 本机自测

不要一上来就跨机，先确认音频链路：

```powershell
.\.venv\Scripts\python bridge.py --area <域ID> --channel <频道ID> --loopback
```

进语音频道说一句话，约 0.4 秒后听到自己的回声 → "听"和"说"都通了。
再试 `--mode tone`，频道里每 3 秒"滴"一声 → 发声也通了。

> 这一步不涉及 MaiBot，不涉及局域网，失败就查 OOPZ 凭证 / 频道 ID / Chromium。

### 1.5 开防火墙

先查 PC-A 的局域网 IP：

```powershell
Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike "127.*" } |
  Select-Object IPAddress, InterfaceAlias
```

假设是 `192.168.1.72`，PC-B 是 `192.168.1.50`，在 PC-A 上放行 8765，并且**只允许 PC-B**：

```powershell
New-NetFirewallRule `
  -DisplayName "OOPZ Voice Bridge 8765" `
  -Direction Inbound -Protocol TCP -LocalPort 8765 `
  -Action Allow -Profile Private -RemoteAddress 192.168.1.50
```

删规则用：

```powershell
Remove-NetFirewallRule -DisplayName "OOPZ Voice Bridge 8765"
```

> 如果 PC-B 的 IP 会变（DHCP），要么给 PC-B 设静态 IP / DHCP 保留，要么把 `-RemoteAddress` 换成 `LocalSubnet`。

### 1.6 跨机模式启动

```powershell
.\.venv\Scripts\python bridge.py `
  --area <域ID> --channel <频道ID> `
  --host 0.0.0.0 --token $token `
  --mode loopback
```

启动日志里会出现：

```
本地音频服务: http://127.0.0.1:8765/?token=...
跨机器部署：MaiBot 插件填 ws://192.168.1.72:8765/maibot?token=...
```

> 页面自己仍然用 `127.0.0.1` 打开，只有 `/maibot` 暴露在局域网。

先保持 `--mode loopback`，等 PC-B 接通、MaiBot 能收到消息后，再换 `--mode ai`。

---

## 2. PC-B：装 MaiBot 插件

### 2.1 找到插件目录

一键版默认在：

```
<MaiBot 安装目录>\resources\modules\MaiBot\plugins\
```

比如：

```
D:\MaiBot\MaiBot OneKey\resources\modules\MaiBot\plugins\
```

如果你是源码部署，就是 `MaiBot\plugins\`。

### 2.2 复制插件

把 PC-A 上的 `oopz-maibot-voice\maibot_plugin\` 整个目录拷到 PC-B 的插件目录，**改名成 `oopz_voice`**：

```
plugins\
└─ oopz_voice\
   ├─ _manifest.json
   ├─ plugin.py
   └─ requirements.txt
```

### 2.3 确认 websockets

MaiBot 的消息协议库 `maim-message` 一般已经带了 `websockets`。保险起见在 MaiBot 的 Python 里确认一下（一键版的运行时路径见 §1.1，或直接看 MaiBot 启动日志有没有报缺包）：

```powershell
& "D:\MaiBot\MaiBot OneKey\resources\runtime\python\python.exe" -c "import websockets; print(websockets.__version__)"
```

没有就：

```powershell
& "D:\MaiBot\MaiBot OneKey\resources\runtime\python\python.exe" -m pip install "websockets>=12"
```

### 2.4 配置插件

重启 MaiBot，打开 WebUI → 插件 → **OOPZ 语音桥**，在「语音桥」分组里填：

| 字段 | 值 | 注意 |
|---|---|---|
| `bridge_url` | `ws://192.168.1.72:8765/maibot` | 用 PC-A 的 IP，不要写 `127.0.0.1` |
| `token` | 和 §1.3 生成的一致 | 不一致会被 401 拒绝 |
| `account_id` | `oopz-voice` | 保持默认也行，只用于路由 |
| `scope` | `voice` | 和默认一致 |
| `channel_id` | 语音频道 ID | 留空也能跑，但日志里群名会是占位 |
| `channel_name` | 开黑房 | 会作为群名注入 MaiBot |
| `reconnect_s` | `2` | 断线重连间隔 |

「语音」分组里：

| 字段 | 建议 |
|---|---|
| `max_reply_chars` | `140`（语音场景别让麦麦长篇大论） |
| `prefix` | 留空；想区分语音输入可以填 `[语音]` |

保存后插件会自动重连。

### 2.5 期望日志

PC-B（MaiBot 控制台）：

```
OOPZ 语音插件已加载，等待语音桥连接
已连接语音桥 ws://192.168.1.72:8765/maibot?token=...
语音输入已注入: 今天开黑吗
```

PC-A（bridge）：

```
MaiBot 插件已连接
```

---

## 3. 连通性检查（按顺序，哪步断查哪步）

**第 1 步：网络通不通**（在 PC-B 上）

```powershell
Test-NetConnection 192.168.1.72 -Port 8765
```

`TcpTestSucceeded : True` 才继续。False → 查 PC-A 的 `--host 0.0.0.0`、防火墙规则、两台机是否同一网段。

**第 2 步：HTTP 通不通**（在 PC-B 上）

```powershell
Invoke-RestMethod "http://192.168.1.72:8765/health"
```

返回：

```text
ok                page_connected maibot_connected
--                -------------- ---------------
True                    False             False
```

**第 3 步：插件连上没有**（在 PC-B 上再查一次 `/health`）

`maibot_connected` 变成 `True` 就说明 token 和插件都对了。

**第 4 步：页面进房没有**（PC-A 的 bridge 日志）

```
浏览器已连接
页面已进房: agora_uid=374010679
身份帧: {'ok': True}
```

`身份帧 ok=False` → Agora 信令 WS 没起来，等几秒重试；一直失败查网络。

---

## 4. 第一次对话

PC-A 换成 AI 模式（MiMo key 填上）：

```powershell
$env:MIMO_API_KEY   = "sk-..."        # 小米 MiMo 开放平台（TTS + ASR）
$env:ASR_PROVIDER   = "mimo"
$env:TTS_PROVIDER   = "mimo"
$env:MIMO_TTS_VOICE = "冰糖"

.\.venv\Scripts\python bridge.py `
  --area <域ID> --channel <频道ID> `
  --host 0.0.0.0 --token $token `
  --mode ai --channel-name "开黑房"
```

对着麦克风说一句。PC-A 应该依次出现：

```
[ASR 420ms] uid=... -> 今天开黑吗
[MaiBot] 开啊，等我两分钟
[TTS 首句 380ms]
TTS 播放完成: id=1 1.80s ok=True
```

同时 PC-B 的 MaiBot 日志里能看到 `语音输入已注入`。

---

## 5. 不在同一个局域网

跨机器最常见的两种情况：一是 PC-B 在公司/学校网，二是 PC-A 在家。**不要**直接把 8765 端口映射到公网（明文 `ws://`，token 会裸奔）。

推荐按顺序选：

### 方案 A：Tailscale（最省事，推荐）

两台机器都装 [Tailscale](https://tailscale.com/)，登录同一账号，然后：

- PC-A 的 Tailscale IP 形如 `100.101.102.103`
- PC-B 插件里填 `ws://100.101.102.103:8765/maibot`
- `--host 0.0.0.0` 照旧，防火墙可以只放行 Tailscale 网段

优点：不用公网 IP、不用端口映射、自动 NAT 穿透、流量加密；延迟通常 10~40ms。
ZeroTier / Netbird 同理。

### 方案 B：SSH 隧道

PC-B 上建一条本地端口转发（需要 PC-B 能 SSH 到 PC-A，或者反过来）：

```powershell
ssh -N -L 8765:127.0.0.1:8765 user@192.168.1.72
```

然后 PC-B 插件里填 `ws://127.0.0.1:8765/maibot`。PC-A 的 bridge 可以继续只监听 `127.0.0.1`，最安全。

### 方案 C：frp / 公网 VPS 中转

需要一台有公网 IP 的服务器。把 8765 通过 frp 暴露成 `wss://` 更安全；仅 `ws://` 时至少要有 token + 只放行固定来源 IP。步骤多，不如 A/B。

---

## 6. 安全清单（跨机部署必看）

- [ ] `--host 0.0.0.0` 时**必须**有 `--token`，且长度 ≥ 32；
- [ ] 防火墙只放行 PC-B 的 IP，或只放行 Tailscale 网段；
- [ ] 网络配置文件是「专用网络」，不要放「公用网络」；
- [ ] 不要把 8765 直接映射到公网；
- [ ] OOPZ 用**小号**，不要把主号凭证放上去；
- [ ] token 泄露了就重新生成，两边一起换。

---

## 7. 排错表

| 症状 | 先查 | 怎么修 |
|---|---|---|
| PC-B 连不上 8765 | `Test-NetConnection` | PC-A 是否 `--host 0.0.0.0`；防火墙；两台机是否同网段 |
| 插件日志 `语音桥拒绝了连接：token ... 不一致` | 两边 token | 重新复制一遍，注意首尾空格 |
| `/health` 里 `maibot_connected=False` | PC-B 插件是否加载、URL 是否写错 | 看 MaiBot 插件日志；确认 `bridge_url` 是 PC-A 的 IP |
| `/health` 里 `page_connected=False` | PC-A 的 Chromium | 看 bridge 日志有没有 `浏览器已连接`；Playwright 是否装好 |
| `语音网关就绪上报被 Host 拒绝` | `account_id / scope` | 改回默认 `oopz-voice / voice`，重启插件 |
| 插件日志 `Host 丢弃了语音消息` | `config/adapter_policy.toml` | 给 `oopz_voice` 平台加 allow 条目，或删除该文件（默认全放行） |
| 有 `[ASR]` 但没有 `[MaiBot]` | PC-B 是否注入成功 | 看 PC-B 日志；没有就走上一行 |
| 有 `[MaiBot]` 但没有声音 | MiMo TTS 或 edge-tts | 看 bridge 日志里 `MiMo TTS HTTP xxx`；临时切 `TTS_PROVIDER=edge` 对比 |
| 回声测试正常，跨机后变慢 | WiFi / Tailscale 中继 | 走有线；Tailscale 用直连不中继；看 `tailscale ping` |
| 两边日志都正常，但频道里没声 | 身份帧 / 音轨 | 看 `身份帧 ok=True`；`TTS 播放完成 ok=True` |

---

## 8. 其他部署变体

**MaiBot 和语音桥在同一台机器**：不用 `--host 0.0.0.0`，插件 `bridge_url` 写 `ws://127.0.0.1:8765/maibot`，token 可以留空。

**MaiBot 在 Linux**：插件目录是 `MaiBot/plugins/oopz_voice/`；WebUI 配置字段一样；确认 `pip install websockets`。

**语音桥在 Linux 无头服务器**：`python -m playwright install --with-deps chromium`；OOPZ 凭证/频道 ID 不变；`--host 0.0.0.0` 并配置 `ufw allow from 192.168.1.50 to any port 8765`。

**MaiBot 那台机器性能更好，想反过来**：把 `maibot_plugin` 照旧装到 MaiBot 那台；语音桥仍然放在有音频/Chromium 需求的那台。两端只通过一个 WS 连，谁在哪台不影响结构。

---

## 9. 两端配置对照

| 配置 | 在哪 | 必须一致 |
|---|---|---|
| `--port 8765` | PC-A 命令行 | 和插件 `bridge_url` 的端口 |
| `--token` | PC-A 命令行 / `BRIDGE_TOKEN` | 和插件 `token` 字段 |
| `--host 0.0.0.0` | PC-A 命令行 | 跨机必须 |
| `bridge_url` | PC-B 插件 WebUI | 指向 PC-A 的 IP |
| `account_id` / `scope` | PC-B 插件 WebUI | 网关内部用，保持默认即可 |
| `channel_id` / `channel_name` | PC-B 插件 WebUI | 只影响注入消息里的群名 |
| `--area` / `--channel` | PC-A 命令行 | OOPZ 的域和语音频道 |
