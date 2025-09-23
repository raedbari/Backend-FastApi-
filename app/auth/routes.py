from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session
from .schemas import LoginIn, MeOut
from .security import verify_pw, make_jwt
from app.models import User
from app.db import get_db  # وفّر دالة جلسة
from .middleware import require_auth

r = APIRouter(prefix="/api/auth", tags=["auth"])

@r.post("/login")
def login(body: LoginIn, resp: Response, db: Session = Depends(get_db)):
    u = db.query(User).filter(User.username==body.username).first()
    if not u or not verify_pw(body.password, u.password_hash):
        raise HTTPException(401, "invalid credentials")
    token = make_jwt(str(u.id), u.tenant_id, u.role)
    resp.set_cookie("auth", token, httponly=True, samesite="lax", secure=True, path="/")
    return {"ok": True}

@r.post("/logout")
def logout(resp: Response):
    resp.delete_cookie("auth", path="/")
    return {"ok": True}

@r.get("/me", response_model=MeOut)
def me(payload=Depends(require_auth)):
    return {"user_id": int(payload["sub"]), "tenant_id": payload["tenant_id"], "role": payload["role"]}
