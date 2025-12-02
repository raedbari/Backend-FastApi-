# app/auth.py
import os
from datetime import datetime, timedelta
from typing import Optional

from jose import jwt, JWTError
from passlib.hash import pbkdf2_sha256
from pydantic import BaseModel, EmailStr
from fastapi import Depends, HTTPException, status, APIRouter, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from .db import get_db
from .models import User, Tenant

# JWT config
from app.config import JWT_SECRET, JWT_ALG, JWT_EXP_HOURS

# Logs
from app.logs.logger import log_event


# ----------------------------
# نماذج
# ----------------------------
class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class LoginUser(BaseModel):
    id: int
    email: EmailStr
    role: str

class LoginTenant(BaseModel):
    id: int
    name: str
    k8s_namespace: str | None = None

class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: LoginUser
    tenant: LoginTenant

class SignupRequest(BaseModel):
    company: str
    email: EmailStr
    password: str

class SignupResponse(BaseModel):
    ok: bool = True
    tenant_id: int
    status: str = "pending"


# ----------------------------
# دوال مساعدة
# ----------------------------
def verify_password(plain: str, hashed: str) -> bool:
    try:
        return pbkdf2_sha256.verify(plain, hashed)
    except Exception:
        return False

def hash_password(plain: str) -> str:
    return pbkdf2_sha256.hash(plain)

def create_access_token(*, sub: str, tid: int, ns: str | None, role: str) -> str:
    now = datetime.utcnow()
    to_encode = {
        "sub": sub,
        "tid": tid,
        "ns": ns,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=JWT_EXP_HOURS)).timestamp()),
    }
    return jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALG)


# ----------------------------
# Router
# ----------------------------
router = APIRouter(prefix="/auth", tags=["auth"])


# ----------------------------
# Self-Signup
# ----------------------------
@router.post("/signup", response_model=SignupResponse, status_code=status.HTTP_201_CREATED)
def signup(payload: SignupRequest, db: Session = Depends(get_db)):
    exists = db.query(User).filter(User.email == payload.email).first()
    if exists:
        raise HTTPException(status_code=409, detail="Email already registered")

    tenant = Tenant(
        name=payload.company,
        status="pending",
        k8s_namespace=None,
    )
    db.add(tenant)
    db.flush()

    user = User(
        email=payload.email,
        password_hash=hash_password(payload.password),
        role="admin",
        tenant_id=tenant.id,
    )
    db.add(user)
    db.commit()

    return SignupResponse(tenant_id=tenant.id, status="pending")


# ----------------------------
# Login
# ----------------------------
@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db), request: Request = None):

    resp = login_user(db, payload.email, payload.password)
    if not resp:
        raise HTTPException(status_code=404, detail="Not Found")

    # Log successful login
    log_event(
        db=db,
        user_id=resp.user.id,
        user_email=resp.user.email,
        tenant_ns=resp.tenant.k8s_namespace,
        action="login",
        details={"email": resp.user.email},
        ip=request.client.host if request else None,
        user_agent=request.headers.get("user-agent", "") if request else "",
    )

    return resp


def login_user(db: Session, email: str, password: str) -> Optional[LoginResponse]:
    user = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(password, user.password_hash):
        return None

    tenant = db.query(Tenant).filter(Tenant.id == user.tenant_id).first()
    if not tenant:
        return None

    if tenant.status != "active" and (user.role or "user") != "platform_admin":
        msg = "Forbidden"
        if tenant.status == "pending":
            msg = "Account pending approval"
        elif tenant.status == "suspended":
            msg = "Account suspended"
        elif tenant.status == "rejected":
            msg = "Account rejected"
        raise HTTPException(status_code=403, detail=msg)

    if user.role == "platform_admin":
        ns = "default"
    else:
        ns = tenant.k8s_namespace
        if not ns:
            raise HTTPException(
                400,
                "Tenant does not have a Kubernetes namespace assigned",
            )

    token = create_access_token(
        sub=user.email,
        tid=tenant.id,
        ns=ns,
        role=user.role or "user",
    )

    return LoginResponse(
        access_token=token,
        expires_in=JWT_EXP_HOURS * 3600,
        user=LoginUser(id=user.id, email=user.email, role=user.role or "user"),
        tenant=LoginTenant(id=tenant.id, name=tenant.name, k8s_namespace=ns),
    )


# ----------------------------
# CurrentContext (dependency)
# ----------------------------
bearer_scheme = HTTPBearer(auto_error=True)

class CurrentContext(BaseModel):
    user_id: int
    email: EmailStr
    role: str
    tenant_id: int
    k8s_namespace: str | None = None


def get_current_context(
    cred: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> CurrentContext:
    token = cred.credentials
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])

        email = payload.get("sub")
        tid = payload.get("tid")
        ns = payload.get("ns")
        role = payload.get("role") or "user"

        if not email or tid is None:
            raise ValueError("bad claims")

        # 👈 تحميل user_id من قاعدة البيانات
        user = db.query(User).filter(User.email == email).first()
        if not user:
            raise ValueError("user not found")

        return CurrentContext(
            user_id=user.id,
            email=email,
            role=role,
            tenant_id=int(tid),
            k8s_namespace=(None if ns is None else str(ns)),
        )

    except (JWTError, ValueError):
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token",
        )
