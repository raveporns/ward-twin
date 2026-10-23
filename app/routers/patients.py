from fastapi import APIRouter, Depends

from app.dependencies.auth import (
    get_current_user,
    require_ward,
)

router = APIRouter(tags=["Screens"])


@router.get("/wards/{ward_id}/patients")
def get_ward_patients(
    ward_id: str,
    user: dict = Depends(get_current_user),
):
    require_ward(ward_id, user)

    return {
        "ward_id": ward_id,
        "authorized": True,
        "actor_id": user["sub"],
    }
