
from app.routers.twin import build_prediction


def test_prediction_id_bigint_to_string():
    latest_prediction = {
        "prediction_id": 10,
        "status": "ready",
        "probability": 0.85,
        "model_version": "test-model",
        "assessed_at": None,
        "unavailable_reason": None,
    }

    result = build_prediction(
        processing=None,
        latest_prediction=latest_prediction,
    )

    assert result["prediction_id"] == "10"
    assert result["risk_score"] == 0.85
    assert result["status"] == "ready"


def test_prediction_not_available():
    result = build_prediction(
        processing=None,
        latest_prediction=None,
    )

    assert result["prediction_id"] is None
    assert result["status"] == "not_available"


def test_prediction_pending():
    processing = {
        "dirty_version": 2,
        "processed_version": 1,
    }

    result = build_prediction(
        processing=processing,
        latest_prediction=None,
    )

    assert result["status"] == "pending"
    assert result["prediction_id"] is None
