"""OOPZ x MaiBot 语音桥 —— 最小可跑骨架（先跑通"听"和"说"，再接大脑）。

这一版不接 ASR/LLM/TTS，只做两件事，用来验证最难的音频链路：

  * 听：订阅语音房里其他人的 Agora 音轨，抓成 16k PCM 回传 Python
  * 说：Python 把 48k PCM 推回浏览器，直接进房间

默认 `--loopback` 模式 = 回音测试：你在频道里说一句，约 350ms 后能听到自己
的声音，说明两条链路都通了。`--tone` 模式 = 机器人进房后每 3 秒"滴"一声，
用来单独验证"说"。

接大脑时只需要替换 `Pipeline.on_pcm()`：把 PCM 喂给 VAD/ASR，拿到文本后
交给 MaiBot，再把 TTS 的 48k PCM 通过 `server.send_pcm()` 推回来。

依赖：
    pip install oopz-sdk playwright aiohttp numpy
    python -m playwright install chromium

凭证（二选一）：
    A. 手机号 + 密码（SDK 会自己用 Playwright 登录并续期）
         set OOPZ_LOGIN_PHONE=...
         set OOPZ_LOGIN_PASSWORD=...
    B. 四件套（更稳，推荐；从网页登录态里提取）
         set OOPZ_DEVICE_ID=...
         set OOPZ_PERSON_UID=...
         set OOPZ_JWT_TOKEN=...
         set OOPZ_PRIVATE_KEY=...        # 换行写成 \\n

运行：
    python bridge.py --area <域ID> --channel <语音频道ID> --loopback
    python bridge.py --area <域ID> --channel <语音频道ID> --tone
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import socket
import struct
import sys
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import numpy as np
from aiohttp import WSMsgType, web

LOG = logging.getLogger("oopz-voice")
HERE = Path(__file__).resolve().parent
DEFAULT_AGORA_APP_ID = "358eebceadb94c2a9fd91ecd7b341602"  # OOPZ 客户端内置的 Agora AppID
# OOPZ 新 ID 是 26 位 ULID（01J4N...），旧版是 32 位十六进制；两种都直接透传
_ID_RE = re.compile(r"^[0-9A-Za-z]{20,40}$")


# --------------------------------------------------------------------------- 管线接口
class Pipeline(Protocol):
    async def on_pcm(self, agora_uid: int, pcm_16k: bytes) -> None: ...
    async def on_identity(self, agora_uid: int, oopz_uid: str) -> None: ...
    async def on_ready(self) -> None: ...
    async def close(self) -> None: ...


# --------------------------------------------------------------------------- 本地音频服务
class AudioServer:
    """给浏览器页面用的 HTTP + WebSocket 服务（只监听 127.0.0.1）。"""

    def __init__(self, host: str, port: int, html: Path, token: str = "") -> None:
        self.host = host
        self.port = port
        self.html = html
        self.token = token
        self.ws: web.WebSocketResponse | None = None
        self.maibot_ws: web.WebSocketResponse | None = None
        self.on_maibot_text = None  # async callable(payload: dict)
        self.pipeline: Pipeline | None = None
        self._runner: web.AppRunner | None = None
        self._tasks: set[asyncio.Task] = set()
        self._speak_id = 0

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.host in {"0.0.0.0", "::"} else self.host
        query = f"?token={quote(self.token)}" if self.token else ""
        return f"http://{host}:{self.port}/{query}"

    @staticmethod
    def _primary_ip() -> str:
        # UDP connect 不发包，只让系统选一条出口路由
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(("223.5.5.5", 80))
                return sock.getsockname()[0]
        except OSError:
            pass
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "<本机局域网 IP>"

    def lan_hint(self) -> str:
        ip = self._primary_ip()
        query = f"?token={quote(self.token)}" if self.token else ""
        return f"ws://{ip}:{self.port}/maibot{query}"

    def _token_ok(self, request: web.Request) -> bool:
        if not self.token:
            return True
        return request.query.get("token") == self.token

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/agora", self._ws)
        app.router.add_get("/maibot", self._maibot_ws)
        app.router.add_get("/health", self._health)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        LOG.info("本地音频服务: %s", self.url)

    async def stop(self) -> None:
        if self.ws and not self.ws.closed:
            with contextlib.suppress(Exception):
                await self.ws.close()
        if self._runner:
            await self._runner.cleanup()

    async def _index(self, _request: web.Request) -> web.StreamResponse:
        return web.FileResponse(self.html)

    async def _health(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "page_connected": self.ws is not None,
                "maibot_connected": self.maibot_ws is not None,
            }
        )

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        if not self._token_ok(request):
            return web.Response(status=401, text="token required")
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=16 << 20)
        await ws.prepare(request)
        self.ws = ws
        LOG.info("浏览器已连接")
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    self._on_binary(msg.data)
                elif msg.type == WSMsgType.TEXT:
                    await self._on_text(msg.data)
        finally:
            if self.ws is ws:
                self.ws = None
            LOG.info("浏览器已断开")
        return ws

    async def _maibot_ws(self, request: web.Request) -> web.WebSocketResponse:
        if not self._token_ok(request):
            LOG.warning("拒绝了一个 token 不匹配的 MaiBot 连接")
            return web.Response(status=401, text="token required")
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=4 << 20)
        await ws.prepare(request)
        self.maibot_ws = ws
        LOG.info("MaiBot 插件已连接")
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._on_maibot_text(msg.data)
        finally:
            if self.maibot_ws is ws:
                self.maibot_ws = None
            LOG.info("MaiBot 插件已断开")
        return ws

    async def _on_maibot_text(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except Exception:
            return
        kind = payload.get("type")
        if kind == "reply":
            callback = self.on_maibot_text
            if callback is not None:
                task = asyncio.create_task(self._safe_reply(callback, payload))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        elif kind == "ping":
            if self.maibot_ws and not self.maibot_ws.closed:
                await self.maibot_ws.send_str(json.dumps({"type": "pong"}))

    @staticmethod
    async def _safe_reply(callback, payload: dict[str, Any]) -> None:
        try:
            await callback(payload)
        except Exception:
            LOG.exception("处理 MaiBot 回复失败")

    async def send_to_maibot(self, payload: dict[str, Any]) -> bool:
        ws = self.maibot_ws
        if ws is None or ws.closed:
            return False
        try:
            await ws.send_str(json.dumps(payload, ensure_ascii=False))
            return True
        except Exception:
            LOG.exception("发给 MaiBot 插件失败")
            return False

    async def send_speak(self, mp3: bytes) -> bool:
        ws = self.ws
        if not mp3 or ws is None or ws.closed:
            return False
        self._speak_id += 1
        try:
            await ws.send_str(
                json.dumps(
                    {
                        "type": "speak",
                        "id": self._speak_id,
                        "data": base64.b64encode(mp3).decode("ascii"),
                    }
                )
            )
            return True
        except Exception:
            LOG.exception("推送 TTS 音频失败")
            return False

    def _on_binary(self, data: bytes) -> None:
        # [u8 kind=1][u32 agoraUid][u32 seq][s16le 16k]
        if len(data) < 9 or data[0] != 1:
            return
        agora_uid, _seq = struct.unpack_from("<II", data, 1)
        if self.pipeline is not None:
            self._spawn(self._feed(agora_uid, data[9:]))

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _feed(self, agora_uid: int, pcm: bytes) -> None:
        try:
            await self.pipeline.on_pcm(agora_uid, pcm)  # type: ignore[union-attr]
        except Exception:
            LOG.exception("管线处理 PCM 失败")

    async def _on_text(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return
        kind = msg.get("type")
        if kind == "hello":
            LOG.info("页面 hello: in=%s out=%s", msg.get("inRate"), msg.get("outRate"))
        elif kind == "joined":
            LOG.info("页面已进房: agora_uid=%s", msg.get("agora_uid"))
            if self.pipeline is not None:
                await self.pipeline.on_ready()
        elif kind == "identity":
            LOG.info("身份映射: agora=%s -> oopz=%s", msg.get("agora_uid"), msg.get("oopz_uid"))
            if self.pipeline is not None:
                await self.pipeline.on_identity(int(msg["agora_uid"]), str(msg["oopz_uid"]))
        elif kind == "speak_done":
            LOG.info("TTS 播放完成: id=%s %.2fs ok=%s", msg.get("id"), msg.get("duration", 0), msg.get("ok"))
        elif kind == "log":
            LOG.debug("[page] %s", msg.get("msg"))

    async def send_pcm(self, pcm_48k_s16le: bytes) -> None:
        if not pcm_48k_s16le:
            return
        if self.ws and not self.ws.closed:
            with contextlib.suppress(Exception):
                await self.ws.send_bytes(pcm_48k_s16le)

    async def flush(self) -> None:
        if self.ws and not self.ws.closed:
            with contextlib.suppress(Exception):
                await self.ws.send_str(json.dumps({"type": "flush"}))


# --------------------------------------------------------------------------- 测试用管线
def upsample_3x(s16le: bytes) -> bytes:
    """16k -> 48k（线性插值，够测试用；正式链路建议 soxr/libsamplerate）。"""
    x = np.frombuffer(s16le, dtype="<i2").astype(np.float32)
    if x.size == 0:
        return b""
    n = x.size
    y = np.interp(
        np.arange(n * 3, dtype=np.float64) / 3.0,
        np.arange(n, dtype=np.float64),
        x,
    )
    return np.clip(y, -32768, 32767).astype("<i2").tobytes()


class Loopback:
    """回音测试：听到谁的声音，延迟 delay 秒后原样播回去。"""

    def __init__(self, server: AudioServer, delay: float = 0.35) -> None:
        self.server = server
        self.delay = delay
        self.chunks = 0
        self._tasks: set[asyncio.Task] = set()

    async def on_pcm(self, agora_uid: int, pcm_16k: bytes) -> None:
        self.chunks += 1
        out = upsample_3x(pcm_16k)
        task = asyncio.create_task(self._later(out))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _later(self, pcm: bytes) -> None:
        await asyncio.sleep(self.delay)
        await self.server.send_pcm(pcm)

    async def on_identity(self, agora_uid: int, oopz_uid: str) -> None:
        pass

    async def on_ready(self) -> None:
        LOG.info("回音测试已就绪：对着麦克风说话，%.0fms 后应该听到自己", self.delay * 1000)

    async def close(self) -> None:
        pass


class ToneFeeder:
    """每 3 秒一声 440Hz，用来单独验证"机器人说话"这一条链路。"""

    def __init__(self, server: AudioServer, freq: float = 440.0, beep_ms: int = 200) -> None:
        self.server = server
        self.freq = freq
        self.beep_ms = beep_ms
        self._task: asyncio.Task | None = None

    def _beep(self) -> bytes:
        n = int(48000 * self.beep_ms / 1000)
        t = np.arange(n, dtype=np.float32) / 48000.0
        env = np.minimum(1.0, np.minimum(t * 200.0, (self.beep_ms / 1000 - t) * 200.0))
        y = np.sin(2 * np.pi * self.freq * t) * env * 0.25 * 32767
        return np.clip(y, -32768, 32767).astype("<i2").tobytes()

    async def on_pcm(self, agora_uid: int, pcm_16k: bytes) -> None:
        pass

    async def on_identity(self, agora_uid: int, oopz_uid: str) -> None:
        pass

    async def on_ready(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())
            LOG.info("蜂鸣模式：每 3 秒一声")

    async def _loop(self) -> None:
        while True:
            await self.server.send_pcm(self._beep())
            await asyncio.sleep(3)

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task


# --------------------------------------------------------------------------- 浏览器
class AgoraBrowser:
    def __init__(self, url: str, headless: bool = True) -> None:
        self.url = url
        self.headless = headless
        self._pw: Any = None
        self._browser: Any = None
        self._page: Any = None

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=self.headless,
            args=[
                "--autoplay-policy=no-user-gesture-required",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--disable-dev-shm-usage",
                # 老 AMD 显卡 + 无头环境用不上 GPU，关掉更稳
                "--disable-gpu",
            ],
        )
        self._page = await self._browser.new_page()
        self._page.on("console", lambda m: LOG.debug("[page] %s", m.text))
        self._page.on("pageerror", lambda e: LOG.warning("[page error] %s", e))
        await self._page.goto(self.url, wait_until="domcontentloaded")
        await self._page.wait_for_function("() => typeof window.agoraJoin === 'function'")
        LOG.info("浏览器页面已就绪")

    async def join(self, app_id: str, token: str, room: str, uid: int) -> dict:
        return await self._page.evaluate(
            "([a,t,c,u]) => window.agoraJoin(a,t,c,u)",
            [str(app_id), token, str(room), int(uid)],
        )

    async def set_identity(self, oopz_uid: str) -> dict:
        return await self._page.evaluate(
            "(u) => window.agoraSetVoiceIdentity(u)", str(oopz_uid)
        )

    async def status(self) -> dict:
        return await self._page.evaluate("() => window.agoraGetStatus()")

    async def leave(self) -> None:
        if self._page:
            with contextlib.suppress(Exception):
                await self._page.evaluate("() => window.agoraLeave()")

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            if self._browser:
                await self._browser.close()
        with contextlib.suppress(Exception):
            if self._pw:
                await self._pw.stop()


# --------------------------------------------------------------------------- OOPZ 配置
def _clean_token(value: str) -> str:
    """去掉控制台复制时混进来的换行/空格。

    Windows 控制台复制长行（尤其 JWT）会在换行处插入 CR/LF，放进 HTTP 头
    会触发 aiohttp 的 `ValueError: Newline or carriage return character...`。
    """
    return "".join(str(value or "").split())


async def build_oopz_config():
    from oopz_sdk import OopzConfig

    if os.environ.get("OOPZ_DEVICE_ID"):
        return OopzConfig(
            device_id=_clean_token(os.environ["OOPZ_DEVICE_ID"]),
            person_uid=_clean_token(os.environ["OOPZ_PERSON_UID"]),
            jwt_token=_clean_token(os.environ["OOPZ_JWT_TOKEN"]),
            private_key=os.environ.get("OOPZ_PRIVATE_KEY", "").replace("\\n", "\n"),
        )

    # 新版 SDK 禁止在运行中的事件循环里调用同步 from_env()
    from_env_async = getattr(OopzConfig, "from_env_async", None)
    if from_env_async is not None:
        return await from_env_async()
    return OopzConfig.from_env()


async def resolve_area(bot: Any, area: str) -> str:
    """--area 可以是域 ID（ULID/hex），也可以是域名。"""
    value = str(area or "").strip()
    if _ID_RE.fullmatch(value):
        return value

    areas = await bot.areas.get_joined_areas()
    exact = [a for a in areas if str(getattr(a, "name", "") or "").strip() == value]
    partial = [a for a in areas if value in str(getattr(a, "name", "") or "")]
    matches = exact or partial
    if len(matches) == 1:
        LOG.info("按名称匹配到域: %s -> %s", matches[0].name, matches[0].area_id)
        return str(matches[0].area_id)
    listing = ", ".join(f"{getattr(a, 'name', '')}({getattr(a, 'area_id', '')})" for a in areas) or "(无)"
    if not matches:
        raise RuntimeError(f"找不到名字含「{value}」的域；已加入的域: {listing}")
    raise RuntimeError("匹配到多个域，请写完整或直接用 ID: " + listing)


async def resolve_voice_channel(bot: Any, area: str, channel: str) -> str:
    """--channel 既可以是频道 ID（ULID/hex），也可以是语音频道名。

    OOPZ 客户端界面不显示频道 ID，所以允许直接填名字：
    在 area 下找同名的 VOICE/AUDIO 频道，唯一命中就返回它的 ID。
    """
    value = str(channel or "").strip()
    if _ID_RE.fullmatch(value):
        return value

    groups = await bot.areas.get_area_channels(area)
    matches: list[tuple[str, str]] = []
    all_voice: list[str] = []
    for group in groups:
        for ch in getattr(group, "channels", []) or []:
            ctype = str(getattr(ch, "channel_type", "") or "").upper()
            if ctype not in {"VOICE", "AUDIO"}:
                continue
            name = str(getattr(ch, "name", "") or "").strip()
            all_voice.append(name)
            if name == value or value in name:
                matches.append((name, str(ch.channel_id)))

    if len(matches) == 1:
        LOG.info("按名称匹配到语音频道: %s -> %s", matches[0][0], matches[0][1])
        return matches[0][1]
    if not matches:
        raise RuntimeError(
            f"在域 {area} 里找不到名字含「{value}」的语音频道；"
            f"现有语音频道: {', '.join(all_voice) or '(无)'}"
        )
    raise RuntimeError(
        "匹配到多个语音频道，请把 --channel 写得更完整或直接用 ID: "
        + ", ".join(f"{n}({i})" for n, i in matches)
    )


# --------------------------------------------------------------------------- 主流程
async def amain(args: argparse.Namespace) -> None:
    token = args.token or os.environ.get("BRIDGE_TOKEN", "")
    server = AudioServer(args.host, args.port, HERE / "agora_ai.html", token=token)

    mode = "tone" if args.tone else args.mode
    if mode == "ai":
        from pipeline import VoiceConfig, VoiceConversation

        voice_cfg = VoiceConfig.from_env()
        voice_cfg.channel_id = args.channel
        voice_cfg.channel_name = args.channel_name
        conversation = VoiceConversation(server, voice_cfg)
        server.pipeline = conversation
        server.on_maibot_text = conversation.on_reply
    elif mode == "tone":
        server.pipeline = ToneFeeder(server)
    else:
        server.pipeline = Loopback(server, args.delay)

    await server.start()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        LOG.info("跨机器部署：MaiBot 插件填 %s", server.lan_hint())
        if not server.token:
            LOG.warning("监听在非本机地址却没有设置 --token，局域网内任何人都能连上来")

    browser = AgoraBrowser(server.url, headless=not args.show)
    await browser.start()

    from oopz_sdk import OopzBot

    config = await build_oopz_config()
    bot = OopzBot(config)
    ready = asyncio.Event()

    @bot.on_ready
    async def _on_ready(_ctx):  # noqa: ANN001
        ready.set()

    bot_task = asyncio.create_task(bot.start(), name="oopz-ws")
    resolved_area = args.area
    resolved_channel = args.channel
    try:
        await asyncio.wait_for(ready.wait(), timeout=60)

        me = await bot.person.get_self_detail()
        rtc_uid = int(getattr(me, "pid", getattr(config, "person_uid", 0)))
        oopz_uid = str(getattr(config, "person_uid", rtc_uid))

        resolved_area = await resolve_area(bot, args.area)
        resolved_channel = await resolve_voice_channel(bot, resolved_area, args.channel)
        if server.pipeline is not None and hasattr(server.pipeline, "cfg"):
            server.pipeline.cfg.channel_id = resolved_channel

        sign = await bot.channels.enter_channel(
            channel=resolved_channel,
            area=resolved_area,
            channel_type="VOICE",
            # SDK 的 voice 实现传的是字符串 pid（rtc_uid = str(pid)），这里保持一致
            pid=str(rtc_uid),
        )
        app_id = getattr(config, "agora_app_id", None) or DEFAULT_AGORA_APP_ID

        result = await browser.join(app_id, sign.rtc_token, sign.rtc_channel_name, rtc_uid)
        if not (result and result.get("ok")):
            raise RuntimeError(f"浏览器进房失败: {result}")

        # 等 Agora 信令 WS 起来，再补身份帧（OOPZ 客户端靠它把 Agora uid 对回账号）
        await asyncio.sleep(1.0)
        ident = await browser.set_identity(oopz_uid)
        LOG.info("身份帧: %s", ident)
        LOG.info(
            "已进房 area=%s channel=%s rtc_uid=%s app=%s",
            resolved_area, resolved_channel, rtc_uid, app_id,
        )
        LOG.info("按 Ctrl+C 结束")
        await asyncio.Event().wait()
    finally:
        with contextlib.suppress(Exception):
            await browser.leave()
        with contextlib.suppress(Exception):
            await browser.close()
        with contextlib.suppress(Exception):
            await bot.channels.leave_voice_channel(
                channel=resolved_channel,
                area=resolved_area,
                target=getattr(config, "person_uid", None),
            )
        with contextlib.suppress(Exception):
            await bot.stop()
        if not bot_task.done():
            bot_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await bot_task
        if server.pipeline is not None:
            with contextlib.suppress(Exception):
                await server.pipeline.close()
        with contextlib.suppress(Exception):
            await server.stop()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="OOPZ <-> MaiBot 语音桥")
    p.add_argument("--area", required=True, help="域 ID（OOPZ 的 area/guild id）")
    p.add_argument("--channel", required=True, help="语音频道 ID 或频道名（如 开黑房）")
    p.add_argument("--host", default="127.0.0.1", help="监听地址；跨机器部署填 0.0.0.0")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--token", default="", help="访问令牌；跨机器部署必填（也可用 BRIDGE_TOKEN 环境变量）")
    p.add_argument("--delay", type=float, default=0.35, help="回音测试的延迟（秒）")
    p.add_argument("--mode", choices=["loopback", "tone", "ai"], default="loopback", help="运行模式")
    p.add_argument("--tone", action="store_true", help="等价于 --mode tone（旧参数）")
    p.add_argument("--channel-name", default="OOPZ 语音频道", help="语音频道显示名（给 MaiBot 的群名）")
    p.add_argument("--show", action="store_true", help="显示浏览器窗口，方便调试")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    if not (HERE / "agora_ai.html").exists():
        sys.exit("缺少 agora_ai.html")
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
