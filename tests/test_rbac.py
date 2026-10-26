from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.database import get_db
from app.dependencies.auth import get_current_user, get_observation_identity
from app.main import app


@pytest.fixture
def client():
    app.dependency_overrides.clear()

    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


def override_db(db):
    def dependency():
        yield db

    app.dependency_overrides[get_db] = dependency


def override_user(role, wards=("WARD-A",)):
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": f"{role}-001",
        "role": role,
        "wards": list(wards),
        "token_type": "access",
    }


def test_viewer_can_read_assigned_ward_patients(client):
    db = MagicMock()
    ward_exists = MagicMock()
    ward_exists.scalar_one.return_value = True
    patient_rows = MagicMock()
    patient_rows.mappings.return_value.all.return_value = []
    db.execute.side_effect = [ward_exists, patient_rows]
    override_db(db)
    override_user("viewer")

    response = client.get("/api/v1/wards/WARD-A/patients")

    assert response.status_code == 200
    assert response.json() == {
        "ward_id": "WARD-A",
        "total": 0,
        "patients": [],
    }


def test_admin_cannot_read_unassigned_ward(client):
    db = MagicMock()
    override_db(db)
    override_user("admin", wards=("WARD-A",))

    response = client.get("/api/v1/wards/WARD-B/patients")

    assert response.status_code == 403
    db.execute.assert_not_called()


def test_gateway_key_does_not_authenticate_human_api(client):
    override_db(MagicMock())

    response = client.get(
        "/api/v1/wards/WARD-A/patients",
        headers={"X-Gateway-Key": settings.gateway_api_key},
    )

    assert response.status_code == 401


def test_gateway_can_submit_iot_observations(client):
    db = MagicMock()

    device = MagicMock()
    device.mappings.return_value.one_or_none.return_value = {
        "ward_id": "WARD-A",
    }
    assignment = MagicMock()
    assignment.scalars.return_value.all.return_value = ["ENC-A"]
    existing = MagicMock()
    existing.scalar_one_or_none.return_value = None
    inserted = MagicMock()
    inserted.scalar_one_or_none.return_value = 17
    db.execute.side_effect = [
        device,
        assignment,
        existing,
        inserted,
        MagicMock(),
        MagicMock(),
    ]
    override_db(db)

    response = client.post(
        "/api/v1/observations",
        headers={"X-Gateway-Key": settings.gateway_api_key},
        json={
            "source": "iot",
            "readings": [{
                "reading_id": str(uuid4()),
                "device_id": "MON-01",
                "measured_at": datetime.now(timezone.utc).isoformat(),
                "measurements": [{
                    "metric": "heart_rate",
                    "value": 80,
                    "unit": "bpm",
                }],
            }],
        },
    )

    assert response.status_code == 200
    assert response.json()["inserted_readings"] == 1
    assert response.json()["items"][0]["encounter_id"] == "ENC-A"
    db.commit.assert_called_once()


def test_viewer_cannot_write_manual_observations(client):
    db = MagicMock()
    override_db(db)
    app.dependency_overrides[get_observation_identity] = lambda: {
        "type": "user",
        "producer_id": "user:viewer-001",
        "user": {
            "sub": "viewer-001",
            "role": "viewer",
            "wards": ["WARD-A"],
            "token_type": "access",
        },
    }

    response = client.post(
        "/api/v1/observations",
        json={
            "source": "manual",
            "readings": [{
                "reading_id": str(uuid4()),
                "encounter_id": "ENC-A",
                "measured_at": datetime.now(timezone.utc).isoformat(),
                "measurements": [{
                    "metric": "heart_rate",
                    "value": 80,
                    "unit": "bpm",
                }],
            }],
        },
    )

    assert response.status_code == 403
    db.execute.assert_not_called()


def test_nurse_can_download_import_template(client):
    override_user("nurse")

    response = client.get("/api/v1/import-templates/encounters")

    assert response.status_code == 200
    assert "encounter_id" in response.text


def test_doctor_cannot_download_import_template(client):
    override_user("doctor")

    response = client.get("/api/v1/import-templates/encounters")

    assert response.status_code == 403


def test_viewer_cannot_read_import_details(client):
    db = MagicMock()
    override_db(db)
    override_user("viewer")

    response = client.get(
        "/api/v1/imports/206b9dd7-ddef-414c-a570-393417f43bfd"
    )

    assert response.status_code == 403
    db.execute.assert_not_called()


def test_doctor_cannot_commit_import(client):
    db = MagicMock()
    override_db(db)
    override_user("doctor")

    response = client.post(
        "/api/v1/imports/206b9dd7-ddef-414c-a570-393417f43bfd/commit"
    )

    assert response.status_code == 403
    db.execute.assert_not_called()


def test_invalid_bearer_token_is_401(client):
    override_db(MagicMock())

    response = client.get(
        "/api/v1/wards/WARD-A/patients",
        headers={"Authorization": "Bearer invalid.token.value"},
    )

    assert response.status_code == 401
