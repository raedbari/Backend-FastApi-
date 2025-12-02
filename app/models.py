# app/models.py
# Data schemas exchanged between frontend and backend. Pydantic v2.

from __future__ import annotations
from typing import List, Dict, Optional
from pydantic import BaseModel, Field
import os
from sqlalchemy import (
    Column, Integer, String, Index, DateTime, ForeignKey, func,
    UniqueConstraint, CheckConstraint, Text
)
from sqlalchemy.orm import relationship
from sqlalchemy import BigInteger

# SQLAlchemy Base
from .db import Base
from sqlalchemy.dialects.postgresql import JSONB

# --------------------------------------------------------------------
# --------------------------- Pydantic -------------------------------
# --------------------------------------------------------------------

DNS1123_LABEL = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"
DEFAULT_NS = os.getenv("DEFAULT_NAMESPACE", "default")

class EnvVar(BaseModel):
    name: str = Field(..., min_length=1)
    value: str = Field(...)

class AppSpec(BaseModel):
    compat_mode: bool = False
    run_as_non_root: bool = True
    run_as_user: Optional[int] = 1001

    name: str = Field(..., pattern=DNS1123_LABEL)
    app_label: Optional[str] = None
    service_name: Optional[str] = None
    container_name: Optional[str] = None

    namespace: str = Field(default=DEFAULT_NS, pattern=DNS1123_LABEL)

    image: str
    tag: str
    port: int = Field(..., ge=1, le=65535)

    health_path: str = "/healthz"
    readiness_path: str = "/ready"
    metrics_path: str = "/metrics"

    replicas: int = Field(1, ge=1, le=50)
    env: List[EnvVar] = Field(default_factory=list)

    resources: Optional[Dict[str, Dict[str, str]]] = None

    @property
    def full_image(self) -> str:
        return f"{self.image}:{self.tag}"

class ScaleRequest(BaseModel):
    name: str
    replicas: int = Field(..., ge=1, le=100)
    namespace: str = Field(default=DEFAULT_NS, pattern=DNS1123_LABEL)

class StatusItem(BaseModel):
    name: str
    image: str
    desired: int
    current: int
    available: int
    updated: int
    conditions: Dict[str, str] = Field(default_factory=dict)
    svc_selector: Optional[Dict[str, str]] = None
    preview_ready: Optional[bool] = None

class StatusResponse(BaseModel):
    items: List[StatusItem]


# --------------------------------------------------------------------
# ------------------------- SQLAlchemy -------------------------------
# --------------------------------------------------------------------

class Tenant(Base):
    __tablename__ = "tenants"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False, unique=True)
    k8s_namespace = Column(String(200), nullable=True, unique=True)
    status = Column(String(50), nullable=False, default="pending")
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    users = relationship("User", back_populates="tenant", cascade="all,delete-orphan")

    __table_args__ = (
        Index("ix_tenants_name", "name"),
        CheckConstraint(
            "status IN ('pending','active','suspended','rejected')",
            name="ck_tenants_status_valid"
        ),
    )


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(200), nullable=False, unique=True, index=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(50), nullable=False, default="admin")
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    tenant = relationship("Tenant", back_populates="users")


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    action = Column(String(100), nullable=False)
    actor_email = Column(String(200), nullable=False)
    result = Column(String(200), nullable=False, default="ok")
    created_at = Column(DateTime, nullable=False, server_default=func.now())


class ProvisioningRun(Base):
    __tablename__ = "provisioning_runs"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    status = Column(String(50), nullable=False, default="queued")
    last_error = Column(Text, nullable=True)
    retries = Column(Integer, nullable=True, default=0)
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


# --------------------------------------------------------------------
# ------------------------- Activity Logs -----------------------------
# --------------------------------------------------------------------

class ActivityLog(Base):
    __tablename__ = "activity_logs"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)

    # 🔥 نستخدم email فقط – user_id غير ضروري
    user_id = Column(String, nullable=True)

    user_email = Column(Text, nullable=False)
    tenant_ns = Column(Text, nullable=True)

    action = Column(Text, nullable=False)
    details = Column(JSONB, nullable=True)

    ip = Column(Text, nullable=True)
    user_agent = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
