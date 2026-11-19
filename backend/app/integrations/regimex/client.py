"""Bounded requests to one configured origin, without redirects or browser credentials."""

import http.client
import logging
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
from .delivery import ZIP_OVERHEAD, Delivery, member_limits, read_delivery
from .errors import GatewayError, invalid_response, unavailable
from .json import json_bytes, strict_json

_RESOLVERS = threading.BoundedSemaphore(16)
_PATH = re.compile(r"/api/v1/integration/[A-Za-z0-9/-]+")
_HISTORY = re.compile(r"/api/v1/integration/analyses\?limit=([0-9]{1,2})(?:&offset=([0-9]{1,5}))?")
_RETRY = re.compile(
    r"/api/v1/integration/analysis-intake\?retry_of="
    r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}"
)
_DELIVERY = re.compile(
    r"/api/v1/integration/analyses/[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}"
    r"/delivery(?:\?include_report=(true|false))?"
)
_LOG = logging.getLogger(__name__)


class TransportFailure(GatewayError):
    """A safe operator category without an address, credential or raw exception."""

    def __init__(self, category: str) -> None:
        error = unavailable()
        super().__init__(error.code, error.message, error.status, retryable=error.retryable)
        self.category = category


class UpstreamStatusFailure(GatewayError):
    def __init__(self, error: GatewayError, upstream_status: int) -> None:
        super().__init__(
            error.code,
            error.message,
            error.status,
            retryable=error.retryable,
            retry_after=error.retry_after,
        )
        self.upstream_status = upstream_status


class _ResolutionFailure(OSError):
    pass


def _upstream_error_code(response: http.client.HTTPResponse) -> str | None:
    media_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
    if response.status not in {404, 409, 429} or media_type != "application/json":
        return None
    # Recognize machine codes without exposing upstream text or buffering a large body.
    try:
        data = response.read(4097)
        if len(data) > 4096:
            return None
        body = strict_json(data)
    except (ValueError, RecursionError, OSError, http.client.HTTPException):
        return None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        code = body["error"].get("code")
        if isinstance(code, str):
            return code
    return None


def _addresses(host: str, port: int, deadline: float) -> list[tuple]:
    # The OS resolver has no timeout. Bound both its concurrency and our wait.
    if not _RESOLVERS.acquire(blocking=False):
        raise TimeoutError
    result: queue.Queue = queue.Queue(1)

    def resolve() -> None:
        try:
            result.put(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[:32])
        except OSError, UnicodeError:
            result.put(_ResolutionFailure("Cannot resolve the configured analysis service"))
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


class _ConfiguredClient:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        # One deadline spans upload, submission and verification in this browser request.
        self.deadline = time.monotonic() + connection.total_timeout

    def _allows(self, method: str, path: str) -> bool:
        raise NotImplementedError

    def _credentials(self, path: str) -> dict[str, str]:
        raise NotImplementedError

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
            or not self._allows(method, path)
            or (body is not None and len(body) > MAX_RESPONSE_BYTES)
            or (
                idempotency_key is not None
                and not re.fullmatch(r"trade-[a-f0-9]{64}", idempotency_key)
            )
        ):
            raise ValueError("Invalid internal analysis request")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TransportFailure("timeout")
        started = time.monotonic()
        settings = self.connection
        limit = settings.response_bytes
        delivery = _DELIVERY.fullmatch(path)
        if delivery:
            limit = sum(member_limits(limit, delivery[1] == "true").values()) + ZIP_OVERHEAD
        parsed = urlsplit(settings.url)
        assert parsed.hostname is not None
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port)
        headers = {
            "Accept": "application/json, application/zip",
            "Accept-Encoding": "identity",
            **self._credentials(path),
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
            last_failure = OSError("Cannot connect to the configured analysis service")
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
                except ssl.SSLError:
                    transport.close()
                    raise
                except OSError as error:
                    last_failure = error
                    transport.close()
            else:
                raise last_failure
            self._timeout(transport)
            connection.request(method, path, body=body, headers=headers)
            self._timeout(transport)
            response = connection.getresponse()
            if response.status not in ({200} if method == "GET" else {200, 201, 202}):
                self._status_error(response, path)
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise invalid_response()
            lengths = response.headers.get_all("Content-Length", [])
            transfer = response.getheader("Transfer-Encoding")
            if len(lengths) > 1 or (transfer and (lengths or transfer.lower() != "chunked")):
                raise invalid_response()
            if lengths and not re.fullmatch(r"[0-9]+", lengths[0]):
                raise invalid_response()
            expected = int(lengths[0]) if lengths else None
            if expected is not None and expected > limit:
                raise invalid_response()
            data = bytearray()
            while not response.isclosed():
                self._timeout(transport)
                block = response.read1(min(65536, limit + 1 - len(data)))
                if not block:
                    break
                data.extend(block)
                if len(data) > limit:
                    raise invalid_response()
            if expected is not None and expected != len(data):
                raise invalid_response()
            if time.monotonic() > self.deadline:
                raise TimeoutError
            return UpstreamResponse(response.getheader("Content-Type", ""), bytes(data))
        except GatewayError as error:
            _LOG.info(
                "regimex_upstream_failed code=%s duration_ms=%d",
                error.code,
                round((time.monotonic() - started) * 1000),
            )
            raise
        except (OSError, http.client.HTTPException, ValueError, UnicodeError) as error:
            category = (
                "certificate_rejected"
                if isinstance(error, ssl.SSLCertVerificationError)
                else "tls_failed"
                if isinstance(error, ssl.SSLError)
                else "dns_failed"
                if isinstance(error, _ResolutionFailure)
                else "timeout"
                if isinstance(error, TimeoutError)
                else "connection_failed"
                if isinstance(error, OSError)
                else "invalid_protocol"
            )
            _LOG.info(
                "regimex_transport_failed category=%s duration_ms=%d",
                category,
                round((time.monotonic() - started) * 1000),
            )
            raise TransportFailure(category) from None
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
    def _status_error(response: http.client.HTTPResponse, path: str) -> None:
        status = response.status
        error_code = _upstream_error_code(response)
        delivery = _DELIVERY.fullmatch(path)
        if delivery and (
            (status == 409 and error_code == "result_pending")
            or (status == 404 and error_code == "report_unavailable")
        ):
            artifact = "report" if delivery[1] == "true" else "result"
            error = GatewayError(
                f"{artifact}_pending", f"The analysis {artifact} is not ready.", 409
            )
        elif status == 404:
            error = GatewayError("not_found", "The analysis resource was not found.", 404)
        elif status == 409 and error_code == "model_unavailable":
            error = GatewayError(
                "model_unavailable",
                "Ask the Regimex administrator to publish an active model before starting analysis.",
                409,
            )
        elif status == 409 and error_code == "retry_not_available":
            error = GatewayError(
                "retry_not_available", "Only failed or cancelled analyses can be retried.", 409
            )
        elif status == 409:
            error = GatewayError(
                "analysis_conflict",
                "The analysis is not available in its current state.",
                409,
            )
        elif status == 429 and error_code == "storage_quota_exceeded":
            error = GatewayError(
                "storage_quota_exceeded",
                "Regimex storage is full. Ask the administrator to free space or raise the quota.",
                429,
            )
        elif status == 429:
            raw = response.getheader("Retry-After", "")
            retry_after = int(raw) if re.fullmatch(r"[0-9]{1,4}", raw) else 5
            error = GatewayError(
                "analysis_busy",
                "Regimex is busy. Retry after the indicated delay.",
                429,
                retryable=True,
                retry_after=max(1, min(3600, retry_after)),
            )
        elif status in {401, 403}:
            error = GatewayError(
                "connection_access_denied",
                "The Regimex connection needs attention from the service administrator.",
                503,
            )
        elif 300 <= status < 400 or status in {400, 413, 415, 422}:
            error = invalid_response()
        else:
            error = unavailable()
        raise UpstreamStatusFailure(error, status)

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


class RegimexClient(_ConfiguredClient):
    def __init__(self, connection: Connection, *, subject: str, run_id: str) -> None:
        if not re.fullmatch(IDENTITY_PATTERN, subject) or not re.fullmatch(
            IDENTITY_PATTERN, run_id
        ):
            raise ValueError("Invalid verified subject or run identifier")
        super().__init__(connection)
        self.subject = subject
        self.run_id = run_id

    def _allows(self, method: str, path: str) -> bool:
        if _DELIVERY.fullmatch(path):
            return method == "GET"
        if _PATH.fullmatch(path):
            return True
        if method == "POST" and _RETRY.fullmatch(path):
            return True
        history = _HISTORY.fullmatch(path)
        return (
            method == "GET"
            and history is not None
            and 1 <= int(history[1]) <= 21
            and (history[2] is None or int(history[2]) <= 10000)
        )

    def delivery(self, analysis_id: str, *, include_report: bool) -> Delivery:
        path = f"/api/v1/integration/analyses/{analysis_id}/delivery"
        if include_report:
            path += "?include_report=true"
        if not _DELIVERY.fullmatch(path):
            raise ValueError("Invalid internal analysis identifier")
        response = self.request("GET", path)
        if response.content_type.split(";", 1)[0].strip().lower() != "application/zip":
            raise invalid_response()
        return read_delivery(response.body, member_limits(self.connection.response_bytes, include_report))

    def _credentials(self, path: str) -> dict[str, str]:
        return {
            "Authorization": "Bearer " + self.connection.token.get_secret_value(),
            "X-Regimex-Subject": self.subject,
            "X-Regimex-Run-ID": self.run_id,
        }


class DiagnosticClient(_ConfiguredClient):
    """Read-only self-checks; never create a delegated user or send secrets to health routes."""

    def _allows(self, method: str, path: str) -> bool:
        return method == "GET" and path in {
            "/health/live",
            "/health/ready",
            "/health/models",
            "/api/v1/connection",
        }

    def _credentials(self, path: str) -> dict[str, str]:
        if path == "/api/v1/connection":
            return {"Authorization": "Bearer " + self.connection.token.get_secret_value()}
        return {}
