
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user, require_ward

router = APIRouter(tags=["Assessments"])


class AssessmentCreate(BaseModel):
    client_record_id: UUID
    note: str = Field(min_length=1, max_length=5000)
    alert_id: str | None = None


def check_encounter_access(
    db: Session,
    encounter_id: str,
    user: dict,
):
    encounter = db.execute(
        text("""
            SELECT encounter_id, ward_id
            FROM ward_twin.encounters
            WHERE encounter_id = :encounter_id
        """),
        {"encounter_id": encounter_id},
    ).mappings().one_or_none()

    if encounter is None:
        raise HTTPException(
            status_code=404,
            detail="Encounter not found",
        )

    require_ward(encounter["ward_id"], user)
    return encounter


@router.post(
    "/encounters/{encounter_id}/assessments",
    status_code=status.HTTP_201_CREATED,
)
def create_assessment(
    encounter_id: str,
    payload: AssessmentCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    check_encounter_access(db, encounter_id, user)

    actor_id = user["sub"]
    note = payload.note.strip()

    if not note:
        raise HTTPException(
            status_code=422,
            detail="Note cannot be empty",
        )

    # ตรวจสอบว่า client_record_id นี้เคยถูกบันทึกแล้วหรือไม่
    existing = db.execute(
        text("""
            SELECT *
            FROM ward_twin.assessments
            WHERE created_by = :created_by
              AND client_record_id = :client_record_id
        """),
        {
            "created_by": actor_id,
            "client_record_id": payload.client_record_id,
        },
    ).mappings().one_or_none()

    if existing is not None:
        if (
            existing["encounter_id"] != encounter_id
            or existing["alert_id"] != payload.alert_id
            or existing["note"] != note
        ):
            raise HTTPException(
                status_code=409,
                detail="client_record_id already used with different data",
            )
        return dict(existing)

    # ตรวจสอบ Alert หากมีการระบุ alert_id
    if payload.alert_id is not None:
        alert = db.execute(
            text("""
                SELECT alert_id
                FROM ward_twin.alerts
                WHERE alert_id = :alert_id
                  AND encounter_id = :encounter_id
            """),
            {
                "alert_id": payload.alert_id,
                "encounter_id": encounter_id,
            },
        ).scalar_one_or_none()

        if alert is None:
            raise HTTPException(
                status_code=404,
                detail="Alert not found for this encounter",
            )

    try:
        result = db.execute(
            text("""
                INSERT INTO ward_twin.assessments (
                    encounter_id,
                    alert_id,
                    client_record_id,
                    note,
                    created_by
                )
                VALUES (
                    :encounter_id,
                    :alert_id,
                    :client_record_id,
                    :note,
                    :created_by
                )
                ON CONFLICT (created_by, client_record_id)
                DO NOTHING
                RETURNING *
            """),
            {
                "encounter_id": encounter_id,
                "alert_id": payload.alert_id,
                "client_record_id": payload.client_record_id,
                "note": note,
                "created_by": actor_id,
            },
        ).mappings().one_or_none()

        if result is None:
            # รองรับกรณีมี request ซ้ำเข้ามาพร้อมกัน
            result = db.execute(
                text("""
                    SELECT *
                    FROM ward_twin.assessments
                    WHERE created_by = :created_by
                      AND client_record_id = :client_record_id
                """),
                {
                    "created_by": actor_id,
                    "client_record_id": payload.client_record_id,
                },
            ).mappings().one()

            if (
                result["encounter_id"] != encounter_id
                or result["alert_id"] != payload.alert_id
                or result["note"] != note
            ):
                raise HTTPException(
                    status_code=409,
                    detail="client_record_id already used with different data",
                )

        db.commit()
        return dict(result)

    except Exception:
        db.rollback()
        raise


@router.get("/encounters/{encounter_id}/assessments")
def get_assessments(
    encounter_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    check_encounter_access(db, encounter_id, user)

    rows = db.execute(
        text("""
            SELECT
                assessment_id,
                encounter_id,
                alert_id,
                client_record_id,
                note,
                created_by,
                created_at
            FROM ward_twin.assessments
            WHERE encounter_id = :encounter_id
            ORDER BY created_at DESC, assessment_id DESC
        """),
        {"encounter_id": encounter_id},
    ).mappings().all()

    return {
        "encounter_id": encounter_id,
        "assessments": [dict(row) for row in rows],
    }
