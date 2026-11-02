
from unittest.mock import Mock, patch

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.main import app
from app.core.config import settings
from app.core.database import get_db


def fake_get_db():
    yield Mock()


@pytest.fixture
def client():
    app.dependency_overrides[get_db] = fake_get_db

    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def login_payload():
    return {
        "email": "nurse@example.com",
        "password": "StrongPassword123!",
    }


def approved_user():
    return {
        "user_id": "test-user-001",
        "email": "nurse@example.com",
        "display_name": "Nurse Example",
        "role": "nurse",
        "wards": ["WARD-A"],
        "accessible_wards": [
            {"ward_id": "WARD-A", "name": "Ward A"},
        ],
    }


@pytest.mark.parametrize(
    "role",
    ["admin", "doctor", "nurse", "viewer"],
)
def test_login_success(client, role):
    user = approved_user()
    user["role"] = role
    if role == "admin":
        user["accessible_wards"] = [
            {"ward_id": "WARD-A", "name": "Ward A"},
            {"ward_id": "WARD-B", "name": "Ward B"},
        ]

    with patch(
        "app.routers.auth.authenticate_user",
        return_value=user,
    ):
        response = client.post(
            "/api/v1/auth/login",
            json=login_payload(),
        )

    assert response.status_code == 200

    data = response.json()
    assert data["token_type"] == "bearer"
    assert data["expires_in"] == 1800
    assert isinstance(data["access_token"], str)
    assert data["role"] == role
    assert data["wards"] == user["accessible_wards"]

    claims = jwt.decode(
        data["access_token"],
        settings.jwt_secret,
        algorithms=["HS256"],
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
    )

    assert claims["sub"] == "test-user-001"
    assert claims["role"] == role
    assert claims["wards"] == user["wards"]
    assert claims["token_type"] == "access"


@pytest.mark.parametrize(
    "status_code,detail",
    [
        (401, "Invalid email or password"),
        (403, "Account is not approved"),
        (403, "Account role is not authorized"),
    ],
)
def test_login_authentication_denied(
    client,
    status_code,
    detail,
):
    from fastapi import HTTPException

    with patch(
        "app.routers.auth.authenticate_user",
        side_effect=HTTPException(
            status_code=status_code,
            detail=detail,
        ),
    ):
        response = client.post(
            "/api/v1/auth/login",
            json=login_payload(),
        )

    assert response.status_code == status_code
    assert response.json()["detail"] == detail


def test_login_no_ward_access(client):
    user = approved_user()
    user["wards"] = []
    user["accessible_wards"] = []

    with patch(
        "app.routers.auth.authenticate_user",
        return_value=user,
    ):
        response = client.post(
            "/api/v1/auth/login",
            json=login_payload(),
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "No ward access assigned"


def test_admin_login_without_membership_lists_all_wards(client):
    user = approved_user()
    user["role"] = "admin"
    user["wards"] = []
    user["accessible_wards"] = [
        {"ward_id": "WARD-A", "name": "Ward A"},
        {"ward_id": "WARD-B", "name": "Ward B"},
    ]

    with patch(
        "app.routers.auth.authenticate_user",
        return_value=user,
    ):
        response = client.post(
            "/api/v1/auth/login",
            json=login_payload(),
        )

    assert response.status_code == 200
    data = response.json()
    assert data["role"] == "admin"
    assert data["wards"] == user["accessible_wards"]
    claims = jwt.decode(
        data["access_token"],
        settings.jwt_secret,
        algorithms=["HS256"],
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
    )
    assert claims["wards"] == []


def test_login_loads_wards_when_auth_service_omits_enrichment(client):
    user = approved_user()
    user.pop("accessible_wards")
    accessible_wards = [
        {"ward_id": "WARD-A", "name": "Ward A"},
        {"ward_id": "WARD-B", "name": "Ward B"},
    ]

    with (
        patch(
            "app.routers.auth.authenticate_user",
            return_value=user,
        ),
        patch(
            "app.routers.auth.get_user_accessible_wards",
            return_value=accessible_wards,
        ) as load_wards,
    ):
        response = client.post(
            "/api/v1/auth/login",
            json=login_payload(),
        )

    assert response.status_code == 200
    data = response.json()
    assert data["role"] == "nurse"
    assert data["wards"] == accessible_wards
    load_wards.assert_called_once()
    assert load_wards.call_args.kwargs == {
        "user_id": "test-user-001",
        "role": "nurse",
    }


@pytest.mark.parametrize(
    "missing_field",
    ["user_id", "role", "wards"],
)
def test_login_incomplete_authenticated_user_returns_service_error(
    client,
    missing_field,
):
    user = approved_user()
    user.pop(missing_field)

    with patch(
        "app.routers.auth.authenticate_user",
        return_value=user,
    ):
        response = client.post(
            "/api/v1/auth/login",
            json=login_payload(),
        )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Authentication temporarily unavailable"
    )


def test_login_database_unavailable(client):
    with patch(
        "app.routers.auth.authenticate_user",
        side_effect=OperationalError(
            statement="SELECT",
            params={},
            orig=Exception("Database unavailable"),
        ),
    ):
        response = client.post(
            "/api/v1/auth/login",
            json=login_payload(),
        )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Authentication temporarily unavailable"
    )


def test_login_invalid_email(client):
    payload = login_payload()
    payload["email"] = "invalid-email"

    response = client.post(
        "/api/v1/auth/login",
        json=payload,
    )

    assert response.status_code == 422


def test_login_rejects_role_in_request(client):
    payload = login_payload()
    payload["role"] = "admin"

    response = client.post(
        "/api/v1/auth/login",
        json=payload,
    )

    assert response.status_code == 422
