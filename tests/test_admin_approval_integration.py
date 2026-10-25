
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import engine
from app.services.admin_repository import approve_account


def test_approval_transaction_rollback():
    admin_id = f"TEST-ADMIN-{uuid4()}"
    user_id = f"TEST-PENDING-{uuid4()}"

    with engine.connect() as conn:
        outer = conn.begin()

        try:
            with Session(
                bind=conn,
                join_transaction_mode="create_savepoint",
            ) as db:

                # Create test-only staff profiles.
                db.execute(
                    text("""
                        INSERT INTO ward_twin.staff_profiles
                            (user_id, display_name, role)
                        VALUES
                            (:admin_id, 'Integration Admin', 'admin'),
                            (:user_id, 'Integration Nurse', 'pending')
                    """),
                    {
                        "admin_id": admin_id,
                        "user_id": user_id,
                    },
                )

                # Create approval records.
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

                # End the current Session transaction before
                # approve_account() opens its own db.begin().
                db.commit()

                result = approve_account(
                    db,
                    user_id=user_id,
                    role="nurse",
                    ward_ids=["WARD-TEST"],
                    reviewed_by=admin_id,
                )

                assert result["status"] == "approved"
                assert result["role"] == "nurse"

                state = db.execute(
                    text("""
                        SELECT sp.role, aa.status, aa.reviewed_by
                        FROM ward_twin.staff_profiles sp
                        JOIN ward_twin.account_approvals aa
                          ON aa.user_id = sp.user_id
                        WHERE sp.user_id = :user_id
                    """),
                    {"user_id": user_id},
                ).mappings().one()

                assert state["role"] == "nurse"
                assert state["status"] == "approved"
                assert state["reviewed_by"] == admin_id

                wards = db.execute(
                    text("""
                        SELECT ward_id
                        FROM ward_twin.ward_access
                        WHERE user_id = :user_id
                    """),
                    {"user_id": user_id},
                ).scalars().all()

                assert wards == ["WARD-TEST"]

                print("Admin approval: PASS")
                print("Role and status update: PASS")
                print("Ward assignment: PASS")

        finally:
            outer.rollback()
            print("Outer rollback: COMPLETE")

        # Verify cleanup using the same connection.
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
