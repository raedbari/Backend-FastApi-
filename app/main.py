# app/main.py

from fastapi import FastAPI, Query, HTTPException, APIRouter
from fastapi.middleware.cors import CORSMiddleware
import os

from .models import AppSpec, ScaleRequest, StatusResponse
from .k8s_ops import (
    upsert_deployment, upsert_service, list_status, scale,
    bg_prepare, bg_promote, bg_rollback,
)
from pydantic import BaseModel

# نستخدم فقط دالة بناء رابط الجرافانا من نفس الباكدند (إن كانت لديك هنا)
# إن كانت موجودة في app/monitor.py:
from .monitor import build_dashboard_url


class NameNS(BaseModel):
    name: str
    namespace: str | None = None


# -------------------------------------------------------------------
# FastAPI app
# -------------------------------------------------------------------
app = FastAPI(
    title="Cloud-Native DevOps Platform API",
    version="0.1.0",
    description="MVP starting point. Deploy/scale/status endpoints for K8s workloads.",
)


# -------------------------------------------------------------------
# CORS configuration
# -------------------------------------------------------------------
origins = [
    o.strip()
    for o in os.getenv(
        "ALLOWED_ORIGINS",
        "http://rango-project.duckdns.org:30001,http://localhost:3001"
    ).split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,  # اجعلها True فقط إذا عندك cookies/sessions
    allow_methods=["*"],
    allow_headers=["*"],
)


# -------------------------------------------------------------------
# Basic routes
# -------------------------------------------------------------------
@app.get("/healthz")
async def healthz():
    """Simple liveness/readiness endpoint."""
    return {"status": "ok"}


@app.get("/")
async def root():
    """Welcome hint."""
    return {"message": "Hello! API is running. Open /docs to try it out."}


# Temporary debug route to validate AppSpec schema via Swagger UI
@app.post("/_debug/validate-appspec")
async def validate_appspec(spec: AppSpec):
    """Echo validated AppSpec back, with computed full_image."""
    return {
        "ok": True,
        "received": spec.model_dump(),
        "full_image": spec.full_image,
    }


# -------------------------------------------------------------------
# Platform routes
# -------------------------------------------------------------------
@app.post("/apps/deploy")
async def deploy_app(spec: AppSpec):
    """Create/patch Deployment and Service (adopt existing if present)."""
    try:
        deployment = upsert_deployment(spec)
        service = upsert_service(spec)
        return {"deployment": deployment, "service": service}
    except Exception as e:
        # Convert unexpected backend errors to an HTTP error.
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/apps/scale")
async def scale_app(req: ScaleRequest):
    """Patch the Scale subresource of a Deployment."""
    try:
        result = scale(req.name, req.replicas, namespace=req.namespace)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/apps/status", response_model=StatusResponse)
async def apps_status(
    name: str | None = Query(default=None),
    namespace: str | None = Query(default=None),
):
    try:
        return list_status(name=name, namespace=namespace)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/apps/bluegreen/prepare")
async def bluegreen_prepare(spec: AppSpec):
    try:
        # نتأكد أن الـService موجودة وتختار دائمًا role=active
        _ = upsert_service(spec)
        res = bg_prepare(spec)
        return {"ok": True, **res}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/apps/bluegreen/promote")
async def bluegreen_promote(req: NameNS):
    try:
        ns = req.namespace or os.getenv("DEFAULT_NAMESPACE", "default")
        res = bg_promote(name=req.name, namespace=ns)
        return {"ok": True, **res}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/apps/bluegreen/rollback")
async def bluegreen_rollback(req: NameNS):
    """يعيد الـ idle القديم ليصبح active والحالي يتحول preview."""
    try:
        ns = req.namespace or os.getenv("DEFAULT_NAMESPACE", "default")
        res = bg_rollback(name=req.name, namespace=ns)
        return {"ok": True, **res}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


# -------------------------------------------------------------------
# Monitor (Grafana URL only) — بدون أي صفحة مراقبة قديمة
# -------------------------------------------------------------------
monitor_router = APIRouter(prefix="/monitor", tags=["monitor"])


@monitor_router.get("/grafana_url")
def grafana_url(
    ns: str = Query(..., alias="ns"),
    app_name: str = Query(..., alias="app"),
):
    """
    يرجّع رابط الداشبورد في Grafana مع المتغيرات.
    """
    try:
        url = build_dashboard_url(ns, app_name)
        return {"url": url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


app.include_router(monitor_router)
