from datetime import datetime, timezone
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.main import app


def query_result(*, row=None, rows=None):
    result = MagicMock()
    result.mappings.return_value.first.return_value = row
    result.mappings.return_value.all.return_value = rows or []
    return result


def test_patient_twin_exposes_same_current_alert_claim_data():
    app.dependency_overrides.clear()
    db = MagicMock()
    claimed_at = datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc)
    current_alert = {
        "alert_id": "ALT-CURRENT",
        "encounter_id": "ENC-TWIN-1",
        "state": "open",
        "created_at": datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc),
        "acknowledged_by": None,
        "assigned_to": "nurse-1",
        "assigned_name": "Nurse One",
        "claim_status": "claimed",
        "claimed_at": claimed_at,
        "has_assessment": False,
    }

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "nurse-1",
        "role": "nurse",
        "wards": ["WARD-A"],
        "token_type": "access",
    }
    db.execute.side_effect = [
        query_result(row={
            "encounter_id": "ENC-TWIN-1",
            "patient_id": "PAT-TWIN-1",
            "hn": "HN-TWIN-1",
            "display_name": "Synthetic Patient",
            "birth_date": None,
            "sex": "unknown",
            "allergy_status": "unknown",
            "an": "AN-TWIN-1",
            "ward_id": "WARD-A",
            "bed_label": "A-01",
            "admitted_at": None,
        }),
        query_result(rows=[]),
        query_result(row=None),
        query_result(row=None),
        query_result(rows=[current_alert]),
    ]

    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/encounters/ENC-TWIN-1/twin"
            )

        assert response.status_code == 200
        assert response.headers["content-type"] == (
            "application/json; charset=utf-8"
        )
        data = response.json()
        assert data["current_alert"] == {
            "alert_id": "ALT-CURRENT",
            "assigned_to": "nurse-1",
            "assigned_name": "Nurse One",
            "claim_status": "claimed",
            "claimed_at": "2026-11-17T10:00:00Z",
        }
        claim_query = str(db.execute.call_args_list[4].args[0])
        assert "ORDER BY" in claim_query
        assert "a.created_at DESC NULLS LAST" in claim_query
        assert "a.alert_id DESC" in claim_query
    finally:
        app.dependency_overrides.clear()
