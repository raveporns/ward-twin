
from fastapi import FastAPI

from app.core.config import settings
from app.routers import health, patients, observations

app = FastAPI(
    title="WARD TWIN Pilot API",
    version="1.0.0-draft",
)

app.include_router(
    health.router,
    prefix="/api/v1",
)

app.include_router(
    patients.router,
    prefix="/api/v1",
)

if settings.app_env == "local":
    from app.routers import dev_auth
    app.include_router(
        dev_auth.router,
        prefix="/api/v1",
    )

app.include_router(observations.router, prefix="/api/v1")