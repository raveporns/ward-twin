from typing import Literal

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
    "nurse-a": {
        "role": "nurse",
        "wards": ["WARD-TEST"],
    },
    "nurse-b": {
        "role": "nurse",
        "wards": ["WARD-B"],
    },
    "admin": {
        "role": "admin",
        "wards": ["WARD-A", "WARD-B", "WARD-TEST"],
    },
}


class DevTokenRequest(BaseModel):
    user_id: Literal["nurse-a", "nurse-b", "admin"]


@router.post("/token")
def issue_test_token(body: DevTokenRequest):
    if settings.app_env != "local":
        raise HTTPException(status_code=404)

    user = TEST_USERS[body.user_id]

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
