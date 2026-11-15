from fastapi import FastAPI

from backend.app.api.routes.auth import router as auth_router
from backend.app.api.routes.market import router as market_router
from backend.app.api.routes.regimex import gateway_error
from backend.app.api.routes.regimex import router as regimex_router
from backend.app.api.routes.simulation_results import router as results_router
from backend.app.api.routes.simulations import router as simulations_router
from backend.app.api.routes.users import router as users_router
from backend.app.core.config import settings
from backend.app.integrations.regimex.errors import GatewayError
from backend.app.integrations.regimex.limiter import RegimexConcurrencyMiddleware

app = FastAPI(
    title="Exchange Simulator",
)
app.add_middleware(
    RegimexConcurrencyMiddleware,
    max_concurrent_requests=settings.regimex_max_concurrent_requests,
)


app.include_router(
    auth_router,
    prefix="/api",
)
app.include_router(
    users_router,
    prefix="/api",
)
app.include_router(
    market_router,
    prefix="/api",
)
app.include_router(
    simulations_router,
    prefix="/api",
)
app.include_router(results_router, prefix="/api")
app.include_router(regimex_router, prefix="/api")
app.add_exception_handler(GatewayError, gateway_error)


@app.get("/health")
def health():
    return {"status": "ok"}
