
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.database import get_db


router = APIRouter(tags=["System Health"])


class ReadinessResponse(BaseModel):
    status: Literal["ready"]
    database: Literal["connected"]


@router.get(
    "/readyz",
    response_model=ReadinessResponse,
    summary="Readiness Check",
    description=(
        "ตรวจสอบว่า Backend สามารถเชื่อมต่อ "
        "PostgreSQL Database ได้"
    ),
    responses={
        503: {
            "description": "Database unavailable",
        },
    },
)
def readiness_check(
    db: Session = Depends(get_db),
):
    try:
        db.execute(text("SELECT 1"))

        return {
            "status": "ready",
            "database": "connected",
        }

    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable",
        ) from exc
