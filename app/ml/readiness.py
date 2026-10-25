from datetime import datetime, timedelta, timezone
from sqlalchemy import text
from sqlalchemy.orm import Session

REQUIRED_METRICS = (
    "heart_rate",
    "respiratory_rate",
    "spo2",
    "systolic_bp",
    "diastolic_bp",
    "temperature",
)

POLICY_VERSION = "demo-readiness-v1"


def check_readiness(
    db: Session,
    encounter_id: str,
    as_of: datetime | None = None,
) -> dict:
    as_of = as_of or datetime.now(timezone.utc)

    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")

    from app.ml.patient_features import build_patient_features

    patient = build_patient_features(db, encounter_id, as_of)

    rows = db.execute(
        text("""
            SELECT
                m.metric,
                COUNT(*) AS sample_count,
                MAX(r.measured_at) AS latest_at
            FROM ward_twin.observation_readings r
            JOIN ward_twin.observation_measurements m
              ON m.reading_pk = r.id
            WHERE r.encounter_id = :encounter_id
              AND r.measured_at >= :start
              AND r.measured_at <= :as_of
              AND m.metric IN (
                  'heart_rate',
                  'respiratory_rate',
                  'spo2',
                  'systolic_bp',
                  'diastolic_bp',
                  'temperature'
              )
              AND m.value IS NOT NULL
              AND (m.quality_flag IS NULL OR m.quality_flag = 'valid')
            GROUP BY m.metric
        """),
        {
            "encounter_id": encounter_id,
            "start": as_of - timedelta(hours=12),
            "as_of": as_of,
        },
    ).mappings().all()

    by_metric = {row["metric"]: row for row in rows}
    reasons = []

    if patient["age_years"] is None:
        reasons.append("missing_age")

    if patient["sex_female"] is None:
        reasons.append("missing_sex")

    for metric in REQUIRED_METRICS:
        row = by_metric.get(metric)

        if row is None:
            reasons.append(f"missing_{metric}")
            continue

        if row["sample_count"] < 2:
            reasons.append(f"insufficient_samples_{metric}")

        if as_of - row["latest_at"] > timedelta(minutes=60):
            reasons.append(f"stale_{metric}")

    return {
        "ready": len(reasons) == 0,
        "status": "ready" if not reasons else "unavailable",
        "reasons": reasons,
        "policy_version": POLICY_VERSION,
    }
