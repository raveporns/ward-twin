
from unittest.mock import MagicMock
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.database import get_db
from app.core.security import create_local_token
from app.dependencies.auth import get_current_user


client = TestClient(app)
BASE = "/api/v1"
ALERT_ID = "ALT-TEST-0009"


def headers(wards=None, role="nurse"):
    token = create_local_token(
        "nurseA1",
        role,
        wards if wards is not None else ["WARD-A"],
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def mock_db():
    db = MagicMock()

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    yield db
    app.dependency_overrides.pop(get_db, None)


def query_result(*, one=None, all_rows=None, scalar=None):
    result = MagicMock()
    result.mappings.return_value.one_or_none.return_value = one
    result.mappings.return_value.one.return_value = one
    result.mappings.return_value.all.return_value = all_rows or []
    result.scalar_one_or_none.return_value = scalar
    return result


def test_get_alerts_requires_auth(mock_db):
    response = client.get(
        f"{BASE}/encounters/ENC-MOCK-A01/alerts"
    )
    assert response.status_code == 401
    mock_db.execute.assert_not_called()


def test_get_alerts_wrong_ward(mock_db):
    mock_db.execute.return_value = query_result(
        one={"ward_id": "WARD-A"}
    )

    response = client.get(
        f"{BASE}/encounters/ENC-MOCK-A01/alerts",
        headers=headers(["WARD-B"]),
    )

    assert response.status_code == 403


def test_get_alerts_success(mock_db):
    mock_db.execute.side_effect = [
        query_result(one={"ward_id": "WARD-A"}),
        query_result(all_rows=[{
            "alert_id": ALERT_ID,
            "encounter_id": "ENC-MOCK-A01",
            "state": "open",
        }]),
    ]

    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "viewerA1",
        "role": "viewer",
        "wards": ["WARD-A"],
        "token_type": "access",
    }

    try:
        response = client.get(
            f"{BASE}/encounters/ENC-MOCK-A01/alerts",
        )

        assert response.status_code == 200
        assert response.json()["alerts"][0]["alert_id"] == ALERT_ID
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_acknowledge_success(mock_db):
    alert = {
        "alert_id": ALERT_ID,
        "encounter_id": "ENC-MOCK-A01",
        "ward_id": "WARD-A",
        "state": "open",
        "assigned_to": None,
        "acknowledged_by": None,
    }

    updated = {
        "alert_id": ALERT_ID,
        "encounter_id": "ENC-MOCK-A01",
        "state": "open",
        "assigned_to": "nurseA1",
        "acknowledged_by": "nurseA1",
        "acknowledged_at": datetime.now(timezone.utc),
    }

    mock_db.execute.side_effect = [
        query_result(one=alert),
        query_result(scalar="nurseA1"),
        query_result(scalar="nurseA1"),
        query_result(one=updated),
    ]

    response = client.put(
        f"{BASE}/alerts/{ALERT_ID}/acknowledgement",
        headers=headers(),
        json={"assigned_to": "nurseA1"},
    )

    assert response.status_code == 200
    assert response.json()["acknowledged_by"] == "nurseA1"
    mock_db.commit.assert_called_once()


def test_acknowledge_duplicate(mock_db):
    mock_db.execute.return_value = query_result(
        one={
            "alert_id": ALERT_ID,
            "encounter_id": "ENC-MOCK-A01",
            "ward_id": "WARD-A",
            "state": "open",
            "assigned_to": "nurseA1",
            "acknowledged_by": "nurseA1",
        }
    )

    response = client.put(
        f"{BASE}/alerts/{ALERT_ID}/acknowledgement",
        headers=headers(),
        json={},
    )

    assert response.status_code == 409
    mock_db.commit.assert_not_called()
    mock_db.rollback.assert_called_once()


def test_viewer_cannot_acknowledge_alert(mock_db):
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "viewerA1",
        "role": "viewer",
        "wards": ["WARD-A"],
        "token_type": "access",
    }

    try:
        response = client.put(
            f"{BASE}/alerts/{ALERT_ID}/acknowledgement",
            json={},
        )

        assert response.status_code == 403
        mock_db.execute.assert_not_called()
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_acknowledge_wrong_ward(mock_db):
    mock_db.execute.return_value = query_result(
        one={
            "alert_id": ALERT_ID,
            "encounter_id": "ENC-MOCK-A01",
            "ward_id": "WARD-A",
            "state": "open",
            "assigned_to": None,
            "acknowledged_by": None,
        }
    )

    response = client.put(
        f"{BASE}/alerts/{ALERT_ID}/acknowledgement",
        headers=headers(["WARD-B"]),
        json={},
    )

    assert response.status_code == 403
    mock_db.commit.assert_not_called()
