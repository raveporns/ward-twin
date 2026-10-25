from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user, require_ward


router = APIRouter(tags=["Alerts"])


class AcknowledgementRequest(BaseModel):
    assigned_to: str | None = None


def get_alert_or_404(db: Session, alert_id: str):
    return db.execute(
        text("""
            SELECT
                a.alert_id,
                a.encounter_id,
                e.ward_id,
                a.state,
                a.assigned_to,
                a.acknowledged_by,
                a.acknowledged_at
            FROM ward_twin.alerts a
            JOIN ward_twin.encounters e
              ON e.encounter_id = a.encounter_id
            WHERE a.alert_id = :alert_id
        """),
        {"alert_id": alert_id},
    ).mappings().one_or_none()


@router.get("/encounters/{encounter_id}/alerts")
def get_encounter_alerts(
    encounter_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    encounter = db.execute(
        text("""
            SELECT ward_id
            FROM ward_twin.encounters
            WHERE encounter_id = :encounter_id
        """),
        {"encounter_id": encounter_id},
    ).mappings().one_or_none()

    if encounter is None:
        raise HTTPException(404, "Encounter not found")

    require_ward(encounter["ward_id"], user)

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
            ORDER BY created_at DESC, alert_id DESC
        """),
        {"encounter_id": encounter_id},
    ).mappings().all()

    return {
        "encounter_id": encounter_id,
        "alerts": [dict(alert) for alert in alerts],
    }


@router.put("/alerts/{alert_id}/acknowledgement")
def acknowledge_alert(
    alert_id: str,
    payload: AcknowledgementRequest,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    try:
        # Lock แถว Alert เพื่อป้องกันการรับทราบพร้อมกัน
        alert = db.execute(
            text("""
                SELECT
                    a.alert_id,
                    a.encounter_id,
                    e.ward_id,
                    a.state,
                    a.acknowledged_by,
                    a.assigned_to
                FROM ward_twin.alerts a
                JOIN ward_twin.encounters e
                  ON e.encounter_id = a.encounter_id
                WHERE a.alert_id = :alert_id
                FOR UPDATE OF a
            """),
            {"alert_id": alert_id},
        ).mappings().one_or_none()

        if alert is None:
            raise HTTPException(404, "Alert not found")

        require_ward(alert["ward_id"], user)

        if alert["state"] != "open":
            raise HTTPException(409, "Alert is not open")

        if alert["acknowledged_by"] is not None:
            raise HTTPException(409, "Alert already acknowledged")

        actor_id = user["sub"]
        assigned_to = payload.assigned_to or alert["assigned_to"] or actor_id

        # ตรวจว่าผู้รับผิดชอบมีตัวตนในฐานข้อมูล
        staff = db.execute(
            text("""
                SELECT user_id
                FROM ward_twin.staff_profiles
                WHERE user_id = :user_id
            """),
            {"user_id": assigned_to},
        ).scalar_one_or_none()

        if staff is None:
            raise HTTPException(422, "Assigned staff not found")

        actor = db.execute(
            text("""
                SELECT user_id
                FROM ward_twin.staff_profiles
                WHERE user_id = :user_id
            """),
            {"user_id": actor_id},
        ).scalar_one_or_none()

        if actor is None:
            raise HTTPException(403, "Current user is not registered staff")

        # ป้องกันการมอบหมายให้เจ้าหน้าที่ที่ไม่มีสิทธิ์ Ward นี้
        if assigned_to != actor_id:
            raise HTTPException(
                422,
                "Assigning another staff member requires ward membership verification",
            )

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

        db.commit()
        return dict(result)

    except Exception:
        db.rollback()
        raise
