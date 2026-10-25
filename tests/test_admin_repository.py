
from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.services.admin_repository import list_pending_accounts


def test_list_pending_accounts_success():
    db = MagicMock()

    mock_rows = [
        {
            "user_id": "USER-001",
            "display_name": "Test Nurse",
            "email": "nurse@example.com",
            "role": "pending",
            "status": "pending",
            "created_at": datetime(
                2026, 10, 26, tzinfo=timezone.utc
            ),
        },
        {
            "user_id": "USER-002",
            "display_name": "Test Doctor",
            "email": "doctor@example.com",
            "role": "pending",
            "status": "pending",
            "created_at": datetime(
                2026, 10, 26, tzinfo=timezone.utc
            ),
        },
    ]

    db.execute.return_value.mappings.return_value.all.return_value = mock_rows

    result = list_pending_accounts(db)

    assert len(result) == 2
    assert result[0]["user_id"] == "USER-001"
    assert result[1]["user_id"] == "USER-002"

    db.execute.assert_called_once()


def test_list_pending_accounts_empty():
    db = MagicMock()

    db.execute.return_value.mappings.return_value.all.return_value = []

    result = list_pending_accounts(db)

    assert result == []


def test_list_pending_accounts_excludes_password_hash():
    db = MagicMock()

    db.execute.return_value.mappings.return_value.all.return_value = [
        {
            "user_id": "USER-001",
            "display_name": "Test Nurse",
            "email": "nurse@example.com",
            "role": "pending",
            "status": "pending",
            "created_at": datetime.now(timezone.utc),
        }
    ]

    result = list_pending_accounts(db)

    assert "password_hash" not in result[0]

    sql = str(db.execute.call_args.args[0])

    assert "password_hash" not in sql.lower()


def test_list_pending_accounts_filters_pending():
    db = MagicMock()

    db.execute.return_value.mappings.return_value.all.return_value = []

    list_pending_accounts(db)

    sql = str(db.execute.call_args.args[0]).lower()

    assert "aa.status = 'pending'" in sql
    assert "sp.role = 'pending'" in sql
