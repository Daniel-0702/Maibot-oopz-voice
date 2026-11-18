"""OOPZ 语音桥 —— MaiBot 插件（消息网关方向）。

职责很简单：

  * 作为 WebSocket **客户端**连到语音桥（默认 ws://127.0.0.1:8765/maibot）；
  * 收到语音桥转写出来的文本，包装成一条 MaiBot 消息注入 Host；
  * Host 生成回复后，把文本段发回语音桥，由语音桥做 TTS 播进频道。

MaiBot 1.3.x 的消息字典格式见 src/plugin_runtime/host/message_utils.py。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from maibot_sdk import (
    CONFIG_RELOAD_SCOPE_SELF,
    Field,
    MaiBotPlugin,
    MessageGateway,
    PluginConfigBase,
)

try:  # websockets 会由插件依赖声明自动安装；缺失时给出可读报错
    import websockets
except ImportError:  # pragma: no cover
    websockets = None  # type: ignore[assignment]

PLATFORM = "oopz_voice"
PROTOCOL = "agora"
GATEWAY_NAME = "oopz_voice"


# --------------------------------------------------------------------------- 配置
class PluginSection(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    config_version: str = Field(default="1.0.0", description="配置结构版本")


class BridgeSection(PluginConfigBase):
    """语音桥连接配置。"""

    __ui_label__ = "语音桥"
    __ui_icon__ = "cable"
    __ui_order__ = 1

    bridge_url: str = Field(
        default="ws://127.0.0.1:8765/maibot",
        description="bridge.py 的 /maibot WebSocket 地址；跨机器时填 ws://<语音桥IP>:8765/maibot",
    )
    token: str = Field(
        default="",
        description="语音桥的 --token，必须和 bridge.py 一致；跨机器部署时必填",
    )
    account_id: str = Field(default="oopz-voice", description="机器人账号标识（写进网关路由）")
    scope: str = Field(default="voice", description="路由作用域，和语音桥保持一致即可")
    channel_id: str = Field(default="", description="OOPZ 语音频道 ID；留空用占位值")
    channel_name: str = Field(default="OOPZ 语音频道", description="频道显示名，会作为群名注入")
    reconnect_s: float = Field(default=2.0, description="断线重连间隔（秒）")


class VoiceSection(PluginConfigBase):
    """语音对话行为。"""

    __ui_label__ = "语音"
    __ui_icon__ = "mic"
    __ui_order__ = 2

    max_reply_chars: int = Field(
        default=140,
        description="语音回复超过这个字数会被截断（语音场景别让麦麦长篇大论）",
    )
    prefix: str = Field(default="", description="注入消息的前缀，便于在日志里区分语音输入")


class OopzVoiceConfig(PluginConfigBase):
    """OOPZ 语音插件完整配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    bridge: BridgeSection = Field(default_factory=BridgeSection)
    voice: VoiceSection = Field(default_factory=VoiceSection)


# --------------------------------------------------------------------------- 插件
class OopzVoicePlugin(MaiBotPlugin):
    """把 OOPZ 语音频道接进麦麦的消息网关。"""

    config_model = OopzVoiceConfig

    def __init__(self) -> None:
        super().__init__()
        self._ws: Any = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._current_turn = ""

    # ------------------------------------------------------------------ 生命周期
    async def on_load(self) -> None:
        if websockets is None:
            self.ctx.logger.error("缺少 websockets：请给插件安装依赖 websockets>=12")
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="oopz-voice-gateway")
        self.ctx.logger.info("OOPZ 语音插件已加载，等待语音桥连接")

    async def on_unload(self) -> None:
        self._stop.set()
        await self._close_ws()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        await self._report_ready(False)
        self.ctx.logger.info("OOPZ 语音插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return
        self.ctx.logger.info("OOPZ 语音配置更新 %s，重连语音桥", version)
        self._stop.set()
        await self._close_ws()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="oopz-voice-gateway")

    # ------------------------------------------------------------------ 网关
    @MessageGateway(
        name=GATEWAY_NAME,
        route_type="duplex",
        platform=PLATFORM,
        protocol=PROTOCOL,
        description="OOPZ 语音频道双工网关（语音桥 ↔ MaiBot）",
    )
    async def handle_voice_gateway(
        self,
        message: dict[str, Any],
        route: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """出站：MaiBot 的回复 → 语音桥 → TTS → OOPZ 频道。"""
        del route, metadata, kwargs

        text = self._extract_text(message)
        if not text:
            return {"success": True, "external_message_id": None}

        max_chars = int(getattr(self.config.voice, "max_reply_chars", 0) or 0)
        if max_chars and len(text) > max_chars:
            text = text[:max_chars] + "。"
        text = str(getattr(self.config.voice, "prefix", "") or "") + text

        ws = self._ws
        if ws is None:
            return {"success": False, "error": "语音桥未连接"}

        payload = {
            "type": "reply",
            "turn_id": self._current_turn or uuid4().hex,
            "text": text,
        }
        try:
            async with self._send_lock:
                await ws.send(json.dumps(payload, ensure_ascii=False))
        except Exception as exc:
            self.ctx.logger.warning("回复推送到语音桥失败: %s", exc)
            return {"success": False, "error": str(exc)}

        return {"success": True, "external_message_id": f"oopz-{uuid4().hex[:10]}"}

    # ------------------------------------------------------------------ 连接循环
    async def _run(self) -> None:
        url = self._ws_url()
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=4 * 1024 * 1024,
                    open_timeout=10,
                ) as ws:
                    self._ws = ws
                    self.ctx.logger.info("已连接语音桥 %s", url)
                    if not await self._report_ready(True):
                        self.ctx.logger.warning("语音网关就绪上报被 Host 拒绝，检查 platform/account_id/scope")
                    async for raw in ws:
                        await self._handle(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._stop.is_set():
                    message = str(exc)
                    if "401" in message or "Unauthorized" in message:
                        self.ctx.logger.warning("语音桥拒绝了连接：token 和 bridge.py 不一致（%s）", message)
                    else:
                        self.ctx.logger.warning("语音桥连接断开: %s", exc)
            finally:
                self._ws = None
                await self._report_ready(False)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=float(self._bridge_cfg().reconnect_s))
            except asyncio.TimeoutError:
                continue

    async def _handle(self, raw: str | bytes) -> None:
        try:
            payload = json.loads(raw)
        except Exception:
            return
        if payload.get("type") != "say":
            return
        await self._inject(payload)

    async def _inject(self, payload: dict[str, Any]) -> None:
        text = str(payload.get("text") or "").strip()
        if not text:
            return
        cfg = self._bridge_cfg()
        channel_id = str(payload.get("channel_id") or cfg.channel_id or "oopz_voice_channel")
        channel_name = str(payload.get("channel_name") or cfg.channel_name)
        turn_id = str(payload.get("turn_id") or uuid4().hex)
        self._current_turn = turn_id

        prefix = str(getattr(self.config.voice, "prefix", "") or "")
        user_id = str(payload.get("user_id") or "voice-user")
        nickname = str(payload.get("nickname") or "语音用户")
        message_id = f"oopz-voice-{turn_id}"

        message = {
            "message_id": message_id,
            "timestamp": str(time.time()),
            "platform": PLATFORM,
            "message_info": {
                "user_info": {
                    "user_id": user_id,
                    "user_nickname": nickname,
                    "user_cardname": None,
                },
                "group_info": {
                    "group_id": channel_id,
                    "group_name": channel_name,
                },
                "additional_config": {
                    "self_id": cfg.account_id,
                    "platform_io_target_group_id": channel_id,
                    "oopz_voice_turn": turn_id,
                },
            },
            "raw_message": [{"type": "text", "data": prefix + text}],
            "is_mentioned": True,
            "is_at": True,
            "is_emoji": False,
            "is_picture": False,
            "is_command": False,
            "is_notify": False,
            "session_id": "",
            "processed_plain_text": prefix + text,
            "display_message": prefix + text,
        }

        try:
            accepted = await self.ctx.gateway.route_message(
                gateway_name=GATEWAY_NAME,
                message=message,
                external_message_id=message_id,
                dedupe_key=message_id,
            )
        except Exception:
            self.ctx.logger.exception("语音消息注入 Host 失败")
            return
        if not accepted:
            self.ctx.logger.warning("Host 丢弃了语音消息（检查 config/adapter_policy.toml）")
        else:
            self.ctx.logger.info("语音输入已注入: %s", text[:60])

    # ------------------------------------------------------------------ 工具
    async def _report_ready(self, ready: bool) -> bool:
        cfg = self._bridge_cfg()
        try:
            return bool(
                await self.ctx.gateway.update_state(
                    gateway_name=GATEWAY_NAME,
                    ready=ready,
                    platform=PLATFORM,
                    account_id=cfg.account_id,
                    scope=cfg.scope,
                )
            )
        except Exception as exc:
            self.ctx.logger.debug("上报网关状态失败: %s", exc)
            return False

    async def _close_ws(self) -> None:
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    def _ws_url(self) -> str:
        cfg = self._bridge_cfg()
        url = str(cfg.bridge_url or "").strip()
        if not url:
            return ""
        token = str(getattr(cfg, "token", "") or "").strip()
        if not token:
            return url
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        query["token"] = token
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    def _bridge_cfg(self) -> BridgeSection:
        try:
            return self.config.bridge
        except Exception:
            return BridgeSection()

    @staticmethod
    def _extract_text(message: dict[str, Any]) -> str:
        parts: list[str] = []
        raw = message.get("raw_message")
        if isinstance(raw, list):
            for segment in raw:
                if not isinstance(segment, dict):
                    continue
                if segment.get("type") != "text":
                    continue
                data = segment.get("data")
                if isinstance(data, str) and data:
                    parts.append(data)
        text = "".join(parts).strip()
        if not text:
            text = str(message.get("processed_plain_text") or "").strip()
        return text


def create_plugin() -> MaiBotPlugin:
    return OopzVoicePlugin()
