# app/logs/routes.py

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Optional
from app.db import get_db
from app.models import ActivityLog
from app.auth import get_current_context, CurrentContext

router = APIRouter(prefix="/api/logs", tags=["logs"])


# -------------------------------------------------------
# Helper: check if user is admin
# -------------------------------------------------------
def is_admin(ctx: CurrentContext) -> bool:
    role = (ctx.role or "").lower()
    return role in ("admin", "platform_admin")


# -------------------------------------------------------
# 1) GET /api/logs/my
# -------------------------------------------------------
@router.get("/my")
def my_logs(
    ctx: CurrentContext = Depends(get_current_context),
    db: Session = Depends(get_db),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0)
):
    logs = (
        db.query(ActivityLog)
        .filter(ActivityLog.user_email == ctx.email)
        .order_by(ActivityLog.created_at.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )

    return {
        "count": len(logs),
        "items": [
            {
                "id": log.id,
                "user_email": log.user_email,         # ← ← إضافة مهمة جداً
                "action": log.action,
                "details": log.details,
                "ip": log.ip,
                "user_agent": log.user_agent,
                "created_at": log.created_at,
            }
            for log in logs
        ]
    }


# -------------------------------------------------------
# 2) GET /api/logs  (ADMIN ONLY)
# -------------------------------------------------------
@router.get("")
def all_logs(
    ctx: CurrentContext = Depends(get_current_context),
    db: Session = Depends(get_db),
    limit: int = Query(20, ge=1, le=500),
    offset: int = Query(0, ge=0),
    action: Optional[str] = None,
    email: Optional[str] = None,
    namespace: Optional[str] = None,
):
    if not is_admin(ctx):
        raise HTTPException(status_code=403, detail="Admins only")

    query = db.query(ActivityLog)

    # ---- Filters ----
    if action:
        query = query.filter(ActivityLog.action == action)
    if email:
        query = query.filter(ActivityLog.user_email == email)
    if namespace:
        query = query.filter(ActivityLog.tenant_ns == namespace)

    items = (
        query.order_by(ActivityLog.created_at.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )

    count = query.count()

    return {
        "count": count,
        "items": [
            {
                "id": log.id,
                "user_email": log.user_email,          # ← نفس التعديل هنا
                "tenant_ns": log.tenant_ns,
                "action": log.action,
                "details": log.details,
                "ip": log.ip,
                "user_agent": log.user_agent,
                "created_at": log.created_at,
            }
            for log in items
        ]
    }
