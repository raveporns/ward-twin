from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.ml.model_loader import load_model


def create_alert_if_needed(
    db: Session,
    prediction_id: int,
    result: dict,
):
    # ไม่สร้าง Alert หากโมเดลยังไม่พร้อม
    if result["status"] != "ready":
        return None

    probability = result["probability"]

    if probability is None:
        return None

    # ใช้ Threshold จาก Metadata ของโมเดล
    _, metadata = load_model()
    threshold = float(metadata["threshold_demo"])

    if probability < threshold:
        return None

    alert_id = f"ALT-{uuid4().hex[:12]}"

    # ดึงข้อมูลจาก Prediction ที่บันทึกจริง
    prediction = db.execute(
        text("""
            SELECT
                prediction_id,
                encounter_id,
                model_version,
                probability,
                status
            FROM ward_twin.risk_predictions
            WHERE prediction_id = :prediction_id
        """),
        {"prediction_id": prediction_id},
    ).mappings().one_or_none()

    if prediction is None:
        raise ValueError("Prediction not found")

    if prediction["status"] != "ready":
        return None

    if abs(float(prediction["probability"]) - probability) > 1e-6:
        raise ValueError("Prediction probability mismatch")

    # UNIQUE(prediction_id) ป้องกัน Alert ซ้ำ
    created = db.execute(
        text("""
            INSERT INTO ward_twin.alerts (
                alert_id,
                encounter_id,
                prediction_id,
                state,
                trigger_probability,
                trigger_threshold,
                model_version
            )
            VALUES (
                :alert_id,
                :encounter_id,
                :prediction_id,
                'open',
                :probability,
                :threshold,
                :model_version
            )
            ON CONFLICT (prediction_id)
            DO NOTHING
            RETURNING alert_id
        """),
        {
            "alert_id": alert_id,
            "encounter_id": prediction["encounter_id"],
            "prediction_id": prediction_id,
            "probability": probability,
            "threshold": threshold,
            "model_version": prediction["model_version"],
        },
    ).scalar_one_or_none()

    return created
