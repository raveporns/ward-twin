
import csv
import io
import json
from datetime import date, datetime
from enum import Enum
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import require_roles, require_ward


router = APIRouter(tags=["CSV Imports"])


# ============================================================
# Configuration
# ============================================================

COLUMNS = [
    "patient_id",
    "hn",
    "display_name",
    "birth_date",
    "sex",
    "encounter_id",
    "an",
    "ward_id",
    "bed_label",
    "admitted_at",
]

REQUIRED = [
    "patient_id",
    "hn",
    "display_name",
    "encounter_id",
    "ward_id",
    "admitted_at",
]

MAX_FILE_SIZE = 2 * 1024 * 1024
MAX_ROWS = 1000


# ============================================================
# Response Models
# ============================================================

class ImportCreatedResponse(BaseModel):
    import_id: str
    kind: str
    status: str
    total_rows: int = Field(ge=0)
    valid_rows: int = Field(ge=0)
    error_rows: int = Field(ge=0)
    committed: bool


class ImportCommittedResponse(BaseModel):
    import_id: str
    status: str
    inserted_rows: int = Field(ge=0)
    committed: bool


class ImportRowResponse(BaseModel):
    row_number: int
    payload: dict
    errors: list[str]
    valid: bool


class ImportDetailResponse(BaseModel):
    import_id: str
    kind: str
    status: str
    filename: str
    ward_id: str
    uploaded_by: str
    total_rows: int
    valid_rows: int
    error_rows: int
    created_at: str
    committed_at: str | None
    committed: bool
    rows: list[ImportRowResponse]


# ============================================================
# 1. POST /imports
# Upload, validate and stage CSV
# ============================================================

@router.post(
    "/imports",
    status_code=201,
    response_model=ImportCreatedResponse,
    summary="Validate and stage encounters CSV",
    responses={
        401: {"description": "Authentication required"},
        403: {"description": "Role or ward access denied"},
        413: {"description": "CSV exceeds 2 MB"},
        422: {"description": "Invalid CSV"},
    },
)
async def create_import(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: dict = Depends(require_roles("admin", "nurse")),
):
    # --------------------------------------------------------
    # Validate file
    # --------------------------------------------------------

    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(
            status_code=422,
            detail="Only CSV files are supported",
        )

    content = await file.read(MAX_FILE_SIZE + 1)

    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail="CSV file exceeds 2 MB",
        )

    try:
        decoded = content.decode("utf-8-sig")

        reader = csv.DictReader(
            io.StringIO(decoded, newline=""),
            restkey="__extra_columns__",
            strict=True,
        )

        if reader.fieldnames != COLUMNS:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "CSV columns do not match template",
                    "expected": COLUMNS,
                    "received": reader.fieldnames,
                },
            )

        records = list(reader)

    except UnicodeDecodeError:
        raise HTTPException(
            status_code=422,
            detail="CSV must use UTF-8 encoding",
        )

    except csv.Error:
        raise HTTPException(
            status_code=422,
            detail="Invalid CSV format",
        )

    if not records:
        raise HTTPException(
            status_code=422,
            detail="CSV contains no data rows",
        )

    if len(records) > MAX_ROWS:
        raise HTTPException(
            status_code=422,
            detail="Maximum 1000 rows per import",
        )

    # --------------------------------------------------------
    # Validate rows
    # --------------------------------------------------------

    seen_encounters = set()
    seen_an = set()

    patient_to_hn = {}
    hn_to_patient = {}

    ward_ids = set()
    staged_rows = []

    for row_number, row in enumerate(records, start=2):
        errors = []

        if "__extra_columns__" in row:
            errors.append("Unexpected extra columns")

        for column in REQUIRED:
            if not (row.get(column) or "").strip():
                errors.append(f"{column} is required")

        patient_id = (row.get("patient_id") or "").strip()
        hn = (row.get("hn") or "").strip()

        if patient_id and hn:
            if (
                patient_id in patient_to_hn
                and patient_to_hn[patient_id] != hn
            ):
                errors.append(
                    "Same patient_id has different HN in CSV"
                )

            if (
                hn in hn_to_patient
                and hn_to_patient[hn] != patient_id
            ):
                errors.append(
                    "Same HN has different patient_id in CSV"
                )

            patient_to_hn.setdefault(patient_id, hn)
            hn_to_patient.setdefault(hn, patient_id)

        # Encounter ID
        encounter_id = (row.get("encounter_id") or "").strip()

        if encounter_id:
            if encounter_id in seen_encounters:
                errors.append("Duplicate encounter_id in CSV")

            seen_encounters.add(encounter_id)

        # AN
        an = (row.get("an") or "").strip()

        if an:
            if an in seen_an:
                errors.append("Duplicate AN in CSV")

            seen_an.add(an)

            existing_an = db.execute(
                text("""
                    SELECT 1
                    FROM ward_twin.encounters
                    WHERE an = :an
                    LIMIT 1
                """),
                {"an": an},
            ).first()

            if existing_an:
                errors.append("AN already exists in database")

        # Ward access
        ward_id = (row.get("ward_id") or "").strip()

        if ward_id:
            ward_ids.add(ward_id)
            require_ward(ward_id, user)

        # Birth date
        birth_date = (row.get("birth_date") or "").strip()

        if birth_date:
            try:
                date.fromisoformat(birth_date)
            except ValueError:
                errors.append(
                    "Invalid birth_date; use YYYY-MM-DD"
                )

        # Admission datetime
        admitted_at = (row.get("admitted_at") or "").strip()

        if admitted_at:
            try:
                parsed = datetime.fromisoformat(
                    admitted_at.replace("Z", "+00:00")
                )

                if parsed.tzinfo is None:
                    errors.append(
                        "admitted_at must include timezone"
                    )

            except ValueError:
                errors.append("Invalid admitted_at")

        staged_rows.append({
            "row_number": row_number,
            "payload": {
                key: (row.get(key) or "").strip()
                for key in COLUMNS
            },
            "errors": errors,
        })

    # --------------------------------------------------------
    # Single ward per import
    # --------------------------------------------------------

    if len(ward_ids) != 1:
        raise HTTPException(
            status_code=422,
            detail="CSV must contain exactly one ward_id",
        )

    ward_id = next(iter(ward_ids))

    error_rows = sum(
        bool(row["errors"])
        for row in staged_rows
    )

    valid_rows = len(staged_rows) - error_rows

    import_status = (
        "invalid" if error_rows else "validated"
    )

    # --------------------------------------------------------
    # Save staging data
    # --------------------------------------------------------

    try:
        import_id = db.execute(
            text("""
                INSERT INTO ward_twin.import_jobs (
                    kind,
                    status,
                    uploaded_by,
                    ward_id,
                    filename,
                    total_rows,
                    valid_rows,
                    error_rows
                )
                VALUES (
                    'encounters',
                    :status,
                    :uploaded_by,
                    :ward_id,
                    :filename,
                    :total_rows,
                    :valid_rows,
                    :error_rows
                )
                RETURNING import_id
            """),
            {
                "status": import_status,
                "uploaded_by": user["sub"],
                "ward_id": ward_id,
                "filename": file.filename,
                "total_rows": len(staged_rows),
                "valid_rows": valid_rows,
                "error_rows": error_rows,
            },
        ).scalar_one()

        for row in staged_rows:
            db.execute(
                text("""
                    INSERT INTO ward_twin.import_rows (
                        import_id,
                        row_number,
                        payload,
                        errors
                    )
                    VALUES (
                        :import_id,
                        :row_number,
                        CAST(:payload AS JSONB),
                        CAST(:errors AS JSONB)
                    )
                """),
                {
                    "import_id": import_id,
                    "row_number": row["row_number"],
                    "payload": json.dumps(
                        row["payload"],
                        ensure_ascii=False,
                    ),
                    "errors": json.dumps(
                        row["errors"]
                    ),
                },
            )

        db.commit()

    except Exception:
        db.rollback()
        raise

    return {
        "import_id": str(import_id),
        "kind": "encounters",
        "status": import_status,
        "total_rows": len(staged_rows),
        "valid_rows": valid_rows,
        "error_rows": error_rows,
        "committed": False,
    }


# ============================================================
# 2. GET /imports/{import_id}
# Get import details
# ============================================================

@router.get(
    "/imports/{import_id}",
    response_model=ImportDetailResponse,
    summary="Get staged import details",
    responses={
        401: {"description": "Authentication required"},
        403: {"description": "Role or ward access denied"},
        404: {"description": "Import not found"},
        422: {"description": "Invalid import ID"},
    },
)
def get_import_detail(
    import_id: UUID,
    db: Session = Depends(get_db),
    user: dict = Depends(require_roles("admin", "nurse")),
):
    # --------------------------------------------------------
    # Find import job
    # --------------------------------------------------------

    job = db.execute(
        text("""
            SELECT
                import_id,
                kind,
                status,
                uploaded_by,
                ward_id,
                filename,
                total_rows,
                valid_rows,
                error_rows,
                created_at,
                committed_at
            FROM ward_twin.import_jobs
            WHERE import_id = :import_id
        """),
        {"import_id": import_id},
    ).mappings().first()

    if job is None:
        raise HTTPException(
            status_code=404,
            detail="Import not found",
        )

    # --------------------------------------------------------
    # Authorization
    # --------------------------------------------------------

    require_ward(job["ward_id"], user)

    # --------------------------------------------------------
    # Get staged rows
    # --------------------------------------------------------

    rows = db.execute(
        text("""
            SELECT
                row_number,
                payload,
                errors
            FROM ward_twin.import_rows
            WHERE import_id = :import_id
            ORDER BY row_number ASC
        """),
        {"import_id": import_id},
    ).mappings().all()

    # --------------------------------------------------------
    # Response
    # --------------------------------------------------------

    return {
        "import_id": str(job["import_id"]),
        "kind": job["kind"],
        "status": job["status"],
        "filename": job["filename"],
        "ward_id": job["ward_id"],
        "uploaded_by": str(job["uploaded_by"]),
        "total_rows": job["total_rows"],
        "valid_rows": job["valid_rows"],
        "error_rows": job["error_rows"],
        "created_at": job["created_at"].isoformat(),
        "committed_at": (
            job["committed_at"].isoformat()
            if job["committed_at"] is not None
            else None
        ),
        "committed": job["status"] == "committed",
        "rows": [
            {
                "row_number": row["row_number"],
                "payload": row["payload"],
                "errors": row["errors"],
                "valid": len(row["errors"]) == 0,
            }
            for row in rows
        ],
    }


# ============================================================
# 3. POST /imports/{import_id}/commit
# Commit validated import
# ============================================================

@router.post(
    "/imports/{import_id}/commit",
    response_model=ImportCommittedResponse,
    summary="Commit a validated encounters import",
    responses={
        401: {"description": "Authentication required"},
        403: {"description": "Role or ward access denied"},
        404: {"description": "Import not found"},
        409: {"description": "Import cannot be committed"},
        422: {"description": "Invalid import ID"},
    },
)
def commit_import(
    import_id: UUID,
    db: Session = Depends(get_db),
    user: dict = Depends(require_roles("admin")),
):
    try:
        # ----------------------------------------------------
        # Lock import job
        # ----------------------------------------------------

        job = db.execute(
            text("""
                SELECT
                    import_id,
                    status,
                    ward_id,
                    total_rows,
                    valid_rows,
                    error_rows
                FROM ward_twin.import_jobs
                WHERE import_id = :import_id
                FOR UPDATE
            """),
            {"import_id": import_id},
        ).mappings().first()

        if job is None:
            raise HTTPException(
                status_code=404,
                detail="Import not found",
            )

        require_ward(job["ward_id"], user)

        if job["status"] == "committed":
            raise HTTPException(
                status_code=409,
                detail="Import has already been committed",
            )

        if (
            job["status"] != "validated"
            or job["error_rows"] != 0
        ):
            raise HTTPException(
                status_code=409,
                detail="Import is not ready to commit",
            )

        # ----------------------------------------------------
        # Read staging data
        # ----------------------------------------------------

        rows = db.execute(
            text("""
                SELECT payload
                FROM ward_twin.import_rows
                WHERE import_id = :import_id
                ORDER BY row_number
            """),
            {"import_id": import_id},
        ).mappings().all()

        if len(rows) != job["total_rows"]:
            raise HTTPException(
                status_code=409,
                detail="Staged row count mismatch",
            )

        # ----------------------------------------------------
        # Revalidate staged data
        # ----------------------------------------------------

        patient_to_hn = {}
        hn_to_patient = {}
        seen_encounters = set()
        seen_an = set()

        for row in rows:
            payload = row["payload"]

            patient_id = (
                payload.get("patient_id") or ""
            ).strip()

            hn = (
                payload.get("hn") or ""
            ).strip()

            encounter_id = (
                payload.get("encounter_id") or ""
            ).strip()

            an = (
                payload.get("an") or ""
            ).strip()

            if not patient_id or not hn or not encounter_id:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Missing required identifiers "
                        "in staged import"
                    ),
                )

            if an:
                if an in seen_an:
                    raise HTTPException(
                        status_code=409,
                        detail="Duplicate AN in staged import",
                    )

                seen_an.add(an)

            if (
                patient_id in patient_to_hn
                and patient_to_hn[patient_id] != hn
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Same patient_id has different "
                        "HN in staged import"
                    ),
                )

            if (
                hn in hn_to_patient
                and hn_to_patient[hn] != patient_id
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Same HN has different "
                        "patient_id in staged import"
                    ),
                )

            if encounter_id in seen_encounters:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Duplicate encounter_id "
                        "in staged import"
                    ),
                )

            if payload.get("ward_id") != job["ward_id"]:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Staged row ward_id does not "
                        "match import ward"
                    ),
                )

            patient_to_hn[patient_id] = hn
            hn_to_patient[hn] = patient_id
            seen_encounters.add(encounter_id)

        # ----------------------------------------------------
        # Check existing encounters
        # ----------------------------------------------------

        for row in rows:
            payload = row["payload"]

            existing = db.execute(
                text("""
                    SELECT 1
                    FROM ward_twin.encounters
                    WHERE encounter_id = :encounter_id
                    LIMIT 1
                """),
                {
                    "encounter_id": payload["encounter_id"],
                },
            ).first()

            if existing:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "Encounter already exists",
                        "encounter_id": payload["encounter_id"],
                    },
                )

            an = (
                payload.get("an") or ""
            ).strip()

            if an:
                existing_an = db.execute(
                    text("""
                        SELECT 1
                        FROM ward_twin.encounters
                        WHERE an = :an
                        LIMIT 1
                    """),
                    {"an": an},
                ).first()

                if existing_an:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "message": "AN already exists",
                            "an": an,
                        },
                    )

        # ----------------------------------------------------
        # Insert patients and encounters
        # ----------------------------------------------------

        for row in rows:
            p = row["payload"]

            existing_patient = db.execute(
                text("""
                    SELECT patient_id, hn
                    FROM ward_twin.patients
                    WHERE patient_id = :patient_id
                       OR hn = :hn
                """),
                {
                    "patient_id": p["patient_id"],
                    "hn": p["hn"],
                },
            ).mappings().all()

            if existing_patient:
                if (
                    len(existing_patient) != 1
                    or str(existing_patient[0]["patient_id"])
                    != p["patient_id"]
                    or existing_patient[0]["hn"] != p["hn"]
                ):
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "message": "Patient ID or HN conflict",
                            "patient_id": p["patient_id"],
                            "hn": p["hn"],
                        },
                    )

            else:
                db.execute(
                    text("""
                        INSERT INTO ward_twin.patients (
                            patient_id,
                            hn,
                            display_name,
                            birth_date,
                            sex
                        )
                        VALUES (
                            :patient_id,
                            :hn,
                            :display_name,
                            CAST(:birth_date AS DATE),
                            :sex
                        )
                    """),
                    {
                        "patient_id": p["patient_id"],
                        "hn": p["hn"],
                        "display_name": p["display_name"],
                        "birth_date": (
                            p.get("birth_date") or None
                        ),
                        "sex": p.get("sex") or None,
                    },
                )

            db.execute(
                text("""
                    INSERT INTO ward_twin.encounters (
                        encounter_id,
                        patient_id,
                        ward_id,
                        an,
                        bed_label,
                        admitted_at
                    )
                    VALUES (
                        :encounter_id,
                        :patient_id,
                        :ward_id,
                        :an,
                        :bed_label,
                        CAST(:admitted_at AS TIMESTAMPTZ)
                    )
                """),
                {
                    "encounter_id": p["encounter_id"],
                    "patient_id": p["patient_id"],
                    "ward_id": p["ward_id"],
                    "an": p.get("an") or None,
                    "bed_label": p.get("bed_label") or None,
                    "admitted_at": p["admitted_at"],
                },
            )

        # ----------------------------------------------------
        # Mark import as committed
        # ----------------------------------------------------

        db.execute(
            text("""
                UPDATE ward_twin.import_jobs
                SET
                    status = 'committed',
                    committed_at = NOW()
                WHERE import_id = :import_id
            """),
            {"import_id": import_id},
        )

        db.commit()

        return {
            "import_id": str(import_id),
            "status": "committed",
            "inserted_rows": len(rows),
            "committed": True,
        }

    except HTTPException:
        db.rollback()
        raise

    except IntegrityError:
        db.rollback()

        raise HTTPException(
            status_code=409,
            detail=(
                "Database constraint conflict during import"
            ),
        )

    except Exception:
        db.rollback()
        raise
