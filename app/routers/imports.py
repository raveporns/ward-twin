
import csv
import io
import json
from datetime import date, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user, require_ward

router = APIRouter(tags=["CSV Imports"])

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
    "patient_id", "hn", "display_name",
    "encounter_id", "ward_id", "admitted_at"
]

MAX_FILE_SIZE = 2 * 1024 * 1024
MAX_ROWS = 1000


@router.post("/imports", status_code=201)
async def create_import(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(422, "Only CSV files are supported")

    content = await file.read(MAX_FILE_SIZE + 1)

    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(413, "CSV file exceeds 2 MB")

    try:
        decoded = content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(decoded))

        if reader.fieldnames != COLUMNS:
            raise HTTPException(
                422,
                detail={
                    "message": "CSV columns do not match template",
                    "expected": COLUMNS,
                    "received": reader.fieldnames,
                },
            )

        records = list(reader)

    except UnicodeDecodeError:
        raise HTTPException(422, "CSV must use UTF-8 encoding")
    except csv.Error:
        raise HTTPException(422, "Invalid CSV format")

    if not records:
        raise HTTPException(422, "CSV contains no data rows")

    if len(records) > MAX_ROWS:
        raise HTTPException(422, "Maximum 1000 rows per import")

    # ป้องกัน CSV ที่มีคอลัมน์เกินหรือข้อมูลซ้ำ
    seen_encounters = set()
    seen_an = set()
    patient_to_hn = {}
    hn_to_patient = {}

    ward_ids = set()
    staged_rows = []

    for row_number, row in enumerate(records, start=2):
        errors = []
                # ตรวจความสัมพันธ์ Patient ID กับ HN ภายใน CSV
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

        if None in row:
            errors.append("Unexpected extra columns")

        for column in REQUIRED:
            if not (row.get(column) or "").strip():
                errors.append(f"{column} is required")

        encounter_id = (row.get("encounter_id") or "").strip()
        if encounter_id:
            if encounter_id in seen_encounters:
                errors.append("Duplicate encounter_id in CSV")
            seen_encounters.add(encounter_id)

                    # ตรวจ AN ซ้ำภายใน CSV
                # ตรวจ AN ซ้ำภายใน CSV และ Database
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
                errors.append("Invalid birth_date; use YYYY-MM-DD")

        for column in ("admitted_at",):
            value = (row.get(column) or "").strip()
            if value:
                try:
                    parsed = datetime.fromisoformat(
                        value.replace("Z", "+00:00")
                    )
                    if parsed.tzinfo is None:
                        errors.append(
                            f"{column} must include timezone"
                        )
                except ValueError:
                    errors.append(f"Invalid {column}")

        staged_rows.append({
            "row_number": row_number,
            "payload": {
                key: (row.get(key) or "").strip()
                for key in COLUMNS
            },
            "errors": errors,
        })

    # Import หนึ่งครั้งรองรับ Ward เดียว
    if len(ward_ids) != 1:
        raise HTTPException(
            422,
            "CSV must contain exactly one ward_id"
        )

    ward_id = next(iter(ward_ids))
    error_rows = sum(bool(r["errors"]) for r in staged_rows)
    valid_rows = len(staged_rows) - error_rows
    status = "invalid" if error_rows else "validated"

    try:
        import_id = db.execute(
            text("""
                INSERT INTO ward_twin.import_jobs (
                    kind, status, uploaded_by, ward_id,
                    filename, total_rows, valid_rows, error_rows
                )
                VALUES (
                    'encounters', :status, :uploaded_by,
                    :ward_id, :filename, :total_rows,
                    :valid_rows, :error_rows
                )
                RETURNING import_id
            """),
            {
                "status": status,
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
                        import_id, row_number, payload, errors
                    )
                    VALUES (
                        :import_id, :row_number,
                        CAST(:payload AS JSONB),
                        CAST(:errors AS JSONB)
                    )
                """),
                {
                    "import_id": import_id,
                    "row_number": row["row_number"],
                    "payload": json.dumps(
                        row["payload"], ensure_ascii=False
                    ),
                    "errors": json.dumps(row["errors"]),
                },
            )

        db.commit()
        

    except Exception:
        db.rollback()
        raise

    return {
        "import_id": str(import_id),
        "kind": "encounters",
        "status": status,
        "total_rows": len(staged_rows),
        "valid_rows": valid_rows,
        "error_rows": error_rows,
        "committed": False,
    }


@router.get("/imports/{import_id}")
def get_import_detail(
    import_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    # 1. ตรวจสอบรูปแบบ UUID
    try:
        import_uuid = UUID(import_id)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Invalid import_id format",
        )

    # 2. ค้นหา Import Job
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
        {"import_id": import_uuid},
    ).mappings().first()

    if job is None:
        raise HTTPException(
            status_code=404,
            detail="Import not found",
        )

    # 3. ตรวจสอบสิทธิ์เข้าถึง Ward
    require_ward(job["ward_id"], user)

    # 4. ดึงข้อมูล CSV ที่เก็บใน Staging
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
        {"import_id": import_uuid},
    ).mappings().all()

    # 5. ส่งผลลัพธ์ให้ Frontend
    return {
        "import_id": str(job["import_id"]),
        "kind": job["kind"],
        "status": job["status"],
        "filename": job["filename"],
        "ward_id": job["ward_id"],
        "uploaded_by": job["uploaded_by"],
        "total_rows": job["total_rows"],
        "valid_rows": job["valid_rows"],
        "error_rows": job["error_rows"],
        "created_at": job["created_at"].isoformat(),
        "committed_at": (
            job["committed_at"].isoformat()
            if job["committed_at"] else None
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


from sqlalchemy.exc import IntegrityError


@router.post("/imports/{import_id}/commit")
def commit_import(
    import_id: UUID,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    try:
        # 1. Lock Import Job เพื่อป้องกัน Commit พร้อมกัน
        job = db.execute(
            text("""
                SELECT import_id, status, ward_id,
                       total_rows, valid_rows, error_rows
                FROM ward_twin.import_jobs
                WHERE import_id = :import_id
                FOR UPDATE
            """),
            {"import_id": import_id},
        ).mappings().first()

        if job is None:
            raise HTTPException(404, "Import not found")

        # 2. ตรวจสอบสิทธิ์ Ward
        require_ward(job["ward_id"], user)

        if job["status"] == "committed":
            raise HTTPException(
                409, "Import has already been committed"
            )

        if (
            job["status"] != "validated"
            or job["error_rows"] != 0
        ):
            raise HTTPException(
                409, "Import is not ready to commit"
            )

        # 3. อ่านข้อมูลที่ผ่าน Validation
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
                409, "Staged row count mismatch"
            )
        # Revalidate staged data before commit
        # ป้องกัน Import Job เก่าที่สร้างก่อนเพิ่ม Validation

        patient_to_hn = {}
        hn_to_patient = {}
        seen_encounters = set()
        seen_an = set()

        for row in rows:
            payload = row["payload"]
            an = (payload.get("an") or "").strip()

            if an:
                if an in seen_an:
                    raise HTTPException(
                        status_code=409,
                        detail="Duplicate AN in staged import"
                    )

                seen_an.add(an)

            patient_id = (payload.get("patient_id") or "").strip()
            hn = (payload.get("hn") or "").strip()
            encounter_id = (payload.get("encounter_id") or "").strip()

            if not patient_id or not hn or not encounter_id:
                raise HTTPException(
                    status_code=409,
                    detail="Missing required identifiers in staged import"
                )

            if (
                patient_id in patient_to_hn
                and patient_to_hn[patient_id] != hn
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Same patient_id has different HN in staged import"
                )

            if (
                hn in hn_to_patient
                and hn_to_patient[hn] != patient_id
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Same HN has different patient_id in staged import"
                )

            if encounter_id in seen_encounters:
                raise HTTPException(
                    status_code=409,
                    detail="Duplicate encounter_id in staged import"
                )

            if payload.get("ward_id") != job["ward_id"]:
                raise HTTPException(
                    status_code=409,
                    detail="Staged row ward_id does not match import ward"
                )

            patient_to_hn[patient_id] = hn
            hn_to_patient[hn] = patient_id
            seen_encounters.add(encounter_id)

        # 4. ตรวจสอบ Encounter ID ซ้ำ
        for row in rows:
            payload = row["payload"]

            existing = db.execute(
                text("""
                    SELECT 1
                    FROM ward_twin.encounters
                    WHERE encounter_id = :encounter_id
                    LIMIT 1
                """),
                {"encounter_id": payload["encounter_id"]},
            ).first()

            if existing:
                raise HTTPException(
                    409,
                    detail={
                        "message": "Encounter already exists",
                        "encounter_id": payload["encounter_id"],
                    },
                )
            an = (payload.get("an") or "").strip()

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
        # 5. เพิ่ม Patient และ Encounter
        # ใช้ Transaction เดียวกับ Import Job

        for row in rows:
            p = row["payload"]

            # ตรวจสอบ Patient เดิมจาก patient_id หรือ HN
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
                # ป้องกัน Patient ID และ HN ไม่ตรงกับข้อมูลเดิม
                if (
                    len(existing_patient) != 1
                    or existing_patient[0]["patient_id"] != p["patient_id"]
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
                        "birth_date": p.get("birth_date") or None,
                        "sex": p.get("sex") or None,
                    },
                )

            # บันทึก Encounter โดยอ้างอิง Patient
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

        # 6. เปลี่ยนสถานะ Import
        db.execute(
            text("""
                UPDATE ward_twin.import_jobs
                SET status = 'committed',
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
            409,
            "Database constraint conflict during import"
        )

    except Exception:
        db.rollback()
        raise
