from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.main import app


WARD_ID = "WARD-TEST"
ENCOUNTER_ID = "ENC-TEST-001"


@pytest.fixture
def client():
    app.dependency_overrides.clear()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def override_db(db):
    def dependency():
        yield db

    app.dependency_overrides[get_db] = dependency


def override_user(role="nurse", wards=(WARD_ID,)):
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "nurse-test",
        "role": role,
        "wards": list(wards),
        "token_type": "access",
    }


def query_result(*, rows=None, scalar=None):
    result = MagicMock()
    result.mappings.return_value.all.return_value = rows or []
    result.scalar_one.return_value = scalar
    return result


def patient_row():
    return {
        "patient_id": "PAT-TEST-001",
        "hn": "HN-TEST-001",
        "display_name": "Synthetic Patient",
        "sex": "unknown",
        "allergy_status": "unknown",
        "encounter_id": ENCOUNTER_ID,
        "an": "AN-TEST-001",
        "bed_label": "T-01",
        "admitted_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "dirty_version": None,
        "processed_version": None,
        "prediction_id": None,
        "prediction_status": None,
        "probability": None,
        "model_version": None,
        "assessed_at": None,
        "unavailable_reason": None,
    }


def alert_row(
    alert_id,
    *,
    state="open",
    acknowledged_by="nurse-test",
    has_assessment=True,
    encounter_id=ENCOUNTER_ID,
):
    return {
        "alert_id": alert_id,
        "encounter_id": encounter_id,
        "state": state,
        "acknowledged_by": acknowledged_by,
        "has_assessment": has_assessment,
    }


def request_patients(client, alerts, row=None):
    db = MagicMock()
    db.execute.side_effect = [
        query_result(scalar=True),
        query_result(rows=[row or patient_row()]),
        query_result(rows=alerts),
    ]
    override_db(db)
    override_user()

    response = client.get(f"/api/v1/wards/{WARD_ID}/patients")
    return response, db


@pytest.mark.parametrize(
    ("alerts", "expected_status", "expected_label"),
    [
        ([], "normal", "ไม่มีรายการรอดำเนินการ"),
        (
            [alert_row("ALT-OPEN", acknowledged_by=None)],
            "pending_acknowledgement",
            "รอรับทราบ",
        ),
        (
            [alert_row("ALT-ASSESSMENT", has_assessment=False)],
            "pending_assessment",
            "รอประเมินผู้ป่วย",
        ),
        (
            [alert_row("ALT-ASSESSED")],
            "assessed",
            "ประเมินผู้ป่วยแล้ว",
        ),
        (
            [
                alert_row("ALT-ACKNOWLEDGED"),
                alert_row("ALT-UNACKNOWLEDGED", acknowledged_by=None),
            ],
            "pending_acknowledgement",
            "รอรับทราบ",
        ),
        (
            [
                alert_row("ALT-ASSESSED"),
                alert_row("ALT-MISSING-ASSESSMENT", has_assessment=False),
            ],
            "pending_assessment",
            "รอประเมินผู้ป่วย",
        ),
        (
            [alert_row("ALT-NOT-ASSESSED", has_assessment=False)],
            "pending_assessment",
            "รอประเมินผู้ป่วย",
        ),
        (
            [alert_row("ALT-CLOSED", state="closed", has_assessment=False)],
            "normal",
            "ไม่มีรายการรอดำเนินการ",
        ),
        (
            [alert_row("ALT-OLD-OPEN", acknowledged_by=None)],
            "pending_acknowledgement",
            "รอรับทราบ",
        ),
    ],
)
def test_ward_patients_workflow_status(
    client,
    alerts,
    expected_status,
    expected_label,
):
    response, _ = request_patients(client, alerts)

    assert response.status_code == 200
    patient = response.json()["patients"][0]
    assert patient["workflow_status"] == expected_status
    assert patient["workflow_status_label"] == expected_label


def test_ward_patients_response_is_valid_utf8_with_thai_text(client):
    thai_name = "ผู้ป่วยทดสอบภาษาไทย"
    row = {**patient_row(), "display_name": thai_name}

    response, _ = request_patients(client, [], row=row)

    decoded_body = response.content.decode("utf-8")
    content_type = response.headers["content-type"].lower()

    assert response.status_code == 200
    assert content_type.split(";", maxsplit=1)[0] == "application/json"
    assert "charset=" not in content_type or "charset=utf-8" in content_type
    assert thai_name in decoded_body
    assert thai_name.encode("utf-8") in response.content
    assert "à¸" not in decoded_body
    assert response.json()["patients"][0]["display_name"] == thai_name


def test_assessment_for_another_alert_does_not_count(client):
    response, db = request_patients(
        client,
        [alert_row("ALT-CURRENT", has_assessment=False)],
    )

    assert response.status_code == 200
    assert response.json()["patients"][0]["workflow_status"] == "pending_assessment"

    workflow_query = str(db.execute.call_args_list[2].args[0])
    assert "assessment.alert_id = a.alert_id" in workflow_query
    assert "assessment.encounter_id = a.encounter_id" in workflow_query
    assert "e.ward_id = :ward_id" in workflow_query


def test_ward_patients_preserve_existing_response_and_get_is_read_only(client):
    response, db = request_patients(client, [])

    assert response.status_code == 200
    body = response.json()
    patient = body["patients"][0]
    assert body["ward_id"] == WARD_ID
    assert body["total"] == 1
    assert patient == {
        "patient_id": "PAT-TEST-001",
        "hn": "HN-TEST-001",
        "display_name": "Synthetic Patient",
        "sex": "unknown",
        "allergy_status": "unknown",
        "encounter_id": ENCOUNTER_ID,
        "an": "AN-TEST-001",
        "bed_label": "T-01",
        "admitted_at": "2026-01-01T00:00:00+00:00",
        "workflow_status": "normal",
        "workflow_status_label": "ไม่มีรายการรอดำเนินการ",
        "prediction": {
            "status": "not_available",
            "risk_score": None,
            "prediction_id": None,
            "model_version": None,
            "assessed_at": None,
            "unavailable_reason": None,
        },
    }
    db.commit.assert_not_called()
    db.flush.assert_not_called()
    db.add.assert_not_called()
    db.delete.assert_not_called()


def test_ward_patients_requires_jwt(client):
    db = MagicMock()
    override_db(db)

    response = client.get(f"/api/v1/wards/{WARD_ID}/patients")

    assert response.status_code == 401
    db.execute.assert_not_called()


def test_ward_patients_requires_ward_membership(client):
    db = MagicMock()
    override_db(db)
    override_user(wards=())

    response = client.get(f"/api/v1/wards/{WARD_ID}/patients")

    assert response.status_code == 403
    db.execute.assert_not_called()


def test_ward_patients_requires_allowed_role(client):
    db = MagicMock()
    override_db(db)
    override_user(role="gateway")

    response = client.get(f"/api/v1/wards/{WARD_ID}/patients")

    assert response.status_code == 403
    db.execute.assert_not_called()
