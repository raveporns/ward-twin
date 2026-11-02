
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.services.auth_repository import (
    get_login_account,
    get_user_accessible_wards,
    get_user_ward_ids,
)


password_hasher = PasswordHasher()

# ใช้ Hash สำรองเมื่อไม่พบบัญชี เพื่อช่วยลดความต่าง
# ของเวลาตอบกลับระหว่าง Email ที่มีและไม่มีในระบบ
DUMMY_PASSWORD_HASH = password_hasher.hash(
    "dummy-password-not-for-login"
)

ALLOWED_ROLES = {"viewer", "nurse", "doctor", "admin"}


def authenticate_user(
    db: Session,
    *,
    email: str,
    password: str,
) -> dict:
    account = get_login_account(db, email=email)

    password_hash = (
        account["password_hash"]
        if account is not None
        else DUMMY_PASSWORD_HASH
    )

    try:
        password_valid = password_hasher.verify(
            password_hash,
            password,
        )
    except (VerifyMismatchError, VerificationError, ValueError):
        password_valid = False

    if account is None or not password_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    if account["approval_status"] != "approved":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is not approved",
        )

    if account["role"] not in ALLOWED_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account role is not authorized",
        )

    accessible_wards = get_user_accessible_wards(
        db,
        user_id=account["user_id"],
        role=account["role"],
    )

    if account["role"] == "admin":
        # Preserve the existing JWT membership claim; Admin's response list
        # is the full directory because Admin endpoints use all-Ward access.
        ward_ids = get_user_ward_ids(
            db,
            user_id=account["user_id"],
        )
    else:
        ward_ids = [
            ward["ward_id"]
            for ward in accessible_wards
        ]

    return {
        "user_id": account["user_id"],
        "email": account["email"],
        "display_name": account["display_name"],
        "role": account["role"],
        "wards": ward_ids,
        "accessible_wards": accessible_wards,
    }
