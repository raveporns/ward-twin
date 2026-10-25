
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import engine
from app.services.admin_repository import approve_account


def test_approval_insert_failure_rolls_back():
    admin_id = f"TEST-ADMIN-{uuid4()}"
    user_id = f"TEST-PENDING-{uuid4()}"

    with engine.connect() as conn:
        outer = conn.begin()

        try:
            with Session(
                bind=conn,
                join_transaction_mode="create_savepoint",
            ) as db:

                # Create temporary test accounts.
                db.execute(
                    text("""
                        INSERT INTO ward_twin.staff_profiles
                            (user_id, display_name, role)
                        VALUES
                            (:admin_id, 'Rollback Test Admin', 'admin'),
                            (:user_id, 'Rollback Test Nurse', 'pending')
                    """),
                    {
                        "admin_id": admin_id,
                        "user_id": user_id,
                    },
                )

                db.execute(
                    text("""
                        INSERT INTO ward_twin.account_approvals
                            (user_id, status)
                        VALUES
                            (:admin_id, 'approved'),
                            (:user_id, 'pending')
                    """),
                    {
                        "admin_id": admin_id,
                        "user_id": user_id,
                    },
                )

                # Pre-insert ward access to trigger a duplicate PK.
                db.execute(
                    text("""
                        INSERT INTO ward_twin.ward_access
                            (user_id, ward_id)
                        VALUES
                            (:user_id, 'WARD-TEST')
                    """),
                    {"user_id": user_id},
                )

                # Release the Session SAVEPOINT, but keep
                # the outer transaction uncommitted.
                db.commit()

                try:
                    approve_account(
                        db,
                        user_id=user_id,
                        role="nurse",
                        ward_ids=["WARD-TEST"],
                        reviewed_by=admin_id,
                    )
                except IntegrityError:
                    print("Duplicate ward insert rejected: PASS")
                else:
                    raise AssertionError(
                        "Expected duplicate ward IntegrityError"
                    )

                # Verify updates inside approve_account were rolled back.
                state = db.execute(
                    text("""
                        SELECT sp.role, aa.status
                        FROM ward_twin.staff_profiles sp
                        JOIN ward_twin.account_approvals aa
                          ON aa.user_id = sp.user_id
                        WHERE sp.user_id = :user_id
                    """),
                    {"user_id": user_id},
                ).mappings().one()

                assert state["role"] == "pending"
                assert state["status"] == "pending"

                print("Role rollback: PASS")
                print("Approval status rollback: PASS")

                ward_count = db.execute(
                    text("""
                        SELECT COUNT(*)
                        FROM ward_twin.ward_access
                        WHERE user_id = :user_id
                    """),
                    {"user_id": user_id},
                ).scalar_one()

                # The pre-existing test ward remains,
                # but no additional ward was inserted.
                assert ward_count == 1

                print("Ward insert rollback: PASS")

        finally:
            outer.rollback()
            print("Outer rollback: COMPLETE")

        # Verify all temporary test records were removed.
        for table in [
            "staff_profiles",
            "account_approvals",
            "ward_access",
        ]:
            count = conn.execute(
                text(f"""
                    SELECT COUNT(*)
                    FROM ward_twin.{table}
                    WHERE user_id IN (:admin_id, :user_id)
                """),
                {
                    "admin_id": admin_id,
                    "user_id": user_id,
                },
            ).scalar_one()

            assert count == 0, (
                f"Test data remains in {table}"
            )

            print(f"{table} cleanup: PASS")

        conn.rollback()
