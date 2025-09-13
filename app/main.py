# app/main.py
"""
Minimal API bootstrap for the platform:
- GET /healthz : health check for probes and load balancers.
- GET /         : quick welcome message.
- POST /_debug/validate-appspec : temporary route to validate AppSpec payloads.
- POST /apps/deploy : upsert Deployment + Service.
- POST /apps/scale  : patch Deployment scale subresource.
- GET  /apps/status : list status for managed apps or a specific app by name.
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import os

from .models import AppSpec, ScaleRequest, StatusResponse
from .k8s_ops import (
    upsert_deployment, upsert_service, list_status, scale,
    bg_prepare, bg_promote, bg_rollback,  
)
from pydantic import BaseModel

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


ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,  # set True only if you use cookies/sessions
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
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
        # كان: result = scale(req.name, req.replicas)
        result = scale(req.name, req.replicas, namespace=req.namespace)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@app.get("/apps/status", response_model=StatusResponse)
async def apps_status(
    name: str | None = Query(default=None),
    namespace: str | None = Query(default=None)
):
    try:
        return list_status(name=name, namespace=namespace)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e



@app.post("/apps/bluegreen/prepare")
async def bluegreen_prepare(spec: AppSpec):
    """
    ينشئ/يحدّث نسخة preview باسم <name>-preview (role=preview) دون تحويل المرور.
    سنضمن وجود Service يختار role=active (لن يُمسّ).
    """
    try:
        # نتأكد أن الـService موجودة وتختار always role=active
        _ = upsert_service(spec)
        res = bg_prepare(spec)
        return {"ok": True, **res}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/apps/bluegreen/promote")
async def bluegreen_promote(req: NameNS):
    """
    ترقية لحظية: preview → active (ويُسكّل الـactive السابق إلى 0 ويُعلَّم idle).
    الـService لا يتغير لأن selector ثابت على role=active.
    """
    try:
        res = bg_promote(name=req.name, namespace=req.namespace or os.getenv("DEFAULT_NAMESPACE", "default"))
        return {"ok": True, **res}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/apps/bluegreen/rollback")
async def bluegreen_rollback(req: NameNS):
    """
    رجوع فوري: يعيد الـidle القديم ليصبح active، والحالي يُحوّل إلى preview.
    """
    try:
        res = bg_rollback(name=req.name, namespace=req.namespace or os.getenv("DEFAULT_NAMESPACE", "default"))
        return {"ok": True, **res}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
