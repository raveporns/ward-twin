
from fastapi import FastAPI

from app.core.config import settings

from app.routers import (
    assessments,
    health,
    patients,
    observations,
    twin,
    observation_history,
    agent_context,
    readiness,
    import_templates,
    imports,
    alerts,
    auth,
)

from app.routers.admin import router as admin_router


app = FastAPI(
    title="WARD TWIN Pilot API",
    version="1.0.0-draft",
    description=(
        "WARD TWIN Backend API for patient monitoring, "
        "clinical assessments, alerts, and ICU risk prediction."
    ),
)


# ============================================================
# Root Endpoint
# ============================================================

@app.get("/", tags=["Operations"])
def root():
    return {
        "service": "WARD TWIN Pilot API",
        "version": "1.0.0-draft",
        "status": "running",
    }


# ============================================================
# Operations / Health Checks
# ============================================================

# Public health endpoints
app.include_router(health.router)
app.include_router(readiness.router)

# Backward-compatible API version endpoints
app.include_router(
    health.router,
    prefix="/api/v1",
    include_in_schema=False,
)

app.include_router(
    readiness.router,
    prefix="/api/v1",
    include_in_schema=False,
)


# ============================================================
# Patient Monitoring
# ============================================================

app.include_router(
    patients.router,
    prefix="/api/v1",
)

app.include_router(
    observations.router,
    prefix="/api/v1",
)

app.include_router(
    observation_history.router,
    prefix="/api/v1",
)


# ============================================================
# Digital Twin
# ============================================================

app.include_router(
    twin.router,
    prefix="/api/v1",
)


# ============================================================
# AI Agent
# ============================================================

app.include_router(
    agent_context.router,
    prefix="/api/v1",
)


# ============================================================
# CSV Imports
# ============================================================

app.include_router(
    import_templates.router,
    prefix="/api/v1",
)

app.include_router(
    imports.router,
    prefix="/api/v1",
)


# ============================================================
# Alerts
# ============================================================

app.include_router(
    alerts.router,
    prefix="/api/v1",
)


# ============================================================
# Nurse Assessments
# ============================================================

app.include_router(
    assessments.router,
    prefix="/api/v1",
)


# ============================================================
# Authentication
# ============================================================

app.include_router(
    auth.router,
    prefix="/api/v1",
)


# ============================================================
# Administration
# ============================================================

app.include_router(
    admin_router,
    prefix="/api/v1",
)


# ============================================================
# Local Development Authentication
# ============================================================

if settings.app_env == "local":
    from app.routers import dev_auth

    app.include_router(
        dev_auth.router,
        prefix="/api/v1",
    )
