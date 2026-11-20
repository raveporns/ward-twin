from datetime import datetime, timezone
from uuid import uuid4
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.main import app


@pytest.fixture
def import_client():
    db = MagicMock()
    fk_rows = []
    statements = []

    def execute(statement, params=None):
        sql = str(statement)
        statements.append((sql, params or {}))
        result = MagicMock()

        if "SELECT encounter_id, ward_id, admitted_at" in sql:
            result.mappings.return_value.all.return_value = fk_rows
        elif "INSERT INTO ward_twin.import_jobs" in sql:
            result.scalar_one.return_value = uuid4()
        return result

    db.execute.side_effect = execute
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "nurse-test",
        "role": "nurse",
        "wards": ["WARD-A"],
        "token_type": "access",
    }

    client = TestClient(app)
    yield client, db, fk_rows, statements

    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)


def upload(client, csv_content, kind=None, ward_id=None):
    data = {}
    if kind is not None:
        data["kind"] = kind
    if ward_id is not None:
        data["ward_id"] = ward_id
    return client.post(
        "/api/v1/imports",
        data=data,
        files={
            "file": (
                "test.csv",
                csv_content.encode("utf-8"),
                "text/csv",
            )
        },
    )


def encounter_csv():
    return (
        "patient_id,hn,display_name,birth_date,sex,encounter_id,"
        "an,ward_id,bed_label,admitted_at\n"
        "PAT-TEST,HN-TEST,ผู้ป่วยทดสอบ,,,ENC-TEST,,WARD-A,"
        "A-01,2026-11-20T08:00:00+07:00\n"
    )


def test_old_encounters_upload_defaults_to_encounters(import_client):
    client, db, _, statements = import_client

    response = upload(client, encounter_csv())

    assert response.status_code == 201
    assert response.json()["kind"] == "encounters"
    assert response.json()["status"] == "validated"
    assert response.json()["valid_rows"] == 1
    job_insert = next(
        params for sql, params in statements
        if "INSERT INTO ward_twin.import_jobs" in sql
    )
    assert job_insert["kind"] == "encounters"
    assert db.commit.call_count == 1


def test_observation_upload_stages_reading_without_prediction_work(import_client):
    client, db, fk_rows, statements = import_client
    fk_rows.append({
        "encounter_id": "ENC-A",
        "ward_id": "WARD-A",
        "admitted_at": datetime(2026, 11, 20, 1, tzinfo=timezone.utc),
    })
    csv_content = (
        "reading_id,encounter_id,measured_at,metric,value,unit\n"
        "123e4567-e89b-12d3-a456-426614174000,ENC-A,"
        "2026-11-20T09:00:00+07:00,heart_rate,80,bpm\n"
        "123e4567-e89b-12d3-a456-426614174000,ENC-A,"
        "2026-11-20T09:00:00+07:00,spo2,98,%\n"
    )

    response = upload(
        client,
        csv_content,
        kind="observations",
    )

    assert response.status_code == 201
    assert response.json()["kind"] == "observations"
    assert response.json()["status"] == "validated"
    assert response.json()["total_rows"] == 2
    assert response.json()["error_rows"] == 0
    inserted_sql = [
        sql for sql, _ in statements
        if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
    ]
    assert len(inserted_sql) == 3
    assert all(
        "import_jobs" in sql or "import_rows" in sql
        for sql in inserted_sql
    )
    assert not any(
        "observation_" in sql or "processing_state" in sql
        or "risk_predictions" in sql or "alerts" in sql
        for sql in inserted_sql
    )
    db.commit.assert_called_once()


def test_observation_duplicate_metric_is_staged_as_invalid(import_client):
    client, _, fk_rows, _ = import_client
    fk_rows.append({
        "encounter_id": "ENC-A",
        "ward_id": "WARD-A",
        "admitted_at": datetime(2026, 11, 20, 1, tzinfo=timezone.utc),
    })
    csv_content = (
        "reading_id,encounter_id,measured_at,metric,value,unit\n"
        "123e4567-e89b-12d3-a456-426614174000,ENC-A,"
        "2026-11-20T09:00:00+07:00,heart_rate,80,bpm\n"
        "123e4567-e89b-12d3-a456-426614174000,ENC-A,"
        "2026-11-20T09:00:00+07:00,heart_rate,82,bpm\n"
    )

    response = upload(client, csv_content, kind="observations")

    assert response.status_code == 201
    assert response.json()["status"] == "invalid"
    assert response.json()["error_rows"] == 1


def test_observation_invalid_metric_value_and_unit_are_staged(import_client):
    client, _, fk_rows, statements = import_client
    fk_rows.append({
        "encounter_id": "ENC-A",
        "ward_id": "WARD-A",
        "admitted_at": datetime(2026, 11, 20, 1, tzinfo=timezone.utc),
    })
    csv_content = (
        "reading_id,encounter_id,measured_at,metric,value,unit\n"
        "123e4567-e89b-12d3-a456-426614174000,ENC-A,"
        "2026-11-20T09:00:00+07:00,spo2,101,bpm\n"
    )

    response = upload(client, csv_content, kind="observations")

    assert response.status_code == 201
    assert response.json()["status"] == "invalid"
    row_insert = next(
        params for sql, params in statements
        if "INSERT INTO ward_twin.import_rows" in sql
    )
    assert "value" in row_insert["payload"]
    assert row_insert["errors"] != "[]"


def test_missing_observation_encounter_is_reported_in_staging(import_client):
    client, _, _, statements = import_client
    csv_content = (
        "reading_id,encounter_id,measured_at,metric,value,unit\n"
        "123e4567-e89b-12d3-a456-426614174000,NOT-FOUND,"
        "2026-11-20T09:00:00+07:00,heart_rate,80,bpm\n"
    )

    response = upload(
        client,
        csv_content,
        kind="observations",
        ward_id="WARD-A",
    )

    assert response.status_code == 201
    assert response.json()["status"] == "invalid"
    row_insert = next(
        params for sql, params in statements
        if "INSERT INTO ward_twin.import_rows" in sql
    )
    assert "encounter_id does not exist" in row_insert["errors"]


def test_observation_encounter_ward_access_is_enforced(import_client):
    client, db, fk_rows, _ = import_client
    fk_rows.append({
        "encounter_id": "ENC-OTHER",
        "ward_id": "WARD-B",
        "admitted_at": datetime(2026, 11, 20, 1, tzinfo=timezone.utc),
    })
    csv_content = (
        "reading_id,encounter_id,measured_at,metric,value,unit\n"
        "123e4567-e89b-12d3-a456-426614174000,ENC-OTHER,"
        "2026-11-20T09:00:00+07:00,heart_rate,80,bpm\n"
    )

    response = upload(client, csv_content, kind="observations")

    assert response.status_code == 403
    assert not any(
        "INSERT INTO ward_twin.import_jobs" in str(call.args[0])
        for call in db.execute.call_args_list
    )


def test_outcome_csv_is_validated_and_staged_only(import_client):
    client, _, fk_rows, statements = import_client
    fk_rows.append({
        "encounter_id": "ENC-A",
        "ward_id": "WARD-A",
        "admitted_at": datetime(2026, 11, 20, 1, tzinfo=timezone.utc),
    })
    csv_content = (
        "event_id,encounter_id,event_type,event_at,unplanned\n"
        "EVENT-1,ENC-A,icu_transfer,2026-11-20T10:00:00+07:00,true\n"
    )

    response = upload(client, csv_content, kind="outcomes")

    assert response.status_code == 201
    assert response.json()["kind"] == "outcomes"
    assert response.json()["status"] == "validated"
    row_insert = next(
        params for sql, params in statements
        if "INSERT INTO ward_twin.import_rows" in sql
    )
    assert '"unplanned": true' in row_insert["payload"]
    assert not any(
        "INSERT INTO ward_twin.outcomes" in sql
        for sql, _ in statements
    )


@pytest.mark.parametrize(
    ("event_type", "unplanned", "expected_error"),
    [
        ("icu_transfer", "", "unplanned must be true or false"),
        ("discharge", "true", "unplanned must be empty"),
        ("unknown", "", "event_type is not supported"),
    ],
)
def test_invalid_outcome_values_are_staged(
    import_client,
    event_type,
    unplanned,
    expected_error,
):
    client, _, fk_rows, statements = import_client
    fk_rows.append({
        "encounter_id": "ENC-A",
        "ward_id": "WARD-A",
        "admitted_at": datetime(2026, 11, 20, 1, tzinfo=timezone.utc),
    })
    csv_content = (
        "event_id,encounter_id,event_type,event_at,unplanned\n"
        f"EVENT-1,ENC-A,{event_type},2026-11-20T10:00:00+07:00,"
        f"{unplanned}\n"
    )

    response = upload(client, csv_content, kind="outcomes")

    assert response.status_code == 201
    assert response.json()["status"] == "invalid"
    row_insert = next(
        params for sql, params in statements
        if "INSERT INTO ward_twin.import_rows" in sql
    )
    assert expected_error in row_insert["errors"]


def test_outcome_before_admission_is_invalid(import_client):
    client, _, fk_rows, statements = import_client
    fk_rows.append({
        "encounter_id": "ENC-A",
        "ward_id": "WARD-A",
        "admitted_at": datetime(2026, 11, 20, 4, tzinfo=timezone.utc),
    })
    csv_content = (
        "event_id,encounter_id,event_type,event_at,unplanned\n"
        "EVENT-1,ENC-A,discharge,2026-11-20T10:00:00+07:00,\n"
    )

    response = upload(client, csv_content, kind="outcomes")

    assert response.status_code == 201
    assert response.json()["status"] == "invalid"
    row_insert = next(
        params for sql, params in statements
        if "INSERT INTO ward_twin.import_rows" in sql
    )
    assert "event_at must not precede encounter admission" in row_insert[
        "errors"
    ]


def test_multiple_encounters_in_same_ward_are_supported(import_client):
    client, _, fk_rows, _ = import_client
    fk_rows.extend([
        {
            "encounter_id": "ENC-A",
            "ward_id": "WARD-A",
            "admitted_at": datetime(2026, 11, 20, 1, tzinfo=timezone.utc),
        },
        {
            "encounter_id": "ENC-B",
            "ward_id": "WARD-A",
            "admitted_at": datetime(2026, 11, 20, 1, tzinfo=timezone.utc),
        },
    ])
    csv_content = (
        "event_id,encounter_id,event_type,event_at,unplanned\n"
        "EVENT-1,ENC-A,discharge,2026-11-20T10:00:00+07:00,\n"
        "EVENT-2,ENC-B,death,2026-11-20T11:00:00+07:00,\n"
    )

    response = upload(client, csv_content, kind="outcomes")

    assert response.status_code == 201
    assert response.json()["status"] == "validated"


def test_csv_referencing_multiple_wards_is_rejected(import_client):
    client, _, fk_rows, _ = import_client
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "nurse-test",
        "role": "nurse",
        "wards": ["WARD-A", "WARD-B"],
        "token_type": "access",
    }
    fk_rows.extend([
        {
            "encounter_id": "ENC-A",
            "ward_id": "WARD-A",
            "admitted_at": datetime(2026, 11, 20, 1, tzinfo=timezone.utc),
        },
        {
            "encounter_id": "ENC-B",
            "ward_id": "WARD-B",
            "admitted_at": datetime(2026, 11, 20, 1, tzinfo=timezone.utc),
        },
    ])
    csv_content = (
        "event_id,encounter_id,event_type,event_at,unplanned\n"
        "EVENT-1,ENC-A,discharge,2026-11-20T10:00:00+07:00,\n"
        "EVENT-2,ENC-B,death,2026-11-20T11:00:00+07:00,\n"
    )

    response = upload(client, csv_content, kind="outcomes")

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "CSV must reference encounters in exactly one ward"
    )


def test_templates_expose_all_three_kinds(import_client):
    client, _, _, _ = import_client

    for kind, expected_header in [
        (
            "encounters",
            "patient_id,hn,display_name,birth_date,sex,encounter_id,"
            "an,ward_id,bed_label,admitted_at",
        ),
        (
            "observations",
            "reading_id,encounter_id,measured_at,metric,value,unit",
        ),
        (
            "outcomes",
            "event_id,encounter_id,event_type,event_at,unplanned",
        ),
    ]:
        response = client.get(
            f"/api/v1/import-templates/{kind}"
        )
        assert response.status_code == 200
        assert response.content.decode("utf-8-sig").strip() == (
            expected_header
        )
