from fastapi import APIRouter
from pydantic import BaseModel
from app.metrics import OPEN_APP_TOTAL


router = APIRouter(prefix="/api/billing", tags=["billing"])

class OpenAppEvent(BaseModel):
    namespace: str
    app: str
    host: str | None = ""
    url: str | None = None

@router.post("/open-app")
def billing_open_app(payload: OpenAppEvent):
    OPEN_APP_TOTAL.labels(
        namespace=payload.namespace,
        app=payload.app,
        host=payload.host or ""
    ).inc()
    return {"ok": True}
