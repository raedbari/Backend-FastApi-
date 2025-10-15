# app/main.py
from fastapi import FastAPI, Query, HTTPException, APIRouter, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os

from .onboarding import router as onboarding_router, admin_router as onboarding_admin_router
from .models import AppSpec, ScaleRequest, StatusResponse, User
from .k8s_ops import (
    upsert_deployment, upsert_service, list_status, scale,
    bg_prepare, bg_promote, bg_rollback,
)
from .db import init_db
from .auth import router as auth_router

from .auth import get_current_context, CurrentContext





# -------------------------------------------------------------------
# FastAPI app
# -------------------------------------------------------------------
app = FastAPI(
    title="Cloud-Native DevOps Platform API",

    version="0.1.0",

    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    description="Multi-tenant Platform API. All app endpoints are tenant-scoped via JWT.",
)

# مصادقة تحت /api
app.include_router(auth_router, prefix="/api")

# راوتر رئيسي لكل مسارات الـAPI
api = APIRouter(prefix="/api", tags=["default"])

# -------------------------------------------------------------------
# CORS configuration
# -------------------------------------------------------------------
origins = [
    o.strip()
    for o in os.getenv(
        "ALLOWED_ORIGINS",
        "https://rango-project.duckdns.org,http://rango-project.duckdns.org,http://localhost:3000,http://localhost:3001"
    ).split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------------------------------------------------
# Helpers: enforce tenant namespace from JWT
# -------------------------------------------------------------------
def _force_ns_on_spec(spec: AppSpec, ctx: CurrentContext) -> AppSpec:
    """فرض namespace من التوكن بدلاً من بيانات العميل"""
    spec.namespace = ctx.k8s_namespace
    return spec

def _ctx_ns(ctx: CurrentContext) -> str:
    """اختصار لاسترجاع namespace الحالي"""
    return ctx.k8s_namespace

# -------------------------------------------------------------------
# 🔒 التحقق الأمني من صلاحيات Namespace
# -------------------------------------------------------------------
def verify_namespace_access(ctx: CurrentContext, requested_ns: str | None = None):
    """
    يتحقق من أن المستخدم لديه صلاحية للوصول إلى الـNamespace المطلوب.
    """
    user_role = getattr(ctx, "role", None)
    user_ns = getattr(ctx, "k8s_namespace", None)

    # المستخدمون العاديون فقط ضمن نطاقهم
    if user_role not in ["admin", "platform_admin"]:
        if requested_ns and requested_ns != user_ns:
            raise HTTPException(status_code=403, detail="Access denied for this namespace")
        return user_ns

    # للمسؤولين: يمكنهم استخدام أي namespace
    return requested_ns or user_ns

# -------------------------------------------------------------------
# Basic routes
# -------------------------------------------------------------------
@api.get("/healthz")
async def healthz():
    return {"status": "ok"}

@api.get("")
async def root():
    return {"message": "Hello! API is running. Open /api/docs to try it out."}

# Temporary debug route to validate AppSpec schema via Swagger UI
@api.post("/_debug/validate-appspec")
async def validate_appspec(spec: AppSpec):
    return {"ok": True, "received": spec.model_dump(), "full_image": spec.full_image}



# -------------------------------------------------------------------
# Platform routes (ALL tenant-scoped via JWT)
# -------------------------------------------------------------------
@api.post("/apps/deploy")
async def deploy_app(spec: AppSpec, ctx: CurrentContext = Depends(get_current_context)):
    try:
        spec = _force_ns_on_spec(spec, ctx)
        _ = verify_namespace_access(ctx, spec.namespace)
        deployment = upsert_deployment(spec)
        service = upsert_service(spec)
        return {"deployment": deployment, "service": service}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@api.post("/apps/scale")
async def scale_app(req: ScaleRequest, ctx: CurrentContext = Depends(get_current_context)):
    try:
        ns = verify_namespace_access(ctx)
        result = scale(req.name, req.replicas, namespace=ns)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@api.get("/apps/status", response_model=StatusResponse)
async def apps_status(
    name: str | None = Query(default=None),
    ctx: CurrentContext = Depends(get_current_context),
):
    try:
        ns = verify_namespace_access(ctx)
        return list_status(name=name, namespace=ns)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@api.post("/apps/bluegreen/prepare")
async def bluegreen_prepare(spec: AppSpec, ctx: CurrentContext = Depends(get_current_context)):
    try:
        spec = _force_ns_on_spec(spec, ctx)
        _ = verify_namespace_access(ctx, spec.namespace)
        _ = upsert_service(spec)
        res = bg_prepare(spec)
        return {"ok": True, **res}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@api.post("/apps/bluegreen/promote")
async def bluegreen_promote(req: BaseModel, ctx: CurrentContext = Depends(get_current_context)):
    try:
        ns = verify_namespace_access(ctx)
        res = bg_promote(name=req.name, namespace=ns)
        return {"ok": True, **res}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@api.post("/apps/bluegreen/rollback")
async def bluegreen_rollback(req: BaseModel, ctx: CurrentContext = Depends(get_current_context)):
    try:
        ns = verify_namespace_access(ctx)
        res = bg_rollback(name=req.name, namespace=ns)
        return {"ok": True, **res}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


# -------------------------------------------------------------------
# Monitor (Grafana URL only) — tenant-scoped
# -------------------------------------------------------------------
def build_dashboard_url(ns: str, app_name: str) -> str:

    base = (os.getenv("GRAFANA_URL") or "").rstrip("/")
    if not base:
        raise RuntimeError("GRAFANA_URL is not set")

    uid = (os.getenv("GRAFANA_DASHBOARD_UID") or "").strip()
    slug = (os.getenv("GRAFANA_DASHBOARD_SLUG") or "kubernetes-app").strip()

    if uid:

        return f"{base}/d/{uid}/{slug}?var-namespace={ns}&var-app={app_name}"

    return f"{base}/?orgId=1"



monitor = APIRouter(prefix="/api/monitor", tags=["monitor"])


@monitor.get("/grafana_url")
def grafana_url(app: str = Query(..., alias="app"), ctx: CurrentContext = Depends(get_current_context)):
    try:
        ns = verify_namespace_access(ctx)
        url = build_dashboard_url(ns, app)
        return {"url": url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@monitor.get("/apps", response_model=StatusResponse)
async def legacy_apps_status(name: str | None = Query(default=None), ctx: CurrentContext = Depends(get_current_context)):
    return await apps_status(name=name, ctx=ctx)

# -------------------------------------------------------------------
# Register Routers
# -------------------------------------------------------------------
app.include_router(api)
app.include_router(monitor)
app.include_router(onboarding_router, prefix="/api")
app.include_router(onboarding_admin_router, prefix="/api")

# -------------------------------------------------------------------
# Startup
# -------------------------------------------------------------------
@app.on_event("startup")
def _startup():

    init_db()

