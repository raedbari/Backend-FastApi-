# app/auth.py
import os
from datetime import datetime, timedelta
from typing import Optional

from jose import jwt, JWTError
from passlib.hash import pbkdf2_sha256
from pydantic import BaseModel, EmailStr
from fastapi import Depends, HTTPException, status
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
    k8s_namespace: str

class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: LoginUser
    tenant: LoginTenant

# ----------------------------
# دوال مساعدة
# ----------------------------
def verify_password(plain: str, hashed: str) -> bool:
    try:
        return pbkdf2_sha256.verify(plain, hashed)
    except Exception:
        return False

def create_access_token(*, sub: str, tid: int, ns: str, role: str) -> str:
    now = datetime.utcnow()
    to_encode = {
        "sub": sub,
        "tid": tid,            # tenant_id
        "ns": ns,              # namespace المسموح
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=JWT_EXP_HOURS)).timestamp()),
    }
    return jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALG)

# ----------------------------
# تسجيل الدخول
# ----------------------------
router = APIRouter(prefix="/auth", tags=["auth"])

def login_user(db: Session, email: str, password: str) -> Optional[LoginResponse]:
    user: Optional[User] = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(password, user.password_hash):
        return None

    tenant: Tenant = db.query(Tenant).filter(Tenant.id == user.tenant_id).first()
    if not tenant or tenant.status != "active":
        return None

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
    k8s_namespace: str

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
        if not email or not tid or not ns:
            raise ValueError("bad claims")
        return CurrentContext(email=email, role=role, tenant_id=int(tid), k8s_namespace=str(ns))
    except (JWTError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )
