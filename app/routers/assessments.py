
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import require_roles, require_ward


router = APIRouter(tags=["Assessments"])


# ============================================================
# Request Models
# ============================================================

class AssessmentCreate(BaseModel):
    client_record_id: UUID = Field(
        description=(
            "Unique client-generated UUID used to prevent "
            "duplicate assessment creation."
        )
    )

    note: str = Field(
        min_length=1,
        max_length=5000,
        description="Nurse assessment note",
    )

    alert_id: str | None = Field(
        default=None,
        description="Optional alert associated with this assessment",
    )


# ============================================================
# Response Models
# ============================================================

class AssessmentResponse(BaseModel):
    assessment_id: str
    encounter_id: str
    alert_id: str | None = None
    client_record_id: UUID
    note: str
    created_by: str
    created_at: datetime | None = None


class AssessmentListResponse(BaseModel):
    encounter_id: str
    assessments: list[AssessmentResponse]


# ============================================================
# Helpers
# ============================================================

def serialize_assessment(row) -> dict:
    """
    Convert PostgreSQL UUID values to JSON-compatible strings.
    """

    result = dict(row)

    for field in (
        "assessment_id",
        "encounter_id",
        "alert_id",
        "created_by",
    ):
        if result.get(field) is not None:
            result[field] = str(result[field])

    return result


def assessment_matches(
    existing,
    encounter_id: str,
    alert_id: str | None,
    note: str,
) -> bool:
    """
    Check whether an existing assessment matches
    an incoming idempotent request.
    """

    existing_encounter_id = str(existing["encounter_id"])

    existing_alert_id = (
        str(existing["alert_id"])
        if existing["alert_id"] is not None
        else None
    )

    incoming_alert_id = (
        str(alert_id)
        if alert_id is not None
        else None
    )

    return (
        existing_encounter_id == str(encounter_id)
        and existing_alert_id == incoming_alert_id
        and existing["note"] == note
    )


def check_encounter_access(
    db: Session,
    encounter_id: str,
    user: dict,
):
    encounter = db.execute(
        text("""
            SELECT
                encounter_id,
                ward_id
            FROM ward_twin.encounters
            WHERE encounter_id = :encounter_id
        """),
        {
            "encounter_id": encounter_id,
        },
    ).mappings().one_or_none()

    if encounter is None:
        raise HTTPException(
            status_code=404,
            detail="Encounter not found",
        )

    require_ward(
        encounter["ward_id"],
        user,
    )

    return encounter


def get_existing_assessment(
    db: Session,
    created_by: str,
    client_record_id: UUID,
):
    return db.execute(
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
            WHERE created_by = :created_by
              AND client_record_id = :client_record_id
        """),
        {
            "created_by": created_by,
            "client_record_id": client_record_id,
        },
    ).mappings().one_or_none()


# ============================================================
# 1. POST /encounters/{encounter_id}/assessments
# ============================================================

@router.post(
    "/encounters/{encounter_id}/assessments",
    status_code=status.HTTP_201_CREATED,
    response_model=AssessmentResponse,
    summary="Create Assessment",
    description=(
        "บันทึกผลการประเมินสำหรับ Encounter โดยผู้ใช้ Role "
        "admin, doctor หรือ nurse "
        "รองรับการเชื่อมโยงกับ Alert และป้องกันข้อมูลซ้ำ "
        "ด้วย client_record_id"
    ),
    responses={
        401: {
            "description": "Authentication required",
        },
        403: {
            "description": "Role or ward access denied",
        },
        404: {
            "description": "Encounter or alert not found",
        },
        409: {
            "description": "Duplicate client_record_id with different data",
        },
        422: {
            "description": "Invalid assessment data",
        },
    },
)
def create_assessment(
    encounter_id: str,
    payload: AssessmentCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(
        require_roles("admin", "doctor", "nurse")
    ),
):
    # --------------------------------------------------------
    # 1. Verify encounter and ward access
    # --------------------------------------------------------

    check_encounter_access(
        db=db,
        encounter_id=encounter_id,
        user=user,
    )

    actor_id = str(user["sub"])

    # --------------------------------------------------------
    # 2. Normalize note
    # --------------------------------------------------------

    note = payload.note.strip()

    if not note:
        raise HTTPException(
            status_code=422,
            detail="Note cannot be empty",
        )

    # --------------------------------------------------------
    # 3. Check idempotency
    # --------------------------------------------------------

    existing = get_existing_assessment(
        db=db,
        created_by=actor_id,
        client_record_id=payload.client_record_id,
    )

    if existing is not None:
        if not assessment_matches(
            existing=existing,
            encounter_id=encounter_id,
            alert_id=payload.alert_id,
            note=note,
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "client_record_id already used "
                    "with different data"
                ),
            )

        return serialize_assessment(existing)

    # --------------------------------------------------------
    # 4. Verify alert if provided
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # 5. Insert assessment
    # --------------------------------------------------------

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
                RETURNING
                    assessment_id,
                    encounter_id,
                    alert_id,
                    client_record_id,
                    note,
                    created_by,
                    created_at
            """),
            {
                "encounter_id": encounter_id,
                "alert_id": payload.alert_id,
                "client_record_id": payload.client_record_id,
                "note": note,
                "created_by": actor_id,
            },
        ).mappings().one_or_none()

        # ----------------------------------------------------
        # 6. Handle concurrent duplicate requests
        # ----------------------------------------------------

        if result is None:
            result = get_existing_assessment(
                db=db,
                created_by=actor_id,
                client_record_id=payload.client_record_id,
            )

            if result is None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Assessment conflict detected, "
                        "but existing record was not found"
                    ),
                )

            if not assessment_matches(
                existing=result,
                encounter_id=encounter_id,
                alert_id=payload.alert_id,
                note=note,
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "client_record_id already used "
                        "with different data"
                    ),
                )

        # ----------------------------------------------------
        # 7. Commit transaction
        # ----------------------------------------------------

        db.commit()

        return serialize_assessment(result)

    except HTTPException:
        db.rollback()
        raise

    except IntegrityError:
        db.rollback()

        raise HTTPException(
            status_code=409,
            detail="Database constraint conflict during assessment",
        )

    except Exception:
        db.rollback()
        raise


# ============================================================
# 2. GET /encounters/{encounter_id}/assessments
# ============================================================

@router.get(
    "/encounters/{encounter_id}/assessments",
    response_model=AssessmentListResponse,
    summary="Get Encounter Assessments",
    description=(
        "ดึงรายการผลการประเมินของพยาบาลทั้งหมด "
        "สำหรับ Encounter โดยเรียงจากรายการล่าสุดไปเก่าสุด"
    ),
    responses={
        401: {
            "description": "Authentication required",
        },
        403: {
            "description": "Role or ward access denied",
        },
        404: {
            "description": "Encounter not found",
        },
        422: {
            "description": "Invalid request parameters",
        },
    },
)
def get_assessments(
    encounter_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(
        require_roles("admin", "doctor", "nurse", "viewer")
    ),
):
    # --------------------------------------------------------
    # 1. Verify encounter and ward access
    # --------------------------------------------------------

    check_encounter_access(
        db=db,
        encounter_id=encounter_id,
        user=user,
    )

    # --------------------------------------------------------
    # 2. Get assessments
    # --------------------------------------------------------

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
            ORDER BY
                created_at DESC,
                assessment_id DESC
        """),
        {
            "encounter_id": encounter_id,
        },
    ).mappings().all()

    # --------------------------------------------------------
    # 3. Build response
    # --------------------------------------------------------

    return {
        "encounter_id": str(encounter_id),
        "assessments": [
            serialize_assessment(row)
            for row in rows
        ],
    }
