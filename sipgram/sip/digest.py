"""HTTP Digest authentication for SIP (RFC 3261 §22, RFC 8760 algorithms)."""
from __future__ import annotations

import hashlib
import os
import re

_PARAM_RE = re.compile(r'(\w+)\s*=\s*("([^"]*)"|([^,\s]+))')


def parse_challenge(value: str) -> dict[str, str]:
    scheme, _, rest = value.strip().partition(" ")
    if scheme.lower() != "digest":
        raise ValueError(f"unsupported auth scheme: {scheme}")
    params: dict[str, str] = {}
    for m in _PARAM_RE.finditer(rest):
        params[m.group(1).lower()] = m.group(3) if m.group(3) is not None else m.group(4)
    return params


def _hasher(algorithm: str):
    algo = algorithm.lower().replace("-sess", "")
    if algo in ("", "md5"):
        return hashlib.md5
    if algo == "sha-256":
        return hashlib.sha256
    if algo == "sha-512-256":
        return lambda: hashlib.new("sha512_256")
    raise ValueError(f"unsupported digest algorithm: {algorithm}")


def build_authorization(
    challenge: dict[str, str],
    username: str,
    password: str,
    method: str,
    uri: str,
    nc: int = 1,
    cnonce: str | None = None,
    body: bytes = b"",
) -> str:
    realm = challenge.get("realm", "")
    nonce = challenge.get("nonce", "")
    algorithm = challenge.get("algorithm", "MD5")
    qop_options = [q.strip() for q in challenge.get("qop", "").split(",") if q.strip()]
    if "auth" in qop_options:
        qop: str | None = "auth"
    elif "auth-int" in qop_options:
        qop = "auth-int"
    else:
        qop = None
    h = _hasher(algorithm)

    def H(data: str) -> str:
        return h(data.encode("utf-8")).hexdigest()

    cnonce = cnonce or os.urandom(8).hex()
    ha1 = H(f"{username}:{realm}:{password}")
    if algorithm.lower().endswith("-sess"):
        ha1 = H(f"{ha1}:{nonce}:{cnonce}")
    if qop == "auth-int":
        ha2 = H(f"{method}:{uri}:{H(body.decode('latin-1'))}")
    else:
        ha2 = H(f"{method}:{uri}")
    nc_str = f"{nc:08x}"
    if qop:
        response = H(f"{ha1}:{nonce}:{nc_str}:{cnonce}:{qop}:{ha2}")
    else:
        response = H(f"{ha1}:{nonce}:{ha2}")

    parts = [
        f'username="{username}"',
        f'realm="{realm}"',
        f'nonce="{nonce}"',
        f'uri="{uri}"',
        f'response="{response}"',
        f"algorithm={algorithm}",
    ]
    if qop:
        parts += [f'cnonce="{cnonce}"', f"qop={qop}", f"nc={nc_str}"]
    if "opaque" in challenge:
        parts.append(f'opaque="{challenge["opaque"]}"')
    return "Digest " + ", ".join(parts)
