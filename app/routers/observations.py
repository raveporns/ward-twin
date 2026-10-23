
import hashlib
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_observation_identity, require_ward
from app.schemas.observation import ObservationBatch


router = APIRouter(tags=["Observations"])


@router.post("/observations", status_code=200)
def ingest_observations(
    batch: ObservationBatch,
    identity: dict = Depends(get_observation_identity),
    db: Session = Depends(get_db),
):
    
    if batch.source == "iot":
        if identity["type"] != "gateway":
            raise HTTPException(
                status_code=403,
                detail="IoT observations require gateway authentication",
            )
    
    else:
        if identity["type"] != "user":
            raise HTTPException(
                status_code=403,
                detail="Manual observations require user authentication",
            )

        # ตรวจสิทธิ์ Ward ของทุก Reading
        for reading in batch.readings:
            ward_id = db.execute(
                text("""
                    SELECT ward_id
                    FROM public.encounters
                    WHERE encounter_id = :encounter_id
                """),
                {"encounter_id": reading.encounter_id},
            ).scalar_one_or_none()

            if ward_id is None:
                raise HTTPException(
                    status_code=404,
                    detail="Encounter not found",
                )

            require_ward(ward_id, identity["user"])

        # ยังไม่เปิดใช้งาน Manual INSERT
        raise HTTPException(
            status_code=501,
            detail="Manual observation persistence not implemented",
        )


    gateway = identity
    producer_id = identity["producer_id"]
    received_at = datetime.now(timezone.utc)

    inserted = 0
    duplicates = 0
    items = []

    # ตรวจ reading_id ซ้ำภายใน batch ก่อนเริ่มบันทึก
    ids = [str(r.reading_id) for r in batch.readings]
    if len(ids) != len(set(ids)):
        raise HTTPException(
            status_code=422,
            detail="Duplicate reading_id within batch",
        )

    try:
        # ใช้ transaction เดียวสำหรับทุก reading
        for reading in batch.readings:
            if reading.device_id not in gateway["allowed_devices"]:
                raise HTTPException(
                    status_code=403,
                    detail="Device not allowed for this gateway",
                )

            assignments = db.execute(
                text("""
                    SELECT encounter_id
                    FROM public.device_assignments
                    WHERE device_id = :device_id
                      AND assigned_at <= :measured_at
                      AND (
                          unassigned_at IS NULL
                          OR :measured_at < unassigned_at
                      )
                """),
                {
                    "device_id": reading.device_id,
                    "measured_at": reading.measured_at,
                },
            ).scalars().all()

            if len(assignments) != 1:
                raise HTTPException(
                    status_code=409,
                    detail="Unmapped or ambiguous device assignment",
                )

            encounter_id = assignments[0]

            measurements = [
                m.model_dump(mode="json")
                for m in reading.measurements
            ]

            # Canonical payload สำหรับตรวจ idempotency
            payload = {
                "source": batch.source,
                "device_id": reading.device_id,
                "encounter_id": encounter_id,
                "measured_at": reading.measured_at.astimezone(
                    timezone.utc
                ).isoformat(),
                "measurements": sorted(
                    measurements,
                    key=lambda m: m["metric"],
                ),
            }

            canonical = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )

            payload_hash = hashlib.sha256(
                canonical.encode("utf-8")
            ).hexdigest()

            # ตรวจว่ามี reading เดิมหรือไม่
            existing = db.execute(
                text("""
                    SELECT payload_hash
                    FROM public.observation_readings
                    WHERE producer_id = :producer_id
                      AND reading_id = :reading_id
                """),
                {
                    "producer_id": producer_id,
                    "reading_id": reading.reading_id,
                },
            ).scalar_one_or_none()

            if existing is not None:
                if existing != payload_hash:
                    raise HTTPException(
                        status_code=409,
                        detail="Reading ID conflict",
                    )

                duplicates += 1
                result = "duplicate"

            else:
                # ON CONFLICT ป้องกันการ INSERT ซ้ำพร้อมกัน
                inserted_row = db.execute(
                    text("""
                        INSERT INTO public.observation_readings (
                            producer_id,
                            reading_id,
                            encounter_id,
                            device_id,
                            source,
                            measured_at,
                            received_at,
                            measurements,
                            payload_hash
                        )
                        VALUES (
                            :producer_id,
                            :reading_id,
                            :encounter_id,
                            :device_id,
                            :source,
                            :measured_at,
                            :received_at,
                            CAST(:measurements AS JSONB),
                            :payload_hash
                        )
                        ON CONFLICT (producer_id, reading_id)
                        DO NOTHING
                        RETURNING id
                    """),
                    {
                        "producer_id": producer_id,
                        "reading_id": reading.reading_id,
                        "encounter_id": encounter_id,
                        "device_id": reading.device_id,
                        "source": batch.source,
                        "measured_at": reading.measured_at,
                        "received_at": received_at,
                        "measurements": json.dumps(measurements),
                        "payload_hash": payload_hash,
                    },
                ).scalar_one_or_none()

                if inserted_row is None:
                    # กรณีมี request อื่น INSERT พร้อมกัน
                    concurrent_hash = db.execute(
                        text("""
                            SELECT payload_hash
                            FROM public.observation_readings
                            WHERE producer_id = :producer_id
                              AND reading_id = :reading_id
                        """),
                        {
                            "producer_id": producer_id,
                            "reading_id": reading.reading_id,
                        },
                    ).scalar_one()

                    if concurrent_hash != payload_hash:
                        raise HTTPException(
                            status_code=409,
                            detail="Reading ID conflict",
                        )

                    duplicates += 1
                    result = "duplicate"
                else:
                    inserted += 1
                    result = "inserted"

            items.append({
                "reading_id": str(reading.reading_id),
                "encounter_id": encounter_id,
                "result": result,
            })

        db.commit()

    except Exception:
        db.rollback()
        raise

    return {
        "inserted_readings": inserted,
        "duplicate_readings": duplicates,
        "received_at": received_at.isoformat().replace("+00:00", "Z"),
        "items": items,
        "prediction_processing": "pending",
    }
