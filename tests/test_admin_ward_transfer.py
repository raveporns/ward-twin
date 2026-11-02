from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.main import app
from app.routers.admin import get_transfer_db


TRANSFER_URL = "/api/v1/admin/encounters/ENC-TEST/ward"


@pytest.fixture
def client():
    app.dependency_overrides.clear()
    db = MagicMock()

    @contextmanager
    def transaction():
        try:
            yield db
            db.test_committed = True
        except Exception:
            db.test_rolled_back = True
            raise

    db.begin.side_effect = transaction

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_transfer_db] = override_db
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


def result(*, mapping=None, scalar=None, rows=None):
    query = MagicMock()
    query.mappings.return_value.one_or_none.return_value = mapping
    query.mappings.return_value.all.return_value = rows or []
    query.scalar_one_or_none.return_value = scalar
    query.scalar_one.return_value = scalar
    return query


def encounter(
    *,
    ward_id="WARD-A",
    bed_label="A-01",
    discharged_at=None,
):
    return {
        "encounter_id": "ENC-TEST",
        "ward_id": ward_id,
        "bed_label": bed_label,
        "discharged_at": discharged_at,
    }


def successful_transfer_db(db, *, source=None, transferred_at=None):
    db.execute.side_effect = [
        result(mapping=source or encounter()),
        result(scalar="WARD-B"),
        result(mapping={"encounter_id": "ENC-TEST"}),
        result(scalar=transferred_at or datetime.now(timezone.utc)),
    ]


def payload(**updates):
    return {
        "ward_id": "WARD-B",
        "bed_label": "B-03",
        "reason": "ย้ายผู้ป่วยไปยัง Ward B",
        **updates,
    }


def test_admin_transfers_encounter_and_records_history(client):
    test_client, db = client
    set_user("admin")
    transfer_time = datetime(2026, 11, 3, tzinfo=timezone.utc)
    successful_transfer_db(db, transferred_at=transfer_time)

    response = test_client.patch(TRANSFER_URL, json=payload())

    assert response.status_code == 200
    assert response.json() == {
        "encounter_id": "ENC-TEST",
        "previous_ward_id": "WARD-A",
        "new_ward_id": "WARD-B",
        "previous_bed_label": "A-01",
        "new_bed_label": "B-03",
        "transferred": True,
        "transferred_at": "2026-11-03T00:00:00Z",
    }
    db.begin.assert_called_once()
    assert db.test_committed
    update_sql = str(db.execute.call_args_list[2].args[0])
    history_sql = str(db.execute.call_args_list[3].args[0])
    assert "UPDATE ward_twin.encounters" in update_sql
    assert "SET ward_id = :ward_id" in update_sql
    assert "bed_label = :bed_label" in update_sql
    assert "INSERT INTO ward_twin.ward_transfer_history" in history_sql
    history_params = db.execute.call_args_list[3].args[1]
    assert history_params["reason"] == "ย้ายผู้ป่วยไปยัง Ward B"
    assert history_params["transferred_by"] == "admin-test"


def test_admin_without_ward_membership_can_transfer(client):
    test_client, db = client
    set_user("admin", wards=())
    successful_transfer_db(db)

    response = test_client.patch(TRANSFER_URL, json=payload())

    assert response.status_code == 200
    assert response.json()["transferred"] is True


@pytest.mark.parametrize("role", ["doctor", "nurse", "viewer"])
def test_non_admin_cannot_transfer(client, role):
    test_client, db = client
    set_user(role, wards=("WARD-A", "WARD-B"))

    response = test_client.patch(TRANSFER_URL, json=payload())

    assert response.status_code == 403
    db.execute.assert_not_called()


def test_missing_jwt_cannot_transfer(client):
    test_client, db = client

    response = test_client.patch(TRANSFER_URL, json=payload())

    assert response.status_code == 401
    db.execute.assert_not_called()


def test_encounter_not_found(client):
    test_client, db = client
    set_user()
    db.execute.return_value = result()

    response = test_client.patch(TRANSFER_URL, json=payload())

    assert response.status_code == 404
    assert db.test_rolled_back
    assert db.execute.call_count == 1


def test_target_ward_not_found(client):
    test_client, db = client
    set_user()
    db.execute.side_effect = [
        result(mapping=encounter()),
        result(scalar=None),
    ]

    response = test_client.patch(TRANSFER_URL, json=payload())

    assert response.status_code == 404
    assert db.test_rolled_back
    assert db.execute.call_count == 2


def test_bed_conflict_returns_409(client):
    test_client, db = client
    set_user()
    conflict = IntegrityError(
        "UPDATE ward_twin.encounters",
        {},
        SimpleNamespace(
            diag=SimpleNamespace(
                constraint_name="encounters_active_bed_uq"
            )
        ),
    )
    db.execute.side_effect = [
        result(mapping=encounter()),
        result(scalar="WARD-B"),
        conflict,
    ]

    response = test_client.patch(TRANSFER_URL, json=payload())

    assert response.status_code == 409
    assert response.json()["detail"] == "Target bed is already assigned"
    assert db.test_rolled_back
    assert db.execute.call_count == 3


def test_locked_encounter_prevents_stale_concurrent_transfer(client):
    test_client, db = client
    set_user()
    db.execute.side_effect = [
        result(mapping=encounter()),
        result(scalar="WARD-B"),
        result(mapping=None),
    ]

    response = test_client.patch(TRANSFER_URL, json=payload())

    assert response.status_code == 409
    assert response.json()["detail"] == "Encounter changed during transfer"
    lock_query = str(db.execute.call_args_list[0].args[0])
    assert "FOR UPDATE" in lock_query
    assert "discharged_at IS NULL" in str(
        db.execute.call_args_list[2].args[0]
    )
    assert db.execute.call_count == 3
    assert db.test_rolled_back


def test_discharged_encounter_is_rejected(client):
    test_client, db = client
    set_user()
    db.execute.return_value = result(
        mapping=encounter(discharged_at=datetime.now(timezone.utc))
    )

    response = test_client.patch(TRANSFER_URL, json=payload())

    assert response.status_code == 409
    assert db.execute.call_count == 1
    assert db.test_rolled_back


def test_same_ward_and_bed_is_noop_without_history(client):
    test_client, db = client
    set_user()
    db.execute.side_effect = [
        result(mapping=encounter(ward_id="WARD-B", bed_label="B-03")),
        result(scalar="WARD-B"),
    ]

    response = test_client.patch(TRANSFER_URL, json=payload())

    assert response.status_code == 200
    assert response.json()["transferred"] is False
    assert response.json()["transferred_at"] is None
    assert db.execute.call_count == 2
    assert db.test_committed


def test_invalid_blank_bed_or_reason_is_rejected(client):
    test_client, db = client
    set_user()

    response = test_client.patch(
        TRANSFER_URL,
        json=payload(bed_label="   "),
    )

    assert response.status_code == 422
    db.execute.assert_not_called()


def test_transfer_history_failure_rolls_back_encounter_update(client):
    test_client, db = client
    set_user()
    db.execute.side_effect = [
        result(mapping=encounter()),
        result(scalar="WARD-B"),
        result(mapping={"encounter_id": "ENC-TEST"}),
        IntegrityError(
            "INSERT ward_transfer_history",
            {},
            Exception("history constraint"),
        ),
    ]

    response = test_client.patch(TRANSFER_URL, json=payload())

    assert response.status_code == 503
    assert db.test_rolled_back
    assert db.test_committed is not True


def test_transfer_preserves_encounter_identity_and_related_clinical_data(client):
    test_client, db = client
    set_user()
    successful_transfer_db(db)

    response = test_client.patch(TRANSFER_URL, json=payload())

    assert response.status_code == 200
    update_sql = str(db.execute.call_args_list[2].args[0])
    assert "WHERE encounter_id = :encounter_id" in update_sql
    assert "patient_id" not in update_sql
    assert "DELETE" not in update_sql.upper()
    assert "observation" not in update_sql.lower()
    assert "risk_predictions" not in update_sql
    assert "alerts" not in update_sql
    assert "assessments" not in update_sql


def test_transfer_is_reflected_by_admin_and_ward_patient_lists(client):
    test_client, db = client
    set_user("admin")
    successful_transfer_db(db)

    transfer_response = test_client.patch(TRANSFER_URL, json=payload())

    assert transfer_response.status_code == 200
    moved_patient = {
        "patient_id": "PAT-TEST",
        "hn": "HN-TEST",
        "display_name": "ผู้ป่วยทดสอบ",
        "birth_date": datetime(1970, 1, 1).date(),
        "sex": "female",
        "allergy_status": "unknown",
        "encounter_id": "ENC-TEST",
        "an": "AN-TEST",
        "ward_id": "WARD-B",
        "bed_label": "B-03",
        "admitted_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "dirty_version": 1,
        "processed_version": 1,
        "prediction_id": None,
        "prediction_status": None,
        "probability": None,
        "model_version": None,
        "assessed_at": None,
        "unavailable_reason": None,
    }
    db.execute.side_effect = [
        result(scalar=1),
        result(rows=[moved_patient]),
        result(rows=[{
            "encounter_id": "ENC-TEST",
            "observation_count": 0,
            "heart_rate_sample_count": 0,
            "respiratory_rate_sample_count": 0,
            "spo2_sample_count": 0,
            "systolic_bp_sample_count": 0,
            "diastolic_bp_sample_count": 0,
            "temperature_sample_count": 0,
        }]),
        result(rows=[]),
    ]

    admin_list_response = test_client.get("/api/v1/admin/patients")

    assert admin_list_response.status_code == 200
    assert admin_list_response.json()["patients"][0]["ward_id"] == "WARD-B"
    assert admin_list_response.json()["patients"][0]["encounter_id"] == "ENC-TEST"

    set_user("admin", wards=("WARD-B",))
    db.execute.side_effect = [
        result(scalar=True),
        result(rows=[moved_patient]),
        result(rows=[{
            "encounter_id": "ENC-TEST",
            "observation_count": 0,
            "heart_rate_sample_count": 0,
            "respiratory_rate_sample_count": 0,
            "spo2_sample_count": 0,
            "systolic_bp_sample_count": 0,
            "diastolic_bp_sample_count": 0,
            "temperature_sample_count": 0,
        }]),
        result(rows=[]),
    ]
    destination_response = test_client.get(
        "/api/v1/wards/WARD-B/patients"
    )

    assert destination_response.status_code == 200
    assert destination_response.json()["patients"][0]["encounter_id"] == "ENC-TEST"

    set_user("admin", wards=("WARD-A",))
    db.execute.side_effect = [
        result(scalar=True),
        result(rows=[]),
    ]
    source_response = test_client.get("/api/v1/wards/WARD-A/patients")

    assert source_response.status_code == 200
    assert source_response.json()["patients"] == []


def test_ward_patients_endpoint_still_requires_membership(client):
    test_client, db = client
    set_user("admin", wards=())

    response = test_client.get("/api/v1/wards/WARD-B/patients")

    assert response.status_code == 403
    db.execute.assert_not_called()


def test_ward_directory_is_admin_only(client):
    test_client, db = client
    set_user("admin")
    db.execute.return_value = MagicMock()
    db.execute.return_value.mappings.return_value.all.return_value = [
        {"ward_id": "WARD-A"},
        {"ward_id": "WARD-B"},
    ]

    response = test_client.get("/api/v1/admin/wards")

    assert response.status_code == 200
    assert response.json() == [
        {"ward_id": "WARD-A"},
        {"ward_id": "WARD-B"},
    ]


def test_non_admin_cannot_read_ward_directory(client):
    test_client, db = client
    set_user("nurse")

    response = test_client.get("/api/v1/admin/wards")

    assert response.status_code == 403
    db.execute.assert_not_called()
