"""Fixed public errors; upstream bodies and credentials never reach the browser."""


class GatewayError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status: int,
        *,
        retryable: bool = False,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after


def invalid_response() -> GatewayError:
    return GatewayError(
        "invalid_analysis_response",
        "The Regimex response could not be verified.",
        502,
        retryable=True,
    )


def unavailable() -> GatewayError:
    return GatewayError(
        "analysis_unavailable",
        "Regimex is temporarily unavailable. The simulation results remain available.",
        503,
        retryable=True,
    )
