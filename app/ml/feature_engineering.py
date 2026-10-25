from datetime import datetime, timedelta, timezone

import numpy as np
from sqlalchemy import text
from sqlalchemy.orm import Session


METRICS = [
    "heart_rate",
    "respiratory_rate",
    "spo2",
    "systolic_bp",
    "diastolic_bp",
    "temperature",
]


def build_vital_features(
    db: Session,
    encounter_id: str,
    as_of: datetime | None = None,
) -> dict:
    if as_of is None:
        as_of = datetime.now(timezone.utc)

    start = as_of - timedelta(hours=12)

    rows = db.execute(
        text("""
            SELECT
                r.measured_at,
                m.metric,
                m.value
            FROM ward_twin.observation_readings r
            JOIN ward_twin.observation_measurements m
              ON m.reading_pk = r.id
            WHERE r.encounter_id = :encounter_id
              AND r.measured_at >= :start
              AND r.measured_at <= :as_of
              AND m.value IS NOT NULL
              AND (m.quality_flag IS NULL OR m.quality_flag = 'valid')
              AND m.metric IN (
                  'heart_rate',
                  'respiratory_rate',
                  'spo2',
                  'systolic_bp',
                  'diastolic_bp',
                  'temperature'
              )
            ORDER BY r.measured_at ASC, r.id ASC
        """),
        {
            "encounter_id": encounter_id,
            "start": start,
            "as_of": as_of,
        },
    ).mappings().all()

    features = {}

    for metric in METRICS:
        samples = [
            (r["measured_at"], float(r["value"]))
            for r in rows
            if r["metric"] == metric
            and r["value"] is not None
        ]

        prefix = f"{metric}_"

        if not samples:
            for suffix in [
                "last_12h",
                "mean_12h",
                "min_12h",
                "max_12h",
                "std_12h",
                "slope_per_hour_12h",
            ]:
                features[prefix + suffix] = None
            continue

        values = np.array([v for _, v in samples], dtype=float)

        features[prefix + "last_12h"] = float(values[-1])
        features[prefix + "mean_12h"] = float(np.mean(values))
        features[prefix + "min_12h"] = float(np.min(values))
        features[prefix + "max_12h"] = float(np.max(values))
        features[prefix + "std_12h"] = (
            float(np.std(values)) if len(values) > 1 else 0.0
        )

        if len(samples) > 1:
            t0 = samples[0][0]
            hours = np.array([
                (t - t0).total_seconds() / 3600
                for t, _ in samples
            ])

            if np.ptp(hours) > 0:
                slope = float(np.polyfit(hours, values, 1)[0])
            else:
                slope = 0.0
        else:
            slope = 0.0

        features[prefix + "slope_per_hour_12h"] = slope

    return features
