
import hashlib
import json
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_observation_identity, require_ward
from app.schemas.observation import ObservationBatch


router = APIRouter(tags=["Observations"])


class ObservationItemResponse(BaseModel):
    reading_id: str
    encounter_id: str
    result: Literal["inserted", "duplicate"]


class ObservationIngestResponse(BaseModel):
    inserted_readings: int = Field(ge=0)
    duplicate_readings: int = Field(ge=0)
    received_at: datetime
    items: list[ObservationItemResponse]
    prediction_processing: Literal["pending", "unchanged"]


@router.post(
    "/observations",
    status_code=200,
    response_model=ObservationIngestResponse,
    summary="Ingest Observations",
    description=(
        "รับข้อมูลสัญญาณชีพจาก IoT Gateway หรือเจ้าหน้าที่ โดยเลือกใช้ "
        "X-Gateway-Key หรือ Bearer JWT อย่างใดอย่างหนึ่งเท่านั้น "
        "source=iot ต้องใช้ Gateway Key; source=manual ต้องใช้ JWT. "
        "ข้อมูลใหม่จะทำเครื่องหมายเพื่อรอประมวลผลการทำนาย "
        "ส่วนข้อมูลซ้ำที่ไม่มีการเพิ่มใหม่จะส่ง prediction_processing=unchanged."
    ),
    responses={
        400: {"description": "ส่ง Gateway Key และ Bearer JWT พร้อมกัน"},
        401: {"description": "ไม่ได้ยืนยันตัวตนหรือข้อมูลยืนยันตัวตนไม่ถูกต้อง"},
        403: {"description": "ไม่มีสิทธิ์ส่งข้อมูลหรือเข้าถึงอุปกรณ์/วอร์ด"},
        404: {"description": "ไม่พบ Encounter สำหรับข้อมูล Manual"},
        409: {"description": "Reading ID ขัดแย้ง หรือ Device Assignment ไม่ชัดเจน"},
        422: {"description": "ข้อมูลไม่ผ่าน Validation"},
    },
)
def ingest_observations(
    batch: ObservationBatch,
    identity: dict = Depends(get_observation_identity),
    db: Session = Depends(get_db),
):
    # Authentication
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

        for reading in batch.readings:
            ward_id = db.execute(
                text("""
                    SELECT ward_id
                    FROM ward_twin.encounters
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

        return save_manual_observations(batch, identity, db)

    producer_id = identity["producer_id"]
    allowed_devices = identity["allowed_devices"]
    received_at = datetime.now(timezone.utc)

    inserted = 0
    duplicates = 0
    items = []

    ids = [str(r.reading_id) for r in batch.readings]
    if len(ids) != len(set(ids)):
        raise HTTPException(
            status_code=422,
            detail="Duplicate reading_id within batch",
        )

    metric_units = {
        "heart_rate": "bpm",
        "respiratory_rate": "breaths/min",
        "spo2": "%",
        "systolic_bp": "mmHg",
        "diastolic_bp": "mmHg",
        "temperature": "Cel",
    }

    try:
        for reading in batch.readings:
            # Check gateway device permission
            if reading.device_id not in allowed_devices:
                raise HTTPException(
                    status_code=403,
                    detail="Device not allowed for this gateway",
                )

            # Check registered and active producer/device
            device = db.execute(
                text("""
                    SELECT d.ward_id
                    FROM ward_twin.devices d
                    JOIN ward_twin.producers p
                      ON p.producer_id = d.producer_id
                     AND p.ward_id = d.ward_id
                    WHERE d.device_id = :device_id
                      AND d.producer_id = :producer_id
                      AND d.active = TRUE
                      AND p.active = TRUE
                """),
                {
                    "device_id": reading.device_id,
                    "producer_id": producer_id,
                },
            ).mappings().one_or_none()

            if device is None:
                raise HTTPException(
                    status_code=403,
                    detail="Device or producer not registered or inactive",
                )

            # Resolve encounter from device assignment
            assignments = db.execute(
                text("""
                    SELECT da.encounter_id
                    FROM ward_twin.device_assignments da
                    JOIN ward_twin.encounters e
                      ON e.encounter_id = da.encounter_id
                    WHERE da.device_id = :device_id
                      AND da.valid_from <= :measured_at
                      AND (
                           da.valid_to IS NULL
                           OR :measured_at < da.valid_to
                      )
                      AND e.ward_id = :ward_id
                """),
                {
                    "device_id": reading.device_id,
                    "measured_at": reading.measured_at,
                    "ward_id": device["ward_id"],
                },
            ).scalars().all()

            if len(assignments) != 1:
                raise HTTPException(
                    status_code=409,
                    detail="Unmapped or ambiguous device assignment",
                )

            encounter_id = assignments[0]

            if (
                reading.encounter_id is not None
                and reading.encounter_id != encounter_id
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Encounter does not match device assignment",
                )

            measurements = [
                m.model_dump(mode="json")
                for m in reading.measurements
            ]

            metrics_seen = set()

            for m in measurements:
                metric = m["metric"]

                if metric not in metric_units:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Unsupported metric: {metric}",
                    )

                if metric in metrics_seen:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Duplicate metric: {metric}",
                    )

                metrics_seen.add(metric)

                if m["unit"] != metric_units[metric]:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Invalid unit for {metric}",
                    )

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

            content_hash = hashlib.sha256(
                canonical.encode("utf-8")
            ).hexdigest()

            # Existing reading / idempotency
            existing_hash = db.execute(
                text("""
                    SELECT content_hash
                    FROM ward_twin.observation_readings
                    WHERE producer_id = :producer_id
                      AND reading_id = :reading_id
                """),
                {
                    "producer_id": producer_id,
                    "reading_id": reading.reading_id,
                },
            ).scalar_one_or_none()

            if existing_hash is not None:
                if existing_hash != content_hash:
                    raise HTTPException(
                        status_code=409,
                        detail="Reading ID conflict",
                    )

                duplicates += 1
                result = "duplicate"

            else:
                reading_pk = db.execute(
                    text("""
                        INSERT INTO ward_twin.observation_readings (
                            producer_id,
                            reading_id,
                            encounter_id,
                            device_id,
                            source,
                            measured_at,
                            received_at,
                            content_hash
                        )
                        VALUES (
                            :producer_id,
                            :reading_id,
                            :encounter_id,
                            :device_id,
                            'iot',
                            :measured_at,
                            :received_at,
                            :content_hash
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
                        "measured_at": reading.measured_at,
                        "received_at": received_at,
                        "content_hash": content_hash,
                    },
                ).scalar_one_or_none()

                if reading_pk is None:
                    concurrent_hash = db.execute(
                        text("""
                            SELECT content_hash
                            FROM ward_twin.observation_readings
                            WHERE producer_id = :producer_id
                              AND reading_id = :reading_id
                        """),
                        {
                            "producer_id": producer_id,
                            "reading_id": reading.reading_id,
                        },
                    ).scalar_one()

                    if concurrent_hash != content_hash:
                        raise HTTPException(
                            status_code=409,
                            detail="Reading ID conflict",
                        )

                    duplicates += 1
                    result = "duplicate"

                else:
                    # Save individual measurements
                    for m in measurements:
                        db.execute(
                            text("""
                                INSERT INTO ward_twin.observation_measurements (
                                    reading_pk,
                                    metric,
                                    value,
                                    unit,
                                    quality_flag
                                )
                                VALUES (
                                    :reading_pk,
                                    :metric,
                                    :value,
                                    :unit,
                                    :quality_flag
                                )
                            """),
                            {
                                "reading_pk": reading_pk,
                                "metric": m["metric"],
                                "value": m["value"],
                                "unit": m["unit"],
                                "quality_flag": m.get("quality_flag"),
                            },
                        )

                    # Mark encounter dirty for prediction worker
                    db.execute(
                        text("""
                            INSERT INTO ward_twin.processing_state (
                                encounter_id,
                                dirty_version,
                                processed_version,
                                attempts
                            )
                            VALUES (:encounter_id, 1, 0, 0)
                            ON CONFLICT (encounter_id)
                            DO UPDATE SET
                                dirty_version =
                                    ward_twin.processing_state.dirty_version + 1
                        """),
                        {"encounter_id": encounter_id},
                    )

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
        "prediction_processing": "pending" if inserted > 0 else "unchanged",
    }


def save_manual_observations(
    batch: ObservationBatch,
    identity: dict,
    db: Session,
):
    if identity["type"] != "user":
        raise HTTPException(
            status_code=403,
            detail="Manual observations require user authentication",
        )

    user = identity["user"]
    user_id = user["sub"]
    if not user_id:
        raise HTTPException(
            status_code=403,
            detail="User identity is missing",
        )

    received_at = datetime.now(timezone.utc)

    metric_units = {
        "heart_rate": "bpm",
        "respiratory_rate": "breaths/min",
        "spo2": "%",
        "systolic_bp": "mmHg",
        "diastolic_bp": "mmHg",
        "temperature": "Cel",
    }

    ids = [str(r.reading_id) for r in batch.readings]
    if len(ids) != len(set(ids)):
        raise HTTPException(
            status_code=422,
            detail="Duplicate reading_id within batch",
        )

    inserted = 0
    duplicates = 0
    items = []

    try:
        for reading in batch.readings:
            if not reading.encounter_id:
                raise HTTPException(
                    status_code=422,
                    detail="Manual reading requires encounter_id",
                )

            ward_id = db.execute(
                text("""
                    SELECT ward_id
                    FROM ward_twin.encounters
                    WHERE encounter_id = :encounter_id
                """),
                {"encounter_id": reading.encounter_id},
            ).scalar_one_or_none()

            if ward_id is None:
                raise HTTPException(
                    status_code=404,
                    detail="Encounter not found",
                )

            require_ward(ward_id, user)

            measurements = [
                m.model_dump(mode="json")
                for m in reading.measurements
            ]

            metrics_seen = set()
            for m in measurements:
                metric = m["metric"]

                if metric not in metric_units:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Unsupported metric: {metric}",
                    )

                if metric in metrics_seen:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Duplicate metric: {metric}",
                    )

                metrics_seen.add(metric)

                if m["unit"] != metric_units[metric]:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Invalid unit for {metric}",
                    )

            payload = {
                "source": "manual",
                "encounter_id": reading.encounter_id,
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

            content_hash = hashlib.sha256(
                canonical.encode("utf-8")
            ).hexdigest()

            # Manual readings need a registered manual producer.
            # The producer ID must match an existing database record.
            producer_id = "manual-api"

            existing_hash = db.execute(
                text("""
                    SELECT content_hash
                    FROM ward_twin.observation_readings
                    WHERE producer_id = :producer_id
                      AND reading_id = :reading_id
                """),
                {
                    "producer_id": producer_id,
                    "reading_id": reading.reading_id,
                },
            ).scalar_one_or_none()

            if existing_hash is not None:
                if existing_hash != content_hash:
                    raise HTTPException(
                        status_code=409,
                        detail="Reading ID conflict",
                    )

                duplicates += 1
                result = "duplicate"

            else:
                reading_pk = db.execute(
                    text("""
                        INSERT INTO ward_twin.observation_readings (
                            producer_id,
                            reading_id,
                            encounter_id,
                            entered_by,
                            source,
                            measured_at,
                            received_at,
                            content_hash
                        )
                        VALUES (
                            :producer_id,
                            :reading_id,
                            :encounter_id,
                            :entered_by,
                            'manual',
                            :measured_at,
                            :received_at,
                            :content_hash
                        )
                        ON CONFLICT (producer_id, reading_id)
                        DO NOTHING
                        RETURNING id
                    """),
                    {
                        "producer_id": producer_id,
                        "reading_id": reading.reading_id,
                        "encounter_id": reading.encounter_id,
                        "entered_by": user_id,
                        "measured_at": reading.measured_at,
                        "received_at": received_at,
                        "content_hash": content_hash,
                    },
                ).scalar_one_or_none()

                if reading_pk is None:
                    concurrent_hash = db.execute(
                        text("""
                            SELECT content_hash
                            FROM ward_twin.observation_readings
                            WHERE producer_id = :producer_id
                              AND reading_id = :reading_id
                        """),
                        {
                            "producer_id": producer_id,
                            "reading_id": reading.reading_id,
                        },
                    ).scalar_one()

                    if concurrent_hash != content_hash:
                        raise HTTPException(
                            status_code=409,
                            detail="Reading ID conflict",
                        )

                    duplicates += 1
                    result = "duplicate"

                else:
                    for m in measurements:
                        db.execute(
                            text("""
                                INSERT INTO ward_twin.observation_measurements (
                                    reading_pk,
                                    metric,
                                    value,
                                    unit,
                                    quality_flag
                                )
                                VALUES (
                                    :reading_pk,
                                    :metric,
                                    :value,
                                    :unit,
                                    NULL
                                )
                            """),
                            {
                                "reading_pk": reading_pk,
                                "metric": m["metric"],
                                "value": m["value"],
                                "unit": m["unit"],
                            },
                        )

                    db.execute(
                        text("""
                            INSERT INTO ward_twin.processing_state (
                                encounter_id,
                                dirty_version,
                                processed_version,
                                attempts
                            )
                            VALUES (:encounter_id, 1, 0, 0)
                            ON CONFLICT (encounter_id)
                            DO UPDATE SET
                                dirty_version =
                                    ward_twin.processing_state.dirty_version + 1
                        """),
                        {"encounter_id": reading.encounter_id},
                    )

                    inserted += 1
                    result = "inserted"

            items.append({
                "reading_id": str(reading.reading_id),
                "encounter_id": reading.encounter_id,
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
        "prediction_processing": "pending" if inserted > 0 else "unchanged",
    }
