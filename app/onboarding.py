# app/onboarding.py
from __future__ import annotations
import os, json, smtplib
from email.message import EmailMessage
from typing import Optional, List

from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from kubernetes import client, config

from app.auth import create_access_token  # ← لاحظ هنا الاسم الصحيح
from app.config import JWT_EXP_HOURS      # لو أردت استخدام القيمة العامة
#from app.utils import _send_email, _send_webhook, _audit  # كما في كودك الحالي
#from kubernetes.client.models import V1Subject

...



from .db import get_db
from .models import Tenant, User, AuditLog, ProvisioningRun
from .auth import CurrentContext, get_current_context, pbkdf2_sha256
from .k8s_ops import create_tenant_namespace

router = APIRouter(prefix="/onboarding", tags=["onboarding"])

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "")
WEBHOOK_URL = os.getenv("ONBOARDING_WEBHOOK_URL", "").strip()

# ---------- Schemas ----------
class RegisterPayload(BaseModel):
    company: str = Field(..., min_length=2, max_length=200)
    email: EmailStr
    password: str = Field(..., min_length=6, max_length=128)
    namespace: str = Field(..., pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=63)
    note: Optional[str] = None


class PendingTenant(BaseModel):
    id: int
    name: str
    email: EmailStr
    k8s_namespace: str


# ---------- utilities ----------
def _send_email(to_email: str, subject: str, body: str) -> None:
    host = os.getenv("SMTP_HOST", "")
    user = os.getenv("SMTP_USER", "")
    pwd = os.getenv("SMTP_PASS", "")
    port = int(os.getenv("SMTP_PORT", "587"))
    sender = os.getenv("SMTP_FROM", "Smart DevOps <noreply@local>")
    if not host or not user or not pwd:
        return
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(host, port) as s:
        s.starttls()
        s.login(user, pwd)
        s.send_message(msg)


def _send_webhook(payload: dict) -> None:
    import urllib.request

    if not WEBHOOK_URL:
        return
    try:
        req = urllib.request.Request(
            WEBHOOK_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def _audit(db: Session, tenant_id: int, action: str, actor: str, result: str = "ok"):
    db.add(AuditLog(tenant_id=tenant_id, action=action, actor_email=actor, result=result))
    db.commit()


# ---------- background task ----------
def _provision_tenant(tenant_id: int):
    from .db import SessionLocal

    db = SessionLocal()
    try:
        t = db.get(Tenant, tenant_id)
        if not t:
            return
        ns = t.k8s_namespace
        _ = create_tenant_namespace(ns)
        pr: ProvisioningRun | None = db.execute(
            select(ProvisioningRun).where(ProvisioningRun.tenant_id == tenant_id)
        ).scalar_one_or_none()
        if pr:
            pr.status = "done"
            pr.last_error = None
        db.add(AuditLog(tenant_id=tenant_id, action="provision", actor_email="system", result="done"))
        db.commit()
    except Exception as e:
        pr: ProvisioningRun | None = db.execute(
            select(ProvisioningRun).where(ProvisioningRun.tenant_id == tenant_id)
        ).scalar_one_or_none()
        if pr:
            pr.status = "failed"
            pr.last_error = str(e)
            pr.retries = (pr.retries or 0) + 1
        db.add(AuditLog(tenant_id=tenant_id, action="provision", actor_email="system", result="failed"))
        db.commit()
    finally:
        db.close()


# ---------- public endpoints ----------

@router.post("/register")
def register(payload: RegisterPayload, bg: BackgroundTasks, db: Session = Depends(get_db)):
    existing = db.execute(select(Tenant).where(Tenant.name == payload.company)).scalar_one_or_none()
    if existing:
        raise HTTPException(409, detail="Company already exists")

    # إنشاء Tenant جديد
    t = Tenant(name=payload.company, k8s_namespace=payload.namespace, status="pending")
    db.add(t)
    db.commit()
    db.refresh(t)

    # إنشاء مستخدم Admin للتينانت
    pwd_hash = pbkdf2_sha256.hash(payload.password)
    admin = User(email=payload.email, password_hash=pwd_hash, role="pending_user", tenant_id=t.id)
    db.add(admin)
    db.commit()
    db.refresh(admin)

    # إشعار المسؤول
    if ADMIN_EMAIL:
        _send_email(
            ADMIN_EMAIL,
            f"[Smart DevOps] New tenant request: {payload.company}",
            f"Tenant: {payload.company}\nNamespace: {payload.namespace}\nAdmin: {payload.email}",
        )
    _send_webhook({"event": "tenant.register", "company": payload.company, "email": payload.email})

    _audit(db, t.id, "register", actor=payload.email)

    # ✅ إنشاء توكن مؤقت مدته 15 دقيقة فقط
    now = datetime.utcnow()
    temp_exp = now + timedelta(minutes=15)

    token = create_access_token(
        sub=admin.email,
        tid=t.id,
        ns=None,  # لا يملك namespace بعد
        role="pending_user",
    )

    return {
        "ok": True,
        "msg": "Tenant registered successfully. Pending approval.",
        "access_token": token,
        "token_type": "bearer"
    }
# ---------- admin endpoints ----------
admin_router = APIRouter(prefix="/admin/tenants", tags=["admin"])


def _ensure_admin(ctx: CurrentContext):
    """
    يُسمح فقط للمستخدم الذي يحمل الدور platform_admin بالوصول إلى هذه المسارات.
    أما أي admin آخر (tenant admin) فسيُرفض طلبه لحماية بيانات المنصة.
    """
    if ctx.role != "platform_admin":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="Access denied: only platform_admin can view or manage platform tenants."
        )


@admin_router.get("/pending", response_model=List[PendingTenant])
def list_pending(ctx: CurrentContext = Depends(get_current_context), db: Session = Depends(get_db)):
    _ensure_admin(ctx)
    rows = db.execute(select(Tenant).where(Tenant.status == "pending")).scalars().all()
    out: List[PendingTenant] = []
    for t in rows:
        u = db.execute(select(User).where(User.tenant_id == t.id)).scalar_one_or_none()
        out.append(
            PendingTenant(id=t.id, name=t.name, email=u.email if u else "", k8s_namespace=t.k8s_namespace)
        )
    return out


class ApprovePayload(BaseModel):
    pass

@admin_router.post("/{tenant_id}/approve")
def approve(
    tenant_id: int,
    bg: BackgroundTasks,
    body: ApprovePayload | None = None,
    ctx: CurrentContext = Depends(get_current_context),
    db: Session = Depends(get_db),
):
    _ensure_admin(ctx)
    t = db.get(Tenant, tenant_id)
    if not t:
        raise HTTPException(404, detail="Tenant not found")

    ns_name = f"tenant-{t.name.lower()}"
    t.k8s_namespace = ns_name

    # تحميل إعدادات Kubernetes
    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()

    k8s = client.CoreV1Api()

    # إنشاء Namespace (idempotent)
    try:
        k8s.read_namespace(name=ns_name)
    except client.exceptions.ApiException as e:
        if e.status == 404:
            ns_body = client.V1Namespace(metadata=client.V1ObjectMeta(name=ns_name))
            k8s.create_namespace(ns_body)

    # إنشاء NetworkPolicy افتراضية (idempotent)
    net_api = client.NetworkingV1Api()
    policy = client.V1NetworkPolicy(
        metadata=client.V1ObjectMeta(name="default-deny", namespace=ns_name),
        spec=client.V1NetworkPolicySpec(
            pod_selector={},
            policy_types=["Ingress", "Egress"],
        ),
    )
    try:
        net_api.create_namespaced_network_policy(ns_name, policy)
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise

    # إنشاء ServiceAccount خاص بالـTenant (idempotent)
    sa_name = "tenant-admin"
    sa_body = client.V1ServiceAccount(
        metadata=client.V1ObjectMeta(name=sa_name, namespace=ns_name)
    )
    try:
        k8s.create_namespaced_service_account(namespace=ns_name, body=sa_body)
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise

    # إنشاء Role محدود الصلاحيات (idempotent)
    rbac_api = client.RbacAuthorizationV1Api()
    role_body = client.V1Role(
        metadata=client.V1ObjectMeta(name="tenant-admin-role", namespace=ns_name),
        rules=[
            client.V1PolicyRule(
                api_groups=["", "apps", "batch", "extensions"],
                resources=[
                    "pods",
                    "deployments",
                    "services",
                    "configmaps",
                    "secrets",
                    "jobs",
                ],
                verbs=["get", "list", "watch", "create", "update", "patch", "delete"],
            )
        ],
    )
    try:
        rbac_api.create_namespaced_role(namespace=ns_name, body=role_body)
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise

    # ربط الـSA بالـRole بدون V1Subject (باستخدام dict) — idempotent
    rb_body = client.V1RoleBinding(
        metadata=client.V1ObjectMeta(name="tenant-admin-binding", namespace=ns_name),
        subjects=[{
            "kind": "ServiceAccount",
            "name": sa_name,
            "namespace": ns_name,
        }],
        role_ref=client.V1RoleRef(
            api_group="rbac.authorization.k8s.io",
            kind="Role",
            name="tenant-admin-role",
        ),
    )
    try:
        rbac_api.create_namespaced_role_binding(namespace=ns_name, body=rb_body)
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise

    # تحديث قاعدة البيانات + تشغيل التزويد الخلفي
    t.status = "active"
    db.add(t); db.commit()
    db.add(ProvisioningRun(tenant_id=tenant_id, status="queued", retries=0)); db.commit()

    bg.add_task(_provision_tenant, tenant_id)
    _audit(db, t.id, "approve", actor=ctx.email)

    # إشعار المستخدم
    u = db.execute(select(User).where(User.tenant_id == t.id)).scalar_one_or_none()
    if u:
        _send_email(u.email, "[Smart DevOps] Your account is approved", "You can sign in now.")

    return {
        "ok": True,
        "msg": f"Tenant '{t.name}' approved and namespace '{ns_name}' with SA created"
    }

class RejectPayload(BaseModel):
    reason: Optional[str] = None


@admin_router.post("/{tenant_id}/reject")
def reject(
    tenant_id: int,
    body: RejectPayload,
    ctx: CurrentContext = Depends(get_current_context),
    db: Session = Depends(get_db),
):
    _ensure_admin(ctx)
    t = db.get(Tenant, tenant_id)
    if not t:
        raise HTTPException(404, detail="Tenant not found")

    t.status = "rejected"
    db.add(t)
    db.commit()
    _audit(db, t.id, "reject", actor=ctx.email, result=body.reason or "rejected")

    # حذف الـnamespace إن وُجد
    try:
        config.load_incluster_config()
    except:
        config.load_kube_config()

    k8s = client.CoreV1Api()
    try:
        k8s.delete_namespace(name=t.k8s_namespace)
    except client.exceptions.ApiException as e:
        if e.status != 404:
            raise

    return {"ok": True, "msg": f"Tenant '{t.name}' rejected and namespace '{t.k8s_namespace}' removed"}


# to let the pending page know that the tenant have been approved 
@router.get("/me/status")
def get_my_tenant_status(
    ctx: CurrentContext = Depends(get_current_context),
    db: Session = Depends(get_db)
):
    # نحاول الحصول على التينانت من الـcontext
    u = db.execute(select(User).where(User.email == ctx.email)).scalar_one_or_none()
    if not u:
        raise HTTPException(404, "User not found")

    tenant = db.get(Tenant, u.tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")

    return {"status": tenant.status}
