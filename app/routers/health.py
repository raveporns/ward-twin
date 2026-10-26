
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel


router = APIRouter(tags=["Operations"])


class HealthResponse(BaseModel):
    status: Literal["ok"]


@router.get(
    "/healthz",
    response_model=HealthResponse,
    summary="Liveness Check",
    description=(
        "ตรวจสอบว่า FastAPI application ยังทำงาน "
        "โดยไม่ตรวจสอบการเชื่อมต่อฐานข้อมูล"
    ),
)
def health_check():
    return {
        "status": "ok",
    }
