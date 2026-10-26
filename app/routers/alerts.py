
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import require_roles, require_ward


router = APIRouter(tags=["Alerts"])


# ============================================================
# Request Models
# ============================================================

class AcknowledgementRequest(BaseModel):
    assigned_to: str | None = Field(
        default=None,
        description=(
            "Staff user ID responsible for this alert. "
            "Currently only self-assignment is supported."
        ),
    )


# ============================================================
# Response Models
# ============================================================

class AlertResponse(BaseModel):
    alert_id: str
    encounter_id: str
    prediction_id: str | None = None
    state: str
    created_at: datetime | None = None
    trigger_probability: float | None = None
    trigger_threshold: float | None = None
    model_version: str | None = None
    assigned_to: str | None = None
    acknowledged_by: str | None = None
    acknowledged_at: datetime | None = None


class EncounterAlertsResponse(BaseModel):
    encounter_id: str
    alerts: list[AlertResponse]


class AlertAcknowledgementResponse(BaseModel):
    alert_id: str
    encounter_id: str
    state: str
    assigned_to: str | None = None
    acknowledged_by: str | None = None
    acknowledged_at: datetime | None = None


# ============================================================
# Helpers
# ============================================================


def serialize_alert(row: dict) -> dict:
    """
    Convert database values to API response types.
    """

    result = dict(row)

    # Convert IDs to strings
    for key in (
        "alert_id",
        "encounter_id",
        "prediction_id",
        "assigned_to",
        "acknowledged_by",
    ):
        value = result.get(key)

        if value is not None:
            result[key] = str(value)

    # Convert numeric values to floats
    for key in (
        "trigger_probability",
        "trigger_threshold",
    ):
        value = result.get(key)

        if value is not None:
            result[key] = float(value)

    return result


def get_alert_or_404(
    db: Session,
    alert_id: str,
):
    alert = db.execute(
        text("""
            SELECT
                a.alert_id,
                a.encounter_id,
                e.ward_id,
                a.state,
                a.assigned_to,
                a.acknowledged_by,
                a.acknowledged_at
            FROM ward_twin.alerts AS a
            JOIN ward_twin.encounters AS e
                ON e.encounter_id = a.encounter_id
            WHERE a.alert_id = :alert_id
        """),
        {
            "alert_id": alert_id,
        },
    ).mappings().one_or_none()

    if alert is None:
        raise HTTPException(
            status_code=404,
            detail="Alert not found",
        )

    return alert


def staff_exists(
    db: Session,
    user_id: str,
) -> bool:
    result = db.execute(
        text("""
            SELECT 1
            FROM ward_twin.staff_profiles
            WHERE user_id = :user_id
            LIMIT 1
        """),
        {
            "user_id": user_id,
        },
    ).first()

    return result is not None


# ============================================================
# 1. GET /encounters/{encounter_id}/alerts
# ============================================================

@router.get(
    "/encounters/{encounter_id}/alerts",
    response_model=EncounterAlertsResponse,
    summary="Get Encounter Alerts",
    description=(
        "ดึงรายการแจ้งเตือนทั้งหมดของ Encounter "
        "โดยเรียงจาก Alert ล่าสุดไปเก่าสุด "
        "ผู้ใช้ต้องมีสิทธิ์เข้าถึง Ward ของ Encounter"
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
def get_encounter_alerts(
    encounter_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(
        require_roles("admin", "doctor", "nurse", "viewer")
    ),
):
    # --------------------------------------------------------
    # 1. Find encounter
    # --------------------------------------------------------

    encounter = db.execute(
        text("""
            SELECT ward_id
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

    # --------------------------------------------------------
    # 2. Ward authorization
    # --------------------------------------------------------

    require_ward(
        encounter["ward_id"],
        user,
    )

    # --------------------------------------------------------
    # 3. Get alerts
    # --------------------------------------------------------

    alerts = db.execute(
        text("""
            SELECT
                alert_id,
                encounter_id,
                prediction_id,
                state,
                created_at,
                trigger_probability,
                trigger_threshold,
                model_version,
                assigned_to,
                acknowledged_by,
                acknowledged_at
            FROM ward_twin.alerts
            WHERE encounter_id = :encounter_id
            ORDER BY
                created_at DESC,
                alert_id DESC
        """),
        {
            "encounter_id": encounter_id,
        },
    ).mappings().all()

    # --------------------------------------------------------
    # 4. Response
    # --------------------------------------------------------

    return {
        "encounter_id": str(encounter_id),
        "alerts": [
            serialize_alert(alert)
            for alert in alerts
        ],
    }


# ============================================================
# 2. PUT /alerts/{alert_id}/acknowledgement
# ============================================================

@router.put(
    "/alerts/{alert_id}/acknowledgement",
    response_model=AlertAcknowledgementResponse,
    summary="Acknowledge Alert",
    description=(
        "บันทึกการรับทราบ Alert โดยเจ้าหน้าที่ที่มีสิทธิ์ "
        "Role admin, doctor หรือ nurse ที่เข้าถึง Ward และมีข้อมูล "
        "ใน staff_profiles "
        "ปัจจุบันรองรับเฉพาะการรับทราบด้วยตัวเอง"
    ),
    responses={
        401: {
            "description": "Authentication required",
        },
        403: {
            "description": "Role or ward access denied, or user is not staff",
        },
        404: {
            "description": "Alert not found",
        },
        409: {
            "description": "Alert cannot be acknowledged",
        },
        422: {
            "description": "Invalid assigned staff",
        },
    },
)
def acknowledge_alert(
    alert_id: str,
    payload: AcknowledgementRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(
        require_roles("admin", "doctor", "nurse")
    ),
):
    try:
        # ----------------------------------------------------
        # 1. Lock alert row
        # ----------------------------------------------------

        alert = db.execute(
            text("""
                SELECT
                    a.alert_id,
                    a.encounter_id,
                    e.ward_id,
                    a.state,
                    a.acknowledged_by,
                    a.assigned_to
                FROM ward_twin.alerts AS a
                JOIN ward_twin.encounters AS e
                    ON e.encounter_id = a.encounter_id
                WHERE a.alert_id = :alert_id
                FOR UPDATE OF a
            """),
            {
                "alert_id": alert_id,
            },
        ).mappings().one_or_none()

        if alert is None:
            raise HTTPException(
                status_code=404,
                detail="Alert not found",
            )

        # ----------------------------------------------------
        # 2. Ward authorization
        # ----------------------------------------------------

        require_ward(
            alert["ward_id"],
            user,
        )

        # ----------------------------------------------------
        # 3. Validate alert state
        # ----------------------------------------------------

        if alert["state"] != "open":
            raise HTTPException(
                status_code=409,
                detail="Alert is not open",
            )

        if alert["acknowledged_by"] is not None:
            raise HTTPException(
                status_code=409,
                detail="Alert already acknowledged",
            )

        # ----------------------------------------------------
        # 4. Resolve actor and assignment
        # ----------------------------------------------------

        actor_id = str(user["sub"])

        assigned_to = (
            payload.assigned_to
            or alert["assigned_to"]
            or actor_id
        )

        assigned_to = str(assigned_to)

        # ----------------------------------------------------
        # 5. Validate current staff
        # ----------------------------------------------------

        if not staff_exists(
            db=db,
            user_id=actor_id,
        ):
            raise HTTPException(
                status_code=403,
                detail="Current user is not registered staff",
            )

        # ----------------------------------------------------
        # 6. Validate assigned staff
        # ----------------------------------------------------

        if not staff_exists(
            db=db,
            user_id=assigned_to,
        ):
            raise HTTPException(
                status_code=422,
                detail="Assigned staff not found",
            )

        # ----------------------------------------------------
        # 7. Restrict assignment to self
        # ----------------------------------------------------

        if assigned_to != actor_id:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Assigning another staff member requires "
                    "ward membership verification"
                ),
            )

        # ----------------------------------------------------
        # 8. Acknowledge alert
        # ----------------------------------------------------

        result = db.execute(
            text("""
                UPDATE ward_twin.alerts
                SET
                    assigned_to = :assigned_to,
                    acknowledged_by = :actor_id,
                    acknowledged_at = :acknowledged_at
                WHERE alert_id = :alert_id
                RETURNING
                    alert_id,
                    encounter_id,
                    state,
                    assigned_to,
                    acknowledged_by,
                    acknowledged_at
            """),
            {
                "alert_id": alert_id,
                "assigned_to": assigned_to,
                "actor_id": actor_id,
                "acknowledged_at": datetime.now(timezone.utc),
            },
        ).mappings().one()

        # ----------------------------------------------------
        # 9. Commit transaction
        # ----------------------------------------------------

        db.commit()

        return serialize_alert(result)

    except HTTPException:
        db.rollback()
        raise

    except Exception:
        db.rollback()
        raise
