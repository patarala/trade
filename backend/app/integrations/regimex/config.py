"""Validate the operator-owned analysis destination before serving requests."""

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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


class RegimexSettings(BaseSettings):
    """The same connection settings for the API and its database-free diagnostic CLI."""

    regimex_url: str | None = None
    regimex_token: SecretStr | None = None
    regimex_source_id: str = "trade"
    regimex_allow_loopback_http: bool = False
    regimex_max_concurrent_requests: int = Field(default=4, ge=1, le=16)

    @model_validator(mode="after")
    def validate_regimex(self) -> Self:
        if not re.fullmatch(IDENTITY_PATTERN, self.regimex_source_id):
            raise ValueError("REGIMEX_SOURCE_ID must be a stable ASCII source identifier")
        validate_connection(self.regimex_url, self.regimex_token, self.regimex_allow_loopback_http)
        return self

    def regimex_connection(self) -> Connection | None:
        if self.regimex_url is None or self.regimex_token is None:
            return None
        return Connection(self.regimex_url, self.regimex_token, self.regimex_source_id)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )
