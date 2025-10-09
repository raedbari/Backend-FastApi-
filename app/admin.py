# app/admin.py
import re
import secrets
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from .db import get_db
from .models import Tenant, User
from .auth import get_current_context, CurrentContext
from .k8s_ops import create_tenant_namespace

router = APIRouter(prefix="/admin", tags=["admin"])

# --------------------------
# Utilities
# --------------------------
_SLUG_RE = re.compile(r"[^a-z0-9-]+")

def slugify(name: str) -> str:
    s = name.strip().lower().replace(" ", "-")
    s = _SLUG_RE.sub("-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "tenant"

def shortid(n: int = 6) -> str:
    # base32-like url-safe
    return secrets.token_urlsafe(8)[:n].lower()

def require_platform_admin(ctx: CurrentContext) -> None:
    if ctx.role != "platform_admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")

# --------------------------
# Schemas
# --------------------------
from pydantic import BaseModel

class TenantItem(BaseModel):
    id: int
    name: str
    status: str
    k8s_namespace: Optional[str] = None

class TenantListResponse(BaseModel):
    items: List[TenantItem]

class ApproveResponse(BaseModel):
    ok: bool = True
    id: int
    name: str
    k8s_namespace: str
    status: str = "active"

class RejectResponse(BaseModel):
    ok: bool = True
    id: int
    status: str = "rejected"

# --------------------------
# Endpoints
# --------------------------
@router.get("/tenants", response_model=TenantListResponse)
def list_tenants(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
    ctx: CurrentContext = Depends(get_current_context),
):
    require_platform_admin(ctx)
    q = db.query(Tenant)
    if status_filter:
        q = q.filter(Tenant.status == status_filter)
    rows = q.order_by(Tenant.id.desc()).all()
    items = [TenantItem(id=t.id, name=t.name, status=t.status, k8s_namespace=t.k8s_namespace) for t in rows]
    return TenantListResponse(items=items)

@router.post("/tenants/{tenant_id}/approve", response_model=ApproveResponse)
def approve_tenant(
    tenant_id: int,
    db: Session = Depends(get_db),
    ctx: CurrentContext = Depends(get_current_context),
):
    require_platform_admin(ctx)
    t: Optional[Tenant] = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Tenant not found")

    if t.status == "active":
        # Idempotent: إعادة نفس النتيجة
        return ApproveResponse(id=t.id, name=t.name, k8s_namespace=t.k8s_namespace or "", status="active")
    if t.status == "rejected":
        raise HTTPException(status_code=409, detail="Tenant is rejected")

    # توليد Namespace
    ns = f"tenant-{slugify(t.name)}-{shortid()}"
    # استدعاء التزويد (Idempotent)
    summary = create_tenant_namespace(ns)

    # تحديث السجل
    t.k8s_namespace = ns
    t.status = "active"
    db.add(t)
    db.commit()

    return ApproveResponse(id=t.id, name=t.name, k8s_namespace=ns, status="active")

@router.post("/tenants/{tenant_id}/reject", response_model=RejectResponse)
def reject_tenant(
    tenant_id: int,
    db: Session = Depends(get_db),
    ctx: CurrentContext = Depends(get_current_context),
):
    require_platform_admin(ctx)
    t: Optional[Tenant] = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Tenant not found")

    if t.status == "active":
        raise HTTPException(status_code=409, detail="Tenant already active")

    t.status = "rejected"
    db.add(t)
    db.commit()
    return RejectResponse(id=t.id, status="rejected")
