from datetime import datetime, timedelta, timezone

import jwt
from fastapi import HTTPException, status

from app.core.config import settings


def create_local_token(
    user_id: str,
    role: str,
    wards: list[str],
) -> str:
    if settings.app_env != "local":
        raise RuntimeError("Local tokens are disabled")

    now = datetime.now(timezone.utc)

    payload = {
        "sub": user_id,
        "role": role,
        "wards": wards,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(minutes=30),
    }

    return jwt.encode(
        payload,
        settings.jwt_secret,
        algorithm="HS256",
    )



def verify_user_token(token: str) -> dict:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={
                "require": [
                    "sub",
                    "iss",
                    "aud",
                    "iat",
                    "nbf",
                    "exp",
                ],
            },
        )

        user_id = payload.get("sub")
        role = payload.get("role")
        wards = payload.get("wards")
        token_type = payload.get("token_type")

        if not isinstance(user_id, str) or not user_id:
            raise jwt.InvalidTokenError("Invalid user ID")

        if (
            not isinstance(wards, list)
            or not wards
            or any(
                not isinstance(w, str) or not w
                for w in wards
            )
        ):
            raise jwt.InvalidTokenError("Invalid wards")

        # Access Token สำหรับบัญชีผู้ใช้จริง
        if token_type == "access":
            if role not in {
                "viewer",
                "nurse",
                "doctor",
                "admin",
            }:
                raise jwt.InvalidTokenError("Invalid role")

        # Local Development Token เดิมไม่มี token_type
        elif token_type is None:
            if settings.app_env != "local":
                raise jwt.InvalidTokenError(
                    "Local token is disabled"
                )

            if role not in {"nurse", "admin"}:
                raise jwt.InvalidTokenError(
                    "Invalid local role"
                )

        else:
            raise jwt.InvalidTokenError(
                "Invalid token type"
            )

        return payload

    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={
                "WWW-Authenticate": "Bearer"
            },
        ) from exc



def create_access_token(
    *,
    user_id: str,
    role: str,
    wards: list[str],
) -> str:
    allowed_roles = {"viewer", "nurse", "doctor", "admin"}

    if role not in allowed_roles:
        raise ValueError("Invalid role for access token")

    if not user_id:
        raise ValueError("User ID is required")

    if not wards:
        raise ValueError("At least one ward is required")

    now = datetime.now(timezone.utc)

    payload = {
        "sub": user_id,
        "role": role,
        "wards": wards,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(minutes=30),
        "token_type": "access",
    }

    return jwt.encode(
        payload,
        settings.jwt_secret,
        algorithm="HS256",
    )
