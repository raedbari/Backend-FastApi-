# app/models.py
# Data schemas exchanged between frontend and backend. Pydantic v2.

from __future__ import annotations
from typing import List, Dict, Optional
from pydantic import BaseModel, Field
import os
from sqlalchemy.orm import declarative_base
from sqlalchemy import Column, Integer, String, Index , DateTime, ForeignKey , func, UniqueConstraint
from datetime import datetime
from sqlalchemy.orm import relationship

# ----- K8s naming pattern & defaults -----
DNS1123_LABEL = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"
DEFAULT_NS = os.getenv("DEFAULT_NAMESPACE", "default")



Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    tenant_id = Column(String, index=True, nullable=False)
    role = Column(String, default="admin")

Index("ix_users_tenant_username", User.tenant_id, User.username)


class EnvVar(BaseModel):
    """Represents a container environment variable, e.g., NODE_ENV=production."""
    name: str = Field(..., min_length=1)
    value: str = Field(...)


class AppSpec(BaseModel):
    """Application contract for deployments/adoption."""
    # Security behavior (used dynamically in k8s_ops.py)
    compat_mode: bool = False
    run_as_non_root: bool = True
    run_as_user: Optional[int] = 1001

    # Resource names / selectors
    name: str = Field(..., pattern=DNS1123_LABEL, description="K8s resource name")
    app_label: Optional[str] = None
    service_name: Optional[str] = None
    container_name: Optional[str] = None

    # Namespace
    namespace: str = Field(default=DEFAULT_NS, pattern=DNS1123_LABEL)

    # Image & runtime
    image: str
    tag: str
    port: int = Field(..., ge=1, le=65535)

    # HTTP paths
    health_path: str = "/healthz"
    readiness_path: str = "/ready"
    metrics_path: str = "/metrics"

    # Scaling & env
    replicas: int = Field(1, ge=1, le=50)
    env: List[EnvVar] = Field(default_factory=list)

    # Resources (leave None -> defaults applied in k8s_ops.py)
    resources: Optional[Dict[str, Dict[str, str]]] = None

    # --------- convenience computed properties ---------
    @property
    def full_image(self) -> str:
        return f"{self.image}:{self.tag}"

    @property
    def effective_app_label(self) -> str:
        return self.app_label or self.name

    @property
    def effective_service_name(self) -> str:
        return self.service_name or self.name

    @property
    def effective_container_name(self) -> str:
        return self.container_name or self.name

    @property
    def effective_port(self) -> int:
        p = self.port or 8080
        return 8080 if p < 1024 else p

    @property
    def effective_health_path(self) -> str:
        return (self.health_path or "/").strip() or "/"


class ScaleRequest(BaseModel):
    """Scaling request for an already-deployed application."""
    name: str
    replicas: int = Field(..., ge=1, le=100)
    namespace: str = Field(default=DEFAULT_NS, pattern=DNS1123_LABEL)


class StatusItem(BaseModel):
    """Describes the status of a managed Deployment."""
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
    """List of managed application statuses."""
    items: List[StatusItem]


class KPIQuery(BaseModel):
    """Simplified query for Prometheus KPIs (used later)."""
    app: Optional[str] = None
    window: str = Field("1m", description="e.g., 1m or 5m")
    namespace: Optional[str] = Field(default=None, pattern=DNS1123_LABEL)


from .db import Base


class Tenant(Base):
    __tablename__ = "tenants"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False, unique=True)           # اسم العميل (شركة/مؤسسة)
    k8s_namespace = Column(String(200), nullable=False, unique=True)  # النيمسبيس المخصص للعميل
    status = Column(String(50), nullable=False, default="pending")    # pending / active / suspended
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    # علاقة مستخدمين ← عميل
    users = relationship("User", back_populates="tenant", cascade="all,delete-orphan")

    __table_args__ = (
        Index("ix_tenants_name", "name"),
    )

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(200), nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(50), nullable=False, default="admin")        # admin / user
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    tenant = relationship("Tenant", back_populates="users")

    __table_args__ = (
        UniqueConstraint("email", "tenant_id", name="uq_users_email_tenant"),
        Index("ix_users_tenant_id", "tenant_id"),
    )