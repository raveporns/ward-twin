from sqlalchemy import text
from sqlalchemy.orm import Session


def list_users(
    db: Session,
    *,
    user_id: str | None = None,
) -> list[dict]:
    result = db.execute(
        text("""
            SELECT
                sp.user_id,
                sp.display_name,
                sp.role,
                w.ward_id,
                w.name AS ward_name
            FROM ward_twin.staff_profiles AS sp
            LEFT JOIN ward_twin.ward_access AS wa
                ON wa.user_id = sp.user_id
               AND sp.role <> 'admin'
            LEFT JOIN ward_twin.wards AS w
                ON w.ward_id = wa.ward_id
                OR sp.role = 'admin'
            WHERE :user_id IS NULL
               OR sp.user_id = :user_id
            ORDER BY sp.user_id, w.ward_id
        """),
        {"user_id": user_id},
    ).mappings().all()

    users_by_id: dict[str, dict] = {}
    seen_wards: dict[str, set[str]] = {}

    for row in result:
        normalized_user_id = str(row["user_id"])
        user = users_by_id.setdefault(
            normalized_user_id,
            {
                "user_id": normalized_user_id,
                "display_name": row["display_name"],
                "role": row["role"],
                "wards": [],
            },
        )
        seen_wards.setdefault(normalized_user_id, set())

        ward_id = row["ward_id"]
        if ward_id is not None and str(ward_id) not in seen_wards[normalized_user_id]:
            normalized_ward_id = str(ward_id)
            user["wards"].append({
                "ward_id": normalized_ward_id,
                "name": row["ward_name"],
            })
            seen_wards[normalized_user_id].add(normalized_ward_id)

    return list(users_by_id.values())
