
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.main import app
from app.core.database import get_db
from app.dependencies.auth import get_current_user


URL = "/api/v1/admin/accounts/USER-001/approve"

PAYLOAD = {
    "role": "nurse",
    "ward_ids": ["WARD-TEST"],
}


@pytest.fixture
def client():
    def override_db():
        yield object()

    app.dependency_overrides[get_db] = override_db

    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


def set_user(role="admin", token_type="access"):
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "ADMIN-001",
        "role": role,
        "wards": ["WARD-TEST"],
        "token_type": token_type,
    }


def test_admin_can_approve_account(client):
    set_user()

    expected = {
        "user_id": "USER-001",
        "role": "nurse",
        "status": "approved",
        "ward_ids": ["WARD-TEST"],
        "reviewed_by": "ADMIN-001",
    }

    with patch(
        "app.routers.admin.approve_account",
        return_value=expected,
    ) as repository:
        response = client.patch(URL, json=PAYLOAD)

    assert response.status_code == 200
    assert response.json() == expected

    repository.assert_called_once()
    assert repository.call_args.kwargs["reviewed_by"] == "ADMIN-001"


@pytest.mark.parametrize(
    "role",
    ["nurse", "doctor", "viewer", "pending"],
)
def test_non_admin_cannot_approve(client, role):
    set_user(role=role)

    with patch(
        "app.routers.admin.approve_account"
    ) as repository:
        response = client.patch(URL, json=PAYLOAD)

    assert response.status_code == 403
    repository.assert_not_called()


def test_legacy_token_cannot_approve(client):
    set_user(role="admin", token_type=None)

    with patch(
        "app.routers.admin.approve_account"
    ) as repository:
        response = client.patch(URL, json=PAYLOAD)

    assert response.status_code == 403
    repository.assert_not_called()


def test_missing_token_cannot_approve(client):
    response = client.patch(URL, json=PAYLOAD)

    assert response.status_code == 401


@pytest.mark.parametrize(
    "payload",
    [
        {"role": "admin", "ward_ids": ["WARD-TEST"]},
        {"role": "nurse", "ward_ids": []},
        {"role": "nurse", "ward_ids": ["WARD-TEST", "WARD-TEST"]},
    ],
)
def test_invalid_payload_rejected(client, payload):
    set_user()

    with patch(
        "app.routers.admin.approve_account"
    ) as repository:
        response = client.patch(URL, json=payload)

    assert response.status_code == 422
    repository.assert_not_called()


def test_account_not_pending_returns_409(client):
    set_user()

    with patch(
        "app.routers.admin.approve_account",
        side_effect=ValueError("Account is not pending"),
    ):
        response = client.patch(URL, json=PAYLOAD)

    assert response.status_code == 409


def test_reviewer_not_authorized_returns_403(client):
    set_user()

    with patch(
        "app.routers.admin.approve_account",
        side_effect=PermissionError(
            "Reviewer is not an approved admin"
        ),
    ):
        response = client.patch(URL, json=PAYLOAD)

    assert response.status_code == 403


def test_database_error_returns_503(client):
    set_user()

    with patch(
        "app.routers.admin.approve_account",
        side_effect=OperationalError(
            "UPDATE account",
            {},
            Exception("Database unavailable"),
        ),
    ):
        response = client.patch(URL, json=PAYLOAD)

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Approval database unavailable"
    )

