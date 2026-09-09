"""SIP message model: parsing and serialization (RFC 3261 subset)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

COMPACT_HEADERS = {
    "v": "Via", "f": "From", "t": "To", "i": "Call-ID", "m": "Contact",
    "c": "Content-Type", "l": "Content-Length", "k": "Supported", "s": "Subject",
    "e": "Content-Encoding", "x": "Session-Expires", "o": "Event",
    "u": "Allow-Events", "r": "Refer-To", "b": "Referred-By",
}

_CANONICAL = {
    "call-id": "Call-ID", "cseq": "CSeq", "www-authenticate": "WWW-Authenticate",
    "content-length": "Content-Length", "content-type": "Content-Type",
    "proxy-authenticate": "Proxy-Authenticate", "proxy-authorization": "Proxy-Authorization",
    "record-route": "Record-Route", "max-forwards": "Max-Forwards", "user-agent": "User-Agent",
    "session-expires": "Session-Expires", "min-se": "Min-SE", "p-asserted-identity": "P-Asserted-Identity",
    "rack": "RAck", "rseq": "RSeq", "mime-version": "MIME-Version",
}

_LIST_HEADERS = ("Via", "Record-Route", "Route", "Contact", "Allow", "Supported")


def canonical_header(name: str) -> str:
    low = name.strip().lower()
    if len(low) == 1 and low in COMPACT_HEADERS:
        return COMPACT_HEADERS[low]
    if low in _CANONICAL:
        return _CANONICAL[low]
    return "-".join(part.capitalize() for part in low.split("-"))


def split_params(text: str) -> tuple[str, dict[str, str | None]]:
    """Split 'value;p1=v1;p2' into (value, {p1: v1, p2: None}); quotes are honoured."""
    parts: list[str] = []
    cur: list[str] = []
    quoted = False
    for ch in text:
        if ch == '"':
            quoted = not quoted
        if ch == ";" and not quoted:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    params: dict[str, str | None] = {}
    for p in parts[1:]:
        p = p.strip()
        if not p:
            continue
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.strip().lower()] = v.strip().strip('"')
        else:
            params[p.lower()] = None
    return parts[0].strip(), params


def format_params(params: dict[str, str | None]) -> str:
    out = ""
    for k, v in params.items():
        out += f";{k}" if v is None else f";{k}={v}"
    return out


@dataclass
class SipUri:
    scheme: str = "sip"
    user: str | None = None
    host: str = ""
    port: int | None = None
    params: dict[str, str | None] = field(default_factory=dict)
    headers: str = ""

    @classmethod
    def parse(cls, text: str) -> SipUri:
        text = text.strip()
        if text.startswith("<") and text.endswith(">"):
            text = text[1:-1]
        m = re.match(r"^(sips?|tel):(.*)$", text, re.I)
        if not m:
            raise ValueError(f"bad SIP URI: {text!r}")
        scheme = m.group(1).lower()
        rest = m.group(2)
        headers = ""
        if "?" in rest:
            rest, headers = rest.split("?", 1)
        rest, params = split_params(rest)
        user = None
        if "@" in rest:
            user, rest = rest.rsplit("@", 1)
            if ":" in user:
                user = user.split(":", 1)[0]
        port = None
        if rest.startswith("["):
            host_part, _, port_part = rest[1:].partition("]")
            host = host_part
            if port_part.startswith(":"):
                port = int(port_part[1:])
        elif ":" in rest:
            host, port_s = rest.rsplit(":", 1)
            port = int(port_s)
        else:
            host = rest
        return cls(scheme, user, host, port, params, headers)

    def __str__(self) -> str:
        s = f"{self.scheme}:"
        if self.user:
            s += f"{self.user}@"
        s += self.host
        if self.port:
            s += f":{self.port}"
        s += format_params(self.params)
        if self.headers:
            s += "?" + self.headers
        return s


@dataclass
class NameAddr:
    """From / To / Contact / Record-Route style header value."""
    uri: SipUri
    display: str = ""
    params: dict[str, str | None] = field(default_factory=dict)

    @classmethod
    def parse(cls, text: str) -> NameAddr:
        text = text.strip()
        if "<" in text:
            before, rest = text.split("<", 1)
            display = before.strip().strip('"')
            uri_text, after = rest.split(">", 1)
            _, params = split_params("x" + after)
            return cls(SipUri.parse(uri_text), display, params)
        value, params = split_params(text)
        return cls(SipUri.parse(value), "", params)

    @property
    def tag(self) -> str | None:
        return self.params.get("tag")

    def __str__(self) -> str:
        s = f'"{self.display}" ' if self.display else ""
        s += f"<{self.uri}>"
        s += format_params(self.params)
        return s


@dataclass
class Via:
    transport: str
    host: str
    port: int | None
    params: dict[str, str | None]

    @classmethod
    def parse(cls, text: str) -> Via:
        value, params = split_params(text)
        m = re.match(r"^SIP/2\.0/(\w+)\s+(.+)$", value.strip(), re.I)
        if not m:
            raise ValueError(f"bad Via: {text!r}")
        hostport = m.group(2).strip()
        port = None
        if hostport.startswith("["):
            host, _, rest = hostport[1:].partition("]")
            if rest.startswith(":"):
                port = int(rest[1:])
        elif ":" in hostport:
            host, port_s = hostport.rsplit(":", 1)
            port = int(port_s)
        else:
            host = hostport
        return cls(m.group(1).upper(), host, port, params)

    @property
    def branch(self) -> str | None:
        return self.params.get("branch")

    def __str__(self) -> str:
        hp = self.host if self.port is None else f"{self.host}:{self.port}"
        return f"SIP/2.0/{self.transport} {hp}{format_params(self.params)}"


class SipMessage:
    def __init__(self) -> None:
        self.method: str | None = None
        self.uri: str | None = None
        self.status: int | None = None
        self.reason: str = ""
        self.headers: list[list[str]] = []
        self.body: bytes = b""

    @classmethod
    def request(cls, method: str, uri: str) -> SipMessage:
        m = cls()
        m.method = method.upper()
        m.uri = uri
        return m

    @classmethod
    def response(cls, status: int, reason: str) -> SipMessage:
        m = cls()
        m.status = status
        m.reason = reason
        return m

    @property
    def is_request(self) -> bool:
        return self.method is not None

    @property
    def is_response(self) -> bool:
        return self.status is not None

    def get(self, name: str, default: str | None = None) -> str | None:
        cname = canonical_header(name)
        for h in self.headers:
            if h[0] == cname:
                return h[1]
        return default

    def get_all(self, name: str) -> list[str]:
        cname = canonical_header(name)
        out: list[str] = []
        for h in self.headers:
            if h[0] == cname:
                if cname in _LIST_HEADERS:
                    out.extend(v.strip() for v in _split_commas(h[1]))
                else:
                    out.append(h[1])
        return out

    def has(self, name: str) -> bool:
        return self.get(name) is not None

    def set(self, name: str, value: object) -> None:
        cname = canonical_header(name)
        self.remove(cname)
        self.headers.append([cname, str(value)])

    def add(self, name: str, value: object) -> None:
        self.headers.append([canonical_header(name), str(value)])

    def remove(self, name: str) -> None:
        cname = canonical_header(name)
        self.headers = [h for h in self.headers if h[0] != cname]

    @property
    def call_id(self) -> str:
        return self.get("Call-ID") or ""

    @property
    def cseq(self) -> tuple[int, str]:
        value = self.get("CSeq") or "0 UNKNOWN"
        num, _, method = value.strip().partition(" ")
        return int(num), method.strip().upper()

    @property
    def from_(self) -> NameAddr:
        return NameAddr.parse(self.get("From") or "")

    @property
    def to(self) -> NameAddr:
        return NameAddr.parse(self.get("To") or "")

    @property
    def top_via(self) -> Via | None:
        vias = self.get_all("Via")
        return Via.parse(vias[0]) if vias else None

    @property
    def branch(self) -> str | None:
        via = self.top_via
        return via.branch if via else None

    def first_line(self) -> str:
        if self.is_request:
            return f"{self.method} {self.uri} SIP/2.0"
        return f"SIP/2.0 {self.status} {self.reason}"

    def serialize(self) -> bytes:
        self.set("Content-Length", len(self.body))
        lines = [self.first_line()]
        for name, value in self.headers:
            lines.append(f"{name}: {value}")
        head = "\r\n".join(lines) + "\r\n\r\n"
        return head.encode("utf-8") + self.body

    def __str__(self) -> str:
        return self.serialize().decode("utf-8", "replace")

    @classmethod
    def parse(cls, data: bytes) -> SipMessage:
        head, _, body = data.partition(b"\r\n\r\n")
        text = head.decode("utf-8", "replace")
        raw_lines = text.split("\r\n")
        if not raw_lines or not raw_lines[0].strip():
            raise ValueError("empty message")
        lines: list[str] = []
        for line in raw_lines:
            if line[:1] in (" ", "\t") and lines:
                lines[-1] += " " + line.strip()
            else:
                lines.append(line)
        msg = cls()
        first = lines[0]
        if first.upper().startswith("SIP/2.0 "):
            parts = first.split(" ", 2)
            msg.status = int(parts[1])
            msg.reason = parts[2] if len(parts) > 2 else ""
        else:
            parts = first.split(" ")
            if len(parts) < 3 or not parts[2].upper().startswith("SIP/2.0"):
                raise ValueError(f"bad request line: {first!r}")
            msg.method = parts[0].upper()
            msg.uri = parts[1]
        for line in lines[1:]:
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            msg.headers.append([canonical_header(name), value.strip()])
        length = msg.get("Content-Length")
        if length is not None and length.strip().isdigit():
            msg.body = body[: int(length)]
        else:
            msg.body = body
        return msg


def _split_commas(value: str) -> list[str]:
    parts: list[str] = []
    cur: list[str] = []
    quoted = False
    depth = 0
    for ch in value:
        if ch == '"':
            quoted = not quoted
        elif ch == "<" and not quoted:
            depth += 1
        elif ch == ">" and not quoted:
            depth -= 1
        if ch == "," and not quoted and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return [p for p in parts if p.strip()]


def message_length(buf: bytes) -> int | None:
    """Length of the first complete SIP message in a stream buffer, or None."""
    idx = buf.find(b"\r\n\r\n")
    if idx < 0:
        return None
    head = buf[:idx].decode("utf-8", "replace")
    length = 0
    for line in head.split("\r\n"):
        name, _, value = line.partition(":")
        if canonical_header(name) == "Content-Length" and value.strip().isdigit():
            length = int(value.strip())
            break
    total = idx + 4 + length
    return total if len(buf) >= total else None
