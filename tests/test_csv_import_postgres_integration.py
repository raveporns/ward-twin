import os
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.main import app


DATABASE_URL_ENV = "WARD_TWIN_TEST_DATABASE_URL"
DATABASE_NAME = "ward_twin_csv_test"
MIGRATIONS_DIR = Path(__file__).parents[1] / "migrations"
pytestmark = pytest.mark.skipif(
    not os.environ.get(DATABASE_URL_ENV),
    reason=f"Set {DATABASE_URL_ENV} to run isolated PostgreSQL tests",
)


SCHEMA_DDL = (
    """
    CREATE TABLE ward_twin.wards (
        ward_id TEXT PRIMARY KEY
    )
    """,
    """
    CREATE TABLE ward_twin.producers (
        producer_id TEXT PRIMARY KEY,
        type TEXT NOT NULL,
        ward_id TEXT NOT NULL REFERENCES ward_twin.wards(ward_id),
        key_hash TEXT UNIQUE,
        active BOOLEAN NOT NULL DEFAULT TRUE,
        UNIQUE (producer_id, ward_id),
        CHECK (type IN ('gateway', 'manual', 'csv')),
        CHECK ((type = 'gateway') = (key_hash IS NOT NULL))
    )
    """,
    """
    CREATE UNIQUE INDEX producers_ward_type_uq
    ON ward_twin.producers (ward_id, type)
    WHERE type IN ('manual', 'csv')
    """,
    """
    CREATE TABLE ward_twin.patients (
        patient_id TEXT PRIMARY KEY,
        hn TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL,
        birth_date DATE,
        sex TEXT
    )
    """,
    """
    CREATE TABLE ward_twin.encounters (
        encounter_id TEXT PRIMARY KEY,
        patient_id TEXT NOT NULL REFERENCES ward_twin.patients(patient_id),
        ward_id TEXT NOT NULL REFERENCES ward_twin.wards(ward_id),
        an TEXT UNIQUE,
        bed_label TEXT,
        admitted_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE ward_twin.import_jobs (
        import_id UUID PRIMARY KEY,
        kind TEXT NOT NULL
            CONSTRAINT import_jobs_kind_check
            CHECK (kind IN ('encounters')),
        status TEXT NOT NULL,
        uploaded_by TEXT NOT NULL,
        ward_id TEXT NOT NULL REFERENCES ward_twin.wards(ward_id),
        filename TEXT NOT NULL,
        total_rows INTEGER NOT NULL,
        valid_rows INTEGER NOT NULL,
        error_rows INTEGER NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        committed_at TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE ward_twin.import_rows (
        import_id UUID NOT NULL
            REFERENCES ward_twin.import_jobs(import_id) ON DELETE CASCADE,
        row_number INTEGER NOT NULL,
        payload JSONB NOT NULL,
        errors JSONB NOT NULL,
        PRIMARY KEY (import_id, row_number)
    )
    """,
    """
    CREATE TABLE ward_twin.observation_readings (
        id BIGSERIAL PRIMARY KEY,
        producer_id TEXT NOT NULL
            REFERENCES ward_twin.producers(producer_id),
        reading_id UUID NOT NULL,
        encounter_id TEXT NOT NULL
            REFERENCES ward_twin.encounters(encounter_id) ON DELETE RESTRICT,
        source TEXT NOT NULL CHECK (source IN ('iot', 'manual', 'csv')),
        measured_at TIMESTAMPTZ NOT NULL,
        received_at TIMESTAMPTZ NOT NULL,
        content_hash TEXT NOT NULL,
        UNIQUE (producer_id, reading_id)
    )
    """,
    """
    CREATE TABLE ward_twin.observation_measurements (
        reading_pk BIGINT NOT NULL
            REFERENCES ward_twin.observation_readings(id) ON DELETE CASCADE,
        metric TEXT NOT NULL,
        value DOUBLE PRECISION NOT NULL,
        unit TEXT NOT NULL,
        quality_flag TEXT,
        PRIMARY KEY (reading_pk, metric)
    )
    """,
    """
    CREATE TABLE ward_twin.outcomes (
        event_id TEXT PRIMARY KEY,
        encounter_id TEXT NOT NULL
            CONSTRAINT outcomes_encounter_fk
            REFERENCES ward_twin.encounters(encounter_id) ON DELETE RESTRICT,
        event_type TEXT NOT NULL
            CONSTRAINT outcomes_event_type_ck
            CHECK (event_type IN (
                'icu_transfer', 'discharge', 'death', 'followup_end'
            )),
        event_at TIMESTAMPTZ NOT NULL,
        unplanned BOOLEAN,
        CONSTRAINT outcomes_unplanned_ck
            CHECK ((event_type = 'icu_transfer') = (unplanned IS NOT NULL))
    )
    """,
    """
    CREATE INDEX outcomes_encounter_event_idx
    ON ward_twin.outcomes (encounter_id, event_at)
    """,
    """
    CREATE TABLE ward_twin.processing_state (
        encounter_id TEXT PRIMARY KEY,
        dirty_version INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE ward_twin.risk_predictions (
        prediction_id BIGSERIAL PRIMARY KEY,
        encounter_id TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE ward_twin.alerts (
        alert_id TEXT PRIMARY KEY,
        encounter_id TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE ward_twin.__csv_integration_test_owned (
        marker TEXT PRIMARY KEY
    )
    """,
)


def _assert_safe_test_database(database_url: str):
    url = make_url(database_url)
    if url.database != DATABASE_NAME:
        pytest.fail(
            f"{DATABASE_URL_ENV} must target the dedicated "
            f"{DATABASE_NAME!r} database"
        )
    if url.host not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail(f"{DATABASE_URL_ENV} must target a local PostgreSQL host")


def _execute_script(engine, sql: str):
    raw_connection = engine.raw_connection()
    try:
        raw_connection.driver_connection.cursor().execute(sql)
        raw_connection.commit()
    except Exception:
        raw_connection.rollback()
        raise
    finally:
        raw_connection.close()


@pytest.fixture(scope="session")
def pg_engine():
    database_url = os.environ.get(DATABASE_URL_ENV)
    if not database_url:
        pytest.skip(f"{DATABASE_URL_ENV} is not set")
    _assert_safe_test_database(database_url)

    engine = create_engine(database_url, pool_size=5, max_overflow=5)
    with engine.begin() as connection:
        database_name = connection.execute(
            text("SELECT current_database()")
        ).scalar_one()
        assert database_name == DATABASE_NAME
        schema_exists = connection.execute(
            text("SELECT to_regnamespace('ward_twin') IS NOT NULL")
        ).scalar_one()
        if schema_exists:
            marker_exists = connection.execute(
                text("""
                    SELECT to_regclass(
                        'ward_twin.__csv_integration_test_owned'
                    ) IS NOT NULL
                """)
            ).scalar_one()
            if not marker_exists:
                pytest.fail(
                    "Refusing to reset ward_twin schema without the "
                    "CSV integration-test ownership marker"
                )
            marker = connection.execute(
                text("""
                    SELECT marker
                    FROM ward_twin.__csv_integration_test_owned
                    LIMIT 1
                """)
            ).scalar_one_or_none()
            if marker != "ward-twin-csv-integration-test":
                pytest.fail("The test schema ownership marker is invalid")
            connection.execute(text("DROP SCHEMA ward_twin CASCADE"))
        connection.execute(text("CREATE SCHEMA ward_twin"))

    with engine.begin() as connection:
        for statement in SCHEMA_DDL:
            connection.exec_driver_sql(statement)
        connection.execute(
            text("""
                INSERT INTO ward_twin.__csv_integration_test_owned (marker)
                VALUES ('ward-twin-csv-integration-test')
            """)
        )
        connection.execute(
            text("""
                INSERT INTO ward_twin.wards (ward_id)
                VALUES ('WARD-A'), ('WARD-B'), ('WARD-C'), ('WARD-TEST')
            """)
        )
        connection.execute(
            text("""
                INSERT INTO ward_twin.producers (
                    producer_id, type, ward_id, key_hash, active
                )
                VALUES
                    ('manual-api', 'manual', 'WARD-A', NULL, TRUE),
                    ('local-gateway', 'gateway', 'WARD-A', 'test-key-hash',
                     TRUE),
                    ('csv-import:WARD-A', 'csv', 'WARD-A', NULL, TRUE)
            """)
        )

    _execute_script(
        engine,
        (MIGRATIONS_DIR / "20261120_expand_import_job_kinds.sql").read_text(),
    )

    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def reset_test_rows(pg_engine):
    tables = (
        "alerts",
        "risk_predictions",
        "processing_state",
        "outcomes",
        "observation_measurements",
        "observation_readings",
        "import_rows",
        "import_jobs",
        "encounters",
        "patients",
        "producers",
        "wards",
    )
    with pg_engine.begin() as connection:
        connection.execute(text(
            "TRUNCATE TABLE ward_twin."
            + ", ward_twin.".join(tables)
            + " RESTART IDENTITY CASCADE"
        ))
        connection.execute(
            text("""
                INSERT INTO ward_twin.wards (ward_id)
            VALUES ('WARD-A'), ('WARD-B'), ('WARD-C'), ('WARD-TEST')
            """)
        )
        connection.execute(
            text("""
                INSERT INTO ward_twin.producers (
                    producer_id, type, ward_id, key_hash, active
                )
                VALUES
                    ('manual-api', 'manual', 'WARD-A', NULL, TRUE),
                    ('local-gateway', 'gateway', 'WARD-A', 'test-key-hash',
                     TRUE),
                    ('csv-import:WARD-A', 'csv', 'WARD-A', NULL, TRUE)
            """)
        )


@pytest.fixture
def api(pg_engine):
    test_session_factory = sessionmaker(
        bind=pg_engine,
        autoflush=False,
        autocommit=False,
    )

    def override_db():
        with test_session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "csv-integration-admin",
        "role": "admin",
        "wards": ["WARD-A"],
        "token_type": "access",
    }
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)


def _add_encounter(engine, encounter_id="ENC-A", ward_id="WARD-A"):
    with engine.begin() as connection:
        connection.execute(
            text("""
                INSERT INTO ward_twin.patients (
                    patient_id, hn, display_name
                )
                VALUES (:patient_id, :hn, :display_name)
            """),
            {
                "patient_id": f"PAT-{encounter_id}",
                "hn": f"HN-{encounter_id}",
                "display_name": "Integration Test Patient",
            },
        )
        connection.execute(
            text("""
                INSERT INTO ward_twin.encounters (
                    encounter_id, patient_id, ward_id, admitted_at
                )
                VALUES (
                    :encounter_id, :patient_id, :ward_id,
                    '2026-11-20T01:00:00Z'
                )
            """),
            {
                "encounter_id": encounter_id,
                "patient_id": f"PAT-{encounter_id}",
                "ward_id": ward_id,
            },
        )


def _stage_import(engine, kind, payloads, ward_id="WARD-A"):
    import_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("""
                INSERT INTO ward_twin.import_jobs (
                    import_id, kind, status, uploaded_by, ward_id, filename,
                    total_rows, valid_rows, error_rows
                )
                VALUES (
                    :import_id, :kind, 'validated', 'integration-test',
                    :ward_id, 'integration.csv', :total_rows, :total_rows, 0
                )
            """),
            {
                "import_id": import_id,
                "kind": kind,
                "ward_id": ward_id,
                "total_rows": len(payloads),
            },
        )
        for row_number, payload in enumerate(payloads, start=2):
            connection.execute(
                text("""
                    INSERT INTO ward_twin.import_rows (
                        import_id, row_number, payload, errors
                    )
                    VALUES (
                        :import_id, :row_number, CAST(:payload AS JSONB),
                        '[]'::jsonb
                    )
                """),
                {
                    "import_id": import_id,
                    "row_number": row_number,
                    "payload": json.dumps(payload),
                },
            )
    return import_id


def _observation_payload(reading_id=None, value="80"):
    reading_id = reading_id or str(uuid4())
    return [
        {
            "reading_id": reading_id,
            "encounter_id": "ENC-A",
            "measured_at": "2026-11-20T09:00:00+07:00",
            "metric": "heart_rate",
            "value": value,
            "unit": "bpm",
        },
        {
            "reading_id": reading_id,
            "encounter_id": "ENC-A",
            "measured_at": "2026-11-20T09:00:00+07:00",
            "metric": "spo2",
            "value": "98",
            "unit": "%",
        },
    ]


def _outcome_payloads():
    return [
        {
            "event_id": "EVENT-1",
            "encounter_id": "ENC-A",
            "event_type": "icu_transfer",
            "event_at": "2026-11-20T10:00:00+07:00",
            "unplanned": True,
        },
        {
            "event_id": "EVENT-2",
            "encounter_id": "ENC-A",
            "event_type": "discharge",
            "event_at": "2026-11-20T11:00:00+07:00",
            "unplanned": None,
        },
    ]


def test_postgres_encounters_commit_and_duplicate_import(api, pg_engine):
    import_id = _stage_import(
        pg_engine,
        "encounters",
        [{
            "patient_id": "PAT-NEW",
            "hn": "HN-NEW",
            "display_name": "Integration Patient",
            "birth_date": "1990-01-01",
            "sex": "F",
            "encounter_id": "ENC-NEW",
            "an": "",
            "ward_id": "WARD-A",
            "bed_label": "A-01",
            "admitted_at": "2026-11-20T08:00:00+07:00",
        }],
    )

    committed = api.post(f"/api/v1/imports/{import_id}/commit")
    repeated = api.post(f"/api/v1/imports/{import_id}/commit")

    assert committed.status_code == 200
    assert committed.json()["inserted_rows"] == 1
    assert repeated.status_code == 409
    with pg_engine.connect() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM ward_twin.encounters")
        ).scalar_one() == 1


def test_postgres_observation_commit_and_no_prediction_or_alert(api, pg_engine):
    _add_encounter(pg_engine)
    import_id = _stage_import(
        pg_engine,
        "observations",
        _observation_payload(),
    )

    response = api.post(f"/api/v1/imports/{import_id}/commit")

    assert response.status_code == 200
    assert response.json()["inserted_rows"] == 1
    with pg_engine.connect() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM ward_twin.observation_readings")
        ).scalar_one() == 1
        assert connection.execute(
            text("SELECT count(*) FROM ward_twin.observation_measurements")
        ).scalar_one() == 2
        assert connection.execute(
            text("SELECT count(*) FROM ward_twin.processing_state")
        ).scalar_one() == 0
        assert connection.execute(
            text("SELECT count(*) FROM ward_twin.risk_predictions")
        ).scalar_one() == 0
        assert connection.execute(
            text("SELECT count(*) FROM ward_twin.alerts")
        ).scalar_one() == 0


def test_postgres_observation_idempotency_and_conflict(api, pg_engine):
    _add_encounter(pg_engine)
    reading_id = str(uuid4())
    payloads = _observation_payload(reading_id=reading_id)
    first_id = _stage_import(pg_engine, "observations", payloads)
    second_id = _stage_import(pg_engine, "observations", payloads)

    first = api.post(f"/api/v1/imports/{first_id}/commit")
    second = api.post(f"/api/v1/imports/{second_id}/commit")

    assert first.status_code == 200
    assert first.json()["inserted_rows"] == 1
    assert second.status_code == 200
    assert second.json()["inserted_rows"] == 0

    conflict_id = _stage_import(
        pg_engine,
        "observations",
        _observation_payload(reading_id=reading_id, value="81"),
    )
    conflict = api.post(f"/api/v1/imports/{conflict_id}/commit")
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "Reading ID conflict"


def test_postgres_outcomes_commit_duplicate_and_check_constraints(api, pg_engine):
    _add_encounter(pg_engine)
    import_id = _stage_import(
        pg_engine,
        "outcomes",
        _outcome_payloads(),
    )

    response = api.post(f"/api/v1/imports/{import_id}/commit")

    assert response.status_code == 200
    assert response.json()["inserted_rows"] == 2
    duplicate_id = _stage_import(
        pg_engine,
        "outcomes",
        [_outcome_payloads()[0]],
    )
    duplicate = api.post(f"/api/v1/imports/{duplicate_id}/commit")
    assert duplicate.status_code == 409
    with pg_engine.connect() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM ward_twin.outcomes")
        ).scalar_one() == 2

    with pytest.raises(IntegrityError):
        with pg_engine.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO ward_twin.outcomes (
                        event_id, encounter_id, event_type, event_at, unplanned
                    )
                    VALUES (
                        'INVALID-CHECK', 'ENC-A', 'discharge', NOW(), TRUE
                    )
                """)
            )
    with pytest.raises(IntegrityError):
        with pg_engine.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO ward_twin.outcomes (
                        event_id, encounter_id, event_type, event_at, unplanned
                    )
                    VALUES (
                        'INVALID-FK', 'MISSING-ENCOUNTER',
                        'discharge', NOW(), NULL
                    )
                """)
            )


def test_postgres_rollback_on_mid_batch_outcome_failure(api, pg_engine):
    _add_encounter(pg_engine)
    with pg_engine.begin() as connection:
        connection.exec_driver_sql("""
            CREATE FUNCTION ward_twin.reject_second_test_outcome()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.event_id = 'EVENT-2' THEN
                    RAISE EXCEPTION 'injected test failure';
                END IF;
                RETURN NEW;
            END;
            $$
        """)
        connection.exec_driver_sql("""
            CREATE TRIGGER reject_second_test_outcome
            BEFORE INSERT ON ward_twin.outcomes
            FOR EACH ROW EXECUTE FUNCTION
                ward_twin.reject_second_test_outcome()
        """)
    import_id = _stage_import(pg_engine, "outcomes", _outcome_payloads())

    response = api.post(f"/api/v1/imports/{import_id}/commit")

    assert response.status_code == 500
    with pg_engine.connect() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM ward_twin.outcomes")
        ).scalar_one() == 0
        assert connection.execute(
            text("""
                SELECT status FROM ward_twin.import_jobs
                WHERE import_id = :import_id
            """),
            {"import_id": import_id},
        ).scalar_one() == "validated"


def test_postgres_ward_authorization_blocks_cross_ward_commit(api, pg_engine):
    _add_encounter(pg_engine, ward_id="WARD-B")
    import_id = _stage_import(
        pg_engine,
        "outcomes",
        [_outcome_payloads()[0]],
        ward_id="WARD-B",
    )

    response = api.post(f"/api/v1/imports/{import_id}/commit")

    assert response.status_code == 403
    with pg_engine.connect() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM ward_twin.outcomes")
        ).scalar_one() == 0


def test_postgres_concurrent_commit_is_serialized(api, pg_engine):
    _add_encounter(pg_engine)
    import_id = _stage_import(
        pg_engine,
        "observations",
        _observation_payload(),
    )
    barrier = Barrier(2)

    def commit_once():
        with TestClient(app) as client:
            barrier.wait(timeout=10)
            return client.post(f"/api/v1/imports/{import_id}/commit")

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: commit_once(), range(2)))

    assert sorted(response.status_code for response in responses) == [200, 409]
    with pg_engine.connect() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM ward_twin.observation_readings")
        ).scalar_one() == 1
        assert connection.execute(
            text("SELECT count(*) FROM ward_twin.observation_measurements")
        ).scalar_one() == 2


def test_postgres_import_kind_migration_and_producer_migration_rerun(
    pg_engine,
):
    expand_sql = (
        MIGRATIONS_DIR / "20261120_expand_import_job_kinds.sql"
    ).read_text()
    producer_sql = (
        MIGRATIONS_DIR / "20261120_register_csv_import_producer.sql"
    ).read_text()

    _execute_script(pg_engine, expand_sql)
    _execute_script(pg_engine, producer_sql)
    _execute_script(pg_engine, producer_sql)

    with pg_engine.connect() as connection:
        producers = connection.execute(
            text("""
                SELECT producer_id, type, active, key_hash
                FROM ward_twin.producers
                WHERE ward_id IN ('WARD-A', 'WARD-TEST')
                ORDER BY producer_id
            """)
        ).mappings().all()
        assert {
            (row["producer_id"], row["type"], row["active"], row["key_hash"])
            for row in producers
        } == {
            ("local-gateway", "gateway", True, "test-key-hash"),
            ("manual-api", "manual", True, None),
            ("csv-import:WARD-A", "csv", True, None),
            ("csv-import:WARD-TEST", "csv", True, None),
        }

    bad_ward_sql = producer_sql.replace(
        "target_ward_id TEXT := 'WARD-TEST'",
        "target_ward_id TEXT := 'WARD-MISSING'",
    )
    with pytest.raises(Exception, match="does not exist"):
        _execute_script(pg_engine, bad_ward_sql)
