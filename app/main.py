
from fastapi import FastAPI

from app.core.config import settings
from app.routers import assessments, health, patients, observations, twin, observation_history, agent_context, readiness, import_templates, imports, alerts, auth
from app.routers.admin import router as admin_router

app = FastAPI(
    title="WARD TWIN Pilot API",
    version="1.0.0-draft",
)

app.include_router(health.router, prefix="/api/v1")
app.include_router(patients.router, prefix="/api/v1")
app.include_router(observations.router, prefix="/api/v1")

# Digital Twin API
app.include_router(twin.router, prefix="/api/v1")

app.include_router(
    observation_history.router,
    prefix="/api/v1",
)

app.include_router(agent_context.router, prefix="/api/v1")

app.include_router(readiness.router, prefix="/api/v1")

app.include_router(import_templates.router, prefix="/api/v1")

app.include_router(imports.router, prefix="/api/v1")

app.include_router(alerts.router, prefix="/api/v1")

app.include_router(assessments.router, prefix="/api/v1")

app.include_router(auth.router, prefix="/api/v1")

app.include_router(
    admin_router,
    prefix="/api/v1",
)

# Development Authentication
if settings.app_env == "local":
    from app.routers import dev_auth
    app.include_router(dev_auth.router, prefix="/api/v1")
