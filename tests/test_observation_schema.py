
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.observation import ObservationBatch


def make_batch(unit="bpm"):
    return {
        "source": "iot",
        "readings": [{
            "reading_id": str(uuid4()),
            "device_id": "MON-01",
            "measured_at": datetime.now(
                timezone.utc
            ).isoformat(),
            "measurements": [{
                "metric": "heart_rate",
                "value": 108,
                "unit": unit
            }]
        }]
    }


def test_valid_batch():
    batch = ObservationBatch.model_validate(
        make_batch()
    )
    assert batch.source == "iot"


def test_invalid_unit():
    with pytest.raises(ValidationError):
        ObservationBatch.model_validate(
            make_batch(unit="%")
        )


def test_missing_device():
    data = make_batch()
    del data["readings"][0]["device_id"]

    with pytest.raises(ValidationError):
        ObservationBatch.model_validate(data)
