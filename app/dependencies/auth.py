import secrets
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.services.auth_repository import get_active_user_access

from fastapi import (
    Depends,
    Header,
    HTTPException,
)
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBearer,
)

from app.core.config import settings
from app.core.security import verify_user_token


bearer = HTTPBearer(auto_error=False)


def resolve_user_identity(
    token: str,
    db: Session,
) -> dict:
    payload = verify_user_token(token)

    # Local Development Token
    # ใช้ได้เฉพาะ APP_ENV=local ตามที่ verify_user_token ตรวจไว้
    if payload.get("token_type") is None:
        return payload

    # Access Token ของบัญชีจริง
    try:
        account = get_active_user_access(
            db,
            user_id=payload["sub"],
        )
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail="Authentication temporarily unavailable",
        ) from exc

    if account is None:
        raise HTTPException(
            status_code=403,
            detail="Account access denied",
        )

    # ใช้ Role และ Ward Access ล่าสุดจากฐานข้อมูล
    return {
        **payload,
        "role": account["role"],
        "wards": account["wards"],
    }

def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
):
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return resolve_user_identity(
        credentials.credentials,
        db,
    )


def require_ward(ward_id: str, user: dict):
    if ward_id not in user["wards"]:
        raise HTTPException(
            status_code=403,
            detail="Ward access denied",
        )
    return user


def require_gateway(
    x_gateway_key: str | None = Header(default=None),
):
    if (
        x_gateway_key is None
        or not secrets.compare_digest(
            x_gateway_key,
            settings.gateway_api_key,
        )
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid gateway credentials",
        )

    allowed_devices = {
        device.strip()
        for device in settings.gateway_allowed_devices.split(",")
        if device.strip()
    }

    return {
        "producer_id": settings.gateway_producer_id,
        "allowed_devices": allowed_devices,
    }


def get_observation_identity(
    x_gateway_key: str | None = Header(default=None),
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
):
    # ไม่อนุญาตให้ส่ง Gateway Key และ Bearer Token พร้อมกัน
    if x_gateway_key is not None and credentials is not None:
        raise HTTPException(
            status_code=400,
            detail="Use only one authentication method",
        )

    # IoT Gateway
    if x_gateway_key is not None:
        gateway = require_gateway(x_gateway_key=x_gateway_key)
        return {
            "type": "gateway",
            "producer_id": gateway["producer_id"],
            "allowed_devices": gateway["allowed_devices"],
        }

    # เจ้าหน้าที่ผ่าน JWT
    if credentials is not None:
        user = resolve_user_identity(
        credentials.credentials,
        db,
    )
        return {
            "type": "user",
            "producer_id": f"user:{user['sub']}",
            "user": user,
        }

    raise HTTPException(
        status_code=401,
        detail="Authentication required",
    )

from pydantic import BaseModel, EmailStr, Field, ConfigDict


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str = Field(
        min_length=12,
        max_length=128,
    )
    display_name: str = Field(
        min_length=1,
        max_length=100,
    )


class RegisterResponse(BaseModel):
    user_id: str
    email: EmailStr
    display_name: str
    role: str
    status: str


def require_admin(
    user: dict = Depends(get_current_user),
) -> dict:
    # ไม่อนุญาต Local Development Token
    # ต้องเป็น Access Token ของบัญชีจริงเท่านั้น
    if user.get("token_type") != "access":
        raise HTTPException(
            status_code=403,
            detail="Admin access denied",
        )

    # Role นี้ต้องมาจาก Database ล่าสุด
    # ผ่าน resolve_user_identity()
    if user.get("role") != "admin":
        raise HTTPException(
            status_code=403,
            detail="Admin access denied",
        )

    return user
