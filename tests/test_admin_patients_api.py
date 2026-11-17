from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.main import app


URL = "/api/v1/admin/patients"


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


def set_user(role="admin", wards=()):
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "admin-test",
        "role": role,
        "wards": list(wards),
        "token_type": "access",
    }


def query_result(*, rows=None, scalar=None):
    result = MagicMock()
    result.mappings.return_value.all.return_value = rows or []
    result.scalar_one.return_value = scalar
    return result


def patient_row(
    encounter_id="ENC-TEST-A",
    ward_id="WARD-A",
    *,
    patient_id="PAT-TEST-A",
    prediction_status="ready",
    probability=0.25,
    prediction_id=7,
    unavailable_reason=None,
):
    return {
        "patient_id": patient_id,
        "hn": f"HN-{patient_id}",
        "display_name": f"ผู้ป่วยจำลอง {patient_id}",
        "birth_date": datetime(1970, 1, 1).date(),
        "sex": "female",
        "allergy_status": "unknown",
        "encounter_id": encounter_id,
        "an": f"AN-{encounter_id}",
        "ward_id": ward_id,
        "bed_label": "A-01",
        "admitted_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "dirty_version": 1,
        "processed_version": 1,
        "prediction_id": prediction_id,
        "prediction_status": prediction_status,
        "probability": probability,
        "model_version": "test-model",
        "assessed_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
        "unavailable_reason": unavailable_reason,
    }


def diagnostic(encounter_id="ENC-TEST-A"):
    return {
        "encounter_id": encounter_id,
        "observation_count": 1,
        "heart_rate_sample_count": 0,
        "respiratory_rate_sample_count": 0,
        "spo2_sample_count": 0,
        "systolic_bp_sample_count": 0,
        "diastolic_bp_sample_count": 0,
        "temperature_sample_count": 0,
    }


def alert(
    encounter_id,
    *,
    alert_id=None,
    state="open",
    acknowledged_by="staff",
    assigned_to=None,
    assigned_name=None,
    created_at=None,
):
    return {
        "alert_id": alert_id or f"ALT-{encounter_id}",
        "encounter_id": encounter_id,
        "state": state,
        "created_at": created_at or datetime(
            2026, 1, 1, tzinfo=timezone.utc
        ),
        "acknowledged_by": acknowledged_by,
        "assigned_to": assigned_to,
        "assigned_name": assigned_name,
        "claim_status": "claimed" if assigned_to else "unclaimed",
        "claimed_at": None,
        "has_assessment": True,
    }


def successful_db(
    db,
    *,
    rows=None,
    total=1,
    diagnostics=None,
    alerts=None,
):
    db.execute.side_effect = [
        query_result(scalar=total),
        query_result(rows=rows or []),
        query_result(rows=diagnostics or []),
        query_result(rows=alerts or []),
    ]


def test_admin_without_ward_membership_can_read_multiple_wards(client):
    test_client, db = client
    set_user("admin", wards=())
    successful_db(
        db,
        rows=[
            patient_row(ward_id="WARD-A", encounter_id="ENC-A"),
            patient_row(
                ward_id="WARD-B",
                encounter_id="ENC-B",
                patient_id="PAT-TEST-B",
            ),
        ],
        total=2,
        diagnostics=[diagnostic("ENC-A"), diagnostic("ENC-B")],
        alerts=[alert("ENC-A"), alert("ENC-B")],
    )

    response = test_client.get(URL)

    assert response.status_code == 200
    assert response.headers["content-type"] == (
        "application/json; charset=utf-8"
    )
    assert "ผู้ป่วยจำลอง PAT-TEST-A".encode("utf-8") in response.content
    body = response.json()
    assert body["total"] == 2
    assert {patient["ward_id"] for patient in body["patients"]} == {
        "WARD-A",
        "WARD-B",
    }
    assert len(db.execute.call_args_list) == 4


@pytest.mark.parametrize("role", ["doctor", "nurse", "viewer"])
def test_non_admin_roles_are_forbidden(client, role):
    test_client, db = client
    set_user(role, wards=("WARD-A",))

    response = test_client.get(URL)

    assert response.status_code == 403
    db.execute.assert_not_called()


def test_missing_jwt_is_unauthorized(client):
    test_client, db = client

    response = test_client.get(URL)

    assert response.status_code == 401
    db.execute.assert_not_called()


def test_ward_filter_is_bound_and_applied_to_total_and_page(client):
    test_client, db = client
    set_user()
    successful_db(
        db,
        rows=[patient_row(ward_id="WARD-B", encounter_id="ENC-B")],
        diagnostics=[diagnostic("ENC-B")],
        alerts=[alert("ENC-B")],
    )

    response = test_client.get(URL, params={"ward_id": "WARD-B"})

    assert response.status_code == 200
    assert response.json()["patients"][0]["ward_id"] == "WARD-B"
    count_sql = str(db.execute.call_args_list[0].args[0])
    page_sql = str(db.execute.call_args_list[1].args[0])
    assert "e.ward_id = :ward_id" in count_sql
    assert "e.ward_id = :ward_id" in page_sql
    assert db.execute.call_args_list[0].args[1]["ward_id"] == "WARD-B"
    assert db.execute.call_args_list[1].args[1]["ward_id"] == "WARD-B"


@pytest.mark.parametrize("search", ["ทดสอบ", "HN-PAT-TEST-A", "AN-ENC-TEST-A"])
def test_search_supports_display_name_hn_and_an(client, search):
    test_client, db = client
    set_user()
    successful_db(db, rows=[patient_row()], diagnostics=[diagnostic()])

    response = test_client.get(URL, params={"search": search})

    assert response.status_code == 200
    count_sql = str(db.execute.call_args_list[0].args[0])
    page_sql = str(db.execute.call_args_list[1].args[0])
    for column in ("p.display_name", "p.hn", "e.an"):
        assert column in count_sql
        assert column in page_sql
    assert db.execute.call_args_list[0].args[1]["search"] == search
    assert db.execute.call_args_list[1].args[1]["search"] == search


def test_pagination_applied_in_database_with_total_before_page(client):
    test_client, db = client
    set_user()
    successful_db(
        db,
        rows=[patient_row(encounter_id="ENC-PAGE-2")],
        total=13,
        diagnostics=[diagnostic("ENC-PAGE-2")],
    )

    response = test_client.get(URL, params={"limit": 5, "offset": 5})

    assert response.status_code == 200
    body = response.json()
    assert (body["total"], body["limit"], body["offset"]) == (13, 5, 5)
    page_sql = str(db.execute.call_args_list[1].args[0])
    assert "LIMIT :limit" in page_sql
    assert "OFFSET :offset" in page_sql
    assert "ORDER BY" in page_sql
    assert db.execute.call_args_list[1].args[1]["limit"] == 5
    assert db.execute.call_args_list[1].args[1]["offset"] == 5


def test_empty_result_does_not_query_page_diagnostics_or_alerts(client):
    test_client, db = client
    set_user()
    db.execute.side_effect = [
        query_result(scalar=0),
        query_result(rows=[]),
    ]

    response = test_client.get(URL)

    assert response.status_code == 200
    assert response.json() == {
        "total": 0,
        "limit": 20,
        "offset": 0,
        "patients": [],
    }
    assert db.execute.call_count == 2


def test_workflow_and_prediction_diagnostics_match_shared_mapping(client):
    test_client, db = client
    set_user()
    row = patient_row(
        encounter_id="ENC-DIAG",
        prediction_status="unavailable",
        probability=None,
        unavailable_reason="missing_spo2,insufficient_samples_heart_rate",
    )
    successful_db(
        db,
        rows=[row],
        diagnostics=[diagnostic("ENC-DIAG")],
        alerts=[
            alert("ENC-DIAG", acknowledged_by=None),
        ],
    )

    response = test_client.get(URL)

    assert response.status_code == 200
    patient = response.json()["patients"][0]
    assert patient["workflow_status"] == "pending_acknowledgement"
    assert patient["workflow_status_label"] == "รอรับทราบ"
    assert patient["prediction"]["status"] == "unavailable"
    assert patient["prediction"]["risk_score"] is None
    assert patient["prediction"]["unavailable_reason"] == (
        "missing_spo2,insufficient_samples_heart_rate"
    )
    assert patient["prediction"]["unavailable_message"]
    assert patient["prediction"]["missing_features"]
    assert patient["prediction"]["recommended_action"]


@pytest.mark.parametrize(
    ("alerts", "expected_status", "expected_label"),
    [
        ([], "normal", "ไม่มีรายการรอดำเนินการ"),
        (
            [alert("ENC-DIAG", acknowledged_by=None)],
            "pending_acknowledgement",
            "รอรับทราบ",
        ),
        (
            [{
                **alert("ENC-DIAG"),
                "has_assessment": False,
            }],
            "pending_assessment",
            "รอประเมินผู้ป่วย",
        ),
        ([alert("ENC-DIAG")], "assessed", "ประเมินผู้ป่วยแล้ว"),
    ],
)
def test_admin_patients_workflow_statuses(
    client,
    alerts,
    expected_status,
    expected_label,
):
    test_client, db = client
    set_user()
    successful_db(
        db,
        rows=[patient_row(encounter_id="ENC-DIAG")],
        diagnostics=[diagnostic("ENC-DIAG")],
        alerts=alerts,
    )

    response = test_client.get(URL)

    assert response.status_code == 200
    patient = response.json()["patients"][0]
    assert patient["workflow_status"] == expected_status
    assert patient["workflow_status_label"] == expected_label


def test_response_does_not_expose_extra_database_fields(client):
    test_client, db = client
    set_user()
    successful_db(
        db,
        rows=[patient_row()],
        diagnostics=[diagnostic()],
    )

    response = test_client.get(URL)

    assert response.status_code == 200
    patient = response.json()["patients"][0]
    assert set(patient) == {
        "patient_id",
        "hn",
        "display_name",
        "sex",
        "allergy_status",
        "encounter_id",
        "an",
        "ward_id",
        "bed_label",
        "admitted_at",
        "workflow_status",
        "workflow_status_label",
        "assigned_to",
        "assigned_name",
        "prediction",
    }
    assert "birth_date" not in patient
    assert "dirty_version" not in patient
    assert "processed_version" not in patient
    assert "unavailable_message" in patient["prediction"]
    assert "missing_features" in patient["prediction"]


@pytest.mark.parametrize(
    "alerts",
    [
        [],
        [
            alert(
                "ENC-TEST-A",
                state="closed",
                assigned_to="nurse-closed",
                assigned_name="Closed Alert Nurse",
            ),
        ],
        [
            alert(
                "ENC-TEST-A",
                assigned_to=None,
                assigned_name="Name without assignee",
            ),
        ],
    ],
)
def test_admin_patients_without_assigned_open_alert_returns_nulls(
    client,
    alerts,
):
    test_client, db = client
    set_user()
    successful_db(
        db,
        rows=[patient_row()],
        diagnostics=[diagnostic()],
        alerts=alerts,
    )

    response = test_client.get(URL)

    assert response.status_code == 200
    patient = response.json()["patients"][0]
    assert patient["assigned_to"] is None
    assert patient["assigned_name"] is None


def test_admin_patients_returns_alert_assignee_and_staff_display_name(client):
    test_client, db = client
    set_user()
    successful_db(
        db,
        rows=[patient_row()],
        diagnostics=[diagnostic()],
        alerts=[
            alert(
                "ENC-TEST-A",
                assigned_to="nurse-001",
                assigned_name="พยาบาลทดสอบ",
            ),
        ],
    )

    response = test_client.get(URL)

    assert response.status_code == 200
    patient = response.json()["patients"][0]
    assert patient["assigned_to"] == "nurse-001"
    assert patient["assigned_name"] == "พยาบาลทดสอบ"


def test_admin_patients_latest_open_alert_matches_ward_order_without_duplicates(
    client,
):
    test_client, db = client
    set_user()
    newest_open = alert(
        "ENC-TEST-A",
        alert_id="ALT-NEW",
        assigned_to="nurse-new",
        assigned_name="Nurse New",
        created_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    older_open = alert(
        "ENC-TEST-A",
        alert_id="ALT-OLD",
        assigned_to="nurse-old",
        assigned_name="Nurse Old",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    successful_db(
        db,
        rows=[patient_row()],
        diagnostics=[diagnostic()],
        alerts=[newest_open, older_open],
    )

    response = test_client.get(URL)

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert len(body["patients"]) == 1
    patient = body["patients"][0]
    assert patient["assigned_to"] == "nurse-new"
    assert patient["assigned_name"] == "Nurse New"
    alert_query = str(db.execute.call_args_list[3].args[0])
    assert "(a.state = 'open') DESC" in alert_query
    assert "a.created_at DESC NULLS LAST" in alert_query
    assert "a.alert_id DESC" in alert_query


def test_admin_patients_keeps_assignee_when_staff_profile_name_is_missing(client):
    test_client, db = client
    set_user()
    successful_db(
        db,
        rows=[patient_row()],
        diagnostics=[diagnostic()],
        alerts=[
            alert(
                "ENC-TEST-A",
                assigned_to="nurse-no-profile",
                assigned_name=None,
            ),
        ],
    )

    response = test_client.get(URL)

    assert response.status_code == 200
    patient = response.json()["patients"][0]
    assert patient["assigned_to"] == "nurse-no-profile"
    assert patient["assigned_name"] is None


def test_invalid_pagination_is_rejected(client):
    test_client, db = client
    set_user()

    response = test_client.get(URL, params={"limit": 101})

    assert response.status_code == 422
    db.execute.assert_not_called()


def test_admin_without_membership_still_cannot_read_other_ward_on_old_endpoint(
    client,
):
    test_client, db = client
    set_user("admin", wards=())

    response = test_client.get("/api/v1/wards/WARD-B/patients")

    assert response.status_code == 403
    db.execute.assert_not_called()


def test_admin_patients_get_is_read_only(client):
    test_client, db = client
    set_user()
    successful_db(
        db,
        rows=[patient_row()],
        diagnostics=[diagnostic()],
        alerts=[alert("ENC-TEST-A")],
    )

    response = test_client.get(URL)

    assert response.status_code == 200
    db.commit.assert_not_called()
    db.flush.assert_not_called()
    db.add.assert_not_called()
    db.delete.assert_not_called()
