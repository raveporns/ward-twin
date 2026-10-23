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
                    "sub", "iss", "aud",
                    "iat", "nbf", "exp",
                ],
            },
        )

        if (
            not isinstance(payload.get("sub"), str)
            or not payload["sub"]
            or payload.get("role") not in ("nurse", "admin")
            or not isinstance(payload.get("wards"), list)
            or not payload["wards"]
            or any(
                not isinstance(w, str) or not w
                for w in payload["wards"]
            )
        ):
            raise jwt.InvalidTokenError("Invalid claims")

        return payload

    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
