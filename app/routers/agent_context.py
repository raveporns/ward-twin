
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user, require_ward


router = APIRouter(tags=["AI Agent"])


# ============================================================
# Response Models
# ============================================================

class AgentVitalResponse(BaseModel):
    metric: str
    value: float | None = None
    unit: str | None = None
    measured_at: datetime | None = None
    source: str | None = None


class AgentRiskAssessmentResponse(BaseModel):
    status: str
    risk_score: float | None = Field(
        default=None,
        ge=0,
        le=1,
    )


class AgentPolicyResponse(BaseModel):
    mode: str = "read_only"
    clinical_decision: bool = False


class AgentContextResponse(BaseModel):
    encounter_id: str
    ward_id: str
    bed_label: str | None = None
    admitted_at: datetime | None = None
    data_version: int = 0
    processed_version: int = 0
    latest_vitals: list[AgentVitalResponse]
    risk_assessment: AgentRiskAssessmentResponse
    agent_policy: AgentPolicyResponse


# ============================================================
# Helpers
# ============================================================

def serialize_id(value):
    """Convert PostgreSQL UUID to JSON-compatible string."""
    if isinstance(value, UUID):
        return str(value)

    return value


def resolve_risk_assessment(
    dirty_version,
    processed_version,
    latest_prediction,
):
    """
    Resolve prediction status.

    pending:
        New observations are waiting for processing.

    ready:
        Latest prediction is available.

    not_available:
        No prediction has been produced.
    """

    result = {
        "status": "not_available",
        "risk_score": None,
    }

    has_pending_work = (
        dirty_version is not None
        and (
            processed_version is None
            or dirty_version > processed_version
        )
    )

    if has_pending_work:
        result["status"] = "pending"
        return result

    if latest_prediction is None:
        return result

    prediction_status = latest_prediction["status"]
    probability = latest_prediction["probability"]

    result["status"] = prediction_status

    if (
        prediction_status == "ready"
        and probability is not None
    ):
        result["risk_score"] = float(probability)

    return result


# ============================================================
# GET /encounters/{encounter_id}/agent-context
# ============================================================

@router.get(
    "/encounters/{encounter_id}/agent-context",
    response_model=AgentContextResponse,
    summary="Get AI Agent Context",
    description=(
        "ดึงข้อมูลบริบทของผู้ป่วยสำหรับ AI Agent "
        "ประกอบด้วยข้อมูล Encounter, Vital Signs ล่าสุด, "
        "สถานะการประมวลผล และผลประเมินความเสี่ยง "
        "Endpoint นี้เป็น Read-only และไม่ทำการตัดสินใจทางคลินิก"
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
def get_agent_context(
    encounter_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    # --------------------------------------------------------
    # 1. Get encounter and processing state
    # --------------------------------------------------------

    encounter = db.execute(
        text("""
            SELECT
                e.encounter_id,
                e.ward_id,
                e.bed_label,
                e.admitted_at,
                p.patient_id,
                p.display_name,
                ps.dirty_version,
                ps.processed_version
            FROM ward_twin.encounters AS e
            JOIN ward_twin.patients AS p
                ON p.patient_id = e.patient_id
            LEFT JOIN ward_twin.processing_state AS ps
                ON ps.encounter_id = e.encounter_id
            WHERE e.encounter_id = :encounter_id
        """),
        {
            "encounter_id": encounter_id,
        },
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
    # 3. Get latest vital signs per metric
    # --------------------------------------------------------

    vitals = db.execute(
        text("""
            SELECT DISTINCT ON (m.metric)
                m.metric,
                m.value,
                m.unit,
                r.measured_at,
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
        {
            "encounter_id": encounter_id,
        },
    ).mappings().all()

    # --------------------------------------------------------
    # 4. Build latest vitals
    # --------------------------------------------------------

    latest_vitals = []

    for row in vitals:
        latest_vitals.append({
            "metric": row["metric"],
            "value": (
                float(row["value"])
                if row["value"] is not None
                else None
            ),
            "unit": row["unit"],
            "measured_at": row["measured_at"],
            "source": row["source"],
        })

    # --------------------------------------------------------
    # 5. Get latest prediction
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
        {
            "encounter_id": encounter_id,
        },
    ).mappings().first()

    # --------------------------------------------------------
    # 6. Resolve risk assessment
    # --------------------------------------------------------

    risk_assessment = resolve_risk_assessment(
        dirty_version=encounter["dirty_version"],
        processed_version=encounter["processed_version"],
        latest_prediction=latest_prediction,
    )

    # --------------------------------------------------------
    # 7. Build response
    # --------------------------------------------------------

    return {
        "encounter_id": str(
            serialize_id(encounter["encounter_id"])
        ),
        "ward_id": encounter["ward_id"],
        "bed_label": encounter["bed_label"],
        "admitted_at": encounter["admitted_at"],
        "data_version": encounter["dirty_version"] or 0,
        "processed_version": (
            encounter["processed_version"] or 0
        ),
        "latest_vitals": latest_vitals,
        "risk_assessment": risk_assessment,
        "agent_policy": {
            "mode": "read_only",
            "clinical_decision": False,
        },
    }
