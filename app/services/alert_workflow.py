from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session


def get_alert_workflow_data(
    db: Session,
    *,
    encounter_ids: list[str],
) -> tuple[dict[str, list[dict]], dict[str, dict]]:
    if not encounter_ids:
        return {}, {}

    rows = db.execute(
        text("""
            SELECT
                a.alert_id,
                a.encounter_id,
                a.state,
                a.created_at,
                a.acknowledged_by,
                a.assigned_to,
                sp.display_name AS assigned_name,
                COALESCE(
                    to_jsonb(a)->>'claim_status',
                    'unclaimed'
                ) AS claim_status,
                (to_jsonb(a)->>'claimed_at')::timestamptz AS claimed_at,
                EXISTS (
                    SELECT 1
                    FROM ward_twin.assessments AS assessment
                    WHERE assessment.alert_id = a.alert_id
                      AND assessment.encounter_id = a.encounter_id
                ) AS has_assessment
            FROM ward_twin.alerts AS a
            LEFT JOIN ward_twin.staff_profiles AS sp
                ON sp.user_id = a.assigned_to
            WHERE a.encounter_id IN :encounter_ids
            ORDER BY
                a.encounter_id,
                (a.state = 'open') DESC,
                a.created_at DESC NULLS LAST,
                a.alert_id DESC
        """).bindparams(
            bindparam("encounter_ids", expanding=True)
        ),
        {"encounter_ids": encounter_ids},
    ).mappings().all()

    alerts_by_encounter: dict[str, list[dict]] = {}
    current_alert_by_encounter: dict[str, dict] = {}

    for row in rows:
        alert = dict(row)
        encounter_id = alert["encounter_id"]
        alerts_by_encounter.setdefault(encounter_id, []).append(alert)

        if (
            alert["state"] == "open"
            and encounter_id not in current_alert_by_encounter
        ):
            current_alert_by_encounter[encounter_id] = alert

    return alerts_by_encounter, current_alert_by_encounter
