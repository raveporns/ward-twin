from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.main import app
from app.ml.model_loader import load_model_metadata


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
        "birth_date": None,
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


def diagnostic_row(*, observation_count=0, **sample_counts):
    row = {
        "encounter_id": ENCOUNTER_ID,
        "observation_count": observation_count,
        **{
            f"{metric}_sample_count": 0
            for metric in (
                "heart_rate",
                "respiratory_rate",
                "spo2",
                "systolic_bp",
                "diastolic_bp",
                "temperature",
            )
        },
    }
    row.update(sample_counts)
    return row


def request_patients(client, alerts, row=None, diagnostics=None):
    db = MagicMock()
    db.execute.side_effect = [
        query_result(scalar=True),
        query_result(rows=[row or patient_row()]),
        query_result(rows=[diagnostics or diagnostic_row()]),
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


def test_no_observations_has_model_features_missing_diagnostics(client):
    row = {
        **patient_row(),
        "birth_date": None,
        "sex": None,
    }

    response, _ = request_patients(client, [], row=row)

    prediction = response.json()["patients"][0]["prediction"]
    assert prediction["status"] == "not_available"
    assert prediction["unavailable_reason"] == "no_observations"
    assert prediction["unavailable_message"] == (
        "ยังไม่มีข้อมูลสัญญาณชีพสำหรับการทำนาย"
    )
    assert prediction["missing_features"] == load_model_metadata()[
        "feature_columns"
    ]


def test_observations_without_prediction_record_are_not_assumed_pending(client):
    response, _ = request_patients(
        client,
        [],
        diagnostics=diagnostic_row(
            observation_count=3,
            heart_rate_sample_count=2,
        ),
    )

    prediction = response.json()["patients"][0]["prediction"]
    assert prediction["status"] == "not_available"
    assert prediction["unavailable_reason"] == "no_prediction_record"
    assert prediction["unavailable_message"] == (
        "ยังไม่มีผลการประมวลผลจากโมเดล"
    )
    assert prediction["recommended_action"] == (
        "ตรวจสอบสถานะการประมวลผลของระบบ"
    )


def test_unavailable_missing_single_feature_group(client):
    row = {
        **patient_row(),
        "birth_date": datetime(1980, 1, 1).date(),
        "sex": "female",
        "prediction_id": 1,
        "prediction_status": "unavailable",
        "unavailable_reason": "missing_spo2",
    }

    response, _ = request_patients(
        client,
        [],
        row=row,
        diagnostics=diagnostic_row(observation_count=2),
    )

    prediction = response.json()["patients"][0]["prediction"]
    assert prediction["unavailable_reason"] == "missing_spo2"
    assert prediction["missing_features"] == [
        feature
        for feature in load_model_metadata()["feature_columns"]
        if feature.startswith("spo2_")
    ]


def test_unavailable_multiple_missing_features(client):
    row = {
        **patient_row(),
        "birth_date": datetime(1980, 1, 1).date(),
        "sex": "female",
        "prediction_id": 1,
        "prediction_status": "unavailable",
        "unavailable_reason": "missing_heart_rate,missing_temperature",
    }

    response, _ = request_patients(
        client,
        [],
        row=row,
        diagnostics=diagnostic_row(observation_count=4),
    )

    prediction = response.json()["patients"][0]["prediction"]
    expected = [
        feature
        for feature in load_model_metadata()["feature_columns"]
        if feature.startswith(("heart_rate_", "temperature_"))
    ]
    assert prediction["missing_features"] == expected
    assert prediction["unavailable_reason"] == (
        "missing_heart_rate,missing_temperature"
    )
    assert "อัตราการเต้นหัวใจ" in prediction["unavailable_message"]
    assert "อุณหภูมิ" in prediction["unavailable_message"]


def test_insufficient_samples_are_not_missing_features(client):
    row = {
        **patient_row(),
        "birth_date": datetime(1980, 1, 1).date(),
        "sex": "female",
        "prediction_id": 1,
        "prediction_status": "unavailable",
        "unavailable_reason": "insufficient_samples_heart_rate",
    }

    response, _ = request_patients(
        client,
        [],
        row=row,
        diagnostics=diagnostic_row(
            observation_count=1,
            heart_rate_sample_count=1,
        ),
    )

    prediction = response.json()["patients"][0]["prediction"]
    assert prediction["unavailable_reason"] == (
        "insufficient_samples_heart_rate"
    )
    assert prediction["missing_features"] == []
    assert "จำนวนตัวอย่างไม่เพียงพอ" in prediction["unavailable_message"]


def test_ready_prediction_keeps_score_and_has_no_unavailability_diagnostics(client):
    row = {
        **patient_row(),
        "prediction_id": 99,
        "prediction_status": "ready",
        "probability": 0.73,
        "model_version": "test-model",
        "assessed_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "unavailable_reason": None,
    }

    response, _ = request_patients(client, [], row=row)

    prediction = response.json()["patients"][0]["prediction"]
    assert prediction["status"] == "ready"
    assert prediction["risk_score"] == 0.73
    assert prediction["unavailable_reason"] is None
    assert prediction["unavailable_message"] is None
    assert prediction["missing_features"] == []
    assert prediction["recommended_action"] is None


def test_failed_prediction_does_not_expose_internal_error(client):
    row = {
        **patient_row(),
        "prediction_id": 100,
        "prediction_status": "failed",
        "unavailable_reason": "Traceback: private database details",
    }

    response, _ = request_patients(client, [], row=row)

    prediction = response.json()["patients"][0]["prediction"]
    assert prediction["status"] == "failed"
    assert prediction["risk_score"] is None
    assert prediction["unavailable_reason"] == "processing_failed"
    assert "Traceback" not in str(prediction)
    assert "private database details" not in str(prediction)


def test_pending_prediction_has_confirmed_processing_message(client):
    row = {
        **patient_row(),
        "dirty_version": 2,
        "processed_version": 1,
    }

    response, _ = request_patients(
        client,
        [],
        row=row,
        diagnostics=diagnostic_row(observation_count=1),
    )

    prediction = response.json()["patients"][0]["prediction"]
    assert prediction["status"] == "pending"
    assert prediction["unavailable_reason"] is None
    assert prediction["unavailable_message"] == (
        "มีข้อมูลใหม่รอการประมวลผล"
    )


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

    workflow_query = str(db.execute.call_args_list[3].args[0])
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
            "unavailable_reason": "no_observations",
            "unavailable_message": "ยังไม่มีข้อมูลสัญญาณชีพสำหรับการทำนาย",
            "missing_features": list(load_model_metadata()["feature_columns"]),
            "recommended_action": (
                "บันทึกข้อมูลสัญญาณชีพที่จำเป็น แล้วรอระบบประมวลผล"
            ),
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
