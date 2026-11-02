from datetime import datetime, timedelta

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.ml.readiness import REQUIRED_METRICS


WORKFLOW_STATUS_LABELS = {
    "normal": "ไม่มีรายการรอดำเนินการ",
    "pending_acknowledgement": "รอรับทราบ",
    "pending_assessment": "รอประเมินผู้ป่วย",
    "assessed": "ประเมินผู้ป่วยแล้ว",
}

METRIC_LABELS = {
    "heart_rate": "อัตราการเต้นหัวใจ",
    "respiratory_rate": "อัตราการหายใจ",
    "spo2": "ค่าออกซิเจนในเลือด",
    "systolic_bp": "ความดันโลหิตตัวบน",
    "diastolic_bp": "ความดันโลหิตตัวล่าง",
    "temperature": "อุณหภูมิ",
}
BASE_REASON_MESSAGES = {
    "missing_age": "ไม่มีข้อมูลอายุ",
    "missing_sex": "ไม่มีข้อมูลเพศ",
}
GENERIC_UNAVAILABLE_MESSAGE = "ไม่สามารถสร้างผลทำนายได้"
UNAVAILABLE_RECOMMENDED_ACTION = (
    "ตรวจสอบข้อมูลที่ขาดหรือไม่เป็นปัจจุบัน แล้วรอระบบประมวลผลอีกครั้ง"
)


def get_workflow_status(alerts: list[dict]) -> str:
    workflow_alerts = [
        alert
        for alert in alerts
        if alert["state"] == "open"
    ]

    if not workflow_alerts:
        return "normal"

    if any(alert["acknowledged_by"] is None for alert in workflow_alerts):
        return "pending_acknowledgement"

    if any(not alert["has_assessment"] for alert in workflow_alerts):
        return "pending_assessment"

    return "assessed"


def get_missing_features(
    diagnostic: dict,
    feature_columns: list[str],
    *,
    missing_age: bool,
    missing_sex: bool,
) -> list[str]:
    missing_metrics = {
        metric
        for metric in REQUIRED_METRICS
        if diagnostic.get(f"{metric}_sample_count", 0) == 0
    }

    return [
        feature
        for feature in feature_columns
        if (
            (feature == "age_years" and missing_age)
            or (feature == "sex_female" and missing_sex)
            or any(
                feature.startswith(f"{metric}_")
                for metric in missing_metrics
            )
        )
    ]


def describe_unavailable_reason(
    unavailable_reason: str | None,
    feature_columns: list[str],
) -> tuple[str, str, list[str], bool]:
    reason_codes = [
        code.strip()
        for code in (unavailable_reason or "").split(",")
        if code.strip()
    ]
    messages = []
    missing_features = []
    public_reason_codes = []
    has_unknown_reason = False

    for code in reason_codes:
        message = BASE_REASON_MESSAGES.get(code)
        if message is None and code.startswith("missing_"):
            metric = code.removeprefix("missing_")
            if metric in METRIC_LABELS:
                message = f"ไม่มีข้อมูล{METRIC_LABELS[metric]}"
                missing_features.extend(
                    feature
                    for feature in feature_columns
                    if feature.startswith(f"{metric}_")
                )

        if message is None and code.startswith("insufficient_samples_"):
            metric = code.removeprefix("insufficient_samples_")
            if metric in METRIC_LABELS:
                message = f"ข้อมูล{METRIC_LABELS[metric]}มีจำนวนตัวอย่างไม่เพียงพอ"

        if message is None and code.startswith("stale_"):
            metric = code.removeprefix("stale_")
            if metric in METRIC_LABELS:
                message = f"ข้อมูล{METRIC_LABELS[metric]}ไม่เป็นปัจจุบัน"

        if message is None:
            has_unknown_reason = True
            continue

        public_reason_codes.append(code)
        messages.append(message)

        if code == "missing_age" and "age_years" in feature_columns:
            missing_features.append("age_years")
        elif code == "missing_sex" and "sex_female" in feature_columns:
            missing_features.append("sex_female")

    if not reason_codes:
        has_unknown_reason = True

    if has_unknown_reason:
        messages.append(GENERIC_UNAVAILABLE_MESSAGE)

    return (
        ",".join(public_reason_codes) or (
            "unavailable" if has_unknown_reason else ""
        ),
        " / ".join(dict.fromkeys(messages)),
        list(dict.fromkeys(missing_features)),
        has_unknown_reason,
    )


def has_missing_age(birth_date, as_of: datetime) -> bool:
    return birth_date is None or birth_date > as_of.date()


def has_missing_sex(sex: str | None) -> bool:
    return sex is None or sex.strip().lower() not in {
        "female",
        "f",
        "male",
        "m",
    }


def get_patient_diagnostics(
    db: Session,
    *,
    ward_id: str | None,
    encounter_ids: list[str],
    as_of: datetime,
) -> dict:
    if not encounter_ids:
        return {}

    ward_filter = (
        "AND e.ward_id = :ward_id"
        if ward_id is not None
        else ""
    )
    params = {
        "encounter_ids": encounter_ids,
        "start": as_of - timedelta(hours=12),
        "as_of": as_of,
    }
    if ward_id is not None:
        params["ward_id"] = ward_id

    rows = db.execute(
        text(f"""
            SELECT
                e.encounter_id,
                COUNT(DISTINCT r.id) AS observation_count,
                COUNT(m.reading_pk) FILTER (
                    WHERE m.metric = 'heart_rate'
                      AND r.measured_at >= :start
                      AND r.measured_at <= :as_of
                      AND m.value IS NOT NULL
                      AND (m.quality_flag IS NULL OR m.quality_flag = 'valid')
                ) AS heart_rate_sample_count,
                COUNT(m.reading_pk) FILTER (
                    WHERE m.metric = 'respiratory_rate'
                      AND r.measured_at >= :start
                      AND r.measured_at <= :as_of
                      AND m.value IS NOT NULL
                      AND (m.quality_flag IS NULL OR m.quality_flag = 'valid')
                ) AS respiratory_rate_sample_count,
                COUNT(m.reading_pk) FILTER (
                    WHERE m.metric = 'spo2'
                      AND r.measured_at >= :start
                      AND r.measured_at <= :as_of
                      AND m.value IS NOT NULL
                      AND (m.quality_flag IS NULL OR m.quality_flag = 'valid')
                ) AS spo2_sample_count,
                COUNT(m.reading_pk) FILTER (
                    WHERE m.metric = 'systolic_bp'
                      AND r.measured_at >= :start
                      AND r.measured_at <= :as_of
                      AND m.value IS NOT NULL
                      AND (m.quality_flag IS NULL OR m.quality_flag = 'valid')
                ) AS systolic_bp_sample_count,
                COUNT(m.reading_pk) FILTER (
                    WHERE m.metric = 'diastolic_bp'
                      AND r.measured_at >= :start
                      AND r.measured_at <= :as_of
                      AND m.value IS NOT NULL
                      AND (m.quality_flag IS NULL OR m.quality_flag = 'valid')
                ) AS diastolic_bp_sample_count,
                COUNT(m.reading_pk) FILTER (
                    WHERE m.metric = 'temperature'
                      AND r.measured_at >= :start
                      AND r.measured_at <= :as_of
                      AND m.value IS NOT NULL
                      AND (m.quality_flag IS NULL OR m.quality_flag = 'valid')
                ) AS temperature_sample_count
            FROM ward_twin.encounters AS e
            LEFT JOIN ward_twin.observation_readings AS r
                ON r.encounter_id = e.encounter_id
            LEFT JOIN ward_twin.observation_measurements AS m
                ON m.reading_pk = r.id
            WHERE TRUE
              {ward_filter}
              AND e.encounter_id IN :encounter_ids
            GROUP BY e.encounter_id
        """).bindparams(
            bindparam("encounter_ids", expanding=True)
        ),
        params,
    ).mappings().all()

    return {
        row["encounter_id"]: row
        for row in rows
    }


def build_patient_prediction(
    row: dict,
    diagnostic: dict,
    *,
    feature_columns: list[str],
    as_of: datetime,
) -> tuple[dict, bool]:
    prediction = {
        "status": "not_available",
        "risk_score": None,
        "prediction_id": None,
        "model_version": None,
        "assessed_at": None,
        "unavailable_reason": None,
        "unavailable_message": None,
        "missing_features": [],
        "recommended_action": None,
    }

    missing_features = get_missing_features(
        diagnostic,
        feature_columns,
        missing_age=has_missing_age(row["birth_date"], as_of),
        missing_sex=has_missing_sex(row["sex"]),
    )
    has_pending_work = (
        row["dirty_version"] is not None
        and (
            row["processed_version"] is None
            or row["dirty_version"] > row["processed_version"]
        )
    )

    if has_pending_work:
        prediction["status"] = "pending"
        prediction["unavailable_message"] = (
            "มีข้อมูลใหม่รอการประมวลผล"
        )
        prediction["recommended_action"] = (
            "รอระบบประมวลผลข้อมูลล่าสุด"
        )
        return prediction, False

    if row["prediction_id"] is not None:
        prediction_status = row["prediction_status"]
        prediction.update({
            "status": prediction_status,
            "risk_score": (
                float(row["probability"])
                if (
                    prediction_status == "ready"
                    and row["probability"] is not None
                )
                else None
            ),
            "prediction_id": row["prediction_id"],
            "model_version": row["model_version"],
            "assessed_at": (
                row["assessed_at"].isoformat()
                if row["assessed_at"] is not None
                else None
            ),
        })

        if prediction_status == "unavailable":
            (
                prediction["unavailable_reason"],
                prediction["unavailable_message"],
                prediction["missing_features"],
                has_unknown_reason,
            ) = describe_unavailable_reason(
                row["unavailable_reason"],
                feature_columns,
            )
            prediction["recommended_action"] = (
                UNAVAILABLE_RECOMMENDED_ACTION
            )
            return prediction, has_unknown_reason

        if prediction_status == "failed":
            prediction["unavailable_reason"] = "processing_failed"
            prediction["unavailable_message"] = (
                "ระบบไม่สามารถประมวลผลข้อมูลได้"
            )
            prediction["recommended_action"] = (
                "ตรวจสอบสถานะระบบหรือติดต่อผู้ดูแล"
            )

        return prediction, False

    if diagnostic.get("observation_count", 0) == 0:
        prediction["unavailable_reason"] = "no_observations"
        prediction["unavailable_message"] = (
            "ยังไม่มีข้อมูลสัญญาณชีพสำหรับการทำนาย"
        )
        prediction["missing_features"] = missing_features
        prediction["recommended_action"] = (
            "บันทึกข้อมูลสัญญาณชีพที่จำเป็น แล้วรอระบบประมวลผล"
        )
    else:
        prediction["unavailable_reason"] = "no_prediction_record"
        prediction["unavailable_message"] = (
            "ยังไม่มีผลการประมวลผลจากโมเดล"
        )
        prediction["missing_features"] = missing_features
        prediction["recommended_action"] = (
            "ตรวจสอบสถานะการประมวลผลของระบบ"
        )

    return prediction, False
