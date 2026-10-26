
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import require_roles, require_ward

router = APIRouter(tags=["Observation History"])


@router.get("/encounters/{encounter_id}/observations")
def get_observation_history(
    encounter_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    before: datetime | None = Query(default=None),
    db: Session = Depends(get_db),
    user: dict = Depends(
        require_roles("admin", "doctor", "nurse", "viewer")
    ),
):
    # 1. ตรวจสอบ Encounter และสิทธิ์ Ward
    encounter = db.execute(
        text("""
            SELECT ward_id
            FROM ward_twin.encounters
            WHERE encounter_id = :encounter_id
        """),
        {"encounter_id": encounter_id},
    ).mappings().first()

    if encounter is None:
        raise HTTPException(
            status_code=404,
            detail="Encounter not found",
        )

    require_ward(encounter["ward_id"], user)

    # 2. ดึง Reading ตามช่วงเวลาและจำนวนที่กำหนด
    rows = db.execute(
        text("""
            SELECT
                r.id,
                r.reading_id,
                r.encounter_id,
                r.device_id,
                r.source,
                r.measured_at,
                r.received_at
            FROM ward_twin.observation_readings AS r
            WHERE r.encounter_id = :encounter_id
              AND (
                  CAST(:before AS TIMESTAMPTZ) IS NULL
                  OR r.measured_at < CAST(:before AS TIMESTAMPTZ)
              )
            ORDER BY r.measured_at DESC, r.id DESC
            LIMIT :limit
        """),
        {
            "encounter_id": encounter_id,
            "before": before,
            "limit": limit,
        },
    ).mappings().all()

    # 3. ดึง Measurements ของ Reading ที่พบ
    observations = []

    for row in rows:
        measurements = db.execute(
            text("""
                SELECT
                    metric,
                    value,
                    unit,
                    quality_flag
                FROM ward_twin.observation_measurements
                WHERE reading_pk = :reading_pk
                ORDER BY metric
            """),
            {"reading_pk": row["id"]},
        ).mappings().all()

        observations.append({
            "reading_id": str(row["reading_id"]),
            "encounter_id": row["encounter_id"],
            "device_id": row["device_id"],
            "source": row["source"],
            "measured_at": row["measured_at"].isoformat(),
            "received_at": row["received_at"].isoformat(),
            "measurements": [
                {
                    "metric": m["metric"],
                    "value": float(m["value"]),
                    "unit": m["unit"],
                    "quality_flag": m["quality_flag"],
                }
                for m in measurements
            ],
        })

    return {
        "encounter_id": encounter_id,
        "count": len(observations),
        "observations": observations,
    }
