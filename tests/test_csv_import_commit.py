import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import IntegrityError
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.main import app


IMPORT_ID = uuid4()
READING_ID = "123e4567-e89b-12d3-a456-426614174000"
STAGED_ROWS = [
    {
        "payload": {
            "reading_id": READING_ID,
            "encounter_id": "ENC-A",
            "measured_at": "2026-11-20T09:00:00+07:00",
            "metric": "heart_rate",
            "value": "80",
            "unit": "bpm",
        }
    },
    {
        "payload": {
            "reading_id": READING_ID,
            "encounter_id": "ENC-A",
            "measured_at": "2026-11-20T09:00:00+07:00",
            "metric": "spo2",
            "value": "98",
            "unit": "%",
        }
    },
]
STAGED_OUTCOME_ROWS = [
    {
        "payload": {
            "event_id": "EVENT-1",
            "encounter_id": "ENC-A",
            "event_type": "icu_transfer",
            "event_at": "2026-11-20T10:00:00+07:00",
            "unplanned": True,
        }
    },
    {
        "payload": {
            "event_id": "EVENT-2",
            "encounter_id": "ENC-A",
            "event_type": "discharge",
            "event_at": "2026-11-20T11:00:00+07:00",
            "unplanned": None,
        }
    },
]


def expected_content_hash():
    canonical_payload = {
        "source": "csv",
        "encounter_id": "ENC-A",
        "measured_at": "2026-11-20T02:00:00+00:00",
        "measurements": [
            {"metric": "heart_rate", "value": 80.0, "unit": "bpm"},
            {"metric": "spo2", "value": 98.0, "unit": "%"},
        ],
    }
    canonical = json.dumps(
        canonical_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@pytest.fixture
def commit_client():
    job = {
        "import_id": IMPORT_ID,
        "kind": "observations",
        "status": "validated",
        "ward_id": "WARD-A",
        "total_rows": 2,
        "valid_rows": 2,
        "error_rows": 0,
    }
    db = type("MockDB", (), {})()
    db.commit = pytest.importorskip("unittest.mock").MagicMock()
    db.rollback = pytest.importorskip("unittest.mock").MagicMock()
    statements = []
    existing_hash = {"value": None}
    producer_registered = {"value": True}
    encounter_rows = [{
        "encounter_id": "ENC-A",
        "ward_id": "WARD-A",
        "admitted_at": datetime(2026, 11, 20, 1, tzinfo=timezone.utc),
    }]
    existing_event = {"value": None}
    fail_outcome_insert = {"value": 0}
    outcome_insert_calls = {"value": 0}

    def execute(statement, params=None):
        sql = str(statement)
        statements.append((sql, params or {}))
        result = MagicMock()

        if "FROM ward_twin.import_jobs" in sql and "FOR UPDATE" in sql:
            result.mappings.return_value.first.return_value = job
        elif "FROM ward_twin.import_rows" in sql:
            result.mappings.return_value.all.return_value = (
                STAGED_OUTCOME_ROWS
                if job["kind"] == "outcomes"
                else STAGED_ROWS
            )
        elif "FROM ward_twin.producers" in sql:
            result.mappings.return_value.one_or_none.return_value = (
                {"producer_id": "csv-import:WARD-A"}
                if producer_registered["value"]
                else None
            )
        elif (
            "SELECT encounter_id, ward_id" in sql
            and "FROM ward_twin.encounters" in sql
        ):
            result.mappings.return_value.all.return_value = encounter_rows
        elif (
            "SELECT event_id" in sql
            and "FROM ward_twin.outcomes" in sql
        ):
            result.first.return_value = existing_event["value"]
        elif "INSERT INTO ward_twin.outcomes" in sql:
            outcome_insert_calls["value"] += 1
            if (
                fail_outcome_insert["value"]
                and outcome_insert_calls["value"]
                == fail_outcome_insert["value"]
            ):
                raise IntegrityError(
                    sql,
                    params,
                    Exception("mocked database constraint failure"),
                )
        elif "SELECT content_hash" in sql:
            result.scalar_one_or_none.return_value = existing_hash["value"]
            result.scalar_one.return_value = existing_hash["value"]
        elif "INSERT INTO ward_twin.observation_readings" in sql:
            result.scalar_one_or_none.return_value = 901
        elif "UPDATE ward_twin.import_jobs" in sql:
            job["status"] = "committed"
        return result

    db.execute = execute
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "admin-import-test",
        "role": "admin",
        "wards": ["WARD-A"],
        "token_type": "access",
    }

    client = TestClient(app)
    yield (
        client,
        db,
        job,
        statements,
        existing_hash,
        producer_registered,
        encounter_rows,
        existing_event,
        fail_outcome_insert,
    )

    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)


def test_observation_commit_inserts_group_and_measurements(commit_client):
    client, db, job, statements, _, _, _, _, _ = commit_client

    response = client.post(
        f"/api/v1/imports/{IMPORT_ID}/commit"
    )

    assert response.status_code == 200
    assert response.json() == {
        "import_id": str(IMPORT_ID),
        "status": "committed",
        "inserted_rows": 1,
        "committed": True,
    }
    assert db.commit.call_count == 1
    assert job["status"] == "committed"
    insert_readings = [
        params for sql, params in statements
        if "INSERT INTO ward_twin.observation_readings" in sql
    ]
    assert len(insert_readings) == 1
    assert insert_readings[0]["producer_id"] == "csv-import:WARD-A"
    assert insert_readings[0]["reading_id"] == READING_ID
    assert "'csv'" in next(
        sql for sql, _ in statements
        if "INSERT INTO ward_twin.observation_readings" in sql
    )
    measurements = [
        params for sql, params in statements
        if "INSERT INTO ward_twin.observation_measurements" in sql
    ]
    assert {row["metric"] for row in measurements} == {
        "heart_rate",
        "spo2",
    }
    assert any("FOR UPDATE" in sql for sql, _ in statements)
    producer_lookup = next(
        (sql, params) for sql, params in statements
        if "FROM ward_twin.producers" in sql
    )
    assert "type = 'csv'" in producer_lookup[0]
    assert "active = TRUE" in producer_lookup[0]
    assert "FOR SHARE" in producer_lookup[0]
    assert producer_lookup[1]["ward_id"] == "WARD-A"
    encounter_lookup = next(
        sql for sql, _ in statements
        if "SELECT encounter_id, ward_id" in sql
    )
    assert "FOR SHARE" in encounter_lookup
    assert any(
        "UPDATE ward_twin.import_jobs" in sql for sql, _ in statements
    )
    assert not any(
        "processing_state" in sql
        or "risk_predictions" in sql
        or "ward_twin.alerts" in sql
        for sql, _ in statements
    )


def test_observation_reimport_with_same_hash_is_idempotent(commit_client):
    client, db, _, statements, existing_hash, *_ = commit_client
    existing_hash["value"] = expected_content_hash()

    response = client.post(
        f"/api/v1/imports/{IMPORT_ID}/commit"
    )

    assert response.status_code == 200
    assert response.json()["inserted_rows"] == 0
    assert not any(
        "INSERT INTO ward_twin.observation_readings" in sql
        or "INSERT INTO ward_twin.observation_measurements" in sql
        for sql, _ in statements
    )
    db.commit.assert_called_once()


def test_observation_reading_id_with_different_hash_rolls_back(
    commit_client,
):
    client, db, _, statements, existing_hash, *_ = commit_client
    existing_hash["value"] = "different-content-hash"

    response = client.post(
        f"/api/v1/imports/{IMPORT_ID}/commit"
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Reading ID conflict"
    db.rollback.assert_called_once()
    db.commit.assert_not_called()
    assert not any(
        "UPDATE ward_twin.import_jobs" in sql for sql, _ in statements
    )


def test_missing_csv_producer_for_ward_blocks_commit(commit_client):
    (
        client,
        db,
        _,
        statements,
        _,
        producer_registered,
        *_,
    ) = commit_client
    producer_registered["value"] = False

    response = client.post(
        f"/api/v1/imports/{IMPORT_ID}/commit"
    )

    assert response.status_code == 409
    assert "no active CSV producer is registered" in (
        response.json()["detail"]
    )
    assert any("ward_twin.producers" in sql for sql, _ in statements)
    assert not any(
        "INSERT INTO ward_twin.observation_readings" in sql
        for sql, _ in statements
    )
    db.rollback.assert_called_once()


def test_encounter_ward_change_after_upload_blocks_commit(commit_client):
    (
        client,
        db,
        _,
        statements,
        _,
        _,
        encounter_rows,
        *_,
    ) = commit_client
    encounter_rows[0]["ward_id"] = "WARD-B"

    response = client.post(
        f"/api/v1/imports/{IMPORT_ID}/commit"
    )

    assert response.status_code == 409
    assert "Ward changed after upload" in response.json()["detail"]
    db.rollback.assert_called_once()
    assert not any(
        "INSERT INTO ward_twin.observation_readings" in sql
        for sql, _ in statements
    )


def test_committed_import_rejects_repeated_commit(commit_client):
    client, db, job, statements, *_ = commit_client
    job["status"] = "committed"

    response = client.post(
        f"/api/v1/imports/{IMPORT_ID}/commit"
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Import has already been committed"
    assert any("FOR UPDATE" in sql for sql, _ in statements)
    db.rollback.assert_called_once()


def test_concurrent_commit_is_serialized_by_import_job_lock(commit_client):
    client, db, job, statements, *_ = commit_client

    first = client.post(f"/api/v1/imports/{IMPORT_ID}/commit")
    second = client.post(f"/api/v1/imports/{IMPORT_ID}/commit")

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["detail"] == "Import has already been committed"
    assert sum("FOR UPDATE" in sql for sql, _ in statements) == 2
    assert sum(
        "INSERT INTO ward_twin.observation_readings" in sql
        for sql, _ in statements
    ) == 1
    db.commit.assert_called_once()
    db.rollback.assert_called_once()


def test_observation_commit_wrong_ward_is_forbidden(commit_client):
    client, db, *_ = commit_client
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "admin-import-test",
        "role": "admin",
        "wards": ["WARD-B"],
        "token_type": "access",
    }

    response = client.post(
        f"/api/v1/imports/{IMPORT_ID}/commit"
    )

    assert response.status_code == 403
    db.rollback.assert_called_once()


def test_outcome_commit_inserts_events_with_check_values(commit_client):
    client, db, job, statements, *_ = commit_client
    job["kind"] = "outcomes"

    response = client.post(
        f"/api/v1/imports/{IMPORT_ID}/commit"
    )

    assert response.status_code == 200
    assert response.json()["inserted_rows"] == 2
    outcome_inserts = [
        params for sql, params in statements
        if "INSERT INTO ward_twin.outcomes" in sql
    ]
    assert len(outcome_inserts) == 2
    assert outcome_inserts[0]["unplanned"] is True
    assert outcome_inserts[1]["unplanned"] is None
    db.commit.assert_called_once()


def test_duplicate_outcome_event_id_in_database_rolls_back(commit_client):
    (
        client,
        db,
        job,
        statements,
        _,
        _,
        _,
        existing_event,
        _,
    ) = commit_client
    job["kind"] = "outcomes"
    existing_event["value"] = ("EVENT-1",)

    response = client.post(
        f"/api/v1/imports/{IMPORT_ID}/commit"
    )

    assert response.status_code == 409
    assert response.json()["detail"]["message"] == (
        "Outcome event_id already exists"
    )
    db.rollback.assert_called_once()
    db.commit.assert_not_called()
    assert not any(
        "INSERT INTO ward_twin.outcomes" in sql
        for sql, _ in statements
    )


def test_outcome_encounter_missing_at_commit_rolls_back(commit_client):
    client, db, job, statements, *_ = commit_client
    job["kind"] = "outcomes"
    commit_client[6].clear()

    response = client.post(
        f"/api/v1/imports/{IMPORT_ID}/commit"
    )

    assert response.status_code == 409
    assert "no longer exists" in response.json()["detail"]
    db.rollback.assert_called_once()
    assert not any(
        "INSERT INTO ward_twin.outcomes" in sql
        for sql, _ in statements
    )


def test_outcome_encounter_ward_change_at_commit_rolls_back(commit_client):
    client, db, job, statements, *_ = commit_client
    job["kind"] = "outcomes"
    commit_client[6][0]["ward_id"] = "WARD-B"

    response = client.post(
        f"/api/v1/imports/{IMPORT_ID}/commit"
    )

    assert response.status_code == 409
    assert "Ward changed after upload" in response.json()["detail"]
    db.rollback.assert_called_once()
    assert not any(
        "INSERT INTO ward_twin.outcomes" in sql
        for sql, _ in statements
    )


def test_outcome_event_before_admission_is_rejected_at_commit(commit_client):
    client, db, job, statements, *_ = commit_client
    job["kind"] = "outcomes"
    commit_client[6][0]["admitted_at"] = datetime(
        2026, 11, 20, 4, tzinfo=timezone.utc
    )

    response = client.post(
        f"/api/v1/imports/{IMPORT_ID}/commit"
    )

    assert response.status_code == 409
    assert "event_at must not precede" in response.json()["detail"]
    db.rollback.assert_called_once()
    assert not any(
        "INSERT INTO ward_twin.outcomes" in sql
        for sql, _ in statements
    )


def test_outcome_insert_constraint_failure_rolls_back_all(commit_client):
    client, db, job, statements, *_ = commit_client
    job["kind"] = "outcomes"
    commit_client[8]["value"] = 2

    response = client.post(
        f"/api/v1/imports/{IMPORT_ID}/commit"
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Database constraint conflict during import"
    )
    db.rollback.assert_called_once()
    db.commit.assert_not_called()
    assert sum(
        "INSERT INTO ward_twin.outcomes" in sql
        for sql, _ in statements
    ) == 2
    assert not any(
        "UPDATE ward_twin.import_jobs" in sql
        for sql, _ in statements
    )
