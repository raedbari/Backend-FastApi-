from __future__ import annotations

from typing import Any, Dict, List, Optional
import logging
import os
import smtplib
from email.mime.text import MIMEText

from fastapi import APIRouter, Request, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.db import get_db
from app.models import Tenant, User

router = APIRouter(prefix="/api/alerts", tags=["alerts"])

logger = logging.getLogger("smartdevops.alerts")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

# SMTP settings from environment variables
SMTP_HOST = os.getenv("ALERTS_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("ALERTS_SMTP_PORT", "587"))
SMTP_USER = os.getenv("ALERTS_SMTP_USER", "raedbari203@gmail.com")
SMTP_PASS = os.getenv("ALERTS_SMTP_PASS", "plds tltg vvzu kgwr")
SMTP_FROM = os.getenv("ALERTS_FROM", f"Smart DevOps Alerts <{SMTP_USER}>")
FALLBACK_EMAIL = os.getenv("ALERTS_FALLBACK_EMAIL", "raedbari203@gmail.com")


def send_email_smtp(to_email: str, subject: str, html_body: str) -> None:
    """Send an email using SMTP."""
    to_email = (to_email or "").strip()
    if not to_email:
        raise ValueError("Recipient email is empty")

    logger.info(
        "[alerts] connecting SMTP host=%s port=%s user=%s to=%s",
        SMTP_HOST,
        SMTP_PORT,
        SMTP_USER,
        to_email,
    )

    msg = MIMEText(html_body, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = to_email

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, [to_email], msg.as_string())

    logger.info("[alerts] email sent successfully to %s", to_email)


def resolve_recipient(db: Session, namespace: str) -> str:
    """
    Determine recipient email from namespace:
      - Find tenant with tenants.k8s_namespace == namespace
      - Pick one user from that tenant by role priority:
        client > devops > tenant_admin > platform_admin
      - Fallback to FALLBACK_EMAIL if nothing matches
    """
    namespace = (namespace or "").strip()
    if not namespace:
        logger.warning("[alerts] empty namespace, using fallback email")
        return FALLBACK_EMAIL

    tenant = db.execute(
        select(Tenant).where(Tenant.k8s_namespace == namespace)
    ).scalar_one_or_none()

    if not tenant:
        logger.warning(
            "[alerts] no tenant found for namespace=%s, using fallback=%s",
            namespace,
            FALLBACK_EMAIL,
        )
        return FALLBACK_EMAIL

    priority = ["client", "devops", "tenant_admin", "platform_admin"]

    users: List[User] = db.execute(
        select(User).where(User.tenant_id == tenant.id)
    ).scalars().all()

    if not users:
        logger.warning(
            "[alerts] tenant found but no users for tenant_id=%s namespace=%s, using fallback=%s",
            tenant.id,
            namespace,
            FALLBACK_EMAIL,
        )
        return FALLBACK_EMAIL

    users_sorted = sorted(
        users,
        key=lambda u: priority.index(u.role) if u.role in priority else len(priority),
    )

    chosen = (users_sorted[0].email or "").strip()
    if not chosen:
        logger.warning(
            "[alerts] chosen user has empty email for tenant_id=%s namespace=%s, using fallback=%s",
            tenant.id,
            namespace,
            FALLBACK_EMAIL,
        )
        return FALLBACK_EMAIL

    logger.info(
        "[alerts] resolved recipient namespace=%s tenant_id=%s recipient=%s role=%s",
        namespace,
        tenant.id,
        chosen,
        users_sorted[0].role,
    )
    return chosen


@router.post("")
async def alertmanager_webhook(
    request: Request, db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """
    Receives Alertmanager JSON payload and emails tenant recipients.
    Returns debug-friendly information while still being 200 OK for Alertmanager.
    """
    try:
        payload: Dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    alerts: List[Dict[str, Any]] = payload.get("alerts") or []
    if not alerts:
        logger.info("[alerts] webhook called with no alerts")
        return {"ok": True, "processed": 0, "failed": 0, "results": []}

    processed = 0
    failed = 0
    results: List[Dict[str, Any]] = []

    logger.info("[alerts] received %d alert(s)", len(alerts))

    for idx, alert in enumerate(alerts, start=1):
        labels = alert.get("labels") or {}
        annotations = alert.get("annotations") or {}

        namespace = (labels.get("namespace") or "unknown").strip()
        alertname = (labels.get("alertname") or "Alert").strip()
        severity = (labels.get("severity") or "info").strip()
        status = (alert.get("status") or "firing").strip()

        description = (
            annotations.get("description")
            or annotations.get("summary")
            or "No description"
        )

        to_email = resolve_recipient(db, namespace)

        logger.info(
            "[alerts] processing #%d alert=%s namespace=%s severity=%s status=%s recipient=%s",
            idx,
            alertname,
            namespace,
            severity,
            status,
            to_email,
        )

        subject = (
            f"[SmartDevOps][{status.upper()}] "
            f"{alertname} ns={namespace} severity={severity}"
        )

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
            results.append(
                {
                    "alertname": alertname,
                    "namespace": namespace,
                    "recipient": to_email,
                    "status": "sent",
                }
            )
        except Exception as e:
            failed += 1
            logger.exception(
                "[alerts] failed to send email alert=%s namespace=%s recipient=%s error=%r",
                alertname,
                namespace,
                to_email,
                e,
            )
            results.append(
                {
                    "alertname": alertname,
                    "namespace": namespace,
                    "recipient": to_email,
                    "status": "failed",
                    "error": repr(e),
                }
            )

    logger.info(
        "[alerts] webhook finished processed=%d failed=%d",
        processed,
        failed,
    )

    return {
        "ok": True,
        "processed": processed,
        "failed": failed,
        "results": results,
    }


@router.post("/test")
def test_send(to: Optional[str] = None) -> Dict[str, Any]:
    to_email = (to or FALLBACK_EMAIL).strip()

    logger.info("[alerts] running test email to=%s", to_email)

    send_email_smtp(
        to_email,
        "[SmartDevOps] Test Alert",
        "<b>This is a test email from SmartDevOps alert webhook.</b>",
    )

    return {"ok": True, "to": to_email}