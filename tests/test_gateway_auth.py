
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.core.config import settings
from app.dependencies.auth import require_gateway

# สร้างแอปแยกเพื่อทดสอบ Gateway Authentication
gateway_app = FastAPI()


@gateway_app.get("/test-gateway")
def gateway_test(
    gateway=Depends(require_gateway),
):
    return {"authenticated": True}


client = TestClient(gateway_app)


def test_gateway_valid_key():
    response = client.get(
        "/test-gateway",
        headers={
            "X-Gateway-Key": settings.gateway_api_key
        },
    )
    assert response.status_code == 200


def test_gateway_invalid_key():
    response = client.get(
        "/test-gateway",
        headers={"X-Gateway-Key": "wrong-key"},
    )
    assert response.status_code == 401


def test_gateway_missing_key():
    response = client.get("/test-gateway")
    assert response.status_code == 401
