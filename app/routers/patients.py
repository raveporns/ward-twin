
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import require_roles, require_ward

router = APIRouter(tags=["Screens"])

WORKFLOW_STATUS_LABELS = {
    "normal": "ไม่มีรายการรอดำเนินการ",
    "pending_acknowledgement": "รอรับทราบ",
    "pending_assessment": "รอประเมินผู้ป่วย",
    "assessed": "ประเมินผู้ป่วยแล้ว",
}


def get_workflow_status(alerts: list[dict]) -> str:
    workflow_alerts = [
        alert
        for alert in alerts
        if alert["state"] == "open"
    ]

    if not workflow_alerts:
        return "normal"

    if any(alert["acknowledged_by"] is None for alert in workflow_alerts):
        return "pending_acknowledgement"

    if any(not alert["has_assessment"] for alert in workflow_alerts):
        return "pending_assessment"

    return "assessed"


@router.get("/wards/{ward_id}/patients")
def get_ward_patients(
    ward_id: str,
    user: dict = Depends(
        require_roles("admin", "doctor", "nurse", "viewer")
    ),
    db: Session = Depends(get_db),
):
    
    # 1. ตรวจสอบสิทธิ์และการมีอยู่ของ Ward
    validate_ward_access(
        ward_id=ward_id,
        user=user,
        db=db,
    )

    # 2. ดึงข้อมูล Encounter พร้อม Processing และ Prediction ล่าสุด
    rows = db.execute(
        text("""
            SELECT
                p.patient_id,
                p.hn,
                p.display_name,
                p.sex,
                p.allergy_status,
                e.encounter_id,
                e.an,
                e.bed_label,
                e.admitted_at,
                ps.dirty_version,
                ps.processed_version,
                rp.prediction_id,
                rp.status AS prediction_status,
                rp.probability,
                rp.model_version,
                rp.assessed_at,
                rp.unavailable_reason
            FROM ward_twin.encounters AS e
            JOIN ward_twin.patients AS p
                ON p.patient_id = e.patient_id
            LEFT JOIN ward_twin.processing_state AS ps
                ON ps.encounter_id = e.encounter_id
            LEFT JOIN LATERAL (
                SELECT
                    prediction_id,
                    status,
                    probability,
                    model_version,
                    assessed_at,
                    unavailable_reason
                FROM ward_twin.risk_predictions
                WHERE encounter_id = e.encounter_id
                ORDER BY
                    assessed_at DESC,
                    prediction_id DESC
                LIMIT 1
            ) AS rp ON TRUE
            WHERE e.ward_id = :ward_id
            ORDER BY e.bed_label, p.display_name, e.encounter_id
        """),
        {"ward_id": ward_id},
    ).mappings().all()

    workflow_alerts_by_encounter = {}
    if rows:
        encounter_ids = [row["encounter_id"] for row in rows]
        workflow_alerts = db.execute(
            text("""
                SELECT
                    a.encounter_id,
                    a.state,
                    a.acknowledged_by,
                    EXISTS (
                        SELECT 1
                        FROM ward_twin.assessments AS assessment
                        WHERE assessment.alert_id = a.alert_id
                          AND assessment.encounter_id = a.encounter_id
                    ) AS has_assessment
                FROM ward_twin.alerts AS a
                JOIN ward_twin.encounters AS e
                    ON e.encounter_id = a.encounter_id
                WHERE e.ward_id = :ward_id
                  AND e.encounter_id IN :encounter_ids
            """).bindparams(
                bindparam("encounter_ids", expanding=True)
            ),
            {
                "ward_id": ward_id,
                "encounter_ids": encounter_ids,
            },
        ).mappings().all()

        for alert in workflow_alerts:
            workflow_alerts_by_encounter.setdefault(
                alert["encounter_id"],
                [],
            ).append(alert)

    # 3. แปลงข้อมูลสำหรับ Frontend
    patients = []

    for row in rows:
        workflow_status = get_workflow_status(
            workflow_alerts_by_encounter.get(row["encounter_id"], [])
        )

        prediction = {
            "status": "not_available",
            "risk_score": None,
            "prediction_id": None,
            "model_version": None,
            "assessed_at": None,
            "unavailable_reason": None,
        }

        has_pending_work = (
            row["dirty_version"] is not None
            and (
                row["processed_version"] is None
                or row["dirty_version"] > row["processed_version"]
            )
        )

        if has_pending_work:
            prediction["status"] = "pending"

        elif row["prediction_id"] is not None:
            prediction_status = row["prediction_status"]

            prediction = {
                "status": prediction_status,
                "risk_score": (
                    float(row["probability"])
                    if (
                        prediction_status == "ready"
                        and row["probability"] is not None
                    )
                    else None
                ),
                "prediction_id": row["prediction_id"],
                "model_version": row["model_version"],
                "assessed_at": (
                    row["assessed_at"].isoformat()
                    if row["assessed_at"] is not None
                    else None
                ),
                "unavailable_reason": row["unavailable_reason"],
            }

        patients.append({
            "patient_id": row["patient_id"],
            "hn": row["hn"],
            "display_name": row["display_name"],
            "sex": row["sex"],
            "allergy_status": row["allergy_status"],
            "encounter_id": row["encounter_id"],
            "an": row["an"],
            "bed_label": row["bed_label"],
            "admitted_at": (
                row["admitted_at"].isoformat()
                if row["admitted_at"] is not None
                else None
            ),
            "workflow_status": workflow_status,
            "workflow_status_label": WORKFLOW_STATUS_LABELS[workflow_status],
            "prediction": prediction,
        })

    # 4. ส่งข้อมูลกลับให้ Ward Dashboard
    return {
        "ward_id": ward_id,
        "total": len(patients),
        "patients": patients,
    }


def validate_ward_access(
    ward_id: str,
    user: dict,
    db: Session,
):
    # ตรวจสิทธิ์ก่อน เพื่อไม่เปิดเผยข้อมูล Ward อื่น
    require_ward(ward_id, user)

    # ตรวจว่า Ward มีอยู่จริงหรือไม่
    ward_exists = db.execute(
        text("""
            SELECT EXISTS (
                SELECT 1
                FROM ward_twin.wards
                WHERE ward_id = :ward_id
            )
        """),
        {"ward_id": ward_id},
    ).scalar_one()

    if not ward_exists:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ward not found",
        )