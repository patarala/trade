"""Bounded, read-only operator checks; never infer why an opaque key was rejected."""

from dataclasses import dataclass, replace
from typing import Literal

from .client import DiagnosticClient, TransportFailure, UpstreamStatusFailure
from .config import RegimexSettings
from .errors import GatewayError


@dataclass(frozen=True)
class Check:
    name: str
    status: Literal["passed", "failed", "not_checked", "disabled"]
    code: str
    message: str


@dataclass(frozen=True)
class DiagnosticReport:
    status: Literal["passed", "failed", "disabled"]
    checks: tuple[Check, ...]


def invalid_configuration() -> DiagnosticReport:
    return DiagnosticReport(
        "failed",
        (
            Check(
                "configuration",
                "failed",
                "invalid_configuration",
                "Invalid Regimex settings. Check the paired URL/key, HTTPS origin, source namespace "
                "and concurrency limit; configuration values are intentionally omitted.",
            ),
        ),
    )


def _failure(name: str, error: GatewayError) -> Check:
    if isinstance(error, TransportFailure):
        messages = {
            "certificate_rejected": (
                "TLS certificate or hostname verification failed. "
                "Check the server certificate and trusted CA store."
            ),
            "tls_failed": "The TLS handshake failed. Verify that the configured port serves HTTPS.",
            "dns_failed": "The configured service hostname could not be resolved.",
            "timeout": "The connection check exceeded its time budget.",
            "connection_failed": (
                "A connection to the configured service could not be established or maintained."
            ),
            "invalid_protocol": "The configured service returned an invalid HTTP response.",
        }
        return Check(name, "failed", error.category, messages[error.category])
    if isinstance(error, UpstreamStatusFailure):
        status = error.upstream_status
        if status == 401:
            return Check(
                name,
                "failed",
                "credential_rejected",
                "The key was rejected. Invalid, expired, revoked and suspended credentials "
                "are deliberately indistinguishable; inspect the connection in Regimex.",
            )
        if status == 403:
            return Check(
                name,
                "failed",
                "access_denied",
                "The server refused access. Use a backend integration key "
                "and check its connection policy.",
            )
        if status == 404:
            return Check(
                name,
                "failed",
                "endpoint_unavailable",
                "The diagnostic endpoint is unavailable. "
                "Check the service origin and Regimex version.",
            )
        if status == 429:
            return Check(
                name,
                "failed",
                "rate_limited",
                f"Regimex rate-limited the check. Retry in {error.retry_after or 5} seconds.",
            )
        if status == 503 and error.code == "analysis_busy":
            return Check(
                name,
                "failed",
                "analysis_busy",
                f"Regimex reached its request limit. Retry the check after {error.retry_after} s.",
            )
        if 300 <= status < 400:
            return Check(
                name,
                "failed",
                "redirect_rejected",
                "The service attempted a redirect. Configure its final HTTPS origin.",
            )
        if status == 503 and name in {"database", "model"}:
            return Check(
                name,
                "failed",
                "not_ready",
                "Regimex reports that this component is not ready. Run its service doctor command.",
            )
        return Check(
            name,
            "failed",
            "upstream_unavailable",
            "The service did not accept the diagnostic request. Inspect its operational status.",
        )
    return Check(
        name,
        "failed",
        "invalid_response",
        "The service response did not match the expected diagnostic contract.",
    )


def diagnose(settings: RegimexSettings) -> DiagnosticReport:
    connection = settings.regimex_connection()
    if connection is None:
        return DiagnosticReport(
            "disabled",
            (
                Check(
                    "configuration",
                    "disabled",
                    "analysis_disabled",
                    "Regimex is disabled. The simulator operates without this connection.",
                ),
            ),
        )
    checks = [Check("configuration", "passed", "configured", "Regimex settings are valid.")]
    client = DiagnosticClient(
        replace(
            connection,
            connect_timeout=3,
            read_timeout=3,
            total_timeout=10,
            response_bytes=64 * 1024,
        )
    )

    def read(name: str, path: str) -> dict | None:
        try:
            return client.json("GET", path)
        except GatewayError as error:
            checks.append(_failure(name, error))
            return None

    api = read("api", "/health/live")
    if api != {"status": "alive"}:
        if api is not None:
            checks.append(
                Check(
                    "api",
                    "failed",
                    "invalid_response",
                    "The origin did not identify a live Regimex API.",
                )
            )
        checks.append(
            Check(
                "connection",
                "not_checked",
                "api_unavailable",
                "Credentials were not sent because the API check failed.",
            )
        )
        return DiagnosticReport("failed", tuple(checks))
    transport = (
        "verified HTTPS"
        if connection.url.startswith("https:")
        else "explicit loopback HTTP (TLS is not used)"
    )
    checks.append(
        Check("api", "passed", "reachable", f"The Regimex API is reachable over {transport}.")
    )

    account = read("connection", "/api/v1/connection")
    if account is not None:
        scopes, source = account.get("scopes"), account.get("source_namespace")
        if (
            not isinstance(source, str)
            or not 1 <= len(source) <= 128
            or not isinstance(scopes, list)
            or not 1 <= len(scopes) <= 4
            or any(scope not in ("read", "upload", "analyze", "delete") for scope in scopes)
            or len(set(scopes)) != len(scopes)
        ):
            checks.append(
                Check(
                    "connection",
                    "failed",
                    "invalid_response",
                    "The connection metadata is invalid.",
                )
            )
        else:
            checks.append(
                Check(
                    "connection",
                    "passed",
                    "credential_accepted",
                    "A current integration credential was accepted.",
                )
            )
            compatible = account.get("analysis_protocol") == "regimex-analysis/1"
            checks.append(
                Check(
                    "protocol",
                    "passed" if compatible else "failed",
                    "protocol_verified" if compatible else "incompatible_protocol",
                    "The service supports analysis submission and delivery."
                    if compatible
                    else "Trade requires regimex-analysis/1. Check the Regimex version.",
                )
            )
            matches = source == connection.source_id
            checks.append(
                Check(
                    "source",
                    "passed" if matches else "failed",
                    "source_verified" if matches else "source_mismatch",
                    "The key matches the configured source namespace."
                    if matches
                    else "The key belongs to a different source namespace. "
                    "Check the connection before submitting data.",
                )
            )
            missing = sorted({"read", "upload", "analyze"} - set(scopes))
            checks.append(
                Check(
                    "capabilities",
                    "failed" if missing else "passed",
                    "missing_capabilities" if missing else "capabilities_verified",
                    "Missing required capabilities: " + ", ".join(missing) + "."
                    if missing
                    else "The key allows read, upload and analysis operations.",
                )
            )

    ready = read("database", "/health/ready")
    database_ready = (
        ready is not None and ready.get("status") == "ready" and ready.get("database") is True
    )
    if ready is not None:
        valid = database_ready
        checks.append(
            Check(
                "database",
                "passed" if valid else "failed",
                "ready" if valid else "not_ready",
                "The database schema and restore state are ready."
                if valid
                else "The database readiness response is not ready or invalid.",
            )
        )
    if database_ready:
        model = read("model", "/health/models")
        if model is not None:
            valid = (
                model.get("deployment_configured") is True
                and model.get("worker_capability") == "not_probed"
            )
            checks.append(
                Check(
                    "model",
                    "passed" if valid else "failed",
                    "configured" if valid else "not_ready",
                    "An active model deployment is configured."
                    if valid
                    else "An active model deployment could not be verified.",
                )
            )
    checks.append(
        Check(
            "worker",
            "not_checked",
            "not_probed",
            "Worker availability is not checked. Run Regimex service doctor in the worker "
            "environment; actual analysis execution remains a separate check.",
        )
    )
    return DiagnosticReport(
        "failed" if any(item.status == "failed" for item in checks) else "passed", tuple(checks)
    )
