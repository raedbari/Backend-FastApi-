# app/main.py
from fastapi import FastAPI, Query, HTTPException, APIRouter, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os

# مكتبات JWT للتحقق من التوكنات
from jose import JWTError, jwt
from fastapi.security import OAuth2PasswordBearer

from .onboarding import router as onboarding_router, admin_router as onboarding_admin_router
from .models import AppSpec, ScaleRequest, StatusResponse
from .k8s_ops import (
    upsert_deployment, upsert_service, list_status, scale,
    bg_prepare, bg_promote, bg_rollback,
)
from .db import init_db
from .auth import router as auth_router
from .auth import get_current_context, CurrentContext
from app.mailer import send_email
import os
# -------------------------------------------------------------------
# إعداد OAuth2 لقراءة التوكن من الهيدر Authorization
# -------------------------------------------------------------------
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

# -------------------------------------------------------------------
# تعريف نموذج المستخدم لتفسير بيانات الـJWT
# -------------------------------------------------------------------
class User(BaseModel):
    email: str
    namespace: str
    role: str | None = None

class NameNS(BaseModel):
    name: str
    namespace: str | None = None  # متروكة للتوافق فقط؛ تُتجاهل

router = APIRouter(prefix="/api")

class ContactPayload(BaseModel):
    name: str
    email: str
    message: str


app.include_router(contact_router)

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


def get_current_user(token: str = Depends(oauth2_scheme)):
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
        email = payload.get("sub")
        namespace = payload.get("ns")
        role = payload.get("role")

        if email is None or namespace is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

        return {"email": email, "namespace": namespace, "role": role}

    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
#----------------------------------------------------
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
    # تجاهل أي namespace وارد من العميل؛ نفرض ns من التوكن
    spec.namespace = ctx.k8s_namespace
    return spec

def _ctx_ns(ctx: CurrentContext) -> str:
    return ctx.k8s_namespace

# -------------------------------------------------------------------
# Platform routes (ALL tenant-scoped via JWT)
# -------------------------------------------------------------------
@api.post("/apps/deploy")
async def deploy_app(spec: AppSpec, ctx: CurrentContext = Depends(get_current_context)):
    """
    Deploy endpoint — نطبق قواعد الخصوصية:
    - إن كان المستخدم عادي (admin/user): نُجبِر الـspec.namespace على قيمة ctx.k8s_namespace.
    - إن كان platform_admin: نسمح له بتمرير namespace في payload (حتى يتمكن من إدارة أي تينانت).
    """
    try:
        # ----- قرر الـnamespace النهائي بناءً على الدور -----
        user_role = (getattr(ctx, "role", "") or "").lower()
        token_ns = ctx.k8s_namespace  # الـnamespace من التوكن (قد تكون "default" للـplatform_admin)

        if user_role == "platform_admin":
            # يسمح للـplatform_admin بتحديد namespace من الـpayload (لأغراض الصيانة)
            # إذا لم يُمرّر الـpayload namespace، نستخدم الـns من التوكن كـfallback
            final_ns = spec.namespace or token_ns or "default"
        else:
            # للمستخدمين العاديين/admins: نُجبِر على استخدام الـnamespace من التوكن
            if not token_ns:
                raise HTTPException(status_code=400, detail="No namespace assigned to your account")
            final_ns = token_ns

        # فرض الـnamespace على الـspec قبل أي عملية
        spec.namespace = final_ns

        # تأكيد أن المستخدم يملك صلاحية هذا namespace (مركزية التحقق)
        _ = verify_namespace_access(ctx, spec.namespace)

        # تنفيذ الإنشاء/التحديث — ممرّر ctx حتى تستخدمه الدوال الداخلية عند الحاجة
        deployment = upsert_deployment(spec)           # upsert_deployment يستخدم spec.namespace
        service = upsert_service(spec, ctx)            # upsert_service يستعمل ctx لحماية الخصوصية
        return {"deployment": deployment, "service": service}
    except HTTPException:
        raise
    except Exception as e:
        # عرض رسالة خطأ واضحة للـclient (يمكن تحسين الرسائل لاحقًا)
        raise HTTPException(status_code=500, detail=str(e)) from e

# @api.post("/apps/scale")
# async def scale_app(req: ScaleRequest, ctx: CurrentContext = Depends(get_current_context)):
#     try:

#         ns = verify_namespace_access(ctx)
#        result = scale(req.name, req.replicas, namespace=ns)

#         return result
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=str(e)) from e

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
        _ = upsert_service(spec)  # تأكد من وجود Service تشير لـ active
        res = bg_prepare(spec)
        return {"ok": True, **res}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@api.post("/apps/bluegreen/promote")
async def bluegreen_promote(req: NameNS, ctx: CurrentContext = Depends(get_current_context)):
    try:
        ns = verify_namespace_access(ctx)
        res = bg_promote(name=req.name, namespace=ns)
        return {"ok": True, **res}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@api.post("/apps/bluegreen/rollback")
async def bluegreen_rollback(req: NameNS, ctx: CurrentContext = Depends(get_current_context)):
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
def grafana_url(

    app: str = Query(..., alias="app"),
    ctx: CurrentContext = Depends(get_current_context),
):

    try:
        ns = verify_namespace_access(ctx)
        url = build_dashboard_url(ns, app)
        return {"url": url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# توافق قديم: يعتمد الآن على JWT
@monitor.get("/apps", response_model=StatusResponse)
async def legacy_apps_status(
    name: str | None = Query(default=None),
    ctx: CurrentContext = Depends(get_current_context),
):
    return await apps_status(name=name, ctx=ctx)

# ضم الراوترا
app.include_router(api)
app.include_router(monitor)

# -------------------------------------------------------------------
# Startup
# -------------------------------------------------------------------
@app.on_event("startup")
def _startup():

    init_db()

# Onboarding (public + admin) تحت /api
app.include_router(onboarding_router, prefix="/api")
app.include_router(onboarding_admin_router, prefix="/api")
router = APIRouter(prefix="/auth")

# -------------------------------------------------------------------
# 🔒 Namespace access guard (centralized)
# -------------------------------------------------------------------
def verify_namespace_access(ctx: CurrentContext, requested_ns: str | None = None) -> str:
    """
    يُعيد الـnamespace المسموح استعماله للطلب الحالي.
    - غير المدير: يُجبر على ctx.k8s_namespace، ويرفض أي requested_ns مختلف.
    - المدير/المالك: يسمح بالـrequested_ns إن وُجد، وإلا يعيد ctx.k8s_namespace.
    ملاحظة: إذا لم يوفّر CurrentContext الدور، نعامل الطلب كـ"غير مدير".
    """
    user_ns = getattr(ctx, "k8s_namespace", None)
    user_role = (getattr(ctx, "role", None) or getattr(ctx, "user_role", None) or "").lower()

    is_admin = user_role in ("admin", "platform_admin")

    if not is_admin:
        if requested_ns and requested_ns != user_ns:
            raise HTTPException(status_code=403, detail="Access denied for this namespace")
        return user_ns or requested_ns  # يظل يجبر على ns من السياق

    # المسؤول مسموح له تحديد أي ns؛ إن لم يمرِّر، استخدم ns من السياق
    return requested_ns or user_ns



@router.post("/contact")
def contact_us(payload: ContactPayload):
    admin = os.getenv("ADMIN_EMAIL", "admin@smartdevops.lat")
    subject = f"📩 Contact message from {payload.name}"
    body = f"From: {payload.email}\n\nMessage:\n{payload.message}"
    send_email(admin, subject, body)
    return {"ok": True}


