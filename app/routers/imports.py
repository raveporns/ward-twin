
import csv
import hashlib
import io
import json
import re
from datetime import date, datetime, timezone
from math import isfinite
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import require_roles, require_ward
from app.routers.import_templates import ImportTemplateKind
from app.schemas.observation import Measurement, Metric, Reading


router = APIRouter(tags=["CSV Imports"])


# ============================================================
# Configuration
# ============================================================

IMPORT_COLUMNS = {
    ImportTemplateKind.encounters: [
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
    ],
    ImportTemplateKind.observations: [
        "reading_id",
        "encounter_id",
        "measured_at",
        "metric",
        "value",
        "unit",
    ],
    ImportTemplateKind.outcomes: [
        "event_id",
        "encounter_id",
        "event_type",
        "event_at",
        "unplanned",
    ],
}

IMPORT_REQUIRED = {
    ImportTemplateKind.encounters: [
        "patient_id",
        "hn",
        "display_name",
        "encounter_id",
        "ward_id",
        "admitted_at",
    ],
    ImportTemplateKind.observations: [
        "reading_id",
        "encounter_id",
        "measured_at",
        "metric",
        "value",
        "unit",
    ],
    ImportTemplateKind.outcomes: [
        "event_id",
        "encounter_id",
        "event_type",
        "event_at",
    ],
}

MAX_FILE_SIZE = 2 * 1024 * 1024
MAX_ROWS = 1000
OUTCOME_TYPES = {
    "icu_transfer",
    "discharge",
    "death",
    "followup_end",
}


def _parse_aware_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _validate_encounter_rows(
    records: list[dict],
    db: Session,
    user: dict,
) -> tuple[list[dict], set[str]]:
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

        for column in IMPORT_REQUIRED[ImportTemplateKind.encounters]:
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

        encounter_id = (row.get("encounter_id") or "").strip()
        if encounter_id:
            if encounter_id in seen_encounters:
                errors.append("Duplicate encounter_id in CSV")
            seen_encounters.add(encounter_id)

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

        ward_id = (row.get("ward_id") or "").strip()
        if ward_id:
            ward_ids.add(ward_id)
            require_ward(ward_id, user)

        birth_date = (row.get("birth_date") or "").strip()
        if birth_date:
            try:
                date.fromisoformat(birth_date)
            except ValueError:
                errors.append(
                    "Invalid birth_date; use YYYY-MM-DD"
                )

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
                for key in IMPORT_COLUMNS[ImportTemplateKind.encounters]
            },
            "errors": errors,
        })

    return staged_rows, ward_ids


def _validate_observation_rows(records: list[dict]) -> list[dict]:
    staged_rows = []
    reading_groups: dict[str, list[dict]] = {}

    for row_number, row in enumerate(records, start=2):
        errors = []
        if "__extra_columns__" in row:
            errors.append("Unexpected extra columns")

        payload = {
            key: (row.get(key) or "").strip()
            for key in IMPORT_COLUMNS[ImportTemplateKind.observations]
        }
        for column in IMPORT_REQUIRED[ImportTemplateKind.observations]:
            if not payload[column]:
                errors.append(f"{column} is required")

        reading_id = None
        if payload["reading_id"]:
            try:
                reading_id = str(UUID(payload["reading_id"]))
                payload["reading_id"] = reading_id
            except ValueError:
                errors.append("reading_id must be a UUID")

        encounter_id = payload["encounter_id"]
        if encounter_id and (
            len(encounter_id) > 64
            or re.fullmatch(r"[A-Za-z0-9_-]+", encounter_id) is None
        ):
            errors.append("encounter_id has an invalid format")

        measured_at = (
            _parse_aware_datetime(payload["measured_at"])
            if payload["measured_at"]
            else None
        )
        if payload["measured_at"] and measured_at is None:
            errors.append(
                "measured_at must be ISO 8601 with timezone"
            )

        metric = None
        if payload["metric"]:
            try:
                metric = Metric(payload["metric"])
            except ValueError:
                errors.append("metric is not supported")

        measurement = None
        if metric is not None and payload["value"] and payload["unit"]:
            try:
                value = float(payload["value"])
                if not isfinite(value):
                    raise ValueError("value must be finite")
                measurement = Measurement(
                    metric=metric,
                    value=value,
                    unit=payload["unit"],
                )
            except ValidationError as exc:
                errors.extend(item["msg"] for item in exc.errors())
            except (ValueError, TypeError) as exc:
                errors.append(str(exc) or "value or unit is invalid")
        elif payload["value"] and metric is not None:
            try:
                value = float(payload["value"])
                if not isfinite(value):
                    errors.append("value must be finite")
            except ValueError:
                errors.append("value must be a number")

        staged_row = {
            "row_number": row_number,
            "payload": payload,
            "errors": errors,
        }
        staged_rows.append(staged_row)

        if (
            reading_id is not None
            and encounter_id
            and measured_at is not None
            and measurement is not None
        ):
            reading_groups.setdefault(reading_id, []).append({
                "staged_row": staged_row,
                "encounter_id": encounter_id,
                "measured_at": measured_at,
                "measurement": measurement,
            })

    for reading_id, group in reading_groups.items():
        first = group[0]
        seen_metrics = set()
        group_measurements = []
        group_errors = []
        duplicate_metric_found = False

        for item in group:
            metric = item["measurement"].metric
            if metric in seen_metrics:
                duplicate_metric_found = True
                item["staged_row"]["errors"].append(
                    "Duplicate metric for reading_id"
                )
            seen_metrics.add(metric)

            if item["encounter_id"] != first["encounter_id"]:
                item["staged_row"]["errors"].append(
                    "encounter_id must match for the same reading_id"
                )
            if (
                item["measured_at"].astimezone(timezone.utc)
                != first["measured_at"].astimezone(timezone.utc)
            ):
                item["staged_row"]["errors"].append(
                    "measured_at must match for the same reading_id"
                )

            group_measurements.append(item["measurement"])

        if len(group_measurements) > 6:
            group_errors.append(
                "A reading may contain at most 6 metrics"
            )

        if not group_errors and not duplicate_metric_found:
            try:
                Reading(
                    reading_id=reading_id,
                    encounter_id=first["encounter_id"],
                    measured_at=first["measured_at"],
                    measurements=group_measurements,
                )
            except ValidationError as exc:
                group_errors.extend(
                    item["msg"] for item in exc.errors()
                )

        for message in group_errors:
            for item in group:
                item["staged_row"]["errors"].append(
                    f"reading_id {reading_id}: {message}"
                )

    return staged_rows


def _validate_outcome_rows(records: list[dict]) -> list[dict]:
    staged_rows = []
    seen_event_ids = set()

    for row_number, row in enumerate(records, start=2):
        errors = []
        if "__extra_columns__" in row:
            errors.append("Unexpected extra columns")

        payload = {
            key: (
                row.get(key)
                if key == "unplanned" and isinstance(row.get(key), bool)
                else str(row.get(key) or "").strip()
            )
            for key in IMPORT_COLUMNS[ImportTemplateKind.outcomes]
        }
        for column in IMPORT_REQUIRED[ImportTemplateKind.outcomes]:
            if not payload[column]:
                errors.append(f"{column} is required")

        event_id = payload["event_id"]
        if event_id:
            if event_id in seen_event_ids:
                errors.append("Duplicate event_id in CSV")
            seen_event_ids.add(event_id)

        event_type = payload["event_type"]
        if event_type and event_type not in OUTCOME_TYPES:
            errors.append("event_type is not supported")

        unplanned = (
            str(payload["unplanned"]).lower()
            if payload["unplanned"] is not None
            else ""
        )
        if event_type == "icu_transfer":
            if unplanned not in {"true", "false"}:
                errors.append(
                    "unplanned must be true or false for icu_transfer"
                )
            else:
                payload["unplanned"] = unplanned == "true"
        elif event_type and unplanned:
            errors.append(
                "unplanned must be empty unless event_type is icu_transfer"
            )
        else:
            payload["unplanned"] = None

        event_at = (
            _parse_aware_datetime(payload["event_at"])
            if payload["event_at"]
            else None
        )
        if payload["event_at"] and event_at is None:
            errors.append("event_at must be ISO 8601 with timezone")

        staged_rows.append({
            "row_number": row_number,
            "payload": payload,
            "errors": errors,
        })

    return staged_rows


def _resolve_encounter_import_ward(
    kind: ImportTemplateKind,
    staged_rows: list[dict],
    db: Session,
    user: dict,
    requested_ward_id: str | None,
) -> str:
    if requested_ward_id:
        require_ward(requested_ward_id, user)

    encounter_ids = sorted({
        row["payload"].get("encounter_id", "")
        for row in staged_rows
        if row["payload"].get("encounter_id")
    })
    encounters = {}
    if encounter_ids:
        encounters = {
            str(row["encounter_id"]): row
            for row in db.execute(
                text("""
                    SELECT encounter_id, ward_id, admitted_at
                    FROM ward_twin.encounters
                    WHERE encounter_id = ANY(:encounter_ids)
                """),
                {"encounter_ids": encounter_ids},
            ).mappings().all()
        }

    found_wards = set()
    for staged_row in staged_rows:
        payload = staged_row["payload"]
        encounter_id = payload.get("encounter_id", "")
        encounter = encounters.get(encounter_id)
        if not encounter:
            if encounter_id:
                staged_row["errors"].append(
                    "encounter_id does not exist"
                )
            continue

        encounter_ward_id = str(encounter["ward_id"])
        found_wards.add(encounter_ward_id)
        require_ward(encounter_ward_id, user)

        if requested_ward_id and encounter_ward_id != requested_ward_id:
            staged_row["errors"].append(
                "encounter_id does not belong to ward_id"
            )

        if kind == ImportTemplateKind.outcomes:
            event_at = _parse_aware_datetime(
                payload.get("event_at", "")
            )
            admitted_at = encounter["admitted_at"]
            if event_at is not None and admitted_at is not None:
                if (
                    admitted_at.tzinfo is None
                    or admitted_at.utcoffset() is None
                ):
                    staged_row["errors"].append(
                        "Encounter admitted_at has no timezone"
                    )
                elif event_at < admitted_at:
                    staged_row["errors"].append(
                        "event_at must not precede encounter admission"
                    )

    if len(found_wards) > 1:
        raise HTTPException(
            status_code=422,
            detail="CSV must reference encounters in exactly one ward",
        )

    if requested_ward_id:
        return requested_ward_id
    if found_wards:
        return next(iter(found_wards))
    raise HTTPException(
        status_code=422,
        detail=(
            "ward_id is required when the CSV has no existing "
            "encounter from which to determine its ward"
        ),
    )


def _commit_observation_import(
    *,
    db: Session,
    rows: list[dict],
    ward_id: str,
) -> int:
    producer = db.execute(
        text("""
            SELECT producer_id
            FROM ward_twin.producers
            WHERE ward_id = :ward_id
              AND type = 'csv'
              AND active = TRUE
            LIMIT 1
            FOR SHARE
        """),
        {"ward_id": ward_id},
    ).mappings().one_or_none()
    if producer is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "Observation import is blocked: no active CSV producer "
                "is registered for this ward"
            ),
        )
    producer_id = producer["producer_id"]

    records = [row["payload"] for row in rows]
    validated_rows = _validate_observation_rows(records)
    if any(row["errors"] for row in validated_rows):
        raise HTTPException(
            status_code=409,
            detail="Staged observation data failed commit validation",
        )

    encounter_ids = sorted({
        row["payload"]["encounter_id"]
        for row in validated_rows
    })
    encounter_rows = db.execute(
        text("""
            SELECT encounter_id, ward_id
            FROM ward_twin.encounters
            WHERE encounter_id = ANY(:encounter_ids)
            FOR SHARE
        """),
        {"encounter_ids": encounter_ids},
    ).mappings().all()
    encounters = {
        str(row["encounter_id"]): str(row["ward_id"])
        for row in encounter_rows
    }

    if len(encounters) != len(encounter_ids):
        raise HTTPException(
            status_code=409,
            detail=(
                "An encounter referenced by the staged import "
                "no longer exists"
            ),
        )
    if any(encounter_ward != ward_id for encounter_ward in encounters.values()):
        raise HTTPException(
            status_code=409,
            detail=(
                "An encounter Ward changed after upload; "
                "the import must be uploaded again"
            ),
        )

    groups: dict[str, list[dict]] = {}
    for row in validated_rows:
        groups.setdefault(row["payload"]["reading_id"], []).append(
            row["payload"]
        )

    inserted_readings = 0
    received_at = datetime.now(timezone.utc)
    for reading_id, reading_rows in groups.items():
        first = reading_rows[0]
        measured_at = _parse_aware_datetime(first["measured_at"])
        measurements = sorted(
            [
                {
                    "metric": row["metric"],
                    "value": float(row["value"]),
                    "unit": row["unit"],
                }
                for row in reading_rows
            ],
            key=lambda measurement: measurement["metric"],
        )
        canonical_payload = {
            "source": "csv",
            "encounter_id": first["encounter_id"],
            "measured_at": measured_at.astimezone(
                timezone.utc
            ).isoformat(),
            "measurements": measurements,
        }
        canonical = json.dumps(
            canonical_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        content_hash = hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest()

        existing_hash = db.execute(
            text("""
                SELECT content_hash
                FROM ward_twin.observation_readings
                WHERE producer_id = :producer_id
                  AND reading_id = :reading_id
            """),
            {
                "producer_id": producer_id,
                "reading_id": reading_id,
            },
        ).scalar_one_or_none()

        if existing_hash is not None:
            if existing_hash != content_hash:
                raise HTTPException(
                    status_code=409,
                    detail="Reading ID conflict",
                )
            continue

        reading_pk = db.execute(
            text("""
                INSERT INTO ward_twin.observation_readings (
                    producer_id,
                    reading_id,
                    encounter_id,
                    source,
                    measured_at,
                    received_at,
                    content_hash
                )
                VALUES (
                    :producer_id,
                    :reading_id,
                    :encounter_id,
                    'csv',
                    :measured_at,
                    :received_at,
                    :content_hash
                )
                ON CONFLICT (producer_id, reading_id)
                DO NOTHING
                RETURNING id
            """),
            {
                "producer_id": producer_id,
                "reading_id": reading_id,
                "encounter_id": first["encounter_id"],
                "measured_at": measured_at,
                "received_at": received_at,
                "content_hash": content_hash,
            },
        ).scalar_one_or_none()

        if reading_pk is None:
            concurrent_hash = db.execute(
                text("""
                    SELECT content_hash
                    FROM ward_twin.observation_readings
                    WHERE producer_id = :producer_id
                      AND reading_id = :reading_id
                """),
                {
                    "producer_id": producer_id,
                    "reading_id": reading_id,
                },
            ).scalar_one()
            if concurrent_hash != content_hash:
                raise HTTPException(
                    status_code=409,
                    detail="Reading ID conflict",
                )
            continue

        for measurement in measurements:
            db.execute(
                text("""
                    INSERT INTO ward_twin.observation_measurements (
                        reading_pk,
                        metric,
                        value,
                        unit,
                        quality_flag
                    )
                    VALUES (
                        :reading_pk,
                        :metric,
                        :value,
                        :unit,
                        NULL
                    )
                """),
                {
                    "reading_pk": reading_pk,
                    **measurement,
                },
            )
        inserted_readings += 1

    return inserted_readings


def _commit_outcome_import(
    *,
    db: Session,
    rows: list[dict],
    ward_id: str,
) -> int:
    records = [row["payload"] for row in rows]
    validated_rows = _validate_outcome_rows(records)
    if any(row["errors"] for row in validated_rows):
        raise HTTPException(
            status_code=409,
            detail="Staged outcome data failed commit validation",
        )

    encounter_ids = sorted({
        row["payload"]["encounter_id"]
        for row in validated_rows
    })
    encounters = db.execute(
        text("""
            SELECT encounter_id, ward_id, admitted_at
            FROM ward_twin.encounters
            WHERE encounter_id = ANY(:encounter_ids)
            FOR SHARE
        """),
        {"encounter_ids": encounter_ids},
    ).mappings().all()
    encounters_by_id = {
        str(encounter["encounter_id"]): encounter
        for encounter in encounters
    }

    if len(encounters_by_id) != len(encounter_ids):
        raise HTTPException(
            status_code=409,
            detail=(
                "An encounter referenced by the staged import "
                "no longer exists"
            ),
        )

    for row in validated_rows:
        payload = row["payload"]
        encounter = encounters_by_id[payload["encounter_id"]]
        if str(encounter["ward_id"]) != ward_id:
            raise HTTPException(
                status_code=409,
                detail=(
                    "An encounter Ward changed after upload; "
                    "the import must be uploaded again"
                ),
            )

        event_at = _parse_aware_datetime(payload["event_at"])
        admitted_at = encounter["admitted_at"]
        if (
            event_at is None
            or admitted_at is None
            or admitted_at.tzinfo is None
            or admitted_at.utcoffset() is None
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Encounter admission time or outcome event time "
                    "is invalid"
                ),
            )
        if event_at < admitted_at:
            raise HTTPException(
                status_code=409,
                detail=(
                    "event_at must not precede encounter admission"
                ),
            )

    event_ids = [row["payload"]["event_id"] for row in validated_rows]
    existing_event = db.execute(
        text("""
            SELECT event_id
            FROM ward_twin.outcomes
            WHERE event_id = ANY(:event_ids)
            LIMIT 1
        """),
        {"event_ids": event_ids},
    ).first()
    if existing_event is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Outcome event_id already exists",
                "event_id": existing_event[0],
            },
        )

    for row in validated_rows:
        payload = row["payload"]
        db.execute(
            text("""
                INSERT INTO ward_twin.outcomes (
                    event_id,
                    encounter_id,
                    event_type,
                    event_at,
                    unplanned
                )
                VALUES (
                    :event_id,
                    :encounter_id,
                    :event_type,
                    :event_at,
                    :unplanned
                )
            """),
            {
                "event_id": payload["event_id"],
                "encounter_id": payload["encounter_id"],
                "event_type": payload["event_type"],
                "event_at": _parse_aware_datetime(payload["event_at"]),
                "unplanned": payload["unplanned"],
            },
        )

    return len(validated_rows)


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
    summary="Validate and stage CSV import",
    responses={
        401: {"description": "Authentication required"},
        403: {"description": "Role or ward access denied"},
        413: {"description": "CSV exceeds 2 MB"},
        422: {"description": "Invalid CSV"},
    },
)
async def create_import(
    file: UploadFile = File(...),
    kind: ImportTemplateKind = Form(
        default=ImportTemplateKind.encounters
    ),
    ward_id: str | None = Form(default=None),
    db: Session = Depends(get_db),
    user: dict = Depends(require_roles("admin", "nurse")),
):
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

        columns = IMPORT_COLUMNS[kind]
        if reader.fieldnames != columns:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "CSV columns do not match template",
                    "kind": kind.value,
                    "expected": columns,
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
            detail=f"Maximum {MAX_ROWS} rows per import",
        )

    if kind == ImportTemplateKind.encounters:
        staged_rows, ward_ids = _validate_encounter_rows(
            records,
            db,
            user,
        )
        if len(ward_ids) != 1:
            raise HTTPException(
                status_code=422,
                detail="CSV must contain exactly one ward_id",
            )
        resolved_ward_id = next(iter(ward_ids))
        if ward_id and ward_id != resolved_ward_id:
            raise HTTPException(
                status_code=422,
                detail="Form ward_id does not match CSV ward_id",
            )
        if ward_id:
            require_ward(ward_id, user)
    else:
        if kind == ImportTemplateKind.observations:
            staged_rows = _validate_observation_rows(records)
        else:
            staged_rows = _validate_outcome_rows(records)
        resolved_ward_id = _resolve_encounter_import_ward(
            kind=kind,
            staged_rows=staged_rows,
            db=db,
            user=user,
            requested_ward_id=ward_id,
        )

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
                    :kind,
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
                "kind": kind.value,
                "status": import_status,
                "uploaded_by": user["sub"],
                "ward_id": resolved_ward_id,
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
        "kind": kind.value,
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
    summary="Commit a validated CSV import",
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
                    kind,
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

        if job["kind"] == ImportTemplateKind.observations.value:
            inserted_rows = _commit_observation_import(
                db=db,
                rows=rows,
                ward_id=job["ward_id"],
            )
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
                "inserted_rows": inserted_rows,
                "committed": True,
            }

        if job["kind"] == ImportTemplateKind.outcomes.value:
            inserted_rows = _commit_outcome_import(
                db=db,
                rows=rows,
                ward_id=job["ward_id"],
            )
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
                "inserted_rows": inserted_rows,
                "committed": True,
            }

        if job["kind"] != ImportTemplateKind.encounters.value:
            raise HTTPException(
                status_code=409,
                detail="Unsupported import kind",
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
