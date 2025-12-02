# app/main.py
from fastapi import FastAPI, Query, HTTPException, APIRouter, Depends, status, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os

# JWT
from jose import JWTError, jwt
from fastapi.security import OAuth2PasswordBearer

# DB
from sqlalchemy.orm import Session
from app.db import init_db, get_db

# Routers
from app.auth import router as auth_router
from app.auth import get_current_context, CurrentContext
from app.onboarding import router as onboarding_router, admin_router as onboarding_admin_router
from app.monitor import router as monitor_router
from app.logs.routes import router as logs_router
from app.alerts.webhook import router as alerts_router

# Models / Ops
from app.models import AppSpec, ScaleRequest, StatusResponse
from app.k8s_ops import (
    upsert_deployment, upsert_service, list_status, scale,
    bg_prepare, bg_promote, bg_rollback, delete_app
)

from app.mailer import send_email
from app.config import JWT_SECRET, JWT_ALG

# Activity Logs
from app.logs.logger import log_event


# -------------------------------------------------------------------
# FastAPI App
# -------------------------------------------------------------------
app = FastAPI(
    title="Cloud-Native DevOps Platform API",
    version="0.1.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


class User(BaseModel):
    email: str
    namespace: str
    role: str | None = None


class NameNS(BaseModel):
    name: str
    namespace: str | None = None


class ContactPayload(BaseModel):
    name: str
    email: str
    message: str


# -------------------------------------------------------------------
# Routers Registration (ORDER MATTERS)
# -------------------------------------------------------------------
app.include_router(auth_router, prefix="/api")
app.include_router(onboarding_router, prefix="/api")
app.include_router(onboarding_admin_router, prefix="/api")
app.include_router(logs_router)            # already has /api/logs
app.include_router(monitor_router, prefix="/api")
app.include_router(alerts_router, prefix="/api")


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


# -------------------------------------------------------------------
# Token Decode
# -------------------------------------------------------------------
def get_current_user(token: str = Depends(oauth2_scheme)):
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
        email = payload.get("sub")
        namespace = payload.get("ns")
        role = payload.get("role")

        if email is None or namespace is None:
            raise HTTPException(status_code=401, detail="Invalid token")

        return {"email": email, "namespace": namespace, "role": role}

    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid credentials")


# -------------------------------------------------------------------
# API Router
# -------------------------------------------------------------------
api = APIRouter(prefix="/api")


@api.get("/healthz")
async def healthz():
    return {"status": "ok"}


@api.post("/contact")
def contact_us(payload: ContactPayload):
    admin = os.getenv("ADMIN_EMAIL", "admin@smartdevops.lat")

    send_email(
        admin,
        f"📩 Contact from {payload.name}",
        f"From: {payload.email}\n\n{payload.message}"
    )

    send_email(
        payload.email,
        "We received your message",
        "Thank you! Our team will reply soon."
    )

    return {"ok": True}


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
def verify_namespace_access(ctx: CurrentContext, requested_ns: str | None = None) -> str:
    user_ns = getattr(ctx, "k8s_namespace", None)
    user_role = (ctx.role or "").lower()

    is_admin = user_role in ("admin", "platform_admin")

    if not is_admin:
        if requested_ns and requested_ns != user_ns:
            raise HTTPException(status_code=403, detail="Namespace not allowed")
        return user_ns or requested_ns

    return requested_ns or user_ns


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
        user_role = (ctx.role or "").lower()
        token_ns = ctx.k8s_namespace

        if user_role == "platform_admin":
            final_ns = spec.namespace or token_ns or "default"
        else:
            final_ns = token_ns

        spec.namespace = final_ns

        verify_namespace_access(ctx, spec.namespace)

        deployment = upsert_deployment(spec)
        service = upsert_service(spec, ctx)

        log_event(
            db=db,
            user_id=ctx.user_id,
            user_email=ctx.email,
            tenant_ns=ctx.k8s_namespace,
            action="deploy_app",
            details={"app_name": spec.name, "image": spec.full_image},
            ip=request.client.host,
            user_agent=request.headers.get("user-agent", "")
        )

        return {"deployment": deployment, "service": service}

    except Exception as e:
        raise HTTPException(500, str(e))


# -------------------------------------------------------------------
# Scale App (WITH LOGS)
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
        raise HTTPException(500, str(e))


# -------------------------------------------------------------------
# Status (NO Logs)
# -------------------------------------------------------------------
@api.get("/apps/status", response_model=StatusResponse)
async def apps_status(name: str | None = None, ctx: CurrentContext = Depends(get_current_context)):
    try:
        ns = verify_namespace_access(ctx)
        return list_status(name=name, namespace=ns)
    except Exception as e:
        raise HTTPException(500, str(e))


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
        raise HTTPException(500, str(e))


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
        raise HTTPException(500, str(e))


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
        raise HTTPException(500, str(e))


# -------------------------------------------------------------------
# Delete App (WITH LOGS)
# -------------------------------------------------------------------
@api.post("/apps/delete")
async def delete_app_api(
    data: NameNS,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    request: Request = None
):
    try:
        ns = user["namespace"]
        res = delete_app(ns, data.name)

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
        raise HTTPException(500, str(e))


# -------------------------------------------------------------------
# Attach API Router
# -------------------------------------------------------------------
app.include_router(api)


# -------------------------------------------------------------------
# Startup
# -------------------------------------------------------------------
@app.on_event("startup")
def _startup():
    init_db()
