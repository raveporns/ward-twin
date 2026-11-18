
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.database import get_db
from app.core.security import create_local_token
from app.dependencies.auth import get_current_user


client = TestClient(app)
BASE = "/api/v1"
ENCOUNTER = "ENC-MOCK-A01"
ALERT = "ALT-TEST-0009"
RECORD_ID = "c5c2e4e8-6a24-4b92-b5b1-872be8db1001"


def headers(wards=None, role="nurse"):
    token = create_local_token(
        "nurseA1",
        role,
        wards if wards is not None else ["WARD-A"],
    )
    return {"Authorization": f"Bearer {token}"}


def payload(note="Test assessment"):
    return {
        "client_record_id": RECORD_ID,
        "alert_id": ALERT,
        "note": note,
    }


def stored_row(note="Test assessment"):
    return {
        "assessment_id": UUID("206b9dd7-ddef-414c-a570-393417f43bfd"),
        "encounter_id": ENCOUNTER,
        "alert_id": ALERT,
        "client_record_id": UUID(RECORD_ID),
        "note": note,
        "created_by": "nurseA1",
        "assessed_by_name": None,
        "created_at": None,
    }


@pytest.fixture
def mock_db():
    db = MagicMock()

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    yield db
    app.dependency_overrides.pop(get_db, None)


def query_result(*, one=None, scalar=None, all_rows=None):
    result = MagicMock()
    result.mappings.return_value.one_or_none.return_value = one
    result.mappings.return_value.one.return_value = one
    result.mappings.return_value.all.return_value = all_rows or []
    result.scalar_one_or_none.return_value = scalar
    return result


def test_create_assessment_requires_auth(mock_db):
    response = client.post(
        f"{BASE}/encounters/{ENCOUNTER}/assessments",
        json=payload(),
    )
    assert response.status_code == 401
    mock_db.execute.assert_not_called()


def test_create_assessment_wrong_ward(mock_db):
    mock_db.execute.return_value = query_result(
        one={"encounter_id": ENCOUNTER, "ward_id": "WARD-A"}
    )

    response = client.post(
        f"{BASE}/encounters/{ENCOUNTER}/assessments",
        headers=headers(["WARD-B"]),
        json=payload(),
    )

    assert response.status_code == 403
    mock_db.commit.assert_not_called()


def test_create_assessment_success(mock_db):
    mock_db.execute.side_effect = [
        query_result(one={"encounter_id": ENCOUNTER, "ward_id": "WARD-A"}),
        query_result(one=None),
        query_result(scalar=ALERT),
        query_result(one=stored_row()),
    ]

    response = client.post(
        f"{BASE}/encounters/{ENCOUNTER}/assessments",
        headers=headers(),
        json=payload(),
    )

    assert response.status_code == 201
    assert response.json()["created_by"] == "nurseA1"
    assert response.json()["alert_id"] == ALERT
    mock_db.commit.assert_called_once()


def test_viewer_cannot_create_assessment(mock_db):
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "viewerA1",
        "role": "viewer",
        "wards": ["WARD-A"],
        "token_type": "access",
    }

    try:
        response = client.post(
            f"{BASE}/encounters/{ENCOUNTER}/assessments",
            json=payload(),
        )

        assert response.status_code == 403
        mock_db.execute.assert_not_called()
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_create_assessment_idempotent(mock_db):
    mock_db.execute.side_effect = [
        query_result(one={"encounter_id": ENCOUNTER, "ward_id": "WARD-A"}),
        query_result(one=stored_row()),
    ]

    response = client.post(
        f"{BASE}/encounters/{ENCOUNTER}/assessments",
        headers=headers(),
        json=payload(),
    )

    assert response.status_code == 201
    assert response.json()["assessment_id"] == str(stored_row()["assessment_id"])
    mock_db.commit.assert_not_called()


def test_create_assessment_conflict(mock_db):
    mock_db.execute.side_effect = [
        query_result(one={"encounter_id": ENCOUNTER, "ward_id": "WARD-A"}),
        query_result(one=stored_row("Original assessment")),
    ]

    response = client.post(
        f"{BASE}/encounters/{ENCOUNTER}/assessments",
        headers=headers(),
        json=payload("Changed assessment"),
    )

    assert response.status_code == 409
    mock_db.commit.assert_not_called()


def test_get_assessments_success(mock_db):
    row = stored_row()
    row["assessed_by_name"] = "พยาบาล A1"
    mock_db.execute.side_effect = [
        query_result(one={"encounter_id": ENCOUNTER, "ward_id": "WARD-A"}),
        query_result(all_rows=[row]),
    ]

    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "viewerA1",
        "role": "viewer",
        "wards": ["WARD-A"],
        "token_type": "access",
    }

    try:
        response = client.get(
            f"{BASE}/encounters/{ENCOUNTER}/assessments",
        )

        assert response.status_code == 200
        assert response.headers["content-type"] == (
            "application/json; charset=utf-8"
        )
        assert len(response.json()["assessments"]) == 1
        item = response.json()["assessments"][0]
        assert item["created_by"] == "nurseA1"
        assert item["assessed_by_name"] == "พยาบาล A1"
        assert set(item) == {
            "assessment_id",
            "encounter_id",
            "alert_id",
            "client_record_id",
            "note",
            "created_by",
            "assessed_by_name",
            "created_at",
        }
        assert "พยาบาล A1".encode("utf-8") in response.content
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.parametrize(
    ("created_by", "display_name", "match_column"),
    [
        ("nurseA1", "พยาบาล A1", "staff.user_id::text"),
        ("ADMIN-001", "ผู้ดูแลระบบ", "staff.user_id::text"),
        (
            "206b9dd7-ddef-414c-a570-393417f43bfd",
            "แพทย์ UUID",
            "auth_user_id",
        ),
    ],
)
def test_get_assessments_resolves_assessor_by_exact_identifier(
    mock_db,
    created_by,
    display_name,
    match_column,
):
    row = stored_row()
    row["created_by"] = created_by
    row["assessed_by_name"] = display_name
    mock_db.execute.side_effect = [
        query_result(one={"encounter_id": ENCOUNTER, "ward_id": "WARD-A"}),
        query_result(all_rows=[row]),
    ]

    response = client.get(
        f"{BASE}/encounters/{ENCOUNTER}/assessments",
        headers=headers(),
    )

    assert response.status_code == 200
    assessment = response.json()["assessments"][0]
    assert assessment["created_by"] == created_by
    assert assessment["assessed_by_name"] == display_name
    sql = str(mock_db.execute.call_args_list[1].args[0])
    assert "LEFT JOIN LATERAL" in sql
    assert match_column in sql
    assert "LIMIT 1" in sql


def test_get_assessments_missing_assessor_returns_null_without_dropping_id(
    mock_db,
):
    row = stored_row()
    row["assessed_by_name"] = None
    mock_db.execute.side_effect = [
        query_result(one={"encounter_id": ENCOUNTER, "ward_id": "WARD-A"}),
        query_result(all_rows=[row]),
    ]

    response = client.get(
        f"{BASE}/encounters/{ENCOUNTER}/assessments",
        headers=headers(),
    )

    assert response.status_code == 200
    assessment = response.json()["assessments"][0]
    assert assessment["created_by"] == "nurseA1"
    assert assessment["assessed_by_name"] is None


def test_multiple_assessments_remain_distinct_and_ordered(mock_db):
    newest = stored_row("Newest")
    newest["assessment_id"] = UUID(
        "306b9dd7-ddef-414c-a570-393417f43bfd"
    )
    newest["assessed_by_name"] = "พยาบาลใหม่"
    oldest = stored_row("Oldest")
    oldest["assessed_by_name"] = "พยาบาลเก่า"
    mock_db.execute.side_effect = [
        query_result(one={"encounter_id": ENCOUNTER, "ward_id": "WARD-A"}),
        query_result(all_rows=[newest, oldest]),
    ]

    response = client.get(
        f"{BASE}/encounters/{ENCOUNTER}/assessments",
        headers=headers(),
    )

    assert response.status_code == 200
    assessments = response.json()["assessments"]
    assert len(assessments) == 2
    assert [item["note"] for item in assessments] == ["Newest", "Oldest"]
    assert [item["assessed_by_name"] for item in assessments] == [
        "พยาบาลใหม่",
        "พยาบาลเก่า",
    ]
    assert len({item["assessment_id"] for item in assessments}) == 2


def test_get_assessments_requires_jwt(mock_db):
    response = client.get(
        f"{BASE}/encounters/{ENCOUNTER}/assessments",
    )

    assert response.status_code == 401
    mock_db.execute.assert_not_called()


def test_get_assessments_wrong_ward(mock_db):
    mock_db.execute.return_value = query_result(
        one={"encounter_id": ENCOUNTER, "ward_id": "WARD-A"}
    )

    response = client.get(
        f"{BASE}/encounters/{ENCOUNTER}/assessments",
        headers=headers(["WARD-B"]),
    )

    assert response.status_code == 403
