# app/alerts/webhook.py
from __future__ import annotations
from typing import Any, Dict, List, Optional
import os
import smtplib
from email.mime.text import MIMEText

from fastapi import APIRouter, Request, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import select

# Use the existing DB dependency
from app.db import get_db
# Import models
from app.models import Tenant, User

router = APIRouter(prefix="/api/alerts", tags=["alerts"])

# SMTP settings from environment variables (safer than hardcoding)
SMTP_HOST = os.getenv("ALERTS_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("ALERTS_SMTP_PORT", "587"))
SMTP_USER = os.getenv("ALERTS_SMTP_USER", "raedbari203@gmail.com")
SMTP_PASS = os.getenv("ALERTS_SMTP_PASS", "plds tltg vvzu kgwr")  # App Password
SMTP_FROM = os.getenv("ALERTS_FROM", f"Smart DevOps Alerts <{SMTP_USER}>")
# Fallback email if no matching tenant user is found
FALLBACK_EMAIL = os.getenv("ALERTS_FALLBACK_EMAIL", "raedbari203@gmail.com")


def send_email_smtp(to_email: str, subject: str, html_body: str) -> None:
    """Send an email using SMTP (Gmail App Password)."""
    msg = MIMEText(html_body, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = to_email

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, [to_email], msg.as_string())


def resolve_recipient(db: Session, namespace: str) -> str:
    """
    Determine the appropriate recipient email based on the namespace:
      - Find tenant with tenants.k8s_namespace == namespace
      - Then pick one of the users from that tenant (priority: client > devops > tenant_admin > platform_admin)
    """
    if not namespace:
        return FALLBACK_EMAIL

    t = db.execute(
        select(Tenant).where(Tenant.k8s_namespace == namespace)
    ).scalar_one_or_none()

    if not t:
        return FALLBACK_EMAIL

    priority = ["client", "devops", "tenant_admin", "platform_admin"]
    users: List[User] = db.execute(
        select(User).where(User.tenant_id == t.id)
    ).scalars().all()

    if not users:
        return FALLBACK_EMAIL

    # Pick user by role priority
    users_sorted = sorted(
        users,
        key=lambda u: priority.index(u.role) if u.role in priority else len(priority)
    )
    return users_sorted[0].email or FALLBACK_EMAIL


@router.post("")
async def alertmanager_webhook(request: Request, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """
    Receives payload from Alertmanager (JSON) and sends emails to appropriate tenant users.
    Alertmanager sends a key 'alerts' containing a list of alerts.
    """
    try:
        payload: Dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(400, detail="Invalid JSON")

    alerts: List[Dict[str, Any]] = payload.get("alerts") or []
    if not alerts:
        return {"ok": True, "processed": 0}

    processed = 0
    for a in alerts:
        labels = a.get("labels") or {}
        annotations = a.get("annotations") or {}
        namespace = labels.get("namespace") or "unknown"
        alertname = labels.get("alertname") or "Alert"
        severity = labels.get("severity") or "info"
        status = a.get("status") or "firing"

        description = (
            annotations.get("description")
            or annotations.get("summary")
            or "No description"
        )

        # Determine recipient
        to_email = resolve_recipient(db, namespace)

        subject = f"[SmartDevOps][{status.upper()}] {alertname} ns={namespace} severity={severity}"
        html = f"""
        <div style="font-family: sans-serif; line-height:1.6">
          <h2>SmartDevOps Alert</h2>
          <p><b>Status:</b> {status}</p>
          <p><b>Alert:</b> {alertname}</p>
          <p><b>Namespace:</b> {namespace}</p>
          <p><b>Severity:</b> {severity}</p>
          <p><b>Description:</b><br/>{description}</p>
          <hr/>
          <small>Sent automatically by SmartDevOps Alert Webhook.</small>
        </div>
        """

        try:
            send_email_smtp(to_email, subject, html)
            processed += 1
        except Exception as e:
            # Continue even if one email fails
            print(f"[alerts] failed to send email to {to_email}: {e}")

    return {"ok": True, "processed": processed}


# Internal test endpoint for quick email testing
@router.post("/test")
def test_send(to: Optional[str] = None) -> Dict[str, Any]:
    to_email = to or FALLBACK_EMAIL
    send_email_smtp(
        to_email,
        "[SmartDevOps] Test Alert",
        "<b>This is a test email from SmartDevOps alert webhook.</b>",
    )
    return {"ok": True, "to": to_email}
