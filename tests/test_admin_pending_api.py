
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from app.main import app
from app.core.database import get_db
from app.dependencies.auth import get_current_user


URL = "/api/v1/admin/accounts/pending"


@pytest.fixture
def client():
    fake_db = object()

    def override_db():
        yield fake_db

    app.dependency_overrides[get_db] = override_db

    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


def set_user(role):
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "TEST-USER",
        "role": role,
        "wards": ["WARD-TEST"],
        "token_type": "access",
    }


def test_admin_can_view_pending(client):
    set_user("admin")

    with patch(
        "app.routers.admin.list_pending_accounts",
        return_value=[],
    ):
        response = client.get(URL)

    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.parametrize(
    "role",
    ["nurse", "doctor", "viewer"],
)
def test_non_admin_denied(client, role):
    set_user(role)

    with patch(
        "app.routers.admin.list_pending_accounts"
    ) as repository:
        response = client.get(URL)

    assert response.status_code == 403
    repository.assert_not_called()


def test_missing_token_denied(client):
    response = client.get(URL)

    assert response.status_code == 401


def test_database_error_returns_503(client):
    set_user("admin")

    with patch(
        "app.routers.admin.list_pending_accounts",
        side_effect=SQLAlchemyError("DB unavailable"),
    ):
        response = client.get(URL)

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Admin accounts temporarily unavailable"
    )
