from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.testclient import TestClient

from app.core.middleware import JsonUtf8CharsetMiddleware


def test_json_charset_middleware_preserves_errors_and_non_json_responses():
    test_app = FastAPI()
    test_app.add_middleware(JsonUtf8CharsetMiddleware)

    @test_app.get("/standard-json")
    def standard_json():
        return JSONResponse({"display_name": "ผู้ป่วยทดสอบ"})

    @test_app.get("/vendor-json")
    def vendor_json():
        return Response(
            content='{"display_name":"ผู้ป่วยทดสอบ"}',
            media_type="application/vnd.ward-twin+json; profile=ward",
        )

    @test_app.get("/error/{status_code}")
    def json_error(status_code: int):
        return JSONResponse(
            {"detail": "เดิม"},
            status_code=status_code,
        )

    @test_app.get("/csv")
    def csv_response():
        return Response(
            content="ward,name\nA,ผู้ป่วย\n",
            media_type="text/csv; charset=utf-8",
        )

    @test_app.get("/stream")
    def stream_response():
        return StreamingResponse(
            iter([b'{"not":"changed"}']),
            media_type="application/octet-stream",
        )

    with TestClient(test_app) as client:
        standard = client.get("/standard-json")
        vendor = client.get("/vendor-json")
        errors = [
            client.get(f"/error/{status_code}")
            for status_code in (401, 403, 404, 409, 500)
        ]
        csv_response = client.get("/csv")
        stream = client.get("/stream")

    assert standard.headers["content-type"] == (
        "application/json; charset=utf-8"
    )
    assert standard.content.decode("utf-8") == (
        '{"display_name":"ผู้ป่วยทดสอบ"}'
    )
    assert "ผู้ป่วยทดสอบ".encode("utf-8") in standard.content
    assert vendor.headers["content-type"] == (
        "application/vnd.ward-twin+json; profile=ward; charset=utf-8"
    )
    for response, status_code in zip(errors, (401, 403, 404, 409, 500)):
        assert response.status_code == status_code
        assert response.headers["content-type"] == (
            "application/json; charset=utf-8"
        )
        assert response.json() == {"detail": "เดิม"}
    assert csv_response.headers["content-type"] == (
        "text/csv; charset=utf-8"
    )
    assert stream.headers["content-type"] == "application/octet-stream"
    assert stream.content == b'{"not":"changed"}'
