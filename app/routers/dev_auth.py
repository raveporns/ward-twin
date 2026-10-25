
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.config import settings
from app.core.security import create_local_token


router = APIRouter(
    prefix="/dev",
    tags=["Local Development"],
)

# บัญชีจำลองสำหรับทดสอบเท่านั้น
TEST_USERS = {
    # Ward A
    "nurseA1": {"role": "nurse", "wards": ["WARD-A"]},
    "nurseA2": {"role": "nurse", "wards": ["WARD-A"]},
    "doctorA": {"role": "doctor", "wards": ["WARD-A"]},

    # Ward B
    "nurseB1": {"role": "nurse", "wards": ["WARD-B"]},
    "nurseB2": {"role": "nurse", "wards": ["WARD-B"]},
    "doctorB": {"role": "doctor", "wards": ["WARD-B"]},

    # Ward C
    "nurseC1": {"role": "nurse", "wards": ["WARD-C"]},
    "nurseC2": {"role": "nurse", "wards": ["WARD-C"]},
    "doctorC": {"role": "doctor", "wards": ["WARD-C"]},

    # บัญชีทดสอบเดิม
    "nurse-a": {"role": "nurse", "wards": ["WARD-TEST"]},
    "nurse-b": {"role": "nurse", "wards": ["WARD-B"]},
    "admin": {
        "role": "admin",
        "wards": ["WARD-A", "WARD-B", "WARD-C", "WARD-TEST"],
    },
}


class DevTokenRequest(BaseModel):
    user_id: str


@router.post("/token")
def issue_test_token(body: DevTokenRequest):
    # ปิด Dev Token นอกสภาพแวดล้อมทดสอบ
    if settings.app_env != "local":
        raise HTTPException(status_code=404)

    user = TEST_USERS.get(body.user_id)

    if user is None:
        raise HTTPException(
            status_code=400,
            detail="Unknown test user",
        )

    token = create_local_token(
        user_id=body.user_id,
        role=user["role"],
        wards=user["wards"],
    )

    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": 1800,
    }
