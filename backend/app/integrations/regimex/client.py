"""Bounded requests to one configured origin, without redirects or browser credentials."""

import http.client
import queue
import re
import socket
import ssl
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from urllib.parse import urlsplit

from .config import IDENTITY_PATTERN, MAX_RESPONSE_BYTES, Connection
from .errors import GatewayError, invalid_response, unavailable
from .json import json_bytes, strict_json

_RESOLVERS = threading.BoundedSemaphore(16)
_PATH = re.compile(r"/api/v1/integration/[A-Za-z0-9/-]+(?:\?limit=1)?")


def _addresses(host: str, port: int, deadline: float) -> list[tuple]:
    # The OS resolver has no timeout. Bound both its concurrency and our wait.
    if not _RESOLVERS.acquire(blocking=False):
        raise TimeoutError
    result: queue.Queue = queue.Queue(1)

    def resolve() -> None:
        try:
            result.put(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[:32])
        except OSError, UnicodeError:
            result.put(OSError("Cannot resolve the configured analysis service"))
        finally:
            _RESOLVERS.release()

    threading.Thread(target=resolve, daemon=True, name="regimex-dns").start()
    try:
        answer = result.get(timeout=max(0.001, deadline - time.monotonic()))
    except queue.Empty:
        raise TimeoutError from None
    if isinstance(answer, OSError):
        raise answer
    return answer


@dataclass(frozen=True)
class UpstreamResponse:
    content_type: str
    body: bytes


class RegimexClient:
    def __init__(self, connection: Connection, *, subject: str, run_id: str) -> None:
        if not re.fullmatch(IDENTITY_PATTERN, subject) or not re.fullmatch(
            IDENTITY_PATTERN, run_id
        ):
            raise ValueError("Invalid verified subject or run identifier")
        self.connection = connection
        self.subject = subject
        self.run_id = run_id
        # One deadline spans upload, submission and verification in this browser request.
        self.deadline = time.monotonic() + connection.total_timeout

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        idempotency_key: str | None = None,
    ) -> UpstreamResponse:
        if (
            method not in {"GET", "POST"}
            or not _PATH.fullmatch(path)
            or (body is not None and len(body) > MAX_RESPONSE_BYTES)
            or (
                idempotency_key is not None
                and not re.fullmatch(r"trade-[a-f0-9]{64}", idempotency_key)
            )
        ):
            raise ValueError("Invalid internal analysis request")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise unavailable()
        settings = self.connection
        parsed = urlsplit(settings.url)
        assert parsed.hostname is not None
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port)
        headers = {
            "Authorization": "Bearer " + settings.token.get_secret_value(),
            "X-Regimex-Subject": self.subject,
            "X-Regimex-Run-ID": self.run_id,
            "Accept": "application/json, text/html",
            "Accept-Encoding": "identity",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        transport = None
        response = None

        def abort() -> None:
            if transport is not None:
                with suppress(OSError):
                    transport.shutdown(socket.SHUT_RDWR)

        watchdog = threading.Timer(remaining, abort)
        watchdog.daemon = True
        watchdog.start()
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            addresses = _addresses(
                parsed.hostname,
                port,
                min(self.deadline, time.monotonic() + settings.connect_timeout),
            )
            for family, kind, protocol, _, address in addresses:
                transport = socket.socket(family, kind, protocol)
                try:
                    self._timeout(transport, settings.connect_timeout)
                    transport.connect(address)
                    if parsed.scheme == "https":
                        transport = ssl.create_default_context().wrap_socket(
                            transport,
                            server_hostname=parsed.hostname,
                            do_handshake_on_connect=False,
                        )
                        # The watchdog must own the SSLSocket before a handshake
                        # can block; wrapping detaches the original raw socket.
                        self._timeout(transport, settings.connect_timeout)
                        transport.do_handshake()
                    connection.sock = transport
                    break
                except OSError:
                    transport.close()
            else:
                raise OSError("Cannot connect to the configured analysis service")
            self._timeout(transport)
            connection.request(method, path, body=body, headers=headers)
            self._timeout(transport)
            response = connection.getresponse()
            if response.status not in ({200} if method == "GET" else {200, 201, 202}):
                self._status_error(response)
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise invalid_response()
            lengths = response.headers.get_all("Content-Length", [])
            transfer = response.getheader("Transfer-Encoding")
            if len(lengths) > 1 or (transfer and (lengths or transfer.lower() != "chunked")):
                raise invalid_response()
            if lengths and not re.fullmatch(r"[0-9]+", lengths[0]):
                raise invalid_response()
            expected = int(lengths[0]) if lengths else None
            if expected is not None and expected > settings.response_bytes:
                raise invalid_response()
            data = bytearray()
            while True:
                self._timeout(transport)
                block = response.read1(min(65536, settings.response_bytes + 1 - len(data)))
                if not block:
                    break
                data.extend(block)
                if len(data) > settings.response_bytes:
                    raise invalid_response()
            if expected is not None and expected != len(data):
                raise invalid_response()
            if time.monotonic() > self.deadline:
                raise TimeoutError
            return UpstreamResponse(response.getheader("Content-Type", ""), bytes(data))
        except GatewayError:
            raise
        except OSError, http.client.HTTPException, ValueError, UnicodeError:
            raise unavailable() from None
        finally:
            watchdog.cancel()
            if response is not None:
                response.close()
            connection.close()
            if transport is not None:
                transport.close()

    def _timeout(self, transport: socket.socket, limit: float | None = None) -> None:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        transport.settimeout(min(limit or self.connection.read_timeout, remaining))

    @staticmethod
    def _status_error(response: http.client.HTTPResponse) -> None:
        status = response.status
        if status == 404:
            raise GatewayError("not_found", "The analysis resource was not found.", 404)
        if status == 409:
            raise GatewayError(
                "analysis_conflict",
                "The analysis is not available in its current state.",
                409,
            )
        if status == 429:
            raw = response.getheader("Retry-After", "")
            retry_after = int(raw) if re.fullmatch(r"[0-9]{1,4}", raw) else 5
            raise GatewayError(
                "analysis_busy",
                "Regimex is busy. Retry after the indicated delay.",
                429,
                retryable=True,
                retry_after=max(1, min(3600, retry_after)),
            )
        if status in {401, 403}:
            raise GatewayError(
                "connection_access_denied",
                "The Regimex connection needs attention from the service administrator.",
                503,
            )
        if 300 <= status < 400 or status in {400, 413, 415, 422}:
            raise invalid_response()
        raise unavailable()

    def json(
        self,
        method: str,
        path: str,
        *,
        payload: object | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        response = self.request(
            method,
            path,
            body=None if payload is None else json_bytes(payload),
            idempotency_key=idempotency_key,
        )
        try:
            if response.content_type.split(";", 1)[0].strip().lower() != "application/json":
                raise ValueError
            value = strict_json(response.body)
            if not isinstance(value, dict):
                raise invalid_response()
            return value
        except ValueError, UnicodeError, RecursionError:
            raise invalid_response() from None
