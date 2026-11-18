"""语音对话管线：VAD 断句 → 云端 ASR → MaiBot → edge-tts → 浏览器播放。

设计目标是在弱机器（2C4T、无 CUDA）上把延迟压到 1~2 秒：

  * VAD 用能量门限（自适应噪声底），CPU 开销可以忽略；
  * ASR 默认走小米 MiMo（`/v1/chat/completions` + 音频 input），也可切 OpenAI 兼容；
  * TTS 默认走小米 MiMo（流式 24k PCM，边收边播），也可切 edge-tts；
  * 回复按句切分，第一句合成完就播，不等整段。

接其他 ASR/TTS 只需要改 `_asr()` / `_speak()` 两个入口。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import time
import wave
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import aiohttp
import numpy as np

if TYPE_CHECKING:
    from bridge import AudioServer

LOG = logging.getLogger("oopz-voice.pipeline")

SAMPLE_RATE = 16000
FRAME_MS = 20
BYTES_PER_MS = SAMPLE_RATE * 2 // 1000  # 32 B/ms


# --------------------------------------------------------------------------- 配置
@dataclass(slots=True)
class VoiceConfig:
    channel_id: str = ""
    channel_name: str = "OOPZ 语音频道"

    # 云端 ASR / TTS 提供方
    asr_provider: str = "mimo"      # mimo | openai
    tts_provider: str = "mimo"      # mimo | edge

    # 小米 MiMo（TTS 与 ASR 都走 /v1/chat/completions）
    mimo_api_key: str = ""
    mimo_asr_key: str = ""
    mimo_tts_key: str = ""
    mimo_base_url: str = "https://api.xiaomimimo.com/v1"
    mimo_asr_model: str = "mimo-v2.5-asr"
    mimo_asr_language: str = "zh"          # auto / zh / en
    mimo_tts_model: str = "mimo-v2.5-tts"
    mimo_tts_voice: str = "茉莉"            # 冰糖 / 茉莉 / 苏打 / 白桦 / mia ...
    mimo_tts_style: str = "自然口语"          # 越短，首包越快；留空则不加 user 消息
    mimo_tts_stream: bool = True           # pcm16 流式，首包更快

    # OpenAI 兼容 ASR（备选：硅基流动 / OpenAI / Groq）
    asr_base: str = "https://api.siliconflow.cn/v1"
    asr_key: str = ""
    asr_model: str = "FunAudioLLM/SenseVoiceSmall"
    asr_language: str = "zh"

    # TTS：edge-tts 备选
    tts_voice: str = "zh-CN-XiaoxiaoNeural"
    tts_rate: str = "+0%"

    # VAD
    vad_floor: float = 0.010        # 绝对门限（RMS，0~1），安静房间 0.008~0.015
    vad_noise_ratio: float = 3.0    # 或 噪声底 * 该倍数
    vad_start_frames: int = 3       # 连续 3 帧（60ms）才算开口
    vad_silence_ms: int = 560       # 尾静音多久算说完
    vad_min_speech_ms: int = 260    # 短于这个当噪声丢掉
    vad_max_utterance_s: float = 12.0
    vad_preroll_frames: int = 8     # 200ms 预录

    # 对话
    half_duplex: bool = True        # 机器人说话时不收音（先稳，后做打断）
    reply_idle_s: float = 1.6       # 最后一条回复后多久认为这一轮结束
    max_reply_chars: int = 300      # 超长回复截断，防止霸麦
    tts_first_max: int = 0           # 0=不切第一块，保证完整句上下文；>0 才会切短

    @classmethod
    def from_env(cls) -> "VoiceConfig":
        cfg = cls()
        cfg.asr_provider = os.environ.get("ASR_PROVIDER", cfg.asr_provider).strip().lower()
        cfg.tts_provider = os.environ.get("TTS_PROVIDER", cfg.tts_provider).strip().lower()

        cfg.mimo_api_key = os.environ.get("MIMO_API_KEY", "")
        cfg.mimo_asr_key = os.environ.get("MIMO_ASR_KEY", "")
        cfg.mimo_tts_key = os.environ.get("MIMO_TTS_KEY", "")
        cfg.mimo_base_url = os.environ.get("MIMO_BASE_URL", cfg.mimo_base_url)
        cfg.mimo_asr_model = os.environ.get("MIMO_ASR_MODEL", cfg.mimo_asr_model)
        cfg.mimo_asr_language = os.environ.get("MIMO_ASR_LANGUAGE", cfg.mimo_asr_language)
        cfg.mimo_tts_model = os.environ.get("MIMO_TTS_MODEL", cfg.mimo_tts_model)
        cfg.mimo_tts_voice = os.environ.get("MIMO_TTS_VOICE", cfg.mimo_tts_voice)
        cfg.mimo_tts_style = os.environ.get("MIMO_TTS_STYLE", cfg.mimo_tts_style)
        cfg.mimo_tts_stream = os.environ.get("MIMO_TTS_STREAM", "1") not in {"0", "false", "no"}

        cfg.asr_base = os.environ.get("ASR_BASE_URL", cfg.asr_base)
        cfg.asr_key = os.environ.get("ASR_API_KEY", "")
        cfg.asr_model = os.environ.get("ASR_MODEL", cfg.asr_model)
        cfg.asr_language = os.environ.get("ASR_LANGUAGE", cfg.asr_language)
        cfg.tts_voice = os.environ.get("TTS_VOICE", cfg.tts_voice)
        cfg.tts_rate = os.environ.get("TTS_RATE", cfg.tts_rate)
        cfg.vad_floor = float(os.environ.get("VAD_FLOOR", cfg.vad_floor))
        cfg.vad_silence_ms = int(os.environ.get("VAD_SILENCE_MS", cfg.vad_silence_ms))
        cfg.tts_first_max = int(os.environ.get("TTS_FIRST_MAX", cfg.tts_first_max))
        cfg.half_duplex = os.environ.get("HALF_DUPLEX", "1") not in {"0", "false", "no"}
        return cfg


# --------------------------------------------------------------------------- 工具
def pcm_to_wav(pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


_SPLIT_RE = re.compile(r"(?<=[。！？!?；;…])")


def split_sentences(text: str, max_len: int = 60, first_max: int = 14) -> list[str]:
    """按标点切句；第一块再切短，让它尽快出声。"""
    text = (text or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in _SPLIT_RE.split(text) if p.strip()]
    out: list[str] = []
    for part in parts:
        while len(part) > max_len:
            out.append(part[:max_len])
            part = part[max_len:]
        if part:
            out.append(part)
    out = out or [text]

    # 第一块优先出声：按逗号/顿号再切，必要时硬切
    if first_max > 0 and len(out[0]) > first_max:
        first = out[0]
        clauses = [c for c in re.split(r"(?<=[，,、；;])", first) if c.strip()]
        chunks: list[str] = []
        current = ""
        for clause in clauses or [first]:
            if not current or len(current) + len(clause) <= first_max:
                current += clause
            else:
                chunks.append(current)
                current = clause
        if current:
            chunks.append(current)
        while chunks and len(chunks[0]) > first_max:
            chunks = [chunks[0][:first_max], chunks[0][first_max:]] + chunks[1:]
        chunks = [c.strip() for c in chunks if c.strip()]
        if chunks:
            out = chunks + out[1:]
    return out


def upsample_2x(s16le: bytes) -> bytes:
    """24k -> 48k。MiMo TTS 的流式输出是 24kHz PCM16LE 单声道。"""
    x = np.frombuffer(s16le, dtype="<i2").astype(np.float32)
    if x.size == 0:
        return b""
    n = x.size
    y = np.interp(
        np.arange(n * 2, dtype=np.float64) / 2.0,
        np.arange(n, dtype=np.float64),
        x,
    )
    return np.clip(y, -32768, 32767).astype("<i2").tobytes()


# --------------------------------------------------------------------------- 单人 VAD
class UserVad:
    """一路 PCM 的断句器。feed() 是同步的，说完后异步回调。"""

    def __init__(self, uid: int, cfg: VoiceConfig, on_utterance) -> None:
        self.uid = uid
        self.cfg = cfg
        self.on_utterance = on_utterance
        self.noise = 0.01
        self.in_speech = False
        self.run = 0
        self.silence_ms = 0
        self.speech_ms = 0
        self.buf = bytearray()
        self.pre: deque[bytes] = deque(maxlen=cfg.vad_preroll_frames)
        self._tasks: set[asyncio.Task] = set()

    def feed(self, pcm: bytes) -> None:
        if len(pcm) < 2:
            return
        x = np.frombuffer(pcm, dtype="<i2")
        if x.size == 0:
            return
        rms = float(np.sqrt(np.mean((x.astype(np.float32) / 32768.0) ** 2)) + 1e-9)
        thr = max(self.cfg.vad_floor, self.noise * self.cfg.vad_noise_ratio)

        if not self.in_speech:
            self.noise = 0.98 * self.noise + 0.02 * rms
            self.pre.append(pcm)
            if rms > thr:
                self.run += 1
                if self.run >= self.cfg.vad_start_frames:
                    self.in_speech = True
                    self.buf = bytearray(b"".join(self.pre))
                    self.pre.clear()
                    self.speech_ms = len(self.buf) // BYTES_PER_MS
                    self.silence_ms = 0
            else:
                self.run = 0
            return

        self.buf.extend(pcm)
        self.speech_ms += FRAME_MS
        if rms > thr * 0.75:
            self.silence_ms = 0
        else:
            self.silence_ms += FRAME_MS

        finished = (
            self.silence_ms >= self.cfg.vad_silence_ms
            or self.speech_ms >= int(self.cfg.vad_max_utterance_s * 1000)
        )
        if not finished:
            return

        trim = self.silence_ms * BYTES_PER_MS
        pcm_out = bytes(self.buf[:-trim]) if 0 < trim < len(self.buf) else bytes(self.buf)
        speech_ms = self.speech_ms
        self._reset()
        if speech_ms >= self.cfg.vad_min_speech_ms:
            task = asyncio.create_task(self.on_utterance(self.uid, pcm_out))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    def _reset(self) -> None:
        self.in_speech = False
        self.run = 0
        self.silence_ms = 0
        self.speech_ms = 0
        self.buf.clear()


# --------------------------------------------------------------------------- 主管线
class VoiceConversation:
    def __init__(self, server: "AudioServer", cfg: VoiceConfig) -> None:
        self.server = server
        self.cfg = cfg
        self.vads: dict[int, UserVad] = {}
        self.identity: dict[int, str] = {}
        self.session: aiohttp.ClientSession | None = None
        self.speaking = False
        self.turn = ""
        self._idle_task: asyncio.Task | None = None
        self._reply_lock = asyncio.Lock()
        self.metrics = {"utt": 0, "asr_ms": 0.0, "reply_ms": 0.0, "tts_ms": 0.0}

    # ------------------------------------------------------------------ 生命周期
    async def on_ready(self) -> None:
        if self.session is None:
            self.session = aiohttp.ClientSession()
        LOG.info(
            "语音管线就绪：ASR=%s  TTS=%s  MiMo=%s",
            self.cfg.asr_provider,
            self.cfg.tts_provider,
            "on" if (self.cfg.mimo_api_key or self.cfg.mimo_asr_key or self.cfg.mimo_tts_key) else "off",
        )
        if self.cfg.asr_provider == "mimo" and not (self.cfg.mimo_asr_key or self.cfg.mimo_api_key):
            LOG.warning("ASR_PROVIDER=mimo 但没有 MIMO_API_KEY，会回退 OpenAI 兼容 ASR")
        if self.cfg.asr_provider == "openai" and not self.cfg.asr_key:
            LOG.warning("ASR_PROVIDER=openai 但没有 ASR_API_KEY")
        if self.cfg.tts_provider == "mimo" and not (self.cfg.mimo_tts_key or self.cfg.mimo_api_key):
            LOG.warning("TTS_PROVIDER=mimo 但没有 MIMO_API_KEY，会回退 edge-tts")
        if self.cfg.tts_provider == "mimo":
            LOG.info(
                "MiMo TTS: model=%s voice=%s stream=%s",
                self.cfg.mimo_tts_model,
                self.cfg.mimo_tts_voice,
                self.cfg.mimo_tts_stream,
            )

    async def close(self) -> None:
        if self._idle_task:
            self._idle_task.cancel()
        if self.session:
            await self.session.close()

    async def on_identity(self, agora_uid: int, oopz_uid: str) -> None:
        self.identity[agora_uid] = oopz_uid

    # ------------------------------------------------------------------ 入站音频
    async def on_pcm(self, agora_uid: int, pcm_16k: bytes) -> None:
        vad = self.vads.get(agora_uid)
        if vad is None:
            vad = UserVad(agora_uid, self.cfg, self._on_utterance)
            self.vads[agora_uid] = vad
        vad.feed(pcm_16k)

    async def _on_utterance(self, uid: int, pcm: bytes) -> None:
        if self.speaking and self.cfg.half_duplex:
            LOG.debug("机器人正在说话，忽略 uid=%s 的这一句", uid)
            return

        self.metrics["utt"] += 1
        t0 = time.perf_counter()
        text = await self._asr(pcm)
        asr_ms = (time.perf_counter() - t0) * 1000
        self.metrics["asr_ms"] = asr_ms
        if not text:
            return
        LOG.info("[ASR %.0fms] uid=%s -> %s", asr_ms, uid, text)

        turn = uuid4().hex
        self.turn = turn
        self.speaking = True
        self._set_idle(20.0)  # 等 MaiBot 回复期间不许被新语音抢掉

        ok = await self.server.send_to_maibot(
            {
                "type": "say",
                "turn_id": turn,
                "text": text,
                "user_id": str(self.identity.get(uid) or uid),
                "nickname": f"语音用户{uid}",
                "channel_id": self.cfg.channel_id,
                "channel_name": self.cfg.channel_name,
            }
        )
        if not ok:
            LOG.warning("MaiBot 插件没连上，丢掉这一句")
            self.speaking = False

    # ------------------------------------------------------------------ MaiBot 回复
    async def on_reply(self, payload: dict[str, Any]) -> None:
        text = str(payload.get("text") or "").strip()
        if not text:
            return
        if len(text) > self.cfg.max_reply_chars:
            text = text[: self.cfg.max_reply_chars] + "。"
        LOG.info("[MaiBot] %s", text)

        # 多段回复按到达顺序串行合成，避免两次 TTS 交错
        async with self._reply_lock:
            self._set_idle(20.0)
            t0 = time.perf_counter()
            first = True
            for sentence in split_sentences(text, first_max=self.cfg.tts_first_max):
                LOG.info("[TTS 文本] %s", sentence)
                sentence_t0 = time.perf_counter()
                duration = await self._speak(sentence, sentence_t0)
                if duration <= 0:
                    continue
                if first:
                    self.metrics["tts_ms"] = (time.perf_counter() - t0) * 1000
                    LOG.info(
                        "[TTS 整句 %.0fms] provider=%s",
                        self.metrics["tts_ms"],
                        self.cfg.tts_provider,
                    )
                    first = False
                self._set_idle(self.cfg.reply_idle_s + duration)

    # ------------------------------------------------------------------ 说话
    async def _speak(self, text: str, started_at: float) -> float:
        """合成一句并推给浏览器；返回估算播放秒数，<=0 表示失败。"""
        if self.cfg.tts_provider == "mimo":
            key = self.cfg.mimo_tts_key or self.cfg.mimo_api_key
            if key:
                if self.cfg.mimo_tts_stream:
                    duration = await self._tts_mimo_stream(text, key, started_at)
                    if duration > 0:
                        return duration
                mp3 = await self._tts_mimo(text, key, started_at)
                if mp3:
                    await self.server.send_speak(mp3)
                    return max(0.5, len(mp3) / 6000.0)
                LOG.warning("MiMo TTS 失败，回退 edge-tts")
            else:
                LOG.warning("TTS_PROVIDER=mimo 但没有 MIMO_API_KEY，回退 edge-tts")

        mp3 = await self._tts_edge(text, started_at)
        if mp3:
            await self.server.send_speak(mp3)
            return max(0.5, len(mp3) / 6000.0)
        return 0.0

    # ------------------------------------------------------------------ MiMo（TTS + ASR）
    def _mimo_url(self) -> str:
        return self.cfg.mimo_base_url.rstrip("/") + "/chat/completions"

    @staticmethod
    def _mimo_headers(key: str) -> dict[str, str]:
        return {"api-key": key, "Content-Type": "application/json"}

    async def _tts_mimo(self, text: str, key: str, started_at: float = 0.0) -> bytes:
        messages = []
        if self.cfg.mimo_tts_style:
            messages.append({"role": "user", "content": self.cfg.mimo_tts_style})
        messages.append({"role": "assistant", "content": text})
        payload = {
            "model": self.cfg.mimo_tts_model,
            "messages": messages,
            "audio": {"format": "mp3", "voice": self.cfg.mimo_tts_voice},
            "stream": False,
        }
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with self.session.post(
                self._mimo_url(), json=payload, headers=self._mimo_headers(key), timeout=timeout
            ) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    LOG.error("MiMo TTS HTTP %s: %s", resp.status, body[:300])
                    return b""
                data = json.loads(body)
                choices = data.get("choices") or []
                if not choices:
                    return b""
                audio = (choices[0].get("message") or {}).get("audio") or {}
                b64 = audio.get("data")
                if not b64:
                    return b""
                if started_at:
                    LOG.info("[TTS 整段 %.0fms] provider=mimo", (time.perf_counter() - started_at) * 1000)
                return base64.b64decode(b64)
        except Exception:
            LOG.exception("MiMo TTS 请求失败")
            return b""

    async def _tts_mimo_stream(self, text: str, key: str, started_at: float = 0.0) -> float:
        """流式 PCM16（24kHz 单声道），边收边推，首包最快。"""
        messages = []
        if self.cfg.mimo_tts_style:
            messages.append({"role": "user", "content": self.cfg.mimo_tts_style})
        messages.append({"role": "assistant", "content": text})
        payload = {
            "model": self.cfg.mimo_tts_model,
            "messages": messages,
            "audio": {"format": "pcm16", "voice": self.cfg.mimo_tts_voice},
            "stream": True,
        }
        sent = 0
        first_logged = False
        try:
            timeout = aiohttp.ClientTimeout(total=60)
            async with self.session.post(
                self._mimo_url(), json=payload, headers=self._mimo_headers(key), timeout=timeout
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    LOG.error("MiMo TTS(stream) HTTP %s: %s", resp.status, body[:300])
                    return 0.0
                async for raw in resp.content:
                    line = raw.strip()
                    if not line.startswith(b"data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == b"[DONE]":
                        break
                    try:
                        obj = json.loads(chunk)
                    except Exception:
                        continue
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    audio = (choices[0].get("delta") or {}).get("audio") or {}
                    b64 = audio.get("data")
                    if not b64:
                        continue
                    pcm = base64.b64decode(b64)
                    if not pcm:
                        continue
                    if not first_logged:
                        first_logged = True
                        if started_at:
                            LOG.info("[TTS 首包 %.0fms] provider=mimo", (time.perf_counter() - started_at) * 1000)
                    out = upsample_2x(pcm)
                    await self.server.send_pcm(out)
                    sent += len(out)
            return sent / 96000.0  # 48k * 2B
        except Exception:
            LOG.exception("MiMo TTS 流式失败")
            return 0.0

    async def _asr_mimo(self, pcm: bytes, key: str) -> str:
        wav = pcm_to_wav(pcm)
        b64 = base64.b64encode(wav).decode("ascii")
        payload = {
            "model": self.cfg.mimo_asr_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {"data": f"data:audio/wav;base64,{b64}"},
                        }
                    ],
                }
            ],
            "asr_options": {"language": self.cfg.mimo_asr_language or "auto"},
            "stream": False,
        }
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with self.session.post(
                self._mimo_url(), json=payload, headers=self._mimo_headers(key), timeout=timeout
            ) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    LOG.error("MiMo ASR HTTP %s: %s", resp.status, body[:300])
                    return ""
                return self._parse_mimo_asr(body)
        except Exception:
            LOG.exception("MiMo ASR 请求失败")
            return ""

    @staticmethod
    def _parse_mimo_asr(body: str) -> str:
        try:
            data = json.loads(body)
        except Exception:
            return ""
        choices = data.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            content = message.get("content")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                parts = [str(p.get("text") or "") for p in content if isinstance(p, dict)]
                return "".join(parts).strip()
        return str(data.get("text") or "").strip()

    # ------------------------------------------------------------------ ASR
    async def _asr(self, pcm: bytes) -> str:
        if self.session is None:
            return ""
        if self.cfg.asr_provider == "mimo":
            key = self.cfg.mimo_asr_key or self.cfg.mimo_api_key
            if key:
                return await self._asr_mimo(pcm, key)
            LOG.warning("ASR_PROVIDER=mimo 但没有 MIMO_API_KEY，回退 OpenAI 兼容 ASR")
        return await self._asr_openai(pcm)

    async def _asr_openai(self, pcm: bytes) -> str:
        if not self.cfg.asr_key or self.session is None:
            return ""
        url = self.cfg.asr_base.rstrip("/") + "/audio/transcriptions"
        form = aiohttp.FormData()
        form.add_field("file", pcm_to_wav(pcm), filename="utter.wav", content_type="audio/wav")
        form.add_field("model", self.cfg.asr_model)
        if self.cfg.asr_language:
            form.add_field("language", self.cfg.asr_language)
        headers = {"Authorization": f"Bearer {self.cfg.asr_key}"}
        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with self.session.post(url, data=form, headers=headers, timeout=timeout) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    LOG.error("ASR HTTP %s: %s", resp.status, body[:300])
                    return ""
                data = json.loads(body)
                return str(data.get("text") or "").strip()
        except Exception:
            LOG.exception("ASR 请求失败")
            return ""

    # ------------------------------------------------------------------ TTS（edge 备选）
    async def _tts_edge(self, text: str, started_at: float = 0.0) -> bytes:
        try:
            import edge_tts
        except ImportError:
            LOG.error("缺少 edge-tts：pip install edge-tts")
            return b""
        try:
            communicate = edge_tts.Communicate(text, self.cfg.tts_voice, rate=self.cfg.tts_rate)
            out = bytearray()
            first = True
            async for chunk in communicate.stream():
                if chunk.get("type") == "audio" and chunk.get("data"):
                    if first and started_at:
                        LOG.info("[TTS 首块 %.0fms] provider=edge", (time.perf_counter() - started_at) * 1000)
                        first = False
                    out.extend(chunk["data"])
            return bytes(out)
        except Exception:
            LOG.exception("edge-tts 失败")
            return b""

    # ------------------------------------------------------------------ 轮次状态
    def _set_idle(self, delay: float) -> None:
        if self._idle_task:
            self._idle_task.cancel()
        self._idle_task = asyncio.create_task(self._idle_after(max(0.5, delay)))

    async def _idle_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        self.speaking = False
        LOG.debug("本轮结束，恢复收音")
