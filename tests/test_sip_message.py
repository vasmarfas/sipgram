from sipgram.sip.digest import build_authorization, parse_challenge
from sipgram.sip.message import NameAddr, SipMessage, SipUri, Via, message_length

INVITE = (
    b"INVITE sip:491@10.0.0.5:5070 SIP/2.0\r\n"
    b"Via: SIP/2.0/UDP 10.0.0.1:5060;rport;branch=z9hG4bKPj1234\r\n"
    b"From: \"Test PBX\" <sip:100@10.0.0.1>;tag=abc\r\n"
    b"To: <sip:491@10.0.0.5:5070>\r\n"
    b"Contact: <sip:asterisk@10.0.0.1:5060>\r\n"
    b"Call-ID: call-1@10.0.0.1\r\n"
    b"CSeq: 12 INVITE\r\n"
    b"Record-Route: <sip:p1;lr>, <sip:p2;lr>\r\n"
    b"Max-Forwards: 70\r\n"
    b"Content-Type: application/sdp\r\n"
    b"Content-Length: 5\r\n"
    b"\r\n"
    b"v=0\r\nextra"
)


def test_parse_request():
    m = SipMessage.parse(INVITE)
    assert m.is_request and m.method == "INVITE"
    assert m.uri == "sip:491@10.0.0.5:5070"
    assert m.call_id == "call-1@10.0.0.1"
    assert m.cseq == (12, "INVITE")
    assert m.from_.display == "Test PBX"
    assert m.from_.tag == "abc"
    assert m.from_.uri.user == "100"
    assert m.to.tag is None
    assert m.body == b"v=0\r\n"
    assert m.get_all("Record-Route") == ["<sip:p1;lr>", "<sip:p2;lr>"]
    via = m.top_via
    assert via and via.branch == "z9hG4bKPj1234" and via.host == "10.0.0.1" and via.port == 5060
    assert "rport" in via.params


def test_compact_and_folding():
    raw = (b"SIP/2.0 200 OK\r\nv: SIP/2.0/UDP 1.2.3.4;branch=z9hG4bK1\r\n"
           b"f: <sip:a@b>;tag=1\r\nt: <sip:c@d>;tag=2\r\ni: x\r\nCSeq: 1 REGISTER\r\n"
           b"Contact: <sip:491@1.2.3.4:5070>;expires=120,\r\n <sip:491@5.6.7.8>;expires=60\r\nl: 0\r\n\r\n")
    m = SipMessage.parse(raw)
    assert m.is_response and m.status == 200
    assert m.get("From") == "<sip:a@b>;tag=1"
    assert m.call_id == "x"
    contacts = m.get_all("Contact")
    assert len(contacts) == 2
    assert NameAddr.parse(contacts[0]).params["expires"] == "120"


def test_serialize_roundtrip():
    m = SipMessage.request("REGISTER", "sip:example.com")
    m.set("Via", "SIP/2.0/UDP 10.0.0.5:5070;branch=z9hG4bKx")
    m.set("CSeq", "1 REGISTER")
    m.body = b"hello"
    data = m.serialize()
    assert data.endswith(b"\r\n\r\nhello")
    assert b"Content-Length: 5\r\n" in data
    back = SipMessage.parse(data)
    assert back.method == "REGISTER" and back.body == b"hello"


def test_uri_and_nameaddr():
    u = SipUri.parse("sip:491@pbx.example.com:5060;transport=tcp?Subject=hi")
    assert (u.user, u.host, u.port, u.params["transport"]) == ("491", "pbx.example.com", 5060, "tcp")
    assert str(u) == "sip:491@pbx.example.com:5060;transport=tcp?Subject=hi"
    na = NameAddr.parse('"Bob" <sip:bob@x.com;transport=udp>;tag=99')
    assert na.display == "Bob" and na.uri.params["transport"] == "udp" and na.tag == "99"
    na2 = NameAddr.parse("sip:bob@x.com;tag=7")
    assert na2.uri.user == "bob" and na2.tag == "7" and "tag" not in na2.uri.params
    v = Via.parse("SIP/2.0/TCP [::1]:5060;branch=z9hG4bKq")
    assert v.host == "::1" and v.port == 5060 and v.transport == "TCP"


def test_message_length_stream():
    buf = INVITE + b"OPTIONS sip:x SIP/2.0\r\n"
    assert message_length(buf) == len(INVITE) - len(b"extra")
    assert message_length(INVITE[:-8]) is None


def test_digest_md5_known_vector():
    # RFC 2617 example: user Mufasa, realm testrealm@host.com, GET /dir/index.html
    challenge = parse_challenge('Digest realm="testrealm@host.com", qop="auth,auth-int", '
                                'nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", opaque="5ccc069c403ebaf9f0171e9517f40e41"')
    auth = build_authorization(challenge, "Mufasa", "Circle Of Life", "GET", "/dir/index.html",
                               nc=1, cnonce="0a4f113b")
    assert 'response="6629fae49393a05397450978507c4ef1"' in auth
    assert 'opaque="5ccc069c403ebaf9f0171e9517f40e41"' in auth
    assert "qop=auth" in auth


def test_digest_no_qop_sha256():
    challenge = parse_challenge('Digest realm="asterisk", nonce="abc", algorithm=SHA-256')
    auth = build_authorization(challenge, "491", "pw", "REGISTER", "sip:pbx")
    assert auth.startswith("Digest ") and "algorithm=SHA-256" in auth and "qop" not in auth
