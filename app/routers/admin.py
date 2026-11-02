
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import bindparam, text
from sqlalchemy.exc import InvalidRequestError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.database import SessionLocal, get_db
from app.dependencies.auth import require_admin
from app.ml.model_loader import load_model_metadata
from app.schemas.auth import (
    ApproveAccountRequest,
    ApproveAccountResponse,
)
from app.services.admin_repository import (
    approve_account,
    list_pending_accounts,
)
from app.services.patient_response import (
    WORKFLOW_STATUS_LABELS,
    build_patient_prediction,
    get_patient_diagnostics,
    get_workflow_status,
)


router = APIRouter(
    prefix="/admin",
    tags=["Admin"],
)


class PendingAccountResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: str
    display_name: str
    email: str
    role: str
    status: str
    created_at: datetime


class AdminPatientPredictionResponse(BaseModel):
    status: str
    risk_score: float | None = None
    prediction_id: int | None = None
    model_version: str | None = None
    assessed_at: datetime | None = None
    unavailable_reason: str | None = None
    unavailable_message: str | None = None
    missing_features: list[str] = Field(default_factory=list)
    recommended_action: str | None = None


class AdminPatientResponse(BaseModel):
    patient_id: str
    hn: str | None = None
    display_name: str | None = None
    sex: str | None = None
    allergy_status: str | None = None
    encounter_id: str
    an: str | None = None
    ward_id: str
    bed_label: str | None = None
    admitted_at: datetime | None = None
    workflow_status: str
    workflow_status_label: str
    prediction: AdminPatientPredictionResponse


class AdminPatientsResponse(BaseModel):
    total: int
    limit: int
    offset: int
    patients: list[AdminPatientResponse]


def get_approval_db():
    """
    Separate database session for approval transactions.

    Authentication uses get_db().
    Approval uses get_approval_db().

    This prevents authentication queries from opening
    a transaction on the approval session.
    """
    with SessionLocal() as db:
        yield db


@router.get(
    "/patients",
    response_model=AdminPatientsResponse,
)
def get_admin_patients(
    ward_id: str | None = Query(default=None),
    search: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    normalized_search = search.strip() if search else None
    if normalized_search == "":
        normalized_search = None

    filters = []
    filter_params = {}
    if ward_id is not None:
        filters.append("e.ward_id = :ward_id")
        filter_params["ward_id"] = ward_id
    if normalized_search is not None:
        filters.append("""
            (
                strpos(
                    lower(coalesce(p.display_name, '')),
                    lower(:search)
                ) > 0
                OR strpos(
                    lower(coalesce(p.hn, '')),
                    lower(:search)
                ) > 0
                OR strpos(
                    lower(coalesce(e.an, '')),
                    lower(:search)
                ) > 0
            )
        """)
        filter_params["search"] = normalized_search
    where_clause = (
        f"WHERE {' AND '.join(filters)}"
        if filters
        else ""
    )

    try:
        total = db.execute(
            text(f"""
                SELECT COUNT(*)
                FROM ward_twin.encounters AS e
                JOIN ward_twin.patients AS p
                    ON p.patient_id = e.patient_id
                {where_clause}
            """),
            filter_params,
        ).scalar_one()

        rows = db.execute(
            text(f"""
                SELECT
                    p.patient_id,
                    p.hn,
                    p.display_name,
                    p.birth_date,
                    p.sex,
                    p.allergy_status,
                    e.encounter_id,
                    e.an,
                    e.ward_id,
                    e.bed_label,
                    e.admitted_at,
                    ps.dirty_version,
                    ps.processed_version,
                    rp.prediction_id,
                    rp.status AS prediction_status,
                    rp.probability,
                    rp.model_version,
                    rp.assessed_at,
                    rp.unavailable_reason
                FROM ward_twin.encounters AS e
                JOIN ward_twin.patients AS p
                    ON p.patient_id = e.patient_id
                LEFT JOIN ward_twin.processing_state AS ps
                    ON ps.encounter_id = e.encounter_id
                LEFT JOIN LATERAL (
                    SELECT
                        prediction_id,
                        status,
                        probability,
                        model_version,
                        assessed_at,
                        unavailable_reason
                    FROM ward_twin.risk_predictions
                    WHERE encounter_id = e.encounter_id
                    ORDER BY assessed_at DESC, prediction_id DESC
                    LIMIT 1
                ) AS rp ON TRUE
                {where_clause}
                ORDER BY
                    e.admitted_at DESC NULLS LAST,
                    e.ward_id,
                    e.bed_label NULLS LAST,
                    e.encounter_id
                LIMIT :limit
                OFFSET :offset
            """),
            {
                **filter_params,
                "limit": limit,
                "offset": offset,
            },
        ).mappings().all()

        page_rows = [dict(row) for row in rows]
        encounter_ids = [row["encounter_id"] for row in page_rows]
        diagnostics_as_of = datetime.now(timezone.utc)
        diagnostics = get_patient_diagnostics(
            db,
            ward_id=ward_id,
            encounter_ids=encounter_ids,
            as_of=diagnostics_as_of,
        )

        workflow_alerts_by_encounter = {}
        if encounter_ids:
            workflow_alerts = db.execute(
                text("""
                    SELECT
                        a.encounter_id,
                        a.state,
                        a.acknowledged_by,
                        EXISTS (
                            SELECT 1
                            FROM ward_twin.assessments AS assessment
                            WHERE assessment.alert_id = a.alert_id
                              AND assessment.encounter_id = a.encounter_id
                        ) AS has_assessment
                    FROM ward_twin.alerts AS a
                    WHERE a.encounter_id IN :encounter_ids
                """).bindparams(
                    bindparam("encounter_ids", expanding=True)
                ),
                {"encounter_ids": encounter_ids},
            ).mappings().all()

            for alert in workflow_alerts:
                workflow_alerts_by_encounter.setdefault(
                    alert["encounter_id"],
                    [],
                ).append(alert)

        feature_columns = load_model_metadata()["feature_columns"]
        patients = []
        for row in page_rows:
            prediction, _ = build_patient_prediction(
                row,
                diagnostics.get(
                    row["encounter_id"],
                    {"observation_count": 0},
                ),
                feature_columns=feature_columns,
                as_of=diagnostics_as_of,
            )
            workflow_status = get_workflow_status(
                workflow_alerts_by_encounter.get(row["encounter_id"], [])
            )
            patients.append({
                "patient_id": row["patient_id"],
                "hn": row["hn"],
                "display_name": row["display_name"],
                "sex": row["sex"],
                "allergy_status": row["allergy_status"],
                "encounter_id": row["encounter_id"],
                "an": row["an"],
                "ward_id": row["ward_id"],
                "bed_label": row["bed_label"],
                "admitted_at": row["admitted_at"],
                "workflow_status": workflow_status,
                "workflow_status_label": WORKFLOW_STATUS_LABELS[workflow_status],
                "prediction": prediction,
            })

        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "patients": patients,
        }

    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail="Admin patients temporarily unavailable",
        ) from exc


@router.get(
    "/accounts/pending",
    response_model=list[PendingAccountResponse],
)
def get_pending_accounts(
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        return list_pending_accounts(db)

    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail="Admin accounts temporarily unavailable",
        ) from exc


@router.patch(
    "/accounts/{user_id}/approve",
    response_model=ApproveAccountResponse,
)
def approve_pending_account(
    user_id: str,
    payload: ApproveAccountRequest,
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_approval_db),
):
    reviewer_id = admin.get("sub")

    if not reviewer_id:
        raise HTTPException(
            status_code=403,
            detail="Admin identity is required",
        )

    try:
        return approve_account(
            db,
            user_id=user_id,
            role=payload.role,
            ward_ids=payload.ward_ids,
            reviewed_by=reviewer_id,
        )

    except PermissionError as exc:
        raise HTTPException(
            status_code=403,
            detail=str(exc),
        ) from exc

    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc

    except InvalidRequestError as exc:
        raise HTTPException(
            status_code=503,
            detail="Approval transaction unavailable",
        ) from exc

    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail="Approval database unavailable",
        ) from exc
