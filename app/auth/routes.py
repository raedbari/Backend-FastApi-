# app/auth/routes.py
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from .schemas import LoginIn, MeOut
from .security import verify_pw, make_jwt, hash_pw
from app.models import User
from app.db import get_db
from app.auth.middleware import require_auth

from pydantic import BaseModel, Field

r = APIRouter(prefix="/api/auth", tags=["auth"])


# -----------------------------
# Schemas (خاصة بالتسجيل فقط)
# -----------------------------
class RegisterIn(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=3, max_length=200)
    role: str = Field(default="user")  # يمكن: "admin" أو "user"


# -----------------------------
# Auth: Login
# -----------------------------
@r.post("/login")
def login(body: LoginIn, resp: Response, db: Session = Depends(get_db)):
    """
    يتحقق من بيانات الدخول ويضع JWT داخل Cookie httpOnly.
    """
    u = db.query(User).filter(User.username == body.username).first()
    if not u or not verify_pw(body.password, u.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")

    token = make_jwt(str(u.id), u.tenant_id, u.role)
    resp.set_cookie(
        key="auth",
        value=token,
        httponly=True,
        samesite="lax",
        secure=True,
        path="/",
    )
    return {"ok": True}


# -----------------------------
# Auth: Logout
# -----------------------------
@r.post("/logout")
def logout(resp: Response):
    """
    يحذف Cookie المصادقة.
    """
    resp.delete_cookie("auth", path="/")
    return {"ok": True}


# -----------------------------
# Me: معلومات المستخدم من الـ JWT
# -----------------------------
@r.get("/me", response_model=MeOut)
def me(payload=Depends(require_auth)):
    return {
        "user_id": int(payload["sub"]),
        "tenant_id": payload.get("tenant_id"),
        "role": payload.get("role"),
    }


# -----------------------------
# Register: إضافة مستخدم (Admin فقط)
# -----------------------------
@r.post("/register", status_code=status.HTTP_201_CREATED)
def register(
    body: RegisterIn,
    payload=Depends(require_auth),                # يتحقق من الـ JWT أولاً
    db: Session = Depends(get_db),
):
    """
    إنشاء مستخدم جديد. مسموح فقط لمن يملك دور admin.
    """
    if payload.get("role") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin only")

    existing = db.query(User).filter(User.username == body.username).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="username already exists")

    u = User(
        username=body.username,
        password_hash=hash_pw(body.password),
        role=body.role,
    )
    db.add(u)
    db.commit()
    db.refresh(u)

    return {"ok": True, "id": u.id, "username": u.username, "role": u.role}
