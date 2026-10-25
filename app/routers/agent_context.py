
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user, require_ward

router = APIRouter(tags=["AI Agent"])


@router.get("/encounters/{encounter_id}/agent-context")
def get_agent_context(
    encounter_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    # 1. ตรวจสอบ Encounter และสิทธิ์ Ward
    encounter = db.execute(
        text("""
            SELECT
                e.encounter_id,
                e.ward_id,
                e.bed_label,
                e.admitted_at,
                p.patient_id,
                p.display_name,
                ps.dirty_version,
                ps.processed_version
            FROM ward_twin.encounters AS e
            JOIN ward_twin.patients AS p
                ON p.patient_id = e.patient_id
            LEFT JOIN ward_twin.processing_state AS ps
                ON ps.encounter_id = e.encounter_id
            WHERE e.encounter_id = :encounter_id
        """),
        {"encounter_id": encounter_id},
    ).mappings().first()

    if encounter is None:
        raise HTTPException(
            status_code=404,
            detail="Encounter not found",
        )

    require_ward(encounter["ward_id"], user)

    # 2. ดึงสัญญาณชีพล่าสุดของแต่ละ Metric
    vitals = db.execute(
        text("""
            SELECT DISTINCT ON (m.metric)
                m.metric,
                m.value,
                m.unit,
                r.measured_at,
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

    latest_vitals = [
        {
            "metric": row["metric"],
            "value": float(row["value"]),
            "unit": row["unit"],
            "measured_at": row["measured_at"].isoformat(),
            "source": row["source"],
        }
        for row in vitals
    ]

    # 3. ส่งข้อมูล Context สำหรับ AI Agent
    return {
        "encounter_id": encounter["encounter_id"],
        "ward_id": encounter["ward_id"],
        "bed_label": encounter["bed_label"],
        "admitted_at": encounter["admitted_at"].isoformat(),
        "data_version": encounter["dirty_version"] or 0,
        "processed_version": encounter["processed_version"] or 0,
        "latest_vitals": latest_vitals,
        "risk_assessment": {
            "status": "not_available",
            "risk_score": None,
        },
        "agent_policy": {
            "mode": "read_only",
            "clinical_decision": False,
        },
    }
