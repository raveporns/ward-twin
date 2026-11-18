from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user, require_admin
from app.schemas.users import UserResponse
from app.services.users_repository import list_users


router = APIRouter(prefix="/users", tags=["Users"])
admin_router = APIRouter(prefix="/admin/users", tags=["Users"])


def get_user_or_404(
    db: Session,
    *,
    user_id: str,
) -> dict:
    users = list_users(db, user_id=user_id)
    if not users:
        raise HTTPException(
            status_code=404,
            detail="User not found",
        )
    return users[0]


@admin_router.get("", response_model=list[UserResponse])
def get_users(
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        return list_users(db)
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail="Users temporarily unavailable",
        ) from exc


@router.get("/me", response_model=UserResponse)
def get_current_user_profile(
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = user.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=403,
            detail="Authenticated user identity is required",
        )

    try:
        return get_user_or_404(db, user_id=str(user_id))
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail="User profile temporarily unavailable",
        ) from exc


@admin_router.get("/{user_id}", response_model=UserResponse)
def get_user(
    user_id: str,
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        return get_user_or_404(db, user_id=user_id)
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail="User profile temporarily unavailable",
        ) from exc
