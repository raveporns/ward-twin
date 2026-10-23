import secrets

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


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
):
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return verify_user_token(credentials.credentials)


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
        user = verify_user_token(credentials.credentials)
        return {
            "type": "user",
            "producer_id": f"user:{user['sub']}",
            "user": user,
        }

    raise HTTPException(
        status_code=401,
        detail="Authentication required",
    )
