"""PCM plumbing: Telegram <-> SIP call bridge and SIP <-> SIP bridge (attended transfer)."""
from __future__ import annotations

import asyncio
import logging
import time

import numpy as np

from .audio.buffer import PcmBuffer
from .audio.record import CallRecorder
from .audio.resample import resample
from .audio.tones import ToneGenerator
from .sip.account import CallState, SipCall
from .tg.calls import TgCall
from .tg.group import TgGroupCall

log = logging.getLogger("sipgram.bridge")

TICK = 0.02


class CallBridge:
    def __init__(self, sip_call: SipCall, tg_call: TgCall, jitter_ms: int = 40, ringback: ToneGenerator | None = None):
        self.sip = sip_call
        self.tg = tg_call
        self.loop = asyncio.get_event_loop()
        self.jitter_ms = jitter_ms
        self.rate = tg_call.sample_rate
        self.frame10 = self.rate * 2 // 100
        self.frame20 = self.frame10 * 2
        self.jitter_bytes = max(self.frame20, self.rate * 2 * jitter_ms // 1000)
        self.buffer = PcmBuffer(self.jitter_bytes * 4)
        self.ringback = ringback
        self._carry = bytearray()
        self._tone: ToneGenerator | None = None
        self._sidetone = bytearray()
        self._recorder: CallRecorder | None = None
        self._rec_far = bytearray()
        self._timer: asyncio.TimerHandle | None = None
        self._next = 0.0
        self._primed = False
        self._running = False
        self.started = time.time()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        if self.sip.rate != self.rate:
            self.update_rate()
        self.sip.on_audio = self._from_sip
        self.tg.on_audio = self.buffer.push
        self._next = time.monotonic() + TICK
        self._timer = self.loop.call_at(self.loop.time() + TICK, self._tick)
        log.info("bridge started at %d Hz (jitter %d ms)", self.rate, self.jitter_bytes * 1000 // (self.rate * 2))

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._timer:
            self._timer.cancel()
            self._timer = None
        if self.sip.on_audio == self._from_sip:
            self.sip.on_audio = None
        if self.tg.on_audio == self.buffer.push:
            self.tg.on_audio = None
        log.info("bridge stopped: sip->tg %d frames, tg->sip %d frames, underruns %d, dropped %d B",
                 self.tg.frames_out, self.tg.frames_in, self.buffer.underruns, self.buffer.dropped)

    def update_rate(self) -> None:
        """Re-derive frame sizes after the Telegram leg switched sample rate."""
        self.rate = self.tg.sample_rate
        self.frame10 = self.rate * 2 // 100
        self.frame20 = self.frame10 * 2
        self.jitter_bytes = max(self.frame20, self.rate * 2 * self.jitter_ms // 1000)
        self.buffer = PcmBuffer(self.jitter_bytes * 4)
        self.tg.on_audio = self.buffer.push
        self._carry.clear()
        self._sidetone.clear()
        self._primed = False

    def play_tone(self, tone: ToneGenerator | None) -> None:
        """Feeds a tone to the Telegram side instead of SIP audio (busy signal etc.)."""
        self._tone = tone

    def start_recording(self, max_seconds: int) -> CallRecorder:
        """Both directions are mixed into an Ogg/Opus buffer while the call runs."""
        if self._recorder is None:
            self._recorder = CallRecorder(max_seconds)
            self._rec_far.clear()
        return self._recorder

    def stop_recording(self) -> CallRecorder | None:
        rec, self._recorder = self._recorder, None
        self._rec_far.clear()
        return rec

    @property
    def recording(self) -> bool:
        return self._recorder is not None

    def _record_tick(self, near: bytes) -> None:
        rec = self._recorder
        if rec is None:
            return
        n = self.frame20
        far = bytes(self._rec_far[:n])
        del self._rec_far[:n]
        if len(far) < n:
            far += bytes(n - len(far))
        near = (near or b"")[:n]
        if len(near) < n:
            near += bytes(n - len(near))
        rec.add(far, near, self.rate)

    def side_tone(self, pcm: bytes) -> None:
        """Keypad feedback for the Telegram user: Telegram plays no tone for digits we send to the PBX,
        so the tone is mixed into what the user hears, like the earpiece of a real phone."""
        self._sidetone.extend(pcm)

    def _mix_sidetone(self, frame: bytes) -> bytes:
        if not self._sidetone:
            return frame
        n = len(frame)
        tone = bytes(self._sidetone[:n])
        del self._sidetone[:n]
        if len(tone) < n:
            tone += b"\x00" * (n - len(tone))
        a = np.frombuffer(frame, dtype="<i2").astype(np.int32)
        b = np.frombuffer(tone, dtype="<i2").astype(np.int32)
        return np.clip(a + b, -32768, 32767).astype("<i2").tobytes()

    def _from_sip(self, pcm: bytes) -> None:
        if self.ringback is not None:
            self.ringback = None
        if self.sip.rate != self.rate:
            pcm = resample(pcm, self.sip.rate, self.rate)
        if self._recorder is not None:
            self._rec_far.extend(pcm)
        self._carry.extend(pcm)
        n = self.frame10
        while len(self._carry) >= n:
            self.tg.send_audio(self._mix_sidetone(bytes(self._carry[:n])))
            del self._carry[:n]

    def _tick(self) -> None:
        if not self._running:
            return
        now = time.monotonic()
        while self._next <= now:
            self._next += TICK
            self._pump()
        self._timer = self.loop.call_at(self.loop.time() + max(0.001, self._next - time.monotonic()), self._tick)

    def _pump(self) -> None:
        if self._tone is not None:
            self.tg.send_audio(self._tone.frame(self.rate // 100))
            self.tg.send_audio(self._tone.frame(self.rate // 100))
            return
        media = self.sip.state in (CallState.EARLY, CallState.CONNECTED)
        if not media and self.ringback is not None:
            self.tg.send_audio(self.ringback.frame(self.rate // 100))
            self.tg.send_audio(self.ringback.frame(self.rate // 100))
            return
        if not media:
            return
        if self._recorder is not None and self._recorder.samples >= self._recorder.max_samples:
            self._recorder.truncated = True
        if not self._primed:
            if len(self.buffer) < self.jitter_bytes:
                self.sip.send_silence(self.sip.rate // 50)
                self._record_tick(b"")
                return
            self._primed = True
        chunk = self.buffer.pull(self.frame20, pad=False)
        if chunk is None:
            self._primed = False
            self.sip.send_silence(self.sip.rate // 50)
            self._record_tick(b"")
            return
        self._record_tick(chunk)
        if self.sip.rate != self.rate:
            chunk = resample(chunk, self.rate, self.sip.rate)
        self.sip.send_pcm(chunk)


class SipSipBridge:
    """Connects two SIP calls directly (the Telegram user has left the conversation)."""

    def __init__(self, a: SipCall, b: SipCall):
        self.a = a
        self.b = b
        self._running = False

    def start(self) -> None:
        self._running = True
        self.a.on_audio = lambda pcm: self.b.send_pcm(resample(pcm, self.a.rate, self.b.rate))
        self.b.on_audio = lambda pcm: self.a.send_pcm(resample(pcm, self.b.rate, self.a.rate))
        log.info("sip<->sip bridge started: %s <-> %s", self.a.call_id[:8], self.b.call_id[:8])

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self.a.on_audio = None
        self.b.on_audio = None


class GroupBridge:
    """Mixes several SIP calls into one Telegram voice chat.

    Everyone hears everyone but themselves: a leg gets the voice chat plus the other legs, the voice
    chat gets the sum of the legs. The chat runs at 48 kHz, so each leg is resampled to its own rate.
    """

    def __init__(self, group: TgGroupCall, jitter_ms: int = 40):
        self.group = group
        self.loop = asyncio.get_event_loop()
        self.rate = group.sample_rate
        self.frame10 = self.rate * 2 // 100
        self.frame20 = self.frame10 * 2
        self.limit = max(self.frame20 * 2, self.rate * 2 * jitter_ms // 1000) * 4
        self.legs: dict[int, tuple[SipCall, PcmBuffer]] = {}
        self.sources: dict[int, PcmBuffer] = {}
        self._timer: asyncio.TimerHandle | None = None
        self._next = 0.0
        self._running = False
        self.started = time.time()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self.group.on_audio = self._from_group
        self._next = time.monotonic() + TICK
        self._timer = self.loop.call_at(self.loop.time() + TICK, self._tick)
        log.info("group bridge started for %s at %d Hz", self.group.title, self.rate)

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._timer:
            self._timer.cancel()
            self._timer = None
        if self.group.on_audio == self._from_group:
            self.group.on_audio = None
        for call, _ in self.legs.values():
            call.on_audio = None
        self.legs.clear()
        self.sources.clear()
        log.info("group bridge stopped: sip->chat %d frames, chat->sip %d frames",
                 self.group.frames_out, self.group.frames_in)

    def add(self, call: SipCall) -> None:
        buffer = PcmBuffer(self.limit)
        self.legs[id(call)] = (call, buffer)
        call.on_audio = lambda pcm, c=call: self._from_leg(c, pcm)
        log.info("group bridge: %s joined, %d leg(s)", call.call_id[:8], len(self.legs))

    def remove(self, call: SipCall) -> None:
        if self.legs.pop(id(call), None) is not None:
            call.on_audio = None
            log.info("group bridge: %s left, %d leg(s)", call.call_id[:8], len(self.legs))

    def __len__(self) -> int:
        return len(self.legs)

    def _from_leg(self, call: SipCall, pcm: bytes) -> None:
        item = self.legs.get(id(call))
        if item is None:
            return
        if call.rate != self.rate:
            pcm = resample(pcm, call.rate, self.rate)
        item[1].push(pcm)

    def _from_group(self, ssrc: int, pcm: bytes) -> None:
        buffer = self.sources.get(ssrc)
        if buffer is None:
            if len(self.sources) >= 32:
                return
            buffer = self.sources[ssrc] = PcmBuffer(self.limit)
        buffer.push(pcm)

    def _tick(self) -> None:
        if not self._running:
            return
        now = time.monotonic()
        while self._next <= now:
            self._next += TICK
            self._pump()
        self._timer = self.loop.call_at(self.loop.time() + max(0.001, self._next - time.monotonic()), self._tick)

    def _pump(self) -> None:
        samples = self.frame20 // 2
        remote = np.zeros(samples, dtype=np.int32)
        for buffer in list(self.sources.values()):
            if len(buffer) == 0:
                continue
            remote += np.frombuffer(buffer.pull(self.frame20), dtype="<i2").astype(np.int32)
        legs: dict[int, np.ndarray] = {}
        pbx = np.zeros(samples, dtype=np.int32)
        for key, (_, buffer) in self.legs.items():
            chunk = buffer.pull(self.frame20) if len(buffer) else b""
            data = np.frombuffer(chunk, dtype="<i2").astype(np.int32) if chunk else np.zeros(samples, dtype=np.int32)
            legs[key] = data
            pbx += data
        out = np.clip(pbx, -32768, 32767).astype("<i2").tobytes()
        self.group.send_audio(out[:self.frame10])
        self.group.send_audio(out[self.frame10:])
        for key, (call, _) in self.legs.items():
            if call.state not in (CallState.EARLY, CallState.CONNECTED):
                continue
            mix = np.clip(remote + pbx - legs[key], -32768, 32767).astype("<i2").tobytes()
            call.send_pcm(resample(mix, self.rate, call.rate) if call.rate != self.rate else mix)
