from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import text
from app.core.database import SessionLocal

WORKER_ID = f"prediction-worker-{uuid4().hex[:8]}"
LOCK_SECONDS = 120


def claim_next_job():
    now = datetime.now(timezone.utc)
    locked_until = now + timedelta(seconds=LOCK_SECONDS)

    db = SessionLocal()

    try:
        with db.begin():
            row = db.execute(
                text("""
                    SELECT encounter_id, dirty_version, processed_version
                    FROM ward_twin.processing_state
                    WHERE dirty_version > processed_version
                      AND (locked_until IS NULL OR locked_until < :now)
                      AND (next_due_at IS NULL OR next_due_at <= :now)
                    ORDER BY encounter_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                """),
                {"now": now},
            ).mappings().first()

            if row is None:
                return None

            db.execute(
                text("""
                    UPDATE ward_twin.processing_state
                    SET locked_at = :now,
                        locked_by = :worker_id,
                        locked_until = :locked_until
                    WHERE encounter_id = :encounter_id
                """),
                {
                    "now": now,
                    "worker_id": WORKER_ID,
                    "locked_until": locked_until,
                    "encounter_id": row["encounter_id"],
                },
            )

            return {
                "encounter_id": row["encounter_id"],
                "claimed_version": row["dirty_version"],
                "worker_id": WORKER_ID,
            }
    finally:
        db.close()

from app.services.prediction_processor import process_prediction
from app.services.prediction_repository import save_prediction


def run_once():
    job = claim_next_job()

    if job is None:
        return {"status": "no_jobs"}

    encounter_id = job["encounter_id"]
    claimed_version = job["claimed_version"]
    worker_id = job["worker_id"]

    db = SessionLocal()

    try:
        with db.begin():
            # ล็อก processing_state ระหว่างบันทึกผล
            state = db.execute(
                text("""
                    SELECT dirty_version, processed_version,
                           locked_by, locked_until
                    FROM ward_twin.processing_state
                    WHERE encounter_id = :encounter_id
                    FOR UPDATE
                """),
                {"encounter_id": encounter_id},
            ).mappings().one()

            now = datetime.now(timezone.utc)

            if (
                state["locked_by"] != worker_id
                or state["locked_until"] is None
                or state["locked_until"] <= now
            ):
                raise RuntimeError("Worker lease expired or ownership lost")

            result = process_prediction(db, encounter_id, as_of=now)
            prediction_id = save_prediction(db, result)

            # ไม่ข้าม version ใหม่ที่เข้ามาระหว่างประมวลผล
            db.execute(
                text("""
                    UPDATE ward_twin.processing_state
                    SET
                        processed_version = GREATEST(
                            processed_version,
                            :claimed_version
                        ),
                        locked_at = NULL,
                        locked_by = NULL,
                        locked_until = NULL,
                        attempts = 0,
                        last_error = NULL,
                        last_success_at = :now
                    WHERE encounter_id = :encounter_id
                      AND locked_by = :worker_id
                      AND locked_until > :now
                """),
                {
                    "claimed_version": claimed_version,
                    "encounter_id": encounter_id,
                    "worker_id": worker_id,
                    "now": now,
                },
            )

            return {
                "status": "processed",
                "encounter_id": encounter_id,
                "prediction_id": prediction_id,
                "prediction_status": result["status"],
                "claimed_version": claimed_version,
            }

    except Exception as exc:
        # บันทึกข้อผิดพลาดเฉพาะกรณีที่ยังเป็นเจ้าของ Lock
        with SessionLocal() as error_db:
            with error_db.begin():
                error_db.execute(
                    text("""
                        UPDATE ward_twin.processing_state
                        SET
                            locked_at = NULL,
                            locked_by = NULL,
                            locked_until = NULL,
                            attempts = attempts + 1,
                            last_error = :error,
                            next_due_at = :next_due
                        WHERE encounter_id = :encounter_id
                          AND locked_by = :worker_id
                    """),
                    {
                        "encounter_id": encounter_id,
                        "worker_id": worker_id,
                        "error": str(exc)[:1000],
                        "next_due": datetime.now(timezone.utc)
                                    + timedelta(seconds=60),
                    },
                )

        raise
    finally:
        db.close()
