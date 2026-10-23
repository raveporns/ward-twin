
from fastapi.testclient import TestClient

from app.main import app
from app.core.security import create_local_token

client = TestClient(app)


def test_missing_token():
    response = client.get(
        "/api/v1/wards/WARD-A/patients"
    )
    assert response.status_code == 401


def test_valid_ward():
    token = create_local_token(
        "nurse-a", "nurse", ["WARD-A"]
    )

    response = client.get(
        "/api/v1/wards/WARD-A/patients",
        headers={
            "Authorization": f"Bearer {token}"
        },
    )
    assert response.status_code == 200


def test_wrong_ward():
    token = create_local_token(
        "nurse-a", "nurse", ["WARD-A"]
    )

    response = client.get(
        "/api/v1/wards/WARD-B/patients",
        headers={
            "Authorization": f"Bearer {token}"
        },
    )
    assert response.status_code == 403


def test_invalid_token():
    response = client.get(
        "/api/v1/wards/WARD-A/patients",
        headers={
            "Authorization": "Bearer invalid-token"
        },
    )
    assert response.status_code == 401
