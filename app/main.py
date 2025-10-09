# # app/main.py
# from fastapi import FastAPI, Query, HTTPException, APIRouter
# from fastapi.middleware.cors import CORSMiddleware
# from pydantic import BaseModel
# import os

# from .admin import router as admin_router

# from .models import AppSpec, ScaleRequest, StatusResponse
# from .k8s_ops import (
#     upsert_deployment, upsert_service, list_status, scale,
#     bg_prepare, bg_promote, bg_rollback,
# )
# from .db import init_db
# from .auth import router as auth_router


# class NameNS(BaseModel):
#     name: str
#     namespace: str | None = None


# # -------------------------------------------------------------------
# # FastAPI app
# # -------------------------------------------------------------------
# app = FastAPI(
#     title="Cloud-Native DevOps Platform API",
#     version="0.1.0",
#     # نوثّق ونفتح OpenAPI تحت /api
#     docs_url="/api/docs",
#     openapi_url="/api/openapi.json",
#     description="MVP starting point. Deploy/scale/status endpoints for K8s workloads.",

# )

# # مصادقة تحت /api
# app.include_router(auth_router, prefix="/api")

# # راوتر رئيسي لكل مسارات الـAPI
# api = APIRouter(prefix="/api", tags=["default"])

# # -------------------------------------------------------------------
# # CORS configuration
# # -------------------------------------------------------------------
# origins = [
#     o.strip()
#     for o in os.getenv(
#         "ALLOWED_ORIGINS",
#         # أضف نطاق موقعك https
#         "https://rango-project.duckdns.org,http://localhost:3001"
#     ).split(",")
#     if o.strip()
# ]

# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=origins,
#     allow_credentials=True,   # True فقط إذا تستخدم cookies/sessions
#     allow_methods=["*"],
#     allow_headers=["*"],
# )


# # -------------------------------------------------------------------
# # Basic routes (كلها الآن تحت /api/...)
# # -------------------------------------------------------------------
# @api.get("/healthz")
# async def healthz():
#     return {"status": "ok"}

# @api.get("")
# async def root():
#     return {"message": "Hello! API is running. Open /api/docs to try it out."}

# # Temporary debug route to validate AppSpec schema via Swagger UI
# @api.post("/_debug/validate-appspec")
# async def validate_appspec(spec: AppSpec):
#     return {"ok": True, "received": spec.model_dump(), "full_image": spec.full_image}

# # -------------------------------------------------------------------
# # Platform routes
# # -------------------------------------------------------------------
# @api.post("/apps/deploy")
# async def deploy_app(spec: AppSpec):
#     try:
#         deployment = upsert_deployment(spec)
#         service = upsert_service(spec)
#         return {"deployment": deployment, "service": service}
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=str(e)) from e

# @api.post("/apps/scale")
# async def scale_app(req: ScaleRequest):
#     try:
#         result = scale(req.name, req.replicas, namespace=req.namespace)
#         return result
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=str(e)) from e

# @api.get("/apps/status", response_model=StatusResponse)
# async def apps_status(
#     name: str | None = Query(default=None),
#     namespace: str | None = Query(default=None),
# ):
#     try:
#         return list_status(name=name, namespace=namespace)
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=str(e)) from e

# @api.post("/apps/bluegreen/prepare")
# async def bluegreen_prepare(spec: AppSpec):
#     try:
#         _ = upsert_service(spec)  # نتأكد أن الـService موجودة
#         res = bg_prepare(spec)
#         return {"ok": True, **res}
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=str(e)) from e

# @api.post("/apps/bluegreen/promote")
# async def bluegreen_promote(req: NameNS):
#     try:
#         ns = req.namespace or os.getenv("DEFAULT_NAMESPACE", "default")
#         res = bg_promote(name=req.name, namespace=ns)
#         return {"ok": True, **res}
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=str(e)) from e

# @api.post("/apps/bluegreen/rollback")
# async def bluegreen_rollback(req: NameNS):

#     try:
#         ns = req.namespace or os.getenv("DEFAULT_NAMESPACE", "default")
#         res = bg_rollback(name=req.name, namespace=ns)
#         return {"ok": True, **res}
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=str(e)) from e


# # -------------------------------------------------------------------
# # Monitor (Grafana URL only)  — تحت /api/monitor
# # -------------------------------------------------------------------
# def build_dashboard_url(ns: str, app_name: str) -> str:

#     base = (os.getenv("GRAFANA_URL") or "").rstrip("/")
#     if not base:
#         raise RuntimeError("GRAFANA_URL is not set")

#     uid = (os.getenv("GRAFANA_DASHBOARD_UID") or "").strip()
#     slug = (os.getenv("GRAFANA_DASHBOARD_SLUG") or "kubernetes-app").strip()
    
#     if uid:

#         return f"{base}/d/{uid}/{slug}?var-namespace={ns}&var-app={app_name}"

#     return f"{base}/?orgId=1"

# monitor = APIRouter(prefix="/api/monitor", tags=["monitor"])

# @monitor.get("/grafana_url")
# def grafana_url(
#     ns: str = Query(..., alias="ns"),
#     app: str = Query(..., alias="app"),
# ):
#     try:
#         url = build_dashboard_url(ns, app)
#         return {"url": url}
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=str(e))

# # مسار قديم متوافق — الآن تحت /api/monitor/apps
# @monitor.get("/apps", response_model=StatusResponse)
# async def legacy_apps_status(
#     name: str | None = Query(default=None),
#     namespace: str | None = Query(default=None),
# ):

#     return await apps_status(name=name, namespace=namespace)

# # ضم الراوترات
# app.include_router(api)
# app.include_router(monitor)

# # -------------------------------------------------------------------
# # Startup
# # -------------------------------------------------------------------
# @app.on_event("startup")
# def _startup():

#     init_db()

# app.include_router(admin_router, prefix="/api")

from fastapi import FastAPI, Query, HTTPException, APIRouter, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os



from .models import AppSpec, ScaleRequest, StatusResponse
from .k8s_ops import (
    upsert_deployment, upsert_service, list_status, scale,
    bg_prepare, bg_promote, bg_rollback,
)
from .db import init_db
from .auth import router as auth_router
from .auth import get_current_context, CurrentContext


class NameNS(BaseModel):
    name: str
    namespace: str | None = None  # سيُتجاهل، نتركه متوافقاً مع الواجهة القديمة


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
        "https://rango-project.duckdns.org,http://rango-project.duckdns.org,http://localhost:3001"
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
# Helpers: enforce tenant namespace from JWT
# -------------------------------------------------------------------
def _force_ns_on_spec(spec: AppSpec, ctx: CurrentContext) -> AppSpec:
    # نُسقط أي namespace من الواجهة ونفرض ns من التوكن
    object.__setattr__(spec, "namespace", ctx.k8s_namespace)
    spec.namespace = ctx.k8s_namespace
    return spec

def _ctx_ns(ctx: CurrentContext) -> str:
    return ctx.k8s_namespace

# -------------------------------------------------------------------
# Platform routes (ALL tenant-scoped via JWT)
# -------------------------------------------------------------------
@api.post("/apps/deploy")
async def deploy_app(spec: AppSpec, ctx: CurrentContext = Depends(get_current_context)):
    try:
        spec = _force_ns_on_spec(spec, ctx)
        deployment = upsert_deployment(spec)
        service = upsert_service(spec)
        return {"deployment": deployment, "service": service}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@api.post("/apps/scale")
async def scale_app(req: ScaleRequest, ctx: CurrentContext = Depends(get_current_context)):
    try:
        # تجاهل أي namespace قادم من الواجهة
        result = scale(req.name, req.replicas, namespace=_ctx_ns(ctx))
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@api.get("/apps/status", response_model=StatusResponse)
async def apps_status(
    name: str | None = Query(default=None),
    ctx: CurrentContext = Depends(get_current_context),
):
    try:
        # يرجع فقط ما بداخل namespace الخاص بالعميل
        return list_status(name=name, namespace=_ctx_ns(ctx))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@api.post("/apps/bluegreen/prepare")
async def bluegreen_prepare(spec: AppSpec, ctx: CurrentContext = Depends(get_current_context)):
    try:
        spec = _force_ns_on_spec(spec, ctx)
        _ = upsert_service(spec)  # نتأكد أن الـService موجودة وتشير لـ active
        res = bg_prepare(spec)
        return {"ok": True, **res}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@api.post("/apps/bluegreen/promote")
async def bluegreen_promote(req: NameNS, ctx: CurrentContext = Depends(get_current_context)):
    try:
        res = bg_promote(name=req.name, namespace=_ctx_ns(ctx))
        return {"ok": True, **res}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@api.post("/apps/bluegreen/rollback")
async def bluegreen_rollback(req: NameNS, ctx: CurrentContext = Depends(get_current_context)):
    try:
        res = bg_rollback(name=req.name, namespace=_ctx_ns(ctx))
        return {"ok": True, **res}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


# -------------------------------------------------------------------
# Monitor (Grafana URL only)  — tenant-scoped
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
def grafana_url(

    app: str = Query(..., alias="app"),
    ctx: CurrentContext = Depends(get_current_context),
):
    # لا نستقبل ns من الواجهة — نستنتجه من الـJWT
    try:
        url = build_dashboard_url(_ctx_ns(ctx), app)
        return {"url": url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# توافق مع القديم: يعتمد على الـJWT الآن
@monitor.get("/apps", response_model=StatusResponse)
async def legacy_apps_status(
    name: str | None = Query(default=None),
    ctx: CurrentContext = Depends(get_current_context),
):
    return await apps_status(name=name, ctx=ctx)

# ضم الراوترات
app.include_router(api)
app.include_router(monitor)

# -------------------------------------------------------------------
# Startup
# -------------------------------------------------------------------
@app.on_event("startup")
def _startup():

    init_db()
    
