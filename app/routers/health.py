from fastapi import APIRouter

router = APIRouter(tags=["Operations"])

@router.get("/healthz")
def health_check():
    return {"status": "ok"}
