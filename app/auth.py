# app/auth.py
import os
from datetime import datetime, timedelta
from typing import Optional

from jose import jwt, JWTError
from passlib.hash import pbkdf2_sha256
from pydantic import BaseModel, EmailStr
from fastapi import Depends, HTTPException, status, APIRouter
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from .db import get_db
from .models import User, Tenant

# ----------------------------
# إعدادات JWT
# ----------------------------
JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-me")
JWT_ALG = "HS256"
JWT_EXP_HOURS = int(os.getenv("JWT_EXP_HOURS", "12"))


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

# ---- Signup payloads/response ----
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
        "tid": tid,            # tenant_id
        "ns": ns,              # namespace المسموح (قد يكون None قبل الموافقة)
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
    # تحقق من فريدية البريد
    exists = db.query(User).filter(User.email == payload.email).first()
    if exists:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    # إنشاء Tenant بحالة pending
    tenant = Tenant(
        name=payload.company,
        status="pending",
        k8s_namespace=None,
    )
    db.add(tenant)
    db.flush()  # للحصول على tenant.id دون إنهاء المعاملة

    # إنشاء User admin مرتبط بالتينانت
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
# تسجيل الدخول
# ----------------------------
@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    """
    سلوك الواجهة:
    - بيانات اعتماد خاطئة -> 404 "Not Found" (حتى لا نفضح وجود الحساب)
    - حساب موجود لكن التينانت ليس active:
        pending    -> 403 "Account pending approval"
        suspended  -> 403 "Account suspended"
        rejected   -> 403 "Account rejected"
    - نجاح -> 200 مع JWT
    """
    resp = login_user(db, payload.email, payload.password)
    if not resp:
        # توحيد الاستجابة عند فشل الاعتماد: 404
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    return resp


def login_user(db: Session, email: str, password: str) -> Optional[LoginResponse]:
    user: Optional[User] = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(password, user.password_hash):
        return None

    tenant: Optional[Tenant] = db.query(Tenant).filter(Tenant.id == user.tenant_id).first()
    if not tenant:
        return None

    # منع الدخول إذا لم يكن التينانت "active" مع رسائل دقيقة
    if tenant.status != "active":
        msg = "Forbidden"
        if tenant.status == "pending":
            msg = "Account pending approval"
        elif tenant.status == "suspended":
            msg = "Account suspended"
        elif tenant.status == "rejected":
            msg = "Account rejected"
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=msg)

    token = create_access_token(
        sub=user.email,
        tid=tenant.id,
        ns=tenant.k8s_namespace,
        role=user.role or "user",
    )
    return LoginResponse(
        access_token=token,
        expires_in=JWT_EXP_HOURS * 3600,
        user=LoginUser(id=user.id, email=user.email, role=user.role or "user"),
        tenant=LoginTenant(id=tenant.id, name=tenant.name, k8s_namespace=tenant.k8s_namespace),
    )


# ----------------------------
# Dependencies لاستخراج الـcontext
# ----------------------------
bearer_scheme = HTTPBearer(auto_error=True)

class CurrentContext(BaseModel):
    email: EmailStr
    role: str
    tenant_id: int
    k8s_namespace: str | None = None

def get_current_context(
    cred: HTTPAuthorizationCredentials = Depends(bearer_scheme),
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
        return CurrentContext(email=email, role=role, tenant_id=int(tid), k8s_namespace=(None if ns is None else str(ns)))
    except (JWTError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )
