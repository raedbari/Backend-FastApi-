from sqlalchemy.orm import Session
from app.models import ActivityLog

def log_event(
    db: Session,
    user_id: str,
    user_email: str,
    tenant_ns: str,
    action: str,
    details: dict,
    ip: str,
    user_agent: str,
):
    log = ActivityLog(
        user_id=user_id,
        user_email=user_email,
        tenant_ns=tenant_ns,
        action=action,
        details=details,
        ip=ip,
        user_agent=user_agent,
    )

    db.add(log)
    db.commit()
