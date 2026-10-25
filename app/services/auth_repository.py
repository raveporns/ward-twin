from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session


def create_pending_account(
    db: Session,
    *,
    email: str,
    password_hash: str,
    display_name: str,
) -> dict:
    user_id = str(uuid4())
    normalized_email = email.strip().lower()
    normalized_name = display_name.strip()

    if not normalized_name:
        raise ValueError("Display name is required")

    with db.begin():
        db.execute(
            text("""
                INSERT INTO ward_twin.staff_profiles
                    (user_id, display_name, role)
                VALUES
                    (:user_id, :display_name, 'pending')
            """),
            {
                "user_id": user_id,
                "display_name": normalized_name,
            },
        )

        db.execute(
            text("""
                INSERT INTO ward_twin.user_credentials
                    (user_id, email, password_hash)
                VALUES
                    (:user_id, :email, :password_hash)
            """),
            {
                "user_id": user_id,
                "email": normalized_email,
                "password_hash": password_hash,
            },
        )

        db.execute(
            text("""
                INSERT INTO ward_twin.account_approvals
                    (user_id, status)
                VALUES
                    (:user_id, 'pending')
            """),
            {"user_id": user_id},
        )

    return {
        "user_id": user_id,
        "email": normalized_email,
        "display_name": normalized_name,
        "role": "pending",
        "status": "pending",
    }


def get_login_account(db: Session, *, email: str) -> dict | None:
    normalized_email = email.strip().lower()

    result = db.execute(
        text("""
            SELECT
                sp.user_id,
                sp.display_name,
                sp.role,
                uc.email,
                uc.password_hash,
                aa.status AS approval_status
            FROM ward_twin.user_credentials AS uc
            JOIN ward_twin.staff_profiles AS sp
                ON sp.user_id = uc.user_id
            JOIN ward_twin.account_approvals AS aa
                ON aa.user_id = sp.user_id
            WHERE lower(trim(uc.email)) = :email
            LIMIT 1
        """),
        {"email": normalized_email},
    ).mappings().first()

    if result is None:
        return None

    return dict(result)


def get_user_ward_ids(
    db: Session,
    *,
    user_id: str,
) -> list[str]:
    result = db.execute(
        text("""
            SELECT ward_id
            FROM ward_twin.ward_access
            WHERE user_id = :user_id
            ORDER BY ward_id
        """),
        {"user_id": user_id},
    )

    return list(result.scalars().all())


def get_active_user_access(
    db: Session,
    *,
    user_id: str,
) -> dict | None:
    result = db.execute(
        text("""
            SELECT
                sp.user_id,
                sp.role,
                aa.status AS approval_status
            FROM ward_twin.staff_profiles AS sp
            JOIN ward_twin.account_approvals AS aa
                ON aa.user_id = sp.user_id
            JOIN ward_twin.user_credentials AS uc
                ON uc.user_id = sp.user_id
            WHERE sp.user_id = :user_id
            LIMIT 1
        """),
        {"user_id": user_id},
    ).mappings().first()

    if result is None:
        return None

    account = dict(result)

    if account["approval_status"] != "approved":
        return None

    if account["role"] not in {
        "viewer",
        "nurse",
        "doctor",
        "admin",
    }:
        return None

    account["wards"] = get_user_ward_ids(
        db,
        user_id=user_id,
    )

    return account
