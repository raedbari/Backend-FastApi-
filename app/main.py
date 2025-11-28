# app/main.py
from fastapi import FastAPI, Query, HTTPException, APIRouter, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os

# JWT libraries for token verification
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
from app.monitor import router as monitor_router
from app.config import JWT_SECRET, JWT_ALG

from app.k8s_ops import delete_app

# -------------------------------------------------------------------
# OAuth2 setup to read the token from the Authorization header
# -------------------------------------------------------------------
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

# -------------------------------------------------------------------
# User model definition for decoding JWT data
# -------------------------------------------------------------------
class User(BaseModel):
    email: str
    namespace: str
    role: str | None = None

class NameNS(BaseModel):
    name: str
    namespace: str | None = None  # kept for backward compatibility; ignored

router = APIRouter(prefix="/api")

class ContactPayload(BaseModel):
    name: str
    email: str
    message: str


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


@router.post("/contact")
def contact_us(payload: ContactPayload):
    admin = os.getenv("ADMIN_EMAIL", "admin@smartdevops.lat")
    
    # Message to admin
    subject_admin = f"📩 Contact message from {payload.name}"
    body_admin = f"From: {payload.email}\n\nMessage:\n{payload.message}"
    send_email(admin, subject_admin, body_admin)

    # Auto-reply to the user
    subject_user = "✅ We've received your message"
    body_user = (
        f"Hi {payload.name},\n\n"
        "Thanks for contacting Smart DevOps Platform. "
        "We received your message and will get back to you soon.\n\n"
        "Best regards,\nSmart DevOps Team"
    )
    send_email(payload.email, subject_user, body_user)

    return {"ok": True}



app.include_router(monitor_router, prefix="/api")

# Authentication under /api
app.include_router(auth_router, prefix="/api")
app.include_router(router)

# Main router for all API endpoints
api = APIRouter(prefix="/api", tags=["default"])

# -------------------------------------------------------------------
# CORS configuration
# -------------------------------------------------------------------
origins = [
    o.strip()
    for o in os.getenv(
        "ALLOWED_ORIGINS",
        "https://localhost:3000,http://localhost:3001"
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
    # Ignore any namespace from client; enforce ns from token
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
        # ----- Decide final namespace based on role -----
        user_role = (getattr(ctx, "role", "") or "").lower()
        token_ns = ctx.k8s_namespace  # namespace from token (may be "default" for platform_admin)

        if user_role == "platform_admin":
            # Allow platform_admin to specify namespace in payload (for maintenance)
            # If not provided, fallback to token namespace
            final_ns = spec.namespace or token_ns or "default"
        else:
            # Force token namespace for regular users/admins
            if not token_ns:
                raise HTTPException(status_code=400, detail="No namespace assigned to your account")
            final_ns = token_ns

        # Apply namespace before proceeding
        spec.namespace = final_ns

        # Verify user has access to this namespace (centralized check)
        _ = verify_namespace_access(ctx, spec.namespace)

        # Execute create/update — pass ctx for internal functions to use
        deployment = upsert_deployment(spec)
        service = upsert_service(spec, ctx)
        return {"deployment": deployment, "service": service}
    except HTTPException:
        raise
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
async def bluegreen_prepare(spec: AppSpec):
    try:
        res = bg_prepare(spec)
        return {"ok": True, **res}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@api.post("/apps/bluegreen/promote")
async def bluegreen_promote(req: NameNS, user=Depends(get_current_user)):
  
    try:
        ns = user["namespace"]
        res = bg_promote(name=req.name, namespace=ns)
        return {"ok": True, **res}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@api.post("/apps/bluegreen/rollback")
async def bluegreen_rollback(req: NameNS, user=Depends(get_current_user)):
 
    try:
        ns = user["namespace"]
        res = bg_rollback(name=req.name, namespace=ns)
        return {"ok": True, **res}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# -------------------------------------------------------------------
# Monitor (Grafana URL only) — tenant-scoped
# -------------------------------------------------------------------
def build_dashboard_url(ns: str, app_name: str, kind: str = "app") -> str:
    base = (os.getenv("GRAFANA_URL", "https://grafana.smartdevops.lat")).rstrip("/")

    dashboards = {
        "app": {
            "uid": os.getenv("GRAFANA_APP_UID", "app-metrics"),
            "slug": os.getenv("GRAFANA_APP_SLUG", "application-metrics"),
        },
        "namespace": {
            "uid": os.getenv("GRAFANA_NS_UID", "ns-overview"),
            "slug": os.getenv("GRAFANA_NS_SLUG", "namespace-overview"),
        },
        "logs": {
            "uid": os.getenv("GRAFANA_LOGS_UID", "app-logs"),
            "slug": os.getenv("GRAFANA_LOGS_SLUG", "application-logs"),
        },
    }

    if kind not in dashboards:
        raise ValueError(f"Unknown dashboard type: {kind}")

    d = dashboards[kind]
    url = f"{base}/d/{d['uid']}/{d['slug']}?orgId=1&var-namespace={ns}"

    if kind in ("app", "logs"):
        url += f"&var-app={app_name}"

    url += "&from=now-1h&to=now"
    return url


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

# Backward compatibility: now relies on JWT
@monitor.get("/apps", response_model=StatusResponse)
async def legacy_apps_status(
    name: str | None = Query(default=None),
    ctx: CurrentContext = Depends(get_current_context),
):
    return await apps_status(name=name, ctx=ctx)

# Include routers
app.include_router(api)
app.include_router(monitor)

# -------------------------------------------------------------------
# Startup
# -------------------------------------------------------------------
@app.on_event("startup")
def _startup():
    init_db()

# Onboarding (public + admin) under /api
app.include_router(onboarding_router, prefix="/api")
app.include_router(onboarding_admin_router, prefix="/api")

# -------------------------------------------------------------------
# 🔒 Namespace access guard (centralized)
# -------------------------------------------------------------------
def verify_namespace_access(ctx: CurrentContext, requested_ns: str | None = None) -> str:
  
    user_ns = getattr(ctx, "k8s_namespace", None)
    user_role = (getattr(ctx, "role", None) or getattr(ctx, "user_role", None) or "").lower()

    is_admin = user_role in ("admin", "platform_admin")

    if not is_admin:
        if requested_ns and requested_ns != user_ns:
            raise HTTPException(status_code=403, detail="Access denied for this namespace")
        return user_ns or requested_ns

    # Admin is allowed any ns; if not provided, use ns from context
    return requested_ns or user_ns


from app.alerts.webhook import router as alerts_router

@app.on_event("startup")
def startup_event():
    init_db()

# Attach alerts router
app.include_router(alerts_router)


@app.post("/apps/delete")
def delete_app_api(ns: str, name: str):
    return delete_app(ns, name)

