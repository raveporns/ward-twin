
from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import require_roles, require_ward


router = APIRouter(tags=["Digital Twin"])


# ============================================================
# Response Models
# ============================================================

class EncounterTwinResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    encounter_id: str
    patient_id: str
    hn: str | None = None
    display_name: str | None = None
    birth_date: date | None = None
    sex: str | None = None
    allergy_status: str | None = None
    an: str | None = None
    ward_id: str
    bed_label: str | None = None
    admitted_at: datetime | None = None


class VitalSignResponse(BaseModel):
    metric: str
    value: float | None = None
    unit: str
    measured_at: datetime | None = None
    device_id: str | None = None
    source: str


class TwinPredictionResponse(BaseModel):
    status: str
    risk_score: float | None = Field(
        default=None,
        ge=0,
        le=1,
    )
    prediction_id: str | None = None
    model_version: str | None = None
    assessed_at: datetime | None = None
    unavailable_reason: str | None = None


class PatientTwinResponse(BaseModel):
    encounter: EncounterTwinResponse
    latest_vitals: list[VitalSignResponse]
    prediction: TwinPredictionResponse


# ============================================================
# Helpers
# ============================================================

def serialize_value(value: Any) -> Any:
    """Convert UUID values to JSON-compatible strings."""
    if isinstance(value, UUID):
        return str(value)

    return value


def build_prediction(
    processing,
    latest_prediction,
) -> dict:
    prediction_data = {
        "status": "not_available",
        "risk_score": None,
        "prediction_id": None,
        "model_version": None,
        "assessed_at": None,
        "unavailable_reason": None,
    }

    has_pending_work = (
        processing is not None
        and processing["dirty_version"] is not None
        and (
            processing["processed_version"] is None
            or processing["dirty_version"]
            > processing["processed_version"]
        )
    )

    if has_pending_work:
        prediction_data["status"] = "pending"
        return prediction_data

    if latest_prediction is None:
        return prediction_data

    prediction_status = latest_prediction["status"]
    probability = latest_prediction["probability"]

    prediction_data = {
        "status": prediction_status,
        "risk_score": (
            float(probability)
            if (
                prediction_status == "ready"
                and probability is not None
            )
            else None
        ),
        "prediction_id": (
        str(latest_prediction["prediction_id"])
        if latest_prediction["prediction_id"] is not None
        else None
        ),
        "model_version": latest_prediction["model_version"],
        "assessed_at": latest_prediction["assessed_at"],
        "unavailable_reason": (
            latest_prediction["unavailable_reason"]
        ),
    }

    return prediction_data


# ============================================================
# GET /encounters/{encounter_id}/twin
# ============================================================

@router.get(
    "/encounters/{encounter_id}/twin",
    response_model=PatientTwinResponse,
    summary="Get Patient Digital Twin",
    description=(
        "ดึงข้อมูล Digital Twin ของผู้ป่วย ประกอบด้วย "
        "ข้อมูล Encounter สัญญาณชีพล่าสุดของแต่ละ Metric "
        "และผลประเมินความเสี่ยงจากโมเดล "
        "หากมีข้อมูลใหม่รอประมวลผล จะแสดง Prediction เป็น pending"
    ),
    responses={
        401: {
            "description": "Authentication required",
        },
        403: {
            "description": "Ward access denied",
        },
        404: {
            "description": "Encounter not found",
        },
        422: {
            "description": "Invalid request parameters",
        },
    },
)
def get_patient_twin(
    encounter_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(
        require_roles("admin", "doctor", "nurse", "viewer")
    ),
):
    # --------------------------------------------------------
    # 1. Get patient and encounter
    # --------------------------------------------------------

    encounter = db.execute(
        text("""
            SELECT
                e.encounter_id,
                e.patient_id,
                p.hn,
                p.display_name,
                p.birth_date,
                p.sex,
                p.allergy_status,
                e.an,
                e.ward_id,
                e.bed_label,
                e.admitted_at
            FROM ward_twin.encounters AS e
            JOIN ward_twin.patients AS p
                ON p.patient_id = e.patient_id
            WHERE e.encounter_id = :encounter_id
        """),
        {"encounter_id": encounter_id},
    ).mappings().first()

    if encounter is None:
        raise HTTPException(
            status_code=404,
            detail="Encounter not found",
        )

    # --------------------------------------------------------
    # 2. Ward authorization
    # --------------------------------------------------------

    require_ward(
        encounter["ward_id"],
        user,
    )

    # --------------------------------------------------------
    # 3. Latest vital signs per metric
    # --------------------------------------------------------

    observations = db.execute(
        text("""
            SELECT DISTINCT ON (m.metric)
                m.metric,
                m.value,
                m.unit,
                r.measured_at,
                r.device_id,
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

    # --------------------------------------------------------
    # 4. Build encounter response
    # --------------------------------------------------------

    encounter_data = {
        key: serialize_value(value)
        for key, value in dict(encounter).items()
    }

    # --------------------------------------------------------
    # 5. Build latest vitals
    # --------------------------------------------------------

    latest_vitals = []

    for row in observations:
        latest_vitals.append({
            "metric": row["metric"],
            "value": (
                float(row["value"])
                if row["value"] is not None
                else None
            ),
            "unit": row["unit"],
            "measured_at": row["measured_at"],
            "device_id": serialize_value(row["device_id"]),
            "source": row["source"],
        })

    # --------------------------------------------------------
    # 6. Processing queue status
    # --------------------------------------------------------

    processing = db.execute(
        text("""
            SELECT
                dirty_version,
                processed_version
            FROM ward_twin.processing_state
            WHERE encounter_id = :encounter_id
        """),
        {"encounter_id": encounter_id},
    ).mappings().first()

    # --------------------------------------------------------
    # 7. Latest risk prediction
    # --------------------------------------------------------

    latest_prediction = db.execute(
        text("""
            SELECT
                prediction_id,
                status,
                probability,
                unavailable_reason,
                model_version,
                assessed_at
            FROM ward_twin.risk_predictions
            WHERE encounter_id = :encounter_id
            ORDER BY
                assessed_at DESC,
                prediction_id DESC
            LIMIT 1
        """),
        {"encounter_id": encounter_id},
    ).mappings().first()

    # --------------------------------------------------------
    # 8. Resolve prediction state
    # --------------------------------------------------------

    prediction_data = build_prediction(
        processing=processing,
        latest_prediction=latest_prediction,
    )

    # --------------------------------------------------------
    # 9. Return Digital Twin
    # --------------------------------------------------------

    return {
        "encounter": encounter_data,
        "latest_vitals": latest_vitals,
        "prediction": prediction_data,
    }
