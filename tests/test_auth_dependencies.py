
from unittest.mock import Mock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import OperationalError

from app.core.security import create_access_token, create_local_token
from app.dependencies.auth import (
    resolve_user_identity,
    require_ward,
    get_observation_identity,
)
from app.core.config import settings


def make_token():
    return create_access_token(
        user_id="test-user-001",
        role="nurse",
        wards=["WARD-A"],
    )


def make_account(
    role="nurse",
    wards=None,
):
    return {
        "user_id": "test-user-001",
        "role": role,
        "approval_status": "approved",
        "wards": ["WARD-A"] if wards is None else wards,
    }


@patch("app.dependencies.auth.get_active_user_access")
def test_approved_account(mock_access):
    mock_access.return_value = make_account()

    result = resolve_user_identity(make_token(), Mock())

    assert result["sub"] == "test-user-001"
    assert result["role"] == "nurse"
    assert result["wards"] == ["WARD-A"]


@patch("app.dependencies.auth.get_active_user_access")
def test_suspended_account_denied(mock_access):
    # Repository ส่ง None สำหรับบัญชีที่ไม่ approved
    mock_access.return_value = None

    with pytest.raises(HTTPException) as exc:
        resolve_user_identity(make_token(), Mock())

    assert exc.value.status_code == 403


@patch("app.dependencies.auth.get_active_user_access")
def test_ward_access_uses_database(mock_access):
    mock_access.return_value = make_account(
        wards=["WARD-B"]
    )

    result = resolve_user_identity(make_token(), Mock())

    assert result["wards"] == ["WARD-B"]

    with pytest.raises(HTTPException) as exc:
        require_ward("WARD-A", result)

    assert exc.value.status_code == 403

    assert require_ward("WARD-B", result) == result


@patch("app.dependencies.auth.get_active_user_access")
def test_role_uses_database(mock_access):
    mock_access.return_value = make_account(
        role="doctor"
    )

    result = resolve_user_identity(make_token(), Mock())

    assert result["role"] == "doctor"


@patch("app.dependencies.auth.get_active_user_access")
def test_database_unavailable(mock_access):
    mock_access.side_effect = OperationalError(
        statement="SELECT",
        params={},
        orig=Exception("Database unavailable"),
    )

    with pytest.raises(HTTPException) as exc:
        resolve_user_identity(make_token(), Mock())

    assert exc.value.status_code == 503


def test_invalid_token_denied():
    with pytest.raises(HTTPException) as exc:
        resolve_user_identity("invalid.jwt.token", Mock())

    assert exc.value.status_code == 401


def test_local_token_works_in_local_environment():
    if settings.app_env != "local":
        pytest.skip("Local token test requires APP_ENV=local")

    token = create_local_token(
        user_id="local-nurse",
        role="nurse",
        wards=["WARD-A"],
    )

    result = resolve_user_identity(token, Mock())

    assert result["sub"] == "local-nurse"
    assert result["role"] == "nurse"


@patch("app.dependencies.auth.get_active_user_access")
def test_observation_identity_uses_database(mock_access):
    from fastapi.security import HTTPAuthorizationCredentials

    mock_access.return_value = make_account(
        wards=["WARD-B"]
    )

    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer",
        credentials=make_token(),
    )

    result = get_observation_identity(
        x_gateway_key=None,
        credentials=credentials,
        db=Mock(),
    )

    assert result["type"] == "user"
    assert result["user"]["wards"] == ["WARD-B"]
    mock_access.assert_called_once()


def test_observation_rejects_both_auth_methods():
    from fastapi.security import HTTPAuthorizationCredentials

    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer",
        credentials=make_token(),
    )

    with pytest.raises(HTTPException) as exc:
        get_observation_identity(
            x_gateway_key="dummy-gateway-key",
            credentials=credentials,
            db=Mock(),
        )

    assert exc.value.status_code == 400
