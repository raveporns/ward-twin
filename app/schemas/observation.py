
from datetime import datetime, timedelta, timezone
from enum import Enum
from math import isfinite
from typing import Literal
from uuid import UUID
from typing import Optional

from pydantic import (
    BaseModel, ConfigDict, Field,
    field_validator, model_validator
)


class Metric(str, Enum):
    heart_rate = "heart_rate"
    respiratory_rate = "respiratory_rate"
    spo2 = "spo2"
    systolic_bp = "systolic_bp"
    diastolic_bp = "diastolic_bp"
    temperature = "temperature"


UNITS = {
    "heart_rate": "bpm",
    "respiratory_rate": "breaths/min",
    "spo2": "%",
    "systolic_bp": "mmHg",
    "diastolic_bp": "mmHg",
    "temperature": "Cel",
}


class Measurement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: Metric
    value: float
    unit: Literal[
        "bpm", "breaths/min", "%",
        "mmHg", "Cel"
    ]

    @model_validator(mode="after")
    def validate_measurement(self):
        if self.unit != UNITS[self.metric.value]:
            raise ValueError("Invalid unit for metric")

        if not isfinite(self.value):
            raise ValueError("Value must be finite")

        if self.metric == Metric.spo2:
            if not 0 <= self.value <= 100:
                raise ValueError("SpO2 must be 0-100")
        elif self.value <= 0:
            raise ValueError("Value must be positive")

        return self


class Reading(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reading_id: UUID
    encounter_id: str | None = Field(
        default=None, min_length=1, max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$"
    )
    device_id: Optional[str] | None = Field(
        default=None, min_length=1, max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$"
    )
    measured_at: datetime
    measurements: list[Measurement] = Field(
        min_length=1, max_length=6
    )

    @field_validator("measured_at")
    @classmethod
    def validate_timestamp(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Timezone is required")

        now = datetime.now(timezone.utc)
        if value > now + timedelta(minutes=5):
            raise ValueError("Timestamp too far in future")

        return value

    @model_validator(mode="after")
    def validate_metrics(self):
        metrics = [m.metric for m in self.measurements]
        if len(metrics) != len(set(metrics)):
            raise ValueError("Duplicate metrics")
        return self


class ObservationBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["iot", "manual"]
    readings: list[Reading] = Field(
        min_length=1, max_length=100
    )

    @model_validator(mode="after")
    def validate_source(self):
        for reading in self.readings:
            if self.source == "iot":
                if not reading.device_id:
                    raise ValueError("IoT requires device_id")
                allowed = {
                    Metric.heart_rate,
                    Metric.respiratory_rate,
                    Metric.spo2,
                }
                if any(m.metric not in allowed
                       for m in reading.measurements):
                    raise ValueError("Invalid IoT metric")
            else:
                if not reading.encounter_id or reading.device_id:
                    raise ValueError(
                        "Manual requires encounter_id "
                        "and must omit device_id"
                    )
        return self
