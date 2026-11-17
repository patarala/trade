"""Limit concurrent requests to the simulator's Regimex routes."""

import logging
import re
import threading

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_PATH = re.compile(r"/api/simulations/[^/]+/regimex(?:/.*)?\Z")
_LOG = logging.getLogger(__name__)


class RegimexConcurrencyMiddleware:
    """Refuse excess requests before they acquire a database session or worker thread."""

    def __init__(self, app: ASGIApp, *, max_concurrent_requests: int) -> None:
        if type(max_concurrent_requests) is not int or not 1 <= max_concurrent_requests <= 16:
            raise ValueError("Regimex concurrency must be an integer between 1 and 16")
        self.app = app
        self._slots = threading.BoundedSemaphore(max_concurrent_requests)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _PATH.fullmatch(scope["path"]):
            await self.app(scope, receive, send)
            return
        if not self._slots.acquire(blocking=False):
            _LOG.info("regimex_admission_rejected capacity_exhausted")
            response = JSONResponse(
                {
                    "detail": "Analysis requests are busy. Retry after the indicated delay.",
                    "code": "analysis_busy",
                    "retryable": True,
                },
                status_code=503,
                headers={
                    "Retry-After": "2",
                    "Cache-Control": "private, no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )
            await response(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            self._slots.release()
