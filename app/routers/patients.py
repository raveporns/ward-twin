
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user, require_ward

router = APIRouter(tags=["Screens"])


@router.get("/wards/{ward_id}/patients")
def get_ward_patients(
    ward_id: str,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # 1. ตรวจสอบสิทธิ์ Ward
    require_ward(ward_id, user)

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

    # 3. แปลงข้อมูลสำหรับ Frontend
    patients = []

    for row in rows:
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
            "prediction": prediction,
        })

    # 4. ส่งข้อมูลกลับให้ Ward Dashboard
    return {
        "ward_id": ward_id,
        "total": len(patients),
        "patients": patients,
    }
