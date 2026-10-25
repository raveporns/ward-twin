
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.core.config import settings
from app.core.security import create_access_token


def decode_token(token: str) -> dict:
    return jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=["HS256"],
        audience=settings.jwt_audience,
        issuer=settings.jwt_issuer,
    )


def test_create_access_token_success():
    token = create_access_token(
        user_id="test-user-001",
        role="nurse",
        wards=["WARD-A"],
    )

    payload = decode_token(token)

    assert payload["sub"] == "test-user-001"
    assert payload["role"] == "nurse"
    assert payload["wards"] == ["WARD-A"]
    assert payload["token_type"] == "access"


@pytest.mark.parametrize(
    "role",
    ["viewer", "nurse", "doctor", "admin"],
)
def test_create_access_token_allowed_roles(role):
    token = create_access_token(
        user_id="test-user-001",
        role=role,
        wards=["WARD-A"],
    )

    payload = decode_token(token)

    assert payload["role"] == role


@pytest.mark.parametrize(
    "role",
    ["pending", "guest", "superadmin", ""],
)
def test_create_access_token_invalid_role(role):
    with pytest.raises(ValueError):
        create_access_token(
            user_id="test-user-001",
            role=role,
            wards=["WARD-A"],
        )


def test_create_access_token_missing_user_id():
    with pytest.raises(ValueError):
        create_access_token(
            user_id="",
            role="nurse",
            wards=["WARD-A"],
        )


def test_create_access_token_missing_wards():
    with pytest.raises(ValueError):
        create_access_token(
            user_id="test-user-001",
            role="nurse",
            wards=[],
        )


def test_create_access_token_multiple_wards():
    token = create_access_token(
        user_id="test-user-001",
        role="doctor",
        wards=["WARD-A", "WARD-B"],
    )

    payload = decode_token(token)

    assert payload["wards"] == ["WARD-A", "WARD-B"]


def test_create_access_token_expiration():
    token = create_access_token(
        user_id="test-user-001",
        role="nurse",
        wards=["WARD-A"],
    )

    payload = decode_token(token)

    issued_at = datetime.fromtimestamp(
        payload["iat"],
        tz=timezone.utc,
    )

    expires_at = datetime.fromtimestamp(
        payload["exp"],
        tz=timezone.utc,
    )

    assert expires_at - issued_at == timedelta(minutes=30)


def test_create_access_token_expired_rejected():
    token = create_access_token(
        user_id="test-user-001",
        role="nurse",
        wards=["WARD-A"],
    )

    payload = decode_token(token)

    # จำลอง Token ที่หมดอายุแล้ว
    payload["exp"] = datetime.now(timezone.utc) - timedelta(
        minutes=1
    )

    expired_token = jwt.encode(
        payload,
        settings.jwt_secret,
        algorithm="HS256",
    )

    with pytest.raises(jwt.ExpiredSignatureError):
        decode_token(expired_token)


def test_create_access_token_invalid_signature():
    token = create_access_token(
        user_id="test-user-001",
        role="nurse",
        wards=["WARD-A"],
    )

    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(
            token,
            "different-secret-for-testing",
            algorithms=["HS256"],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
        )
