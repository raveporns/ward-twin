
from unittest.mock import MagicMock
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.database import get_db
from app.core.security import create_local_token
from app.dependencies.auth import get_current_user
from app.services.patient_response import get_workflow_status


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
    result.first.return_value = ("staff",)
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
        "assigned_to": "nurseA1",
        "acknowledged_by": None,
        "acknowledged_at": None,
        "claim_status": "claimed",
        "claimed_at": datetime(2026, 11, 17, 9, 0, tzinfo=timezone.utc),
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
        query_result(),
        query_result(one=updated),
    ]

    response = client.put(
        f"{BASE}/alerts/{ALERT_ID}/acknowledgement",
        headers=headers(),
        json={"assigned_to": "nurseA1"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == (
        "application/json; charset=utf-8"
    )
    assert response.json()["acknowledged_by"] == "nurseA1"
    update_sql = str(mock_db.execute.call_args_list[1].args[0])
    assert "assigned_to =" not in update_sql
    assert "claimed_at =" not in update_sql
    assert "claim_status =" not in update_sql
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
            "acknowledged_at": datetime(
                2026, 11, 17, 9, 0, tzinfo=timezone.utc
            ),
        }
    )

    response = client.put(
        f"{BASE}/alerts/{ALERT_ID}/acknowledgement",
        headers=headers(),
        json={},
    )

    assert response.status_code == 200
    assert response.json()["acknowledged_by"] == "nurseA1"
    mock_db.execute.assert_called_once()
    mock_db.commit.assert_called_once()


def test_acknowledge_requires_claim_by_current_nurse(mock_db):
    mock_db.execute.return_value = query_result(
        one={
            "alert_id": ALERT_ID,
            "encounter_id": "ENC-MOCK-A01",
            "ward_id": "WARD-A",
            "state": "open",
            "acknowledged_by": None,
            "acknowledged_at": None,
            "assigned_to": None,
            "claim_status": "unclaimed",
            "claimed_at": None,
        }
    )

    response = client.put(
        f"{BASE}/alerts/{ALERT_ID}/acknowledgement",
        headers=headers(),
        json={},
    )

    assert response.status_code == 403
    mock_db.execute.assert_called_once()
    mock_db.commit.assert_not_called()
    mock_db.rollback.assert_called_once()


def test_claimed_nurse_cannot_be_replaced_during_acknowledgement(mock_db):
    mock_db.execute.return_value = query_result(
        one={
            "alert_id": ALERT_ID,
            "encounter_id": "ENC-MOCK-A01",
            "ward_id": "WARD-A",
            "state": "open",
            "acknowledged_by": None,
            "acknowledged_at": None,
            "assigned_to": "nurseB1",
            "claim_status": "claimed",
            "claimed_at": datetime(2026, 11, 17, 9, 0, tzinfo=timezone.utc),
        }
    )

    response = client.put(
        f"{BASE}/alerts/{ALERT_ID}/acknowledgement",
        headers=headers(),
        json={},
    )

    assert response.status_code == 403
    mock_db.execute.assert_called_once()
    mock_db.commit.assert_not_called()


def test_acknowledge_does_not_change_assignee_when_payload_targets_other_user(
    mock_db,
):
    mock_db.execute.return_value = query_result(
        one={
            "alert_id": ALERT_ID,
            "encounter_id": "ENC-MOCK-A01",
            "ward_id": "WARD-A",
            "state": "open",
            "acknowledged_by": None,
            "acknowledged_at": None,
            "assigned_to": "nurseA1",
            "claim_status": "claimed",
            "claimed_at": datetime(2026, 11, 17, 9, 0, tzinfo=timezone.utc),
        }
    )

    response = client.put(
        f"{BASE}/alerts/{ALERT_ID}/acknowledgement",
        headers=headers(),
        json={"assigned_to": "nurseB1"},
    )

    assert response.status_code == 422
    mock_db.execute.assert_called_once()
    assert "FOR UPDATE OF a" in str(
        mock_db.execute.call_args.args[0]
    )


def test_acknowledged_legacy_alert_cannot_be_claimed_or_backfilled(mock_db):
    mock_db.execute.return_value = query_result(
        one=claimable_alert(
            assigned_to="nurseA1",
            acknowledged_by="nurseA1",
        )
    )

    response = client.post(
        f"{BASE}/alerts/{ALERT_ID}/claim",
        headers=headers(),
    )

    assert response.status_code == 409
    mock_db.execute.assert_called_once()
    mock_db.commit.assert_not_called()


def test_legacy_assignment_can_only_be_claimed_by_existing_assignee(mock_db):
    claimed_at = datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc)
    mock_db.execute.side_effect = [
        query_result(one=claimable_alert(assigned_to="nurseA1")),
        query_result(one={
            "alert_id": ALERT_ID,
            "claim_status": "claimed",
            "assigned_to": "nurseA1",
            "claimed_at": claimed_at,
        }),
    ]

    response = client.post(
        f"{BASE}/alerts/{ALERT_ID}/claim",
        headers=headers(),
    )

    assert response.status_code == 200
    update_sql = str(mock_db.execute.call_args_list[1].args[0])
    assert "claimed_at = CURRENT_TIMESTAMP" in update_sql
    assert "assigned_to = :assigned_to" in update_sql
    assert mock_db.execute.call_args_list[1].args[1]["assigned_to"] == "nurseA1"


def test_claim_acknowledge_assess_workflow_sequence(mock_db):
    state = claimable_alert()
    state["has_assessment"] = False

    def execute(statement, params=None):
        sql = str(statement)
        if "FOR UPDATE OF a" in sql:
            return query_result(one=dict(state))
        if "FROM ward_twin.staff_profiles" in sql:
            result = MagicMock()
            result.first.return_value = ("nurseA1",)
            return result
        if "claim_status = 'unclaimed'" in sql:
            state["assigned_to"] = params["assigned_to"]
            state["claim_status"] = "claimed"
            state["claimed_at"] = datetime(
                2026, 11, 17, 10, 0, tzinfo=timezone.utc
            )
            return query_result(one={
                "alert_id": ALERT_ID,
                "claim_status": state["claim_status"],
                "assigned_to": state["assigned_to"],
                "claimed_at": state["claimed_at"],
            })
        if "SET acknowledged_by = :actor_id" in sql:
            state["acknowledged_by"] = params["actor_id"]
            state["acknowledged_at"] = params["acknowledged_at"]
            return query_result(one={
                "alert_id": ALERT_ID,
                "encounter_id": "ENC-MOCK-A01",
                "state": state["state"],
                "assigned_to": state["assigned_to"],
                "acknowledged_by": state["acknowledged_by"],
                "acknowledged_at": state["acknowledged_at"],
            })
        raise AssertionError(f"Unexpected query: {sql}")

    mock_db.execute.side_effect = execute

    claim_response = client.post(
        f"{BASE}/alerts/{ALERT_ID}/claim",
        headers=headers(),
    )
    assert claim_response.status_code == 200
    assert get_workflow_status([state]) == "pending_acknowledgement"

    acknowledgement_response = client.put(
        f"{BASE}/alerts/{ALERT_ID}/acknowledgement",
        headers=headers(),
        json={},
    )
    assert acknowledgement_response.status_code == 200
    assert state["assigned_to"] == "nurseA1"
    assert state["claim_status"] == "claimed"
    assert get_workflow_status([state]) == "pending_assessment"

    state["has_assessment"] = True
    assert get_workflow_status([state]) == "assessed"


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


def claimable_alert(**updates):
    return {
        "alert_id": ALERT_ID,
        "state": "open",
        "assigned_to": None,
        "acknowledged_by": None,
        "claim_status": "unclaimed",
        "claimed_at": None,
        "ward_id": "WARD-A",
        **updates,
    }


def test_claim_alert_success(mock_db):
    claimed_at = datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc)
    mock_db.execute.side_effect = [
        query_result(one=claimable_alert()),
        query_result(one={
            "alert_id": ALERT_ID,
            "claim_status": "claimed",
            "assigned_to": "nurseA1",
            "claimed_at": claimed_at,
        }),
    ]

    response = client.post(
        f"{BASE}/alerts/{ALERT_ID}/claim",
        headers=headers(),
        json={"assigned_to": "nurseB1"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == (
        "application/json; charset=utf-8"
    )
    assert response.json() == {
        "alert_id": ALERT_ID,
        "status": "claimed",
        "assigned_to": "nurseA1",
        "claimed_at": "2026-11-17T10:00:00Z",
    }
    select_sql = str(mock_db.execute.call_args_list[0].args[0])
    update_sql = str(mock_db.execute.call_args_list[1].args[0])
    assert "FOR UPDATE OF a" in select_sql
    assert "a.acknowledged_by" in select_sql
    assert "claim_status = 'unclaimed'" in update_sql
    assert "acknowledged_by" not in update_sql
    assert mock_db.execute.call_args_list[1].args[1] == {
        "alert_id": ALERT_ID,
        "assigned_to": "nurseA1",
    }
    mock_db.commit.assert_called_once()


@pytest.mark.parametrize("role", ["admin", "doctor", "viewer"])
def test_non_nurse_cannot_claim_alert(mock_db, role):
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": f"{role}-test",
        "role": role,
        "wards": ["WARD-A"],
        "token_type": "access",
    }

    try:
        response = client.post(
            f"{BASE}/alerts/{ALERT_ID}/claim",
        )

        assert response.status_code == 403
        mock_db.execute.assert_not_called()
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_claim_alert_requires_authentication(mock_db):
    response = client.post(f"{BASE}/alerts/{ALERT_ID}/claim")

    assert response.status_code == 401
    mock_db.execute.assert_not_called()


def test_nurse_without_ward_access_cannot_claim_alert(mock_db):
    mock_db.execute.return_value = query_result(
        one=claimable_alert()
    )

    response = client.post(
        f"{BASE}/alerts/{ALERT_ID}/claim",
        headers=headers(["WARD-B"]),
    )

    assert response.status_code == 403
    mock_db.commit.assert_not_called()
    mock_db.rollback.assert_called_once()


def test_claim_missing_alert_returns_404(mock_db):
    mock_db.execute.return_value = query_result(one=None)

    response = client.post(
        f"{BASE}/alerts/{ALERT_ID}/claim",
        headers=headers(),
    )

    assert response.status_code == 404
    mock_db.rollback.assert_called_once()


def test_alert_claimed_by_another_nurse_returns_409(mock_db):
    mock_db.execute.return_value = query_result(
        one=claimable_alert(
            assigned_to="nurseB1",
            claim_status="claimed",
            claimed_at=datetime(2026, 11, 17, 9, 0, tzinfo=timezone.utc),
        )
    )

    response = client.post(
        f"{BASE}/alerts/{ALERT_ID}/claim",
        headers=headers(),
    )

    assert response.status_code == 409
    mock_db.execute.assert_called_once()
    mock_db.rollback.assert_called_once()


def test_same_nurse_reclaim_is_idempotent(mock_db):
    claimed_at = datetime(2026, 11, 17, 9, 0, tzinfo=timezone.utc)
    mock_db.execute.return_value = query_result(
        one=claimable_alert(
            assigned_to="nurseA1",
            claim_status="claimed",
            claimed_at=claimed_at,
        )
    )

    response = client.post(
        f"{BASE}/alerts/{ALERT_ID}/claim",
        headers=headers(),
    )

    assert response.status_code == 200
    assert response.json()["claimed_at"] == "2026-11-17T09:00:00Z"
    assert response.json()["assigned_to"] == "nurseA1"
    mock_db.execute.assert_called_once()
    mock_db.commit.assert_called_once()


def test_concurrent_claim_losing_conditional_update_returns_409(mock_db):
    mock_db.execute.side_effect = [
        query_result(one=claimable_alert()),
        query_result(one=None),
    ]

    response = client.post(
        f"{BASE}/alerts/{ALERT_ID}/claim",
        headers=headers(),
    )

    assert response.status_code == 409
    update_sql = str(mock_db.execute.call_args_list[1].args[0])
    assert "claim_status = 'unclaimed'" in update_sql
    assert "assigned_to IS NULL" in update_sql
    mock_db.commit.assert_not_called()
    mock_db.rollback.assert_called_once()


def test_claim_database_failure_rolls_back(mock_db):
    from sqlalchemy.exc import OperationalError

    mock_db.execute.side_effect = OperationalError(
        statement="SELECT",
        params={},
        orig=Exception("database unavailable"),
    )

    response = client.post(
        f"{BASE}/alerts/{ALERT_ID}/claim",
        headers=headers(),
    )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Alert claim temporarily unavailable"
    )
    mock_db.rollback.assert_called_once()


def test_claim_commit_failure_rolls_back_updated_claim(mock_db):
    from sqlalchemy.exc import OperationalError

    claimed_at = datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc)
    mock_db.execute.side_effect = [
        query_result(one=claimable_alert()),
        query_result(one={
            "alert_id": ALERT_ID,
            "claim_status": "claimed",
            "assigned_to": "nurseA1",
            "claimed_at": claimed_at,
        }),
    ]
    mock_db.commit.side_effect = OperationalError(
        statement="COMMIT",
        params={},
        orig=Exception("commit failed"),
    )

    response = client.post(
        f"{BASE}/alerts/{ALERT_ID}/claim",
        headers=headers(),
    )

    assert response.status_code == 503
    mock_db.rollback.assert_called_once()
