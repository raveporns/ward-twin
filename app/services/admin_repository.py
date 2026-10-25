
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session


def list_pending_accounts(db: Session) -> list[dict]:
    result = db.execute(
        text("""
            SELECT
                sp.user_id,
                sp.display_name,
                uc.email,
                sp.role,
                aa.status,
                aa.created_at
            FROM ward_twin.account_approvals AS aa
            JOIN ward_twin.staff_profiles AS sp
                ON sp.user_id = aa.user_id
            JOIN ward_twin.user_credentials AS uc
                ON uc.user_id = aa.user_id
            WHERE aa.status = 'pending'
              AND sp.role = 'pending'
            ORDER BY aa.created_at ASC, sp.user_id ASC
        """)
    ).mappings().all()

    return [dict(row) for row in result]


def approve_account(
    db: Session,
    *,
    user_id: str,
    role: str,
    ward_ids: list[str],
    reviewed_by: str,
) -> dict:
    allowed_roles = {"viewer", "nurse", "doctor"}

    # Validate role.
    if role not in allowed_roles:
        raise ValueError("Invalid role")

    # Validate ward IDs.
    if (
        not ward_ids
        or len(ward_ids) > 20
        or any(
            not isinstance(ward_id, str)
            or not ward_id.strip()
            or ward_id != ward_id.strip()
            for ward_id in ward_ids
        )
        or len(set(ward_ids)) != len(ward_ids)
    ):
        raise ValueError("Invalid ward IDs")

    # Validate identities.
    if not user_id or not reviewed_by:
        raise ValueError("User ID and reviewer are required")

    # Prevent self-approval.
    if user_id == reviewed_by:
        raise PermissionError("Self-approval is not allowed")

    with db.begin():

        # 1. Verify and lock reviewer permissions.
        reviewer = db.execute(
            text("""
                SELECT sp.user_id
                FROM ward_twin.staff_profiles AS sp
                JOIN ward_twin.account_approvals AS aa
                    ON aa.user_id = sp.user_id
                WHERE sp.user_id = :reviewed_by
                  AND sp.role = 'admin'
                  AND aa.status = 'approved'
                FOR SHARE OF sp, aa
            """),
            {"reviewed_by": reviewed_by},
        ).mappings().first()

        if reviewer is None:
            raise PermissionError(
                "Reviewer is not an approved admin"
            )

        # 2. Verify and lock the pending account.
        account = db.execute(
            text("""
                SELECT sp.user_id
                FROM ward_twin.staff_profiles AS sp
                JOIN ward_twin.account_approvals AS aa
                    ON aa.user_id = sp.user_id
                WHERE sp.user_id = :user_id
                  AND sp.role = 'pending'
                  AND aa.status = 'pending'
                FOR UPDATE OF sp, aa
            """),
            {"user_id": user_id},
        ).mappings().first()

        if account is None:
            raise ValueError("Account is not pending")

        # 3. Verify that all requested wards exist.
        existing_wards = db.execute(
            text("""
                SELECT ward_id
                FROM ward_twin.wards
                WHERE ward_id IN :ward_ids
            """).bindparams(
                bindparam("ward_ids", expanding=True)
            ),
            {"ward_ids": ward_ids},
        ).scalars().all()

        if set(existing_wards) != set(ward_ids):
            raise ValueError(
                "One or more wards do not exist"
            )

        # 4. Assign the approved role.
        db.execute(
            text("""
                UPDATE ward_twin.staff_profiles
                SET role = :role
                WHERE user_id = :user_id
            """),
            {
                "user_id": user_id,
                "role": role,
            },
        )

        # 5. Record approval information.
        db.execute(
            text("""
                UPDATE ward_twin.account_approvals
                SET status = 'approved',
                    reviewed_by = :reviewed_by,
                    reviewed_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = :user_id
            """),
            {
                "user_id": user_id,
                "reviewed_by": reviewed_by,
            },
        )

        # 6. Assign ward access.
        for ward_id in ward_ids:
            db.execute(
                text("""
                    INSERT INTO ward_twin.ward_access
                        (user_id, ward_id)
                    VALUES
                        (:user_id, :ward_id)
                """),
                {
                    "user_id": user_id,
                    "ward_id": ward_id,
                },
            )

    return {
        "user_id": user_id,
        "role": role,
        "status": "approved",
        "ward_ids": ward_ids,
        "reviewed_by": reviewed_by,
    }
