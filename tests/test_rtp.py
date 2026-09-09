import asyncio

import pytest

from sipgram.sip.rtp import RtpPortPool, RtpSession


async def _pair():
    pool = RtpPortPool(20000, 20020)
    a = RtpSession(pool.bind("127.0.0.1"), 8000)
    b = RtpSession(pool.bind("127.0.0.1"), 8000)
    await a.start()
    await b.start()
    a.set_remote("127.0.0.1", b.local_port)
    b.set_remote("127.0.0.1", a.local_port)
    return a, b


@pytest.mark.asyncio
async def test_rtp_send_receive_and_symmetric():
    a, b = await _pair()
    got = []
    b.on_payload = lambda payload, marker, ts, seq: got.append((payload, marker, ts, seq))
    a.payload_type = b.payload_type = 8
    a.send(b"\xd5" * 160, 160)
    a.send(b"\xd5" * 160, 160)
    for _ in range(50):
        if len(got) == 2:
            break
        await asyncio.sleep(0.01)
    assert len(got) == 2
    assert got[0][1] is True and got[1][1] is False
    assert (got[1][2] - got[0][2]) % (1 << 32) == 160
    assert (got[1][3] - got[0][3]) % 65536 == 1
    a.close()
    b.close()


@pytest.mark.asyncio
async def test_rtp_dtmf_events():
    a, b = await _pair()
    a.payload_type = b.payload_type = 8
    a.dtmf_pt = b.dtmf_pt = 101
    digits = []
    b.on_dtmf = digits.append
    task = a.send_dtmf("1#", duration_ms=60, gap_ms=20)
    await task
    await asyncio.sleep(0.05)
    assert digits == ["1", "#"]
    a.close()
    b.close()


def test_port_pool_even_ports():
    pool = RtpPortPool(20101, 20110)
    s1 = pool.bind("127.0.0.1")
    s2 = pool.bind("127.0.0.1")
    p1, p2 = s1.getsockname()[1], s2.getsockname()[1]
    assert p1 % 2 == 0 and p2 % 2 == 0 and p1 != p2
    s1.close()
    s2.close()
