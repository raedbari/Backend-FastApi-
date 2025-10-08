# app/main.py

from fastapi import FastAPI, Query, HTTPException, APIRouter , Depends
from fastapi.middleware.cors import CORSMiddleware
import os

from .models import AppSpec, ScaleRequest, StatusResponse
from .k8s_ops import (
    upsert_deployment, upsert_service, list_status, scale,
    bg_prepare, bg_promote, bg_rollback,
)
from pydantic import BaseModel
from .db import init_db ,get_db


class NameNS(BaseModel):
    name: str
    namespace: str | None = None


# -------------------------------------------------------------------
# FastAPI app
# -------------------------------------------------------------------
app = FastAPI(
    title="Cloud-Native DevOps Platform API",
    version="0.1.0",
    docs_url="/api/docs",           
    openapi_url="/api/openapi.json", 
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
    allow_credentials=True,   # اجعلها True فقط إذا عندك cookies/sessions
    allow_methods=["*"],
    allow_headers=["*"],
)


# -------------------------------------------------------------------
# Basic routes
# -------------------------------------------------------------------
@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/")
async def root():
    return {"message": "Hello! API is running. Open /docs to try it out."}


# Temporary debug route to validate AppSpec schema via Swagger UI
@app.post("/_debug/validate-appspec")
async def validate_appspec(spec: AppSpec):
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
    try:
        deployment = upsert_deployment(spec)
        service = upsert_service(spec)
        return {"deployment": deployment, "service": service}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/apps/scale")
async def scale_app(req: ScaleRequest):
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
        _ = upsert_service(spec)  # نتأكد أن الـService موجودة وتختار دائمًا role=active
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
# Monitor (Grafana URL only)
# -------------------------------------------------------------------
def build_dashboard_url(ns: str, app_name: str) -> str:
    """
    يبني رابط الداشبورد في Grafana.
    اضبط هذه المتغيرات في الـ Deployment:
      - GRAFANA_URL = https://rango-project.duckdns.org/grafana
      - (اختياري) GRAFANA_DASHBOARD_UID = <UID>
      - (اختياري) GRAFANA_DASHBOARD_SLUG = kubernetes-app
    """
    base = (os.getenv("GRAFANA_URL") or "").rstrip("/")
    if not base:
        raise RuntimeError("GRAFANA_URL is not set")

    uid = (os.getenv("GRAFANA_DASHBOARD_UID") or "").strip()
    slug = (os.getenv("GRAFANA_DASHBOARD_SLUG") or "kubernetes-app").strip()

    if uid:
        # مرّر متغيرات dashboard كما تعتمدها لوحتك (عدّل أسماء vars لو عندك غيرها)
        return f"{base}/d/{uid}/{slug}?var-namespace={ns}&var-app={app_name}"
    # رجّع الصفحة الرئيسية إن ما فيه UID محدد
    return f"{base}/?orgId=1"


monitor_router = APIRouter(prefix="/monitor", tags=["monitor"])


@monitor_router.get("/grafana_url")
def grafana_url(
    ns: str = Query(..., alias="ns"),
    app: str = Query(..., alias="app"),
):
    try:
        url = build_dashboard_url(ns, app)
        return {"url": url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


app.include_router(monitor_router)

@app.get("/monitor/apps", response_model=StatusResponse)
async def legacy_apps_status(
    name: str | None = Query(default=None),
    namespace: str | None = Query(default=None)
):
    # أعِد استخدام نفس منطق /apps/status
    return await apps_status(name=name, namespace=namespace)


@app.on_event("startup")
def _startup():
    # إنشاء الجداول + Seed لعميل Demo
    init_db()
