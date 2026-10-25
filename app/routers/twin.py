
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user, require_ward

router = APIRouter(tags=["Digital Twin"])


@router.get("/encounters/{encounter_id}/twin")
def get_patient_twin(
    encounter_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    # 1. ดึงข้อมูลผู้ป่วยและ Encounter
    encounter = db.execute(
        text("""
            SELECT
                e.encounter_id,
                e.patient_id,
                p.hn,
                p.display_name,
                p.birth_date,
                p.sex,
                p.allergy_status,
                e.an,
                e.ward_id,
                e.bed_label,
                e.admitted_at
            FROM ward_twin.encounters AS e
            JOIN ward_twin.patients AS p
                ON p.patient_id = e.patient_id
            WHERE e.encounter_id = :encounter_id
        """),
        {"encounter_id": encounter_id},
    ).mappings().first()

    if encounter is None:
        raise HTTPException(
            status_code=404,
            detail="Encounter not found",
        )

    # 2. ตรวจสอบสิทธิ์ Ward
    require_ward(encounter["ward_id"], user)

    # 3. ดึง Vital Signs ล่าสุดของแต่ละ Metric
    observations = db.execute(
        text("""
            SELECT DISTINCT ON (m.metric)
                m.metric,
                m.value,
                m.unit,
                r.measured_at,
                r.device_id,
                r.source
            FROM ward_twin.observation_readings AS r
            JOIN ward_twin.observation_measurements AS m
                ON m.reading_pk = r.id
            WHERE r.encounter_id = :encounter_id
            ORDER BY
                m.metric,
                r.measured_at DESC,
                r.id DESC
        """),
        {"encounter_id": encounter_id},
    ).mappings().all()

    # 4. แปลงข้อมูลเป็น JSON-compatible values
    encounter_data = dict(encounter)

    for field in ("birth_date", "admitted_at"):
        if encounter_data.get(field) is not None:
            encounter_data[field] = encounter_data[field].isoformat()

    latest_vitals = []

    for row in observations:
        latest_vitals.append({
            "metric": row["metric"],
            "value": (
                float(row["value"])
                if row["value"] is not None
                else None
            ),
            "unit": row["unit"],
            "measured_at": (
                row["measured_at"].isoformat()
                if row["measured_at"] is not None
                else None
            ),
            "device_id": row["device_id"],
            "source": row["source"],
        })

    # 5. ตรวจสถานะ Processing Queue
    processing = db.execute(
        text("""
            SELECT
                dirty_version,
                processed_version
            FROM ward_twin.processing_state
            WHERE encounter_id = :encounter_id
        """),
        {"encounter_id": encounter_id},
    ).mappings().first()

    # 6. ดึง Prediction ล่าสุดจากฐานข้อมูล
    latest_prediction = db.execute(
        text("""
            SELECT
                prediction_id,
                status,
                probability,
                unavailable_reason,
                model_version,
                assessed_at
            FROM ward_twin.risk_predictions
            WHERE encounter_id = :encounter_id
            ORDER BY
                assessed_at DESC,
                prediction_id DESC
            LIMIT 1
        """),
        {"encounter_id": encounter_id},
    ).mappings().first()

    # 7. กำหนดค่าเริ่มต้น
    prediction_data = {
        "status": "not_available",
        "risk_score": None,
        "prediction_id": None,
        "model_version": None,
        "assessed_at": None,
        "unavailable_reason": None,
    }

    # 8. ตรวจว่ามีข้อมูลใหม่รอ Worker หรือไม่
    has_pending_work = (
        processing is not None
        and processing["dirty_version"] is not None
        and (
            processing["processed_version"] is None
            or processing["dirty_version"]
            > processing["processed_version"]
        )
    )

    if has_pending_work:
        prediction_data["status"] = "pending"

    # 9. ถ้าไม่มีงานค้าง ให้แสดง Prediction ล่าสุด
    elif latest_prediction is not None:
        prediction_status = latest_prediction["status"]

        prediction_data = {
            "status": prediction_status,
            "risk_score": (
                float(latest_prediction["probability"])
                if (
                    prediction_status == "ready"
                    and latest_prediction["probability"] is not None
                )
                else None
            ),
            "prediction_id": latest_prediction["prediction_id"],
            "model_version": latest_prediction["model_version"],
            "assessed_at": (
                latest_prediction["assessed_at"].isoformat()
                if latest_prediction["assessed_at"] is not None
                else None
            ),
            "unavailable_reason": (
                latest_prediction["unavailable_reason"]
            ),
        }

    # 10. ส่งข้อมูลทั้งหมดกลับให้ Frontend
    return {
        "encounter": encounter_data,
        "latest_vitals": latest_vitals,
        "prediction": prediction_data,
    }
