# app/main.py

from fastapi import FastAPI, Query   # <-- خلي Query هنا
from fastapi.middleware.cors import CORSMiddleware
import os

from .models import AppSpec, ScaleRequest, StatusResponse
from .k8s_ops import (
    upsert_deployment, upsert_service, list_status, scale,
    bg_prepare, bg_promote, bg_rollback,
)
from pydantic import BaseModel

from app.monitor import router as monitor_router

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
# Routers (disabled login)
# -------------------------------------------------------------------
# from app.monitor.routes import r as monitor_router
# app.include_router(monitor_router)  # /api/monitor/...

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
    allow_credentials=True,  # set True فقط إذا عندك cookies/sessions
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
  
    try:
        # نتأكد أن الـService موجودة وتختار always role=activ
        _ = upsert_service(spec)
        res = bg_prepare(spec)
        return {"ok": True, **res}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/apps/bluegreen/promote")
async def bluegreen_promote(req: NameNS):
   
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


app = FastAPI()
app.include_router(monitor_router)
