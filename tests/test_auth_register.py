
from unittest.mock import Mock, MagicMock, patch

from fastapi.testclient import TestClient
from psycopg import errors as pg_errors
from sqlalchemy.exc import IntegrityError, OperationalError

from app.main import app
from app.core.database import get_db


def fake_get_db():
    yield Mock()


def test_register_success():
    app.dependency_overrides[get_db] = fake_get_db

    try:
        with patch(
            "app.routers.auth.create_pending_account"
        ) as mock_create:
            mock_create.return_value = {
                "user_id": "test-user-001",
                "email": "nurse@example.com",
                "display_name": "Nurse Example",
                "role": "pending",
                "status": "pending",
            }

            client = TestClient(app)

            response = client.post(
                "/api/v1/auth/register",
                json={
                    "email": "nurse@example.com",
                    "password": "StrongPassword123!",
                    "display_name": "Nurse Example",
                },
            )

            assert response.status_code == 201
            assert response.json()["role"] == "pending"
            assert response.json()["status"] == "pending"
            assert "password" not in response.json()
            assert "password_hash" not in response.json()

            mock_create.assert_called_once()

            args = mock_create.call_args.kwargs
            assert args["email"] == "nurse@example.com"
            assert args["password_hash"].startswith("$argon2id$")

    finally:
        app.dependency_overrides.clear()


def test_register_rejects_invalid_email():
    client = TestClient(app)

    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": "invalid-email",
            "password": "StrongPassword123!",
            "display_name": "Nurse Example",
        },
    )

    assert response.status_code == 422


def test_register_rejects_short_password():
    client = TestClient(app)

    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": "nurse@example.com",
            "password": "123",
            "display_name": "Nurse Example",
        },
    )

    assert response.status_code == 422


def test_register_rejects_role_escalation():
    client = TestClient(app)

    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": "nurse@example.com",
            "password": "StrongPassword123!",
            "display_name": "Nurse Example",
            "role": "admin",
        },
    )

    assert response.status_code == 422


def test_register_duplicate_email():
    app.dependency_overrides[get_db] = fake_get_db

    try:
        with patch(
            "app.routers.auth.create_pending_account"
        ) as mock_create:

            # จำลอง PostgreSQL UniqueViolation
            original_error = MagicMock(
                spec=pg_errors.UniqueViolation
            )

            original_error.diag.constraint_name = (
                "user_credentials_email_uidx"
            )

            mock_create.side_effect = IntegrityError(
                statement="INSERT",
                params={},
                orig=original_error,
            )

            client = TestClient(app)

            response = client.post(
                "/api/v1/auth/register",
                json={
                    "email": "nurse@example.com",
                    "password": "StrongPassword123!",
                    "display_name": "Nurse Example",
                },
            )

            assert response.status_code == 409
            assert response.json()["detail"] == (
                "Email already registered"
            )

    finally:
        app.dependency_overrides.clear()


def test_register_database_error():
    app.dependency_overrides[get_db] = fake_get_db

    try:
        with patch(
            "app.routers.auth.create_pending_account"
        ) as mock_create:
            mock_create.side_effect = OperationalError(
                statement="INSERT",
                params={},
                orig=Exception("Database unavailable"),
            )

            client = TestClient(app)

            response = client.post(
                "/api/v1/auth/register",
                json={
                    "email": "nurse@example.com",
                    "password": "StrongPassword123!",
                    "display_name": "Nurse Example",
                },
            )

            assert response.status_code == 503
            assert response.json()["detail"] == (
                "Registration temporarily unavailable"
            )

    finally:
        app.dependency_overrides.clear()


def test_register_other_integrity_error():
    app.dependency_overrides[get_db] = fake_get_db

    try:
        with patch(
            "app.routers.auth.create_pending_account"
        ) as mock_create:

            mock_create.side_effect = IntegrityError(
                statement="INSERT",
                params={},
                orig=Exception("Foreign key violation"),
            )

            client = TestClient(app)

            response = client.post(
                "/api/v1/auth/register",
                json={
                    "email": "nurse@example.com",
                    "password": "StrongPassword123!",
                    "display_name": "Nurse Example",
                },
            )

            assert response.status_code == 503
            assert response.json()["detail"] == (
                "Registration temporarily unavailable"
            )

    finally:
        app.dependency_overrides.clear()
