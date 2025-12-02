# app/main.py
from fastapi import FastAPI, Query, HTTPException, APIRouter, Depends, status, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
from sqlalchemy.orm import Session

# JWT
from jose import JWTError, jwt
from fastapi.security import OAuth2PasswordBearer

# Routers
from .onboarding import router as onboarding_router, admin_router as onboarding_admin_router
from .models import AppSpec, ScaleRequest, StatusResponse
from .k8s_ops import (
    upsert_deployment, upsert_service, list_status, scale,
    bg_prepare, bg_promote, bg_rollback,
)
from .db import init_db, get_db
from .auth import router as auth_router
from .auth import get_current_context, CurrentContext
from app.mailer import send_email
from app.monitor import router as monitor_router
from app.config import JWT_SECRET, JWT_ALG

from app.k8s_ops import delete_app

#  Activity Logs
from app.logs.logger import log_event
from app.logs.routes import router as logs_router
app.include_router(logs_router)


# -------------------------------------------------------------------
# OAuth2 for token
# -------------------------------------------------------------------
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


class User(BaseModel):
    email: str
    namespace: str
    role: str | None = None


class NameNS(BaseModel):
    name: str
    namespace: str | None = None


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

    subject_admin = f"📩 Contact message from {payload.name}"
    body_admin = f"From: {payload.email}\n\nMessage:\n{payload.message}"
    send_email(admin, subject_admin, body_admin)

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
app.include_router(auth_router, prefix="/api")
app.include_router(router)

api = APIRouter(prefix="/api", tags=["default"])


# -------------------------------------------------------------------
# CORS
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
    return {"message": "API is running. Open /api/docs"}


@api.post("/_debug/validate-appspec")
async def validate_appspec(spec: AppSpec):
    return {"ok": True, "received": spec.model_dump(), "full_image": spec.full_image}


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
def _force_ns_on_spec(spec: AppSpec, ctx: CurrentContext) -> AppSpec:
    spec.namespace = ctx.k8s_namespace
    return spec


def _ctx_ns(ctx: CurrentContext) -> str:
    return ctx.k8s_namespace


# -------------------------------------------------------------------
# Deploy App (WITH LOGS)
# -------------------------------------------------------------------
@api.post("/apps/deploy")
async def deploy_app(
    spec: AppSpec,
    ctx: CurrentContext = Depends(get_current_context),
    db: Session = Depends(get_db),
    request: Request = None
):
    try:
        user_role = (getattr(ctx, "role", "") or "").lower()
        token_ns = ctx.k8s_namespace

        if user_role == "platform_admin":
            final_ns = spec.namespace or token_ns or "default"
        else:
            if not token_ns:
                raise HTTPException(status_code=400, detail="No namespace assigned")
            final_ns = token_ns

        spec.namespace = final_ns
        _ = verify_namespace_access(ctx, spec.namespace)

        deployment = upsert_deployment(spec)
        service = upsert_service(spec, ctx)

        # 🔥 LOG
        log_event(
            db=db,
            user_id=ctx.user_id,
            user_email=ctx.email,
            tenant_ns=ctx.k8s_namespace,
            action="deploy_app",
            details={
                "app_name": spec.name,
                "image": spec.full_image,
                "replicas": spec.replicas
            },
            ip=request.client.host,
            user_agent=request.headers.get("user-agent", "")
        )

        return {"deployment": deployment, "service": service}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -------------------------------------------------------------------
# Scale (WITH LOGS)
# -------------------------------------------------------------------
@api.post("/apps/scale")
async def scale_app(
    req: ScaleRequest,
    ctx: CurrentContext = Depends(get_current_context),
    db: Session = Depends(get_db),
    request: Request = None
):
    try:
        ns = verify_namespace_access(ctx)
        result = scale(req.name, req.replicas, namespace=ns)

        # 🔥 LOG
        log_event(
            db=db,
            user_id=ctx.user_id,
            user_email=ctx.email,
            tenant_ns=ctx.k8s_namespace,
            action="scale_app",
            details={"app_name": req.name, "replicas": req.replicas},
            ip=request.client.host,
            user_agent=request.headers.get("user-agent", "")
        )

        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -------------------------------------------------------------------
# Status (NO Logs)
# -------------------------------------------------------------------
@api.get("/apps/status", response_model=StatusResponse)
async def apps_status(
    name: str | None = Query(default=None),
    ctx: CurrentContext = Depends(get_current_context),
):
    try:
        ns = verify_namespace_access(ctx)
        return list_status(name=name, namespace=ns)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -------------------------------------------------------------------
# Blue/Green Prepare (WITH LOGS)
# -------------------------------------------------------------------
@api.post("/apps/bluegreen/prepare")
async def bluegreen_prepare(
    spec: AppSpec,
    ctx: CurrentContext = Depends(get_current_context),
    db: Session = Depends(get_db),
    request: Request = None
):
    try:
        res = bg_prepare(spec)

        log_event(
            db=db,
            user_id=ctx.user_id,
            user_email=ctx.email,
            tenant_ns=ctx.k8s_namespace,
            action="bluegreen_prepare",
            details={"app_name": spec.name},
            ip=request.client.host,
            user_agent=request.headers.get("user-agent", "")
        )

        return {"ok": True, **res}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -------------------------------------------------------------------
# Blue/Green Promote (WITH LOGS)
# -------------------------------------------------------------------
@api.post("/apps/bluegreen/promote")
async def bluegreen_promote(
    req: NameNS,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
    request: Request = None
):
    try:
        ns = user["namespace"]
        res = bg_promote(name=req.name, namespace=ns)

        # 🔥 LOG
        log_event(
            db=db,
            user_id=user["email"],
            user_email=user["email"],
            tenant_ns=user["namespace"],
            action="bluegreen_promote",
            details={"app_name": req.name},
            ip=request.client.host,
            user_agent=request.headers.get("user-agent", "")
        )

        return {"ok": True, **res}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -------------------------------------------------------------------
# Blue/Green Rollback (WITH LOGS)
# -------------------------------------------------------------------
@api.post("/apps/bluegreen/rollback")
async def bluegreen_rollback(
    req: NameNS,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
    request: Request = None
):
    try:
        ns = user["namespace"]
        res = bg_rollback(name=req.name, namespace=ns)

        # 🔥 LOG
        log_event(
            db=db,
            user_id=user["email"],
            user_email=user["email"],
            tenant_ns=user["namespace"],
            action="bluegreen_rollback",
            details={"app_name": req.name},
            ip=request.client.host,
            user_agent=request.headers.get("user-agent", "")
        )

        return {"ok": True, **res}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -------------------------------------------------------------------
# Delete App (WITH LOGS)
# -------------------------------------------------------------------
@api.post("/apps/delete")
async def delete_app_api(
    data: NameNS,
    db: Session = Depends(get_db),
    request: Request = None,
    user=Depends(get_current_user)
):
    try:
        ns = user["namespace"]
        res = delete_app(ns, data.name)

        # 🔥 LOG
        log_event(
            db=db,
            user_id=user["email"],
            user_email=user["email"],
            tenant_ns=user["namespace"],
            action="delete_app",
            details={"app_name": data.name},
            ip=request.client.host,
            user_agent=request.headers.get("user-agent", "")
        )

        return res

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -------------------------------------------------------------------
# Monitor Routes
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


# -------------------------------------------------------------------
# Startup
# -------------------------------------------------------------------
@app.on_event("startup")
def _startup():
    init_db()


# -------------------------------------------------------------------
# Namespace access guard
# -------------------------------------------------------------------
def verify_namespace_access(ctx: CurrentContext, requested_ns: str | None = None) -> str:
    user_ns = getattr(ctx, "k8s_namespace", None)
    user_role = (getattr(ctx, "role", None) or getattr(ctx, "user_role", None) or "").lower()

    is_admin = user_role in ("admin", "platform_admin")

    if not is_admin:
        if requested_ns and requested_ns != user_ns:
            raise HTTPException(status_code=403, detail="Access denied for this namespace")
        return user_ns or requested_ns

    return requested_ns or user_ns
