"""Validate the operator-owned analysis destination before serving requests."""

import ipaddress
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from pydantic import SecretStr

IDENTITY_PATTERN = r"[A-Za-z0-9._:@-]{1,128}"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


def validate_connection(url: str | None, token: SecretStr | None, allow_http: bool) -> None:
    if (url is None) != (token is None):
        raise ValueError("REGIMEX_URL and REGIMEX_TOKEN must be configured together")
    if url is None:
        if allow_http:
            raise ValueError("REGIMEX_ALLOW_LOOPBACK_HTTP requires a configured connection")
        return
    try:
        parsed = urlsplit(url)
        host, port = parsed.hostname, parsed.port
        if (
            not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or "\\" in url
            or any(ord(char) <= 32 or ord(char) == 127 for char in url)
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise ValueError
        if parsed.scheme == "http":
            if not allow_http or not ipaddress.ip_address(host).is_loopback:
                raise ValueError
        elif parsed.scheme != "https" or allow_http:
            raise ValueError
    except ValueError:
        raise ValueError(
            "REGIMEX_URL must be an HTTPS origin; HTTP requires an explicitly allowed "
            "numeric loopback address"
        ) from None
    assert token is not None
    if not re.fullmatch(r"[A-Za-z0-9._~-]{16,512}", token.get_secret_value()):
        raise ValueError("REGIMEX_TOKEN is not a valid backend integration credential")


@dataclass(frozen=True)
class Connection:
    url: str
    token: SecretStr = field(repr=False)
    source_id: str = "trade"
    connect_timeout: float = 3.0
    read_timeout: float = 10.0
    total_timeout: float = 30.0
    response_bytes: int = MAX_RESPONSE_BYTES
