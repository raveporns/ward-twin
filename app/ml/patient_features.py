from datetime import datetime, timezone
from sqlalchemy import text
from sqlalchemy.orm import Session


def build_patient_features(
    db: Session,
    encounter_id: str,
    as_of: datetime | None = None,
) -> dict:
    if as_of is None:
        as_of = datetime.now(timezone.utc)

    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")

    row = db.execute(
        text("""
            SELECT
                p.birth_date,
                p.sex
            FROM ward_twin.encounters e
            JOIN ward_twin.patients p
              ON p.patient_id = e.patient_id
            WHERE e.encounter_id = :encounter_id
        """),
        {"encounter_id": encounter_id},
    ).mappings().one_or_none()

    if row is None:
        raise ValueError("Encounter or patient not found")

    age_years = None

    if row["birth_date"] is not None:
        today = as_of.date()
        dob = row["birth_date"]

        if dob <= today:
            age_years = (
                today.year - dob.year
                - ((today.month, today.day) < (dob.month, dob.day))
            )

    sex_female = None

    if row["sex"] is not None:
        sex = row["sex"].strip().lower()

        if sex in ("female", "f"):
            sex_female = 1
        elif sex in ("male", "m"):
            sex_female = 0

    return {
        "age_years": age_years,
        "sex_female": sex_female,
    }
