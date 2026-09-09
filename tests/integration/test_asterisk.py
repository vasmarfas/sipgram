"""End-to-end SIP tests against a real Asterisk (see docker-compose.yml in this directory).

Run:  cd tests/integration && docker compose up --build --abort-on-container-exit --exit-code-from tester
"""
from __future__ import annotations

import asyncio
import math
import os
import time

import numpy as np
import pytest

from sipgram.audio.tones import ToneGenerator
from sipgram.bridge import CallBridge
from sipgram.config import SipConfig
from sipgram.sip import opus
from sipgram.sip.account import CallError, CallState, SipAccount, SipCall
from sipgram.sip.codecs import codec_list
from sipgram.util import detect_local_ip

HOST = os.environ.get("ASTERISK_HOST")
pytestmark = pytest.mark.skipif(not HOST, reason="ASTERISK_HOST not set")


class Ami:
    """Just enough of the Asterisk Manager Interface to originate calls."""

    def __init__(self, host: str):
        self.host = host

    async def originate(self, channel: str, app: str, data: str = "", timeout_ms: int = 20000) -> None:
        reader, writer = await asyncio.open_connection(self.host, 5038)
        await reader.readline()
        writer.write(b"Action: Login\r\nUsername: test\r\nSecret: test\r\nEvents: off\r\n\r\n")
        await writer.drain()
        await self._response(reader)
        writer.write(
            f"Action: Originate\r\nChannel: {channel}\r\nApplication: {app}\r\nData: {data}\r\n"
            f"Timeout: {timeout_ms}\r\nAsync: true\r\nCallerID: \"PBX Test\" <100>\r\n\r\n".encode()
        )
        await writer.drain()
        resp = await self._response(reader)
        writer.close()
        assert "Success" in resp, resp

    @staticmethod
    async def _response(reader: asyncio.StreamReader) -> str:
        lines = []
        while True:
            line = await asyncio.wait_for(reader.readline(), 10)
            if line in (b"\r\n", b""):
                break
            lines.append(line.decode())
        return "".join(lines)


def make_account(**over) -> SipAccount:
    params = dict(server=HOST, username="491", password="test491", local_port=0, expires=60,
                  rtp_port_min=41000, rtp_port_max=41100, keepalive=0)
    params.update(over)
    cfg = SipConfig(**params)
    return SipAccount(cfg, detect_local_ip(HOST, 5060))


@pytest.fixture
async def account():
    acc = make_account()
    await acc.start()
    for _ in range(100):
        if acc.registered:
            break
        await asyncio.sleep(0.1)
    assert acc.registered, "registration failed"
    yield acc
    await acc.stop()


async def wait_for(pred, timeout: float, what: str = "condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timeout waiting for {what}")


def tone(rate: int, freq: float, ms: int) -> bytes:
    t = np.arange(rate * ms // 1000) / rate
    return (np.sin(2 * math.pi * freq * t) * 10000).astype("<i2").tobytes()


def dominant_freq(pcm: bytes, rate: int) -> float:
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    if len(x) < rate // 4:
        return 0.0
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return float(np.fft.rfftfreq(len(x), 1 / rate)[np.argmax(spec[1:]) + 1])


class Streamer:
    """Pushes a tone into a call at 20 ms cadence and records what comes back."""

    def __init__(self, call: SipCall, freq: float):
        self.call = call
        self.freq = freq
        self.received = bytearray()
        self.gen = ToneGenerator([freq], 10_000, 0, call.rate, amplitude=0.3)
        call.on_audio = self.received.extend

    async def run(self, seconds: float) -> None:
        samples = self.call.rate // 50
        end = time.time() + seconds
        while time.time() < end:
            self.call.send_pcm(self.gen.frame(samples))
            await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_register_udp():
    acc = make_account()
    await acc.start()
    try:
        await wait_for(lambda: acc.registered, 10, "UDP registration")
    finally:
        await acc.stop()


@pytest.mark.asyncio
async def test_register_tcp():
    acc = make_account(transport="tcp")
    await acc.start()
    try:
        await wait_for(lambda: acc.registered, 10, "TCP registration")
    finally:
        await acc.stop()


@pytest.mark.asyncio
async def test_register_bad_password():
    acc = make_account(password="wrong")
    acc.cfg.register = False
    await acc.start()
    try:
        with pytest.raises(CallError) as ei:
            await acc._register_once(60)
        assert ei.value.code in (401, 403)
    finally:
        await acc.transport.stop()


@pytest.mark.asyncio
async def test_outgoing_echo(account: SipAccount):
    call = await account.invite("600")
    await asyncio.wait_for(call.answered, 15)
    assert call.state == CallState.CONNECTED and call.codec is not None
    s = Streamer(call, 700.0)
    await s.run(1.5)
    await asyncio.sleep(0.3)
    assert len(s.received) > call.rate * 2 * 0.8, "echo returned too little audio"
    assert abs(dominant_freq(bytes(s.received[call.rate:]), call.rate) - 700.0) < 20
    await call.hangup()
    assert call.state == CallState.TERMINATED
    assert call.rtp is None


@pytest.mark.asyncio
async def test_outgoing_milliwatt_pcmu_preferred(account: SipAccount):
    account.codecs = account.codecs[::-1]  # prefer the other G.711 flavour
    call = await account.invite("601")
    await asyncio.wait_for(call.answered, 15)
    received = bytearray()
    call.on_audio = received.extend
    await asyncio.sleep(1.5)
    assert abs(dominant_freq(bytes(received[call.rate // 2:]), call.rate) - 1004.0) < 30
    await call.hangup()


@pytest.mark.asyncio
async def test_outgoing_busy(account: SipAccount):
    call = await account.invite("604")
    with pytest.raises(CallError) as ei:
        await asyncio.wait_for(call.answered, 15)
    assert ei.value.code == 486
    assert call.state == CallState.TERMINATED


@pytest.mark.asyncio
async def test_outgoing_ringing_then_answer(account: SipAccount):
    states = []
    call = await account.invite("603")
    call.on_state = lambda c, st: states.append(st)
    await asyncio.wait_for(call.answered, 20)
    assert CallState.RINGING in states
    await call.hangup()


@pytest.mark.asyncio
async def test_early_media_then_pbx_hangup(account: SipAccount):
    call = await account.invite("605")
    received = bytearray()
    call.on_audio = received.extend
    await wait_for(lambda: call.state == CallState.EARLY, 10, "183 with SDP")
    await asyncio.sleep(1.5)
    assert len(received) > call.rate * 2 * 0.5, "no early media audio"
    with pytest.raises(CallError):
        await asyncio.wait_for(call.answered, 15)
    assert call.state == CallState.TERMINATED


@pytest.mark.asyncio
async def test_cancel_outgoing(account: SipAccount):
    call = await account.invite("603")
    await wait_for(lambda: call.state == CallState.RINGING, 10, "180 Ringing")
    await call.hangup()
    assert call.state == CallState.TERMINATED and call.end_code == 487
    await asyncio.sleep(0.5)


@pytest.mark.asyncio
async def test_pbx_hangs_up(account: SipAccount):
    call = await account.invite("607")
    await asyncio.wait_for(call.answered, 15)
    await asyncio.wait_for(call.ended, 10)
    assert call.end_reason == "remote hangup"


@pytest.mark.asyncio
async def test_dtmf_send_rfc2833(account: SipAccount):
    call = await account.invite("606")
    await asyncio.wait_for(call.answered, 15)
    await asyncio.sleep(0.5)
    await call.send_dtmf("123")
    s = Streamer(call, 500.0)
    await s.run(1.5)
    assert call.state == CallState.CONNECTED, "PBX did not accept our DTMF"
    assert len(s.received) > call.rate * 2 * 0.5
    await call.hangup()


@pytest.mark.asyncio
async def test_dtmf_receive(account: SipAccount):
    call = await account.invite("602")
    digits: list[str] = []
    call.on_dtmf = digits.append
    await asyncio.wait_for(call.answered, 15)
    await asyncio.wait_for(call.ended, 15)
    assert "".join(digits) == "123#"


@pytest.mark.asyncio
async def test_incoming_answer_echo(account: SipAccount):
    incoming: list[SipCall] = []
    account.on_incoming_call = incoming.append
    await Ami(HOST).originate("PJSIP/491", "Echo")
    await wait_for(lambda: bool(incoming), 15, "INVITE from PBX")
    call = incoming[0]
    assert call.caller_number == "100" and call.caller_name == "PBX Test"
    call.ringing()
    await asyncio.sleep(0.3)
    call.answer()
    await wait_for(lambda: call.ack_received, 5, "ACK")
    s = Streamer(call, 900.0)
    await s.run(1.5)
    await asyncio.sleep(0.3)
    assert abs(dominant_freq(bytes(s.received[call.rate:]), call.rate) - 900.0) < 20
    await call.hangup()
    assert call.state == CallState.TERMINATED


@pytest.mark.asyncio
async def test_incoming_reject_busy(account: SipAccount):
    incoming: list[SipCall] = []
    account.on_incoming_call = incoming.append
    await Ami(HOST).originate("PJSIP/491", "Echo")
    await wait_for(lambda: bool(incoming), 15, "INVITE from PBX")
    call = incoming[0]
    call.ringing()
    call.reject(486)
    assert call.state == CallState.TERMINATED
    await asyncio.sleep(0.5)
    assert not account._calls


@pytest.mark.asyncio
async def test_incoming_cancelled_by_pbx(account: SipAccount):
    incoming: list[SipCall] = []
    account.on_incoming_call = incoming.append
    await Ami(HOST).originate("PJSIP/491", "Echo", timeout_ms=2000)
    await wait_for(lambda: bool(incoming), 15, "INVITE from PBX")
    call = incoming[0]
    call.ringing()
    await asyncio.wait_for(call.ended, 10)
    assert call.end_code == 487 and call.cancelled


class FakeTgCall:
    """Stands in for a Telegram call: 10 ms frames in/out at sample_rate."""

    def __init__(self, rate: int):
        self.sample_rate = rate
        self.sent = bytearray()
        self.on_audio = None
        self.frames_in = 0
        self.frames_out = 0

    def send_audio(self, frame: bytes) -> None:
        self.sent.extend(frame)
        self.frames_out += 1


@pytest.mark.asyncio
async def test_bridge_echo_path(account: SipAccount):
    """SIP echo through CallBridge: tone injected as 'Telegram audio' must come back as 'Telegram audio'."""
    call = await account.invite("600")
    await asyncio.wait_for(call.answered, 15)
    tg = FakeTgCall(call.rate)
    bridge = CallBridge(call, tg, jitter_ms=40)
    bridge.start()
    gen = ToneGenerator([600.0], 10_000, 0, call.rate, amplitude=0.3)
    end = time.time() + 2.0
    while time.time() < end:
        tg.on_audio(gen.frame(call.rate // 100))
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.3)
    bridge.stop()
    assert tg.frames_out > 120, f"only {tg.frames_out} frames came back"
    assert abs(dominant_freq(bytes(tg.sent[call.rate:]), call.rate) - 600.0) < 20
    await call.hangup()


@pytest.mark.asyncio
async def test_hold_and_resume(account: SipAccount):
    """re-INVITE sendonly puts the far end on hold (Asterisk answers recvonly); sendrecv resumes echo."""
    call = await account.invite("600")
    await asyncio.wait_for(call.answered, 15)
    assert await call.hold() is True
    assert call.local_hold and call.remote_sdp is not None and call.remote_sdp.direction == "recvonly"
    s = Streamer(call, 650.0)
    await s.run(0.5)
    assert len(s.received) == 0, "audio must not flow while on hold"
    assert await call.unhold() is True
    assert not call.local_hold and call.remote_sdp.direction == "sendrecv"
    s = Streamer(call, 650.0)
    await s.run(1.5)
    await asyncio.sleep(0.3)
    assert abs(dominant_freq(bytes(s.received[call.rate:]), call.rate) - 650.0) < 20
    await call.hangup()


@pytest.mark.asyncio
async def test_blind_transfer_refer(account: SipAccount):
    """REFER on a bridged call: Asterisk accepts (202), reports via NOTIFY and drops our leg.
    The PBX side is a Local channel running Echo, so our leg is in a real bridge (a bare
    application call cannot be transferred and Asterisk reports 400 for it)."""
    incoming: list[SipCall] = []
    account.on_incoming_call = incoming.append
    await Ami(HOST).originate("Local/491@from-internal", "Echo")
    await wait_for(lambda: bool(incoming), 15, "INVITE from PBX")
    call = incoming[0]
    call.answer()
    await wait_for(lambda: call.ack_received, 5, "ACK")
    await asyncio.sleep(0.5)
    ok, detail = await call.refer("601", timeout=10)
    assert ok, detail
    await asyncio.wait_for(call.ended, 10)
    assert call.state == CallState.TERMINATED


@pytest.mark.asyncio
async def test_refer_on_unbridged_call_fails_gracefully(account: SipAccount):
    call = await account.invite("600")
    await asyncio.wait_for(call.answered, 15)
    ok, detail = await call.refer("601", timeout=10)
    assert not ok and call.state == CallState.CONNECTED
    await call.hangup()


@pytest.mark.asyncio
async def test_concurrent_calls_on_one_account(account: SipAccount):
    """Two simultaneous dialogs (echo + milliwatt) on the same registration, audio kept apart."""
    a = await account.invite("600")
    b = await account.invite("601")
    await asyncio.wait_for(a.answered, 15)
    await asyncio.wait_for(b.answered, 15)
    sa = Streamer(a, 500.0)
    got_b = bytearray()
    b.on_audio = got_b.extend
    await sa.run(1.5)
    await asyncio.sleep(0.3)
    assert abs(dominant_freq(bytes(sa.received[a.rate:]), a.rate) - 500.0) < 20
    assert abs(dominant_freq(bytes(got_b[b.rate // 2:]), b.rate) - 1004.0) < 30
    await a.hangup()
    await b.hangup()


@pytest.mark.asyncio
async def test_dtmf_while_audio_is_streaming(account: SipAccount):
    """The bridge feeds audio every 20 ms; RFC 4733 requires the sender to stop it during a digit.
    Extension 606 reads three digits and only echoes when it got "123"."""
    call = await account.invite("606")
    await asyncio.wait_for(call.answered, 15)
    await asyncio.sleep(0.5)

    pumping = True

    async def pump() -> None:
        gen = ToneGenerator([300.0], 10_000, 0, call.rate, amplitude=0.2)
        while pumping:
            call.send_pcm(gen.frame(call.rate // 50))
            await asyncio.sleep(0.02)

    task = asyncio.create_task(pump())
    await asyncio.sleep(0.3)
    await call.send_dtmf("123")
    pumping = False
    await task

    s = Streamer(call, 500.0)
    await s.run(2.0)
    await asyncio.sleep(0.3)
    assert call.state == CallState.CONNECTED, "the PBX hung up: it did not read the digits"
    assert len(s.received) > call.rate, "no echo: the PBX is still waiting for digits"
    assert abs(dominant_freq(bytes(s.received[call.rate:]), call.rate) - 500.0) < 20
    await call.hangup()


def peak_freqs(pcm: bytes, rate: int, n: int = 2) -> list[float]:
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    freqs = np.fft.rfftfreq(len(x), 1 / rate)
    order = np.argsort(spec)[::-1]
    found: list[float] = []
    for i in order:
        f = float(freqs[i])
        if all(abs(f - g) > 60 for g in found):
            found.append(f)
        if len(found) == n:
            break
    return sorted(found)


@pytest.mark.asyncio
async def test_dtmf_inband_is_audible(account: SipAccount):
    """dtmf: inband plays the keypad tones into the audio itself, so any far end hears them."""
    account.cfg.dtmf = "inband"
    call = await account.invite("600")
    await asyncio.wait_for(call.answered, 15)
    await asyncio.sleep(0.4)
    received = bytearray()
    call.on_audio = received.extend
    await call.send_dtmf("1")
    await asyncio.sleep(0.6)
    assert len(received) > call.rate // 4, "the echo returned no audio"
    low, high = peak_freqs(bytes(received), call.rate)
    assert abs(low - 697) < 30 and abs(high - 1209) < 30, f"digit 1 should be 697+1209 Hz, got {low:.0f}+{high:.0f}"
    await call.hangup()


@pytest.mark.asyncio
async def test_opus_call_is_wideband(account: SipAccount):
    """With Opus the bridge runs at 48 kHz: a 6 kHz tone survives the echo, which G.711 could never carry."""
    if not opus.available():
        pytest.skip("libopus is not installed")
    account.codecs = codec_list(["opus", "pcma"])
    call = await account.invite("600")
    await asyncio.wait_for(call.answered, 15)
    assert call.codec is not None and call.codec.name == "opus" and call.rate == 48000
    assert call.rtp.dtmf_pt is not None, "telephone-event must be negotiated at the Opus clock rate"
    s = Streamer(call, 6000.0)
    await s.run(2.0)
    await asyncio.sleep(0.4)
    assert len(s.received) > call.rate, "no echo came back over Opus"
    assert abs(dominant_freq(bytes(s.received[call.rate:]), call.rate) - 6000) < 80
    await call.hangup()


@pytest.mark.asyncio
async def test_rtcp_reports_are_exchanged(account: SipAccount):
    """Asterisk sends RTCP sender reports; we answer with receiver reports and get loss/jitter numbers."""
    call = await account.invite("601")                 # Milliwatt: a steady stream both ways
    await asyncio.wait_for(call.answered, 15)
    assert call.rtp is not None and call.rtp.rtcp is not None, "RTCP socket was not opened"
    s = Streamer(call, 700.0)
    await s.run(11.0)                                  # reports go out every 5 seconds
    report = call.rtp.rtcp.report()
    assert report["reports_out"] >= 1, "we never sent a receiver report"
    assert call.rtp.rtcp.reports_in >= 1, "no RTCP came back from Asterisk"
    assert report["rx_packets"] > 300 and report["tx_packets"] > 300
    assert report["loss_percent_in"] < 5, report
    await call.hangup()
