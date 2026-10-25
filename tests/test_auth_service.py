
from unittest.mock import Mock, patch

import pytest
from argon2 import PasswordHasher
from fastapi import HTTPException

from app.services.auth_service import authenticate_user


hasher = PasswordHasher()
TEST_PASSWORD = "StrongPassword123!"
TEST_HASH = hasher.hash(TEST_PASSWORD)


def make_account(
    approval_status="approved",
    role="nurse",
):
    return {
        "user_id": "test-user-001",
        "email": "nurse@example.com",
        "display_name": "Nurse Example",
        "password_hash": TEST_HASH,
        "approval_status": approval_status,
        "role": role,
    }


@patch("app.services.auth_service.get_user_ward_ids")
@patch("app.services.auth_service.get_login_account")
def test_login_success(mock_account, mock_wards):
    mock_account.return_value = make_account()
    mock_wards.return_value = ["WARD-A"]

    result = authenticate_user(
        Mock(),
        email="nurse@example.com",
        password=TEST_PASSWORD,
    )

    assert result["user_id"] == "test-user-001"
    assert result["role"] == "nurse"
    assert result["wards"] == ["WARD-A"]
    assert "password_hash" not in result

    mock_wards.assert_called_once()


@patch("app.services.auth_service.get_login_account")
def test_login_invalid_email(mock_account):
    mock_account.return_value = None

    with pytest.raises(HTTPException) as exc:
        authenticate_user(
            Mock(),
            email="unknown@example.com",
            password=TEST_PASSWORD,
        )

    assert exc.value.status_code == 401
    assert exc.value.detail == "Invalid email or password"


@patch("app.services.auth_service.get_login_account")
def test_login_wrong_password(mock_account):
    mock_account.return_value = make_account()

    with pytest.raises(HTTPException) as exc:
        authenticate_user(
            Mock(),
            email="nurse@example.com",
            password="WrongPassword123!",
        )

    assert exc.value.status_code == 401


@pytest.mark.parametrize(
    "approval_status",
    ["pending", "rejected", "suspended"],
)
@patch("app.services.auth_service.get_login_account")
def test_login_unapproved_account(
    mock_account,
    approval_status,
):
    mock_account.return_value = make_account(
        approval_status=approval_status,
    )

    with pytest.raises(HTTPException) as exc:
        authenticate_user(
            Mock(),
            email="nurse@example.com",
            password=TEST_PASSWORD,
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "Account is not approved"


@patch("app.services.auth_service.get_login_account")
def test_login_invalid_role(mock_account):
    mock_account.return_value = make_account(
        role="pending",
    )

    with pytest.raises(HTTPException) as exc:
        authenticate_user(
            Mock(),
            email="nurse@example.com",
            password=TEST_PASSWORD,
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "Account role is not authorized"


@patch("app.services.auth_service.get_user_ward_ids")
@patch("app.services.auth_service.get_login_account")
def test_login_multiple_wards(mock_account, mock_wards):
    mock_account.return_value = make_account(
        role="doctor",
    )
    mock_wards.return_value = ["WARD-A", "WARD-B"]

    result = authenticate_user(
        Mock(),
        email="nurse@example.com",
        password=TEST_PASSWORD,
    )

    assert result["role"] == "doctor"
    assert result["wards"] == ["WARD-A", "WARD-B"]
