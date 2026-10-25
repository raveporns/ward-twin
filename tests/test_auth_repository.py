from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from app.services.auth_repository import create_pending_account


def test_create_pending_account_commits_all_three_inserts():
    db = MagicMock()

    result = create_pending_account(
        db,
        email="Nurse@Example.com",
        password_hash="$argon2id$test-hash",
        display_name=" Nurse Example ",
    )

    assert db.begin.call_count == 1
    assert db.execute.call_count == 3

    assert result["email"] == "nurse@example.com"
    assert result["display_name"] == "Nurse Example"
    assert result["role"] == "pending"
    assert result["status"] == "pending"

    statements = [
        str(call.args[0])
        for call in db.execute.call_args_list
    ]

    assert "staff_profiles" in statements[0]
    assert "user_credentials" in statements[1]
    assert "account_approvals" in statements[2]

    # Transaction จบโดยไม่มี Exception
    exit_args = db.begin.return_value.__exit__.call_args.args
    assert exit_args == (None, None, None)


def test_create_pending_account_rolls_back_on_failure():
    db = MagicMock()

    db.execute.side_effect = [
        MagicMock(),
        IntegrityError(
            statement="INSERT",
            params={},
            orig=Exception("Duplicate email"),
        ),
    ]

    with pytest.raises(IntegrityError):
        create_pending_account(
            db,
            email="nurse@example.com",
            password_hash="$argon2id$test-hash",
            display_name="Nurse Example",
        )

    assert db.execute.call_count == 2

    # ตรวจว่า Transaction Context ได้รับ Exception
    db.begin.return_value.__exit__.assert_called_once()

    exit_args = db.begin.return_value.__exit__.call_args.args

    assert exit_args[0] is IntegrityError
    assert isinstance(exit_args[1], IntegrityError)