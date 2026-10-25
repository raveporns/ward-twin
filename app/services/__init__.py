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
                    SELECT
                        encounter_id,
                        dirty_version,
                        processed_version
                    FROM ward_twin.processing_state
                    WHERE dirty_version > processed_version
                      AND (
                          locked_until IS NULL
                          OR locked_until < :now
                      )
                      AND (
                          next_due_at IS NULL
                          OR next_due_at <= :now
                      )
                    ORDER BY encounter_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                """),
                {"now": now},
            ).mappings().first()

            if row is None:
                return None

            encounter_id = row["encounter_id"]

            db.execute(
                text("""
                    UPDATE ward_twin.processing_state
                    SET
                        locked_at = :now,
                        locked_by = :worker_id,
                        locked_until = :locked_until
                    WHERE encounter_id = :encounter_id
                """),
                {
                    "now": now,
                    "worker_id": WORKER_ID,
                    "locked_until": locked_until,
                    "encounter_id": encounter_id,
                },
            )

            return {
                "encounter_id": encounter_id,
                "claimed_version": row["dirty_version"],
                "worker_id": WORKER_ID,
            }

    finally:
        db.close()