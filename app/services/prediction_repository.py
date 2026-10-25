from datetime import datetime, timedelta, timezone
import json

from sqlalchemy import text
from sqlalchemy.orm import Session

MODEL_VERSION = "ward-twin-mock-xgb-v1"
HORIZON_HOURS = 6


def save_prediction(db: Session, result: dict) -> int:
    as_of = result["as_of"]
    assessed_at = datetime.now(timezone.utc)

    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")

    if assessed_at < as_of:
        raise ValueError("assessed_at must not precede as_of")

    status = result["status"]
    probability = result["probability"]
    reason = result["unavailable_reason"]

    if status == "ready":
        if probability is None or not 0 <= probability <= 1:
            raise ValueError("ready requires a valid probability")
        if reason is not None:
            raise ValueError("ready must not have unavailable_reason")
    elif status == "unavailable":
        if probability is not None or not reason:
            raise ValueError(
                "unavailable requires null probability and a reason"
            )
    else:
        raise ValueError(f"Invalid status: {status}")

    features_json = (
        json.dumps(result["features"], allow_nan=False)
        if result["features"] is not None
        else None
    )

    prediction_id = db.execute(
        text("""
            INSERT INTO ward_twin.risk_predictions (
                encounter_id,
                probability,
                model_version,
                as_of,
                assessed_at,
                prediction_window_end,
                horizon_hours,
                feature_cutoff,
                freshness_policy_version,
                status,
                unavailable_reason,
                features
            )
            VALUES (
                :encounter_id,
                :probability,
                :model_version,
                :as_of,
                :assessed_at,
                :prediction_window_end,
                :horizon_hours,
                :feature_cutoff,
                :policy_version,
                :status,
                :unavailable_reason,
                CAST(:features AS jsonb)
            )
            ON CONFLICT (encounter_id, model_version, as_of)
            DO NOTHING
            RETURNING prediction_id
        """),
        {
            "encounter_id": result["encounter_id"],
            "probability": probability,
            "model_version": MODEL_VERSION,
            "as_of": as_of,
            "assessed_at": assessed_at,
            "prediction_window_end": as_of + timedelta(hours=HORIZON_HOURS),
            "horizon_hours": HORIZON_HOURS,
            "feature_cutoff": as_of,
            "policy_version": result["policy_version"],
            "status": status,
            "unavailable_reason": reason,
            "features": features_json,
        },
    ).scalar_one_or_none()

    if prediction_id is None:
        prediction_id = db.execute(
            text("""
                SELECT prediction_id
                FROM ward_twin.risk_predictions
                WHERE encounter_id = :encounter_id
                  AND model_version = :model_version
                  AND as_of = :as_of
            """),
            {
                "encounter_id": result["encounter_id"],
                "model_version": MODEL_VERSION,
                "as_of": as_of,
            },
        ).scalar_one()

    return prediction_id
