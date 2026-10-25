
from unittest.mock import MagicMock

import pytest

from app.services.admin_repository import approve_account


@pytest.mark.parametrize(
    "role",
    ["admin", "pending", "invalid", ""],
)
def test_reject_invalid_role(role):
    db = MagicMock()

    with pytest.raises(ValueError, match="Invalid role"):
        approve_account(
            db,
            user_id="USER-001",
            role=role,
            ward_ids=["WARD-TEST"],
            reviewed_by="ADMIN-001",
        )

    db.begin.assert_not_called()
    db.execute.assert_not_called()


@pytest.mark.parametrize(
    "ward_ids",
    [
        [],
        [""],
        ["   "],
        ["WARD-TEST", "WARD-TEST"],
        [" WARD-TEST "],
        ["WARD-TEST"] * 21,
    ],
)
def test_reject_invalid_ward_ids(ward_ids):
    db = MagicMock()

    with pytest.raises(ValueError, match="Invalid ward IDs"):
        approve_account(
            db,
            user_id="USER-001",
            role="nurse",
            ward_ids=ward_ids,
            reviewed_by="ADMIN-001",
        )

    db.begin.assert_not_called()
    db.execute.assert_not_called()


@pytest.mark.parametrize(
    "user_id,reviewed_by",
    [
        ("", "ADMIN-001"),
        ("USER-001", ""),
    ],
)
def test_reject_missing_identity(user_id, reviewed_by):
    db = MagicMock()

    with pytest.raises(
        ValueError,
        match="User ID and reviewer are required",
    ):
        approve_account(
            db,
            user_id=user_id,
            role="nurse",
            ward_ids=["WARD-TEST"],
            reviewed_by=reviewed_by,
        )

    db.begin.assert_not_called()
    db.execute.assert_not_called()


def make_db(
    *,
    account_exists=True,
    reviewer_exists=True,
    wards=None,
):
    db = MagicMock()

    if wards is None:
        wards = ["WARD-TEST"]

    account_result = MagicMock()
    account_result.mappings.return_value.first.return_value = (
        {"user_id": "USER-001"} if account_exists else None
    )

    reviewer_result = MagicMock()
    reviewer_result.mappings.return_value.first.return_value = (
        {"user_id": "ADMIN-001"} if reviewer_exists else None
    )

    wards_result = MagicMock()
    wards_result.scalars.return_value.all.return_value = wards

    # New SQL order:
    # 1. Reviewer
    # 2. Target account
    # 3. Wards
    # 4. UPDATE staff_profiles
    # 5. UPDATE account_approvals
    # 6. INSERT ward_access
    db.execute.side_effect = [
        reviewer_result,
        account_result,
        wards_result,
        MagicMock(),
        MagicMock(),
        MagicMock(),
    ]

    return db


def test_approve_account_success():
    db = make_db()

    result = approve_account(
        db,
        user_id="USER-001",
        role="nurse",
        ward_ids=["WARD-TEST"],
        reviewed_by="ADMIN-001",
    )

    assert result == {
        "user_id": "USER-001",
        "role": "nurse",
        "status": "approved",
        "ward_ids": ["WARD-TEST"],
        "reviewed_by": "ADMIN-001",
    }

    db.begin.assert_called_once()
    assert db.execute.call_count == 6

    sql_calls = [
        str(call.args[0]).lower()
        for call in db.execute.call_args_list
    ]

    assert "for share of sp, aa" in sql_calls[0]
    assert "for update of sp, aa" in sql_calls[1]
    assert "update ward_twin.staff_profiles" in sql_calls[3]
    assert "update ward_twin.account_approvals" in sql_calls[4]
    assert "insert into ward_twin.ward_access" in sql_calls[5]


def test_reject_account_not_pending():
    db = make_db(account_exists=False)

    with pytest.raises(ValueError, match="Account is not pending"):
        approve_account(
            db,
            user_id="USER-001",
            role="nurse",
            ward_ids=["WARD-TEST"],
            reviewed_by="ADMIN-001",
        )

    assert db.execute.call_count == 2


def test_reject_unauthorized_reviewer():
    db = make_db(reviewer_exists=False)

    with pytest.raises(
        PermissionError,
        match="Reviewer is not an approved admin",
    ):
        approve_account(
            db,
            user_id="USER-001",
            role="nurse",
            ward_ids=["WARD-TEST"],
            reviewed_by="ADMIN-001",
        )

    assert db.execute.call_count == 1


def test_reject_nonexistent_ward():
    db = make_db(wards=[])

    with pytest.raises(
        ValueError,
        match="One or more wards do not exist",
    ):
        approve_account(
            db,
            user_id="USER-001",
            role="nurse",
            ward_ids=["WARD-TEST"],
            reviewed_by="ADMIN-001",
        )

    assert db.execute.call_count == 3



def test_ward_insert_failure_propagates():
    db = make_db()

    # Convert iterator back to list before modifying.
    original_effects = list(db.execute.side_effect)

    db.execute.side_effect = [
        *original_effects[:5],
        RuntimeError("Simulated insert failure"),
    ]

    with pytest.raises(
        RuntimeError,
        match="Simulated insert failure",
    ):
        approve_account(
            db,
            user_id="USER-001",
            role="nurse",
            ward_ids=["WARD-TEST"],
            reviewed_by="ADMIN-001",
        )

    assert db.execute.call_count == 6

    # Verify transaction context receives the exception.
    db.begin.return_value.__exit__.assert_called_once()

    exit_args = db.begin.return_value.__exit__.call_args.args
    assert exit_args[0] is RuntimeError


def test_approval_uses_transaction():
    db = make_db()

    approve_account(
        db,
        user_id="USER-001",
        role="nurse",
        ward_ids=["WARD-TEST"],
        reviewed_by="ADMIN-001",
    )

    db.begin.assert_called_once()

    transaction = db.begin.return_value

    transaction.__enter__.assert_called_once()
    transaction.__exit__.assert_called_once()

    exit_args = transaction.__exit__.call_args.args
    assert exit_args == (None, None, None)


def test_approval_locks_target_account():
    db = make_db()

    approve_account(
        db,
        user_id="USER-001",
        role="nurse",
        ward_ids=["WARD-TEST"],
        reviewed_by="ADMIN-001",
    )

    first_sql = str(
        db.execute.call_args_list[1].args[0]
    ).lower()

    assert "for update of sp, aa" in first_sql
    assert "aa.status = 'pending'" in first_sql
    assert "sp.role = 'pending'" in first_sql


from sqlalchemy.exc import OperationalError


def test_database_error_during_account_lookup():
    db = MagicMock()

    db.execute.side_effect = OperationalError(
        "SELECT account",
        {},
        Exception("Database unavailable"),
    )

    with pytest.raises(OperationalError):
        approve_account(
            db,
            user_id="USER-001",
            role="nurse",
            ward_ids=["WARD-TEST"],
            reviewed_by="ADMIN-001",
        )

    assert db.execute.call_count == 1

    transaction = db.begin.return_value
    transaction.__exit__.assert_called_once()

    exit_args = transaction.__exit__.call_args.args
    assert exit_args[0] is OperationalError


def test_database_error_during_ward_insert():
    db = make_db()

    original_effects = list(db.execute.side_effect)

    db.execute.side_effect = [
        *original_effects[:5],
        OperationalError(
            "INSERT ward_access",
            {},
            Exception("Database unavailable"),
        ),
    ]

    with pytest.raises(OperationalError):
        approve_account(
            db,
            user_id="USER-001",
            role="nurse",
            ward_ids=["WARD-TEST"],
            reviewed_by="ADMIN-001",
        )

    assert db.execute.call_count == 6

    transaction = db.begin.return_value
    transaction.__exit__.assert_called_once()

    exit_args = transaction.__exit__.call_args.args
    assert exit_args[0] is OperationalError


def test_approval_locks_reviewer_permissions():
    db = make_db()

    approve_account(
        db,
        user_id="USER-001",
        role="nurse",
        ward_ids=["WARD-TEST"],
        reviewed_by="ADMIN-001",
    )

    reviewer_sql = str(
        db.execute.call_args_list[0].args[0]
    ).lower()

    assert "sp.role = 'admin'" in reviewer_sql
    assert "aa.status = 'approved'" in reviewer_sql
    assert "for share of sp, aa" in reviewer_sql



def test_self_approval_rejected():
    db = MagicMock()

    with pytest.raises(
        PermissionError,
        match="Self-approval is not allowed",
    ):
        approve_account(
            db,
            user_id="USER-001",
            role="nurse",
            ward_ids=["WARD-TEST"],
            reviewed_by="USER-001",
        )

    db.begin.assert_not_called()
    db.execute.assert_not_called()


def test_nonexistent_ward_rolls_back_transaction():
    db = make_db(wards=[])

    with pytest.raises(
        ValueError,
        match="One or more wards do not exist",
    ):
        approve_account(
            db,
            user_id="USER-001",
            role="nurse",
            ward_ids=["WARD-NOT-EXIST"],
            reviewed_by="ADMIN-001",
        )

    # Reviewer, target account, ward lookup
    assert db.execute.call_count == 3

    # No UPDATE or INSERT should be executed.
    sql_calls = [
        str(call.args[0]).lower()
        for call in db.execute.call_args_list
    ]

    assert not any(
        "update ward_twin." in sql
        or "insert into ward_twin." in sql
        for sql in sql_calls
    )

    transaction = db.begin.return_value
    transaction.__exit__.assert_called_once()

    exit_args = transaction.__exit__.call_args.args
    assert exit_args[0] is ValueError
