from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.main import app


@pytest.fixture
def client():
    app.dependency_overrides.clear()
    db = MagicMock()

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as test_client:
        yield test_client, db
    app.dependency_overrides.clear()


def set_user(*, role="admin", user_id="admin-1", token_type="access"):
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": user_id,
        "role": role,
        "wards": [],
        "token_type": token_type,
    }


def query_result(rows=()):
    result = MagicMock()
    result.mappings.return_value.all.return_value = rows
    return result


def user_row(
    user_id,
    display_name,
    role,
    ward_id=None,
    ward_name=None,
):
    return {
        "user_id": user_id,
        "display_name": display_name,
        "role": role,
        "ward_id": ward_id,
        "ward_name": ward_name,
    }


def test_admin_lists_users_and_effective_ward_access(client):
    test_client, db = client
    set_user()
    db.execute.return_value = query_result([
        user_row("admin-1", "ผู้ดูแลระบบ", "admin", "WARD-A", "Ward A"),
        user_row("admin-1", "ผู้ดูแลระบบ", "admin", "WARD-B", "Ward B"),
        user_row("nurse-1", "พยาบาลหนึ่ง", "nurse", "WARD-A", "Ward A"),
        user_row("nurse-1", "พยาบาลหนึ่ง", "nurse", "WARD-B", "Ward B"),
        user_row("viewer-1", "Viewer ไม่มี Ward", "viewer"),
    ])

    response = test_client.get("/api/v1/admin/users")

    assert response.status_code == 200
    assert response.headers["content-type"] == (
        "application/json; charset=utf-8"
    )
    assert "พยาบาลหนึ่ง".encode("utf-8") in response.content
    assert response.json() == [
        {
            "user_id": "admin-1",
            "display_name": "ผู้ดูแลระบบ",
            "role": "admin",
            "wards": [
                {"ward_id": "WARD-A", "name": "Ward A"},
                {"ward_id": "WARD-B", "name": "Ward B"},
            ],
        },
        {
            "user_id": "nurse-1",
            "display_name": "พยาบาลหนึ่ง",
            "role": "nurse",
            "wards": [
                {"ward_id": "WARD-A", "name": "Ward A"},
                {"ward_id": "WARD-B", "name": "Ward B"},
            ],
        },
        {
            "user_id": "viewer-1",
            "display_name": "Viewer ไม่มี Ward",
            "role": "viewer",
            "wards": [],
        },
    ]
    assert len(response.json()) == 3
    assert all(
        not {"password", "password_hash", "token", "email"}.intersection(user)
        for user in response.json()
    )
    sql = str(db.execute.call_args.args[0])
    assert "ward_twin.staff_profiles" in sql
    assert "ward_twin.ward_access" in sql
    assert "ward_twin.wards" in sql
    assert "ORDER BY sp.user_id, w.ward_id" in sql
    assert "user_credentials" not in sql


@pytest.mark.parametrize("role", ["nurse", "doctor", "viewer"])
def test_non_admin_cannot_list_users(client, role):
    test_client, db = client
    set_user(role=role, user_id=f"{role}-1")

    response = test_client.get("/api/v1/admin/users")

    assert response.status_code == 403
    db.execute.assert_not_called()


def test_missing_jwt_cannot_list_users(client):
    test_client, db = client

    response = test_client.get("/api/v1/admin/users")

    assert response.status_code == 401
    db.execute.assert_not_called()


def test_admin_gets_user_detail_without_sensitive_data(client):
    test_client, db = client
    set_user()
    db.execute.return_value = query_result([
        user_row("nurse-1", "พยาบาลหนึ่ง", "nurse", "WARD-A", "Ward A"),
        user_row("nurse-1", "พยาบาลหนึ่ง", "nurse", "WARD-B", "Ward B"),
    ])

    response = test_client.get("/api/v1/admin/users/nurse-1")

    assert response.status_code == 200
    assert response.json() == {
        "user_id": "nurse-1",
        "display_name": "พยาบาลหนึ่ง",
        "role": "nurse",
        "wards": [
            {"ward_id": "WARD-A", "name": "Ward A"},
            {"ward_id": "WARD-B", "name": "Ward B"},
        ],
    }
    assert "password_hash" not in response.text
    assert "token" not in response.json()
    assert db.execute.call_args.kwargs == {
        "params": {"user_id": "nurse-1"}
    } or db.execute.call_args.args[1] == {"user_id": "nurse-1"}


def test_admin_get_user_not_found_returns_404(client):
    test_client, db = client
    set_user()
    db.execute.return_value = query_result()

    response = test_client.get("/api/v1/admin/users/missing-user")

    assert response.status_code == 404
    assert response.json() == {"detail": "User not found"}


def test_non_admin_cannot_get_another_users_details(client):
    test_client, db = client
    set_user(role="nurse", user_id="nurse-1")

    response = test_client.get("/api/v1/admin/users/other-user")

    assert response.status_code == 403
    db.execute.assert_not_called()


@pytest.mark.parametrize(
    ("role", "wards", "rows"),
    [
        (
            "nurse",
            ["WARD-A", "WARD-B"],
            [
                user_row("nurse-1", "พยาบาลหนึ่ง", "nurse", "WARD-A", "Ward A"),
                user_row("nurse-1", "พยาบาลหนึ่ง", "nurse", "WARD-B", "Ward B"),
            ],
        ),
        (
            "doctor",
            ["WARD-A"],
            [
                user_row(
                    "doctor-1",
                    "แพทย์หนึ่ง",
                    "doctor",
                    "WARD-A",
                    "Ward A",
                ),
            ],
        ),
        (
            "viewer",
            [],
            [user_row("viewer-1", "ผู้ดูแลแบบอ่าน", "viewer")],
        ),
        (
            "admin",
            ["WARD-A", "WARD-B"],
            [
                user_row("admin-1", "ผู้ดูแลระบบ", "admin", "WARD-A", "Ward A"),
                user_row("admin-1", "ผู้ดูแลระบบ", "admin", "WARD-B", "Ward B"),
            ],
        ),
    ],
)
def test_user_me_returns_authenticated_database_profile(
    client,
    role,
    wards,
    rows,
):
    test_client, db = client
    authenticated_user_id = f"{role}-1"
    set_user(
        role=role,
        user_id=authenticated_user_id,
        token_type="access",
    )
    db.execute.return_value = query_result(rows)

    response = test_client.get("/api/v1/users/me")

    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == authenticated_user_id
    assert body["role"] == role
    assert [ward["ward_id"] for ward in body["wards"]] == wards
    assert response.headers["content-type"] == (
        "application/json; charset=utf-8"
    )
    assert body["display_name"].encode("utf-8") in response.content
    assert db.execute.call_args.args[1] == {
        "user_id": authenticated_user_id
    }


def test_user_me_uses_database_role_not_client_supplied_identity_fields(client):
    test_client, db = client
    set_user(role="viewer", user_id="nurse-1")
    db.execute.return_value = query_result([
        user_row("nurse-1", "พยาบาลหนึ่ง", "nurse", "WARD-A", "Ward A")
    ])

    response = test_client.get(
        "/api/v1/users/me?user_id=attacker&role=admin"
    )

    assert response.status_code == 200
    assert response.json()["user_id"] == "nurse-1"
    assert response.json()["role"] == "nurse"
    assert response.json()["wards"] == [
        {"ward_id": "WARD-A", "name": "Ward A"}
    ]
    assert db.execute.call_args.args[1] == {"user_id": "nurse-1"}


def test_user_me_missing_database_profile_returns_404(client):
    test_client, db = client
    set_user(role="nurse", user_id="nurse-1")
    db.execute.return_value = query_result()

    response = test_client.get("/api/v1/users/me")

    assert response.status_code == 404
    assert response.json() == {"detail": "User not found"}


def test_missing_jwt_cannot_get_user_me(client):
    test_client, db = client

    response = test_client.get("/api/v1/users/me")

    assert response.status_code == 401
    db.execute.assert_not_called()


def test_users_openapi_exposes_only_agreed_routes(client):
    test_client, _ = client

    response = test_client.get("/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/api/v1/admin/users" in paths
    assert "/api/v1/admin/users/{user_id}" in paths
    assert "/api/v1/users/me" in paths
    assert "/api/v1/users" not in paths
    assert "/api/v1/users/{user_id}" not in paths


def test_users_me_is_not_matched_as_user_id_route(client):
    test_client, db = client
    set_user(role="nurse", user_id="nurse-1")
    db.execute.return_value = query_result([
        user_row("nurse-1", "พยาบาลหนึ่ง", "nurse", "WARD-A", "Ward A")
    ])

    response = test_client.get("/api/v1/users/me")

    assert response.status_code == 200
    assert response.json()["user_id"] == "nurse-1"
    assert db.execute.call_args.args[1] == {"user_id": "nurse-1"}
