
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import InvalidRequestError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.database import SessionLocal, get_db
from app.dependencies.auth import require_admin
from app.schemas.auth import (
    ApproveAccountRequest,
    ApproveAccountResponse,
)
from app.services.admin_repository import (
    approve_account,
    list_pending_accounts,
)


router = APIRouter(
    prefix="/admin",
    tags=["Admin"],
)


class PendingAccountResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: str
    display_name: str
    email: str
    role: str
    status: str
    created_at: datetime


def get_approval_db():
    """
    Separate database session for approval transactions.

    Authentication uses get_db().
    Approval uses get_approval_db().

    This prevents authentication queries from opening
    a transaction on the approval session.
    """
    with SessionLocal() as db:
        yield db


@router.get(
    "/accounts/pending",
    response_model=list[PendingAccountResponse],
)
def get_pending_accounts(
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        return list_pending_accounts(db)

    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail="Admin accounts temporarily unavailable",
        ) from exc


@router.patch(
    "/accounts/{user_id}/approve",
    response_model=ApproveAccountResponse,
)
def approve_pending_account(
    user_id: str,
    payload: ApproveAccountRequest,
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_approval_db),
):
    reviewer_id = admin.get("sub")

    if not reviewer_id:
        raise HTTPException(
            status_code=403,
            detail="Admin identity is required",
        )

    try:
        return approve_account(
            db,
            user_id=user_id,
            role=payload.role,
            ward_ids=payload.ward_ids,
            reviewed_by=reviewer_id,
        )

    except PermissionError as exc:
        raise HTTPException(
            status_code=403,
            detail=str(exc),
        ) from exc

    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc

    except InvalidRequestError as exc:
        raise HTTPException(
            status_code=503,
            detail="Approval transaction unavailable",
        ) from exc

    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail="Approval database unavailable",
        ) from exc
