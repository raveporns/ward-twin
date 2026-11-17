
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
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


class AlertClaimResponse(BaseModel):
    alert_id: str
    status: Literal["claimed"]
    assigned_to: str
    claimed_at: datetime


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
    actor_id = user.get("sub")
    if not actor_id:
        raise HTTPException(
            status_code=403,
            detail="Authenticated user identity is required",
        )
    actor_id = str(actor_id)

    try:
        alert = db.execute(
            text("""
                SELECT
                    a.alert_id,
                    a.encounter_id,
                    e.ward_id,
                    a.state,
                    a.acknowledged_by,
                    a.acknowledged_at,
                    a.assigned_to,
                    a.claim_status,
                    a.claimed_at
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

        require_ward(alert["ward_id"], user)

        if payload.assigned_to and str(payload.assigned_to) != actor_id:
            raise HTTPException(
                status_code=422,
                detail="Acknowledgement cannot change the alert assignee",
            )

        # Preserve idempotency for records acknowledged before claims existed.
        if alert["acknowledged_by"] is not None:
            if str(alert["acknowledged_by"]) != actor_id:
                raise HTTPException(
                    status_code=409,
                    detail="Alert was acknowledged by another user",
                )
            db.commit()
            return serialize_alert({
                "alert_id": alert["alert_id"],
                "encounter_id": alert["encounter_id"],
                "state": alert["state"],
                "assigned_to": alert["assigned_to"],
                "acknowledged_by": alert["acknowledged_by"],
                "acknowledged_at": alert["acknowledged_at"],
            })

        if alert["state"] != "open":
            raise HTTPException(
                status_code=409,
                detail="Alert is not open",
            )

        if user.get("role") != "nurse":
            raise HTTPException(
                status_code=403,
                detail="Only the claiming nurse can acknowledge this alert",
            )

        if (
            alert["claim_status"] != "claimed"
            or alert["assigned_to"] is None
            or str(alert["assigned_to"]) != actor_id
        ):
            raise HTTPException(
                status_code=403,
                detail="Alert must be claimed by the current nurse first",
            )

        if not staff_exists(db=db, user_id=actor_id):
            raise HTTPException(
                status_code=403,
                detail="Current user is not registered staff",
            )

        result = db.execute(
            text("""
                UPDATE ward_twin.alerts
                SET acknowledged_by = :actor_id,
                    acknowledged_at = :acknowledged_at
                WHERE alert_id = :alert_id
                  AND claim_status = 'claimed'
                  AND assigned_to = :actor_id
                  AND acknowledged_by IS NULL
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
                "actor_id": actor_id,
                "acknowledged_at": datetime.now(timezone.utc),
            },
        ).mappings().one_or_none()

        if result is None:
            raise HTTPException(
                status_code=409,
                detail="Alert acknowledgement changed concurrently",
            )

        db.commit()

        return serialize_alert(result)

    except HTTPException:
        db.rollback()
        raise

    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=503,
            detail="Alert acknowledgement temporarily unavailable",
        ) from exc


@router.post(
    "/alerts/{alert_id}/claim",
    response_model=AlertClaimResponse,
    summary="Claim Alert Task",
    description=(
        "พยาบาลรับผิดชอบ Alert ใน Ward ที่ตนมีสิทธิ์ "
        "การ Claim แยกจากการรับทราบ Alert"
    ),
    responses={
        401: {"description": "Authentication required"},
        403: {"description": "Nurse role or Ward access required"},
        404: {"description": "Alert not found"},
        409: {"description": "Alert is closed or claimed by another nurse"},
        503: {"description": "Claim temporarily unavailable"},
    },
)
def claim_alert(
    alert_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(require_roles("nurse")),
):
    actor_id = user.get("sub")
    if not actor_id:
        raise HTTPException(
            status_code=403,
            detail="Authenticated user identity is required",
        )
    actor_id = str(actor_id)

    try:
        alert = db.execute(
            text("""
                SELECT
                    a.alert_id,
                    a.state,
                    a.assigned_to,
                    a.acknowledged_by,
                    a.claim_status,
                    a.claimed_at,
                    e.ward_id
                FROM ward_twin.alerts AS a
                JOIN ward_twin.encounters AS e
                    ON e.encounter_id = a.encounter_id
                WHERE a.alert_id = :alert_id
                FOR UPDATE OF a
            """),
            {"alert_id": alert_id},
        ).mappings().one_or_none()

        if alert is None:
            raise HTTPException(
                status_code=404,
                detail="Alert not found",
            )

        require_ward(alert["ward_id"], user)

        if alert["claim_status"] == "claimed":
            if str(alert["assigned_to"]) != actor_id:
                raise HTTPException(
                    status_code=409,
                    detail="Alert is claimed by another nurse",
                )

            db.commit()
            return {
                "alert_id": str(alert["alert_id"]),
                "status": "claimed",
                "assigned_to": actor_id,
                "claimed_at": alert["claimed_at"],
            }

        if alert["acknowledged_by"] is not None:
            raise HTTPException(
                status_code=409,
                detail="Acknowledged legacy alerts cannot be claimed",
            )

        if alert["state"] != "open":
            raise HTTPException(
                status_code=409,
                detail="Alert is not open",
            )

        if (
            alert["assigned_to"] is not None
            and str(alert["assigned_to"]) != actor_id
        ):
            raise HTTPException(
                status_code=409,
                detail="Alert is assigned to another nurse",
            )

        claimed = db.execute(
            text("""
                UPDATE ward_twin.alerts
                SET assigned_to = :assigned_to,
                    claim_status = 'claimed',
                    claimed_at = CURRENT_TIMESTAMP
                WHERE alert_id = :alert_id
                  AND claim_status = 'unclaimed'
                  AND state = 'open'
                  AND (
                      assigned_to IS NULL
                      OR assigned_to = :assigned_to
                  )
                RETURNING
                    alert_id,
                    claim_status,
                    assigned_to,
                    claimed_at
            """),
            {
                "alert_id": alert_id,
                "assigned_to": actor_id,
            },
        ).mappings().one_or_none()

        if claimed is None:
            raise HTTPException(
                status_code=409,
                detail="Alert claim changed concurrently",
            )

        db.commit()
        return {
            "alert_id": str(claimed["alert_id"]),
            "status": claimed["claim_status"],
            "assigned_to": str(claimed["assigned_to"]),
            "claimed_at": claimed["claimed_at"],
        }

    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=503,
            detail="Alert claim temporarily unavailable",
        ) from exc
