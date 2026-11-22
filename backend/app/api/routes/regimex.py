"""Optional analysis of an explicitly selected, owned, completed simulator run."""

import asyncio
import re
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response

from backend.app.api.dependencies import CurrentUser, DbSession
from backend.app.core.config import settings
from backend.app.integrations.regimex.bindings import VerifiedExports, binding_key
from backend.app.integrations.regimex.client import RegimexClient
from backend.app.integrations.regimex.config import Connection
from backend.app.integrations.regimex.errors import GatewayError
from backend.app.integrations.regimex.export import (
    ExportError,
    build_export,
    export_instruments,
    incomplete_run_reason,
)
from backend.app.integrations.regimex.json import json_bytes, strict_json
from backend.app.integrations.regimex.service import AnalysisGateway, public_view
from backend.app.models import SimulationRun
from backend.app.schemas.simulation_run import SimulationRunRead
from backend.app.services.simulation_results import get_owned_run
from backend.app.services.simulations import SimulationNotFoundError

router = APIRouter(prefix="/simulations", tags=["Regimex analysis"])
_verified_exports = VerifiedExports()
_MAX_PREVIEW_BYTES = 16 * 1024 * 1024
_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
_REQUEST_BODY_TIMEOUT = 5.0
_REQUEST_BODY_BYTES = 4096
_ASSERTIONS = {
    "x-regimex-subject",
    "x-regimex-run-id",
    "x-external-user-id",
    "x-external-owner-id",
    "x-owner-id",
    "x-source-namespace",
}


async def gateway_error(request: Request, error: GatewayError) -> JSONResponse:
    headers = dict(_HEADERS)
    if error.retry_after is not None:
        headers["Retry-After"] = str(error.retry_after)
    body = {"detail": error.message, "code": error.code, "retryable": error.retryable}
    if getattr(request.state, "regimex_export_available", False):
        body["export_available"] = True
    return JSONResponse(body, error.status, headers=headers)


@dataclass(frozen=True)
class OwnedRun:
    run: SimulationRun
    subject: str


def owned_run(
    simulation_id: int,
    request: Request,
    db: DbSession,
    user: CurrentUser,
    run_id: Annotated[int, Query(gt=0)],
) -> OwnedRun:
    if (
        _ASSERTIONS.intersection(request.headers)
        or len(request.headers.getlist("authorization")) != 1
    ):
        raise GatewayError(
            "invalid_identity",
            "Use simulator authentication without delegated identity headers.",
            400,
        )
    subject = str(user.id)
    try:
        run = get_owned_run(db, simulation_id, user.id, run_id)
    except SimulationNotFoundError:
        raise GatewayError("not_found", "Experiment or run not found.", 404) from None
    return OwnedRun(run, subject)


Owned = Annotated[OwnedRun, Depends(owned_run)]


def _export(db: DbSession, owned: OwnedRun) -> dict:
    reason = incomplete_run_reason(owned.run)
    if reason:
        raise GatewayError("run_ineligible", reason, 409)
    try:
        result = build_export(db, owned.run, settings.regimex_source_id)
    except ExportError as error:
        raise GatewayError("export_unavailable", str(error), 422) from None
    # End the read transaction so network calls do not hold the completed run's lock.
    db.rollback()
    return result


def _connection() -> Connection:
    connection = settings.regimex_connection()
    if connection is None:
        raise GatewayError(
            "analysis_disabled", "Regimex is not configured for this simulator.", 503
        )
    return connection


def _gateway(
    db: DbSession, owned: OwnedRun, request: Request, *, metadata_only: bool = False
) -> AnalysisGateway:
    connection = _connection()
    reason = incomplete_run_reason(owned.run)
    if reason:
        raise GatewayError("run_ineligible", reason, 409)
    key = (
        binding_key(
            db, owned.run, subject=owned.subject,
            source_id=connection.source_id, origin=connection.url,
        )
        if metadata_only else None
    )
    export = _verified_exports.get(key) if key is not None else None
    cached = export is not None
    if export is None:
        export = _export(db, owned)
    else:
        db.rollback()
    # Cache a freshly built export's identity only after Regimex confirms the snapshot.
    remember = (
        (lambda: _verified_exports.remember(key, export))
        if key is not None and not cached else None
    )
    # An upstream failure can still leave the local export available. Only set
    # the flag once both run ownership and its local export have been verified.
    request.state.regimex_export_available = True
    return AnalysisGateway(
        RegimexClient(connection, subject=owned.subject, run_id=export["source"]["run_id"]),
        export, on_verified=remember,
    )


@router.get("/{simulation_id}/regimex")
def overview(db: DbSession, owned: Owned, request: Request) -> JSONResponse:
    run_id = owned.run.id
    connection = settings.regimex_connection()
    reason = incomplete_run_reason(owned.run)
    if connection is None and reason is None:
        try:
            export_instruments(db, owned.run)
        except ExportError as error:
            reason = str(error)
    if connection is None or reason is not None:
        return JSONResponse(
            {
                "enabled": connection is not None,
                "eligible": reason is None,
                "reason": reason,
                "analysis": None,
            },
            headers=_HEADERS,
        )
    try:
        gateway = _gateway(db, owned, request, metadata_only=True)
    except GatewayError as error:
        if error.code != "export_unavailable":
            raise
        return JSONResponse(
            {"enabled": True, "eligible": False, "reason": error.message, "analysis": None},
            headers=_HEADERS,
        )
    value = gateway.latest()
    return JSONResponse(
        {
            "enabled": True,
            "eligible": True,
            "reason": None,
            "analysis": None if value is None else public_view(value, run_id),
        },
        headers=_HEADERS,
    )


def _attachment(content: bytes, content_type: str, filename: str) -> Response:
    return Response(
        content,
        media_type=content_type,
        headers={
            **_HEADERS,
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Security-Policy": "sandbox; default-src 'none'",
        },
    )


@router.get("/{simulation_id}/regimex/export")
def export_run(db: DbSession, owned: Owned) -> Response:
    run_id = owned.run.id
    export = _export(db, owned)
    return _attachment(json_bytes(export), "application/json", f"run-{run_id}-regimex.json")


@router.get("/{simulation_id}/regimex/run")
def linked_run(owned: Owned) -> JSONResponse:
    """Resolve exactly one owned run for a saved analysis link."""
    return JSONResponse(
        SimulationRunRead.model_validate(owned.run).model_dump(mode="json"), headers=_HEADERS
    )


@router.get("/{simulation_id}/regimex/analyses")
def history(
    db: DbSession,
    owned: Owned,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=20)] = 10,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
) -> JSONResponse:
    run_id = owned.run.id
    page = _gateway(db, owned, request, metadata_only=True).history(limit=limit, offset=offset)
    return JSONResponse(
        {**page, "items": [public_view(value, run_id) for value in page["items"]]},
        headers=_HEADERS,
    )


async def retry_request(request: Request) -> str | None:
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
    ):
        raise GatewayError("invalid_request", "Use an application/json body.", 415)
    if request.headers.get("content-encoding", "identity").lower() != "identity":
        raise GatewayError("invalid_request", "Encoded request bodies are unsupported.", 415)
    length = request.headers.get("content-length")
    if length is not None:
        if not re.fullmatch(r"[0-9]{1,20}", length):
            raise GatewayError("invalid_request", "Invalid request body length.", 400)
        if int(length) > _REQUEST_BODY_BYTES:
            raise GatewayError("invalid_request", "The analysis request is too large.", 413)
    data = bytearray()
    try:
        async with asyncio.timeout(_REQUEST_BODY_TIMEOUT):
            async for block in request.stream():
                if len(data) + len(block) > _REQUEST_BODY_BYTES:
                    raise GatewayError("invalid_request", "The analysis request is too large.", 413)
                data.extend(block)
    except TimeoutError:
        raise GatewayError(
            "request_timeout",
            "The analysis request body was not received in time.",
            408,
            retryable=True,
        ) from None
    try:
        payload = strict_json(bytes(data))
        if not isinstance(payload, dict) or set(payload) - {"retry_of"}:
            raise ValueError
        value = payload.get("retry_of")
        if value is not None and (not isinstance(value, str) or str(UUID(value)) != value):
            raise ValueError
        return value
    except ValueError, UnicodeError, RecursionError:
        raise GatewayError(
            "invalid_request", "Use an empty object or a valid retry_of analysis ID.", 422
        ) from None


Retry = Annotated[str | None, Depends(retry_request)]


@router.post("/{simulation_id}/regimex/analyses", status_code=202)
def submit(retry_of: Retry, db: DbSession, owned: Owned, request: Request) -> JSONResponse:
    # FastAPI resolves dependencies in order: finish the bounded body read before
    # acquiring a database connection or the owned run's shared read lock.
    run_id = owned.run.id
    result = _gateway(db, owned, request).submit(retry_of)
    return JSONResponse(public_view(result, run_id), status_code=202, headers=_HEADERS)


@router.get("/{simulation_id}/regimex/analyses/{analysis_id}")
def status(analysis_id: UUID, db: DbSession, owned: Owned, request: Request) -> JSONResponse:
    run_id = owned.run.id
    value = _gateway(db, owned, request, metadata_only=True).bound(str(analysis_id))
    return JSONResponse(public_view(value, run_id), headers=_HEADERS)


@router.get("/{simulation_id}/regimex/analyses/{analysis_id}/result")
def result(analysis_id: UUID, db: DbSession, owned: Owned, request: Request) -> Response:
    value = _gateway(db, owned, request).result(str(analysis_id))
    return _attachment(json_bytes(value), "application/json", f"analysis-{analysis_id}.json")


@router.get("/{simulation_id}/regimex/analyses/{analysis_id}/preview")
def preview(analysis_id: UUID, db: DbSession, owned: Owned, request: Request) -> Response:
    """Build observations once and pair them with their verified saved result."""
    gateway = _gateway(db, owned, request)
    content = json_bytes({"result": gateway.result(str(analysis_id)), "input": gateway.export})
    if len(content) > _MAX_PREVIEW_BYTES:
        raise GatewayError("preview_too_large", "The analysis preview is too large.", 413)
    return Response(content, media_type="application/json", headers=_HEADERS)


@router.get("/{simulation_id}/regimex/analyses/{analysis_id}/report")
def report(analysis_id: UUID, db: DbSession, owned: Owned, request: Request) -> Response:
    content = _gateway(db, owned, request).report(str(analysis_id))
    return _attachment(content, "text/html", f"analysis-{analysis_id}.html")
