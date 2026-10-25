from argon2 import PasswordHasher
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.auth import RegisterRequest, RegisterResponse
from app.services.auth_repository import create_pending_account
from psycopg import errors as pg_errors

from app.schemas.auth import LoginRequest, LoginResponse
from app.services.auth_service import authenticate_user
from app.core.security import create_access_token

router = APIRouter(prefix="/auth", tags=["Authentication"])
password_hasher = PasswordHasher()


@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
)
def register(
    payload: RegisterRequest,
    db: Session = Depends(get_db),
):
    password_hash = password_hasher.hash(payload.password)

    try:
        return create_pending_account(
            db,
            email=str(payload.email),
            password_hash=password_hash,
            display_name=payload.display_name,
        )

    except IntegrityError as exc:
        db.rollback()

        original_error = exc.orig

        if (
            isinstance(original_error, pg_errors.UniqueViolation)
            and getattr(original_error.diag, "constraint_name", None)
            == "user_credentials_email_uidx"
        ):
            raise HTTPException(
                status_code=409,
                detail="Email already registered",
            ) from exc

        raise HTTPException(
            status_code=503,
            detail="Registration temporarily unavailable",
        ) from exc

    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(
            status_code=503,
            detail="Registration temporarily unavailable",
        )


@router.post(
    "/login",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
)
def login(
    payload: LoginRequest,
    db: Session = Depends(get_db),
):
    try:
        user = authenticate_user(
            db,
            email=str(payload.email),
            password=payload.password,
        )

        # ไม่ออก JWT หากบัญชีไม่มี Ward Access
        if not user["wards"]:
            raise HTTPException(
                status_code=403,
                detail="No ward access assigned",
            )

        access_token = create_access_token(
            user_id=user["user_id"],
            role=user["role"],
            wards=user["wards"],
        )

        return LoginResponse(
            access_token=access_token,
            token_type="bearer",
            expires_in=1800,
        )

    except HTTPException:
        raise

    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=503,
            detail="Authentication temporarily unavailable",
        ) from exc
