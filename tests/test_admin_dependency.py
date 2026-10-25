
import pytest
from fastapi import HTTPException

from app.dependencies.auth import require_admin


def test_admin_allowed():
    user = {
        "sub": "ADMIN-001",
        "role": "admin",
        "wards": ["WARD-TEST"],
        "token_type": "access",
    }

    result = require_admin(user=user)

    assert result == user


@pytest.mark.parametrize(
    "role",
    ["nurse", "doctor", "viewer", "pending"],
)
def test_non_admin_denied(role):
    user = {
        "sub": "USER-001",
        "role": role,
        "wards": ["WARD-TEST"],
        "token_type": "access",
    }

    with pytest.raises(HTTPException) as exc:
        require_admin(user=user)

    assert exc.value.status_code == 403
    assert exc.value.detail == "Admin access denied"


def test_local_token_denied():
    user = {
        "sub": "LOCAL-ADMIN",
        "role": "admin",
        "wards": ["WARD-TEST"],
    }

    with pytest.raises(HTTPException) as exc:
        require_admin(user=user)

    assert exc.value.status_code == 403


def test_invalid_token_type_denied():
    user = {
        "sub": "ADMIN-001",
        "role": "admin",
        "wards": ["WARD-TEST"],
        "token_type": "refresh",
    }

    with pytest.raises(HTTPException) as exc:
        require_admin(user=user)

    assert exc.value.status_code == 403
