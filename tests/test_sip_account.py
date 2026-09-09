import ipaddress

import pytest

from sipgram.config import SipConfig
from sipgram.sip.account import CallError, SipAccount


def make(**over) -> SipAccount:
    params = dict(server="localhost", username="1", password="x", register=False, keepalive=0,
                  local_port=0, rtp_port_min=45000, rtp_port_max=45010)
    params.update(over)
    return SipAccount(SipConfig(**params), "127.0.0.1")


@pytest.mark.asyncio
async def test_server_name_is_resolved_to_ip():
    """Windows' proactor loop rejects a host name in sendto() (WSAEINVAL), so the
    destination must be numeric before any datagram is sent."""
    acc = make()
    assert acc.server_addr[0] == "localhost"
    await acc.start()
    try:
        ipaddress.ip_address(acc.server_addr[0])
        assert acc.server_addr[1] == 5060
    finally:
        await acc.transport.stop()


@pytest.mark.asyncio
async def test_tcp_transport_target_follows_resolution():
    acc = make(transport="tcp", server="localhost", port=15060)
    await acc.resolve_server()
    ipaddress.ip_address(acc.server_addr[0])
    assert acc.transport.remote == acc.server_addr


@pytest.mark.asyncio
async def test_unresolvable_server_reports_clearly():
    acc = make(server="pbx.invalid-tld-for-tests.example")
    with pytest.raises(CallError) as ei:
        await acc.start()
    assert ei.value.code == 503 and "cannot resolve" in ei.value.text
