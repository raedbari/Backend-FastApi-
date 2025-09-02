# app/models.py
# Data schemas exchanged between frontend and backend.
# Pydantic v2 compatible (note: use 'pattern' instead of the removed 'regex').

from __future__ import annotations
from typing import List, Dict, Optional
from pydantic import BaseModel, Field


class EnvVar(BaseModel):
    """
    Represents a container environment variable, e.g., NODE_ENV=production.
    """
    name: str = Field(..., min_length=1)
    value: str = Field(...)


class AppSpec(BaseModel):
    """
    Application contract for deployments/adoption.

    Key fields:
    - name: Kubernetes resource name (Deployment name; lowercase, digits, hyphen).
    - app_label: label value for selector 'app'; defaults to 'name'.
    - service_name: K8s Service name to manage/adopt; defaults to 'name'.
    - container_name: the container name inside the Deployment; defaults to 'name'.
      (Set this to your actual container name, e.g., 'nodejs' in your Deployment.)
    - image + tag -> full_image as 'image:tag'.
    - port, health_path, readiness_path, metrics_path, replicas, env, resources.
    """
    # Resource names / selectors
    name: str = Field(
        ...,
        pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$",
        description="K8s resource name (lowercase, digits, hyphen)."
    )
    app_label: Optional[str] = Field(
        None, description="Label value used by selectors (app=<value>); defaults to 'name'."
    )
    service_name: Optional[str] = Field(
        None, description="Existing Service name to manage; defaults to 'name'."
    )
    container_name: Optional[str] = Field(
        None, description="Container name inside the Deployment; defaults to 'name'."
    )

    # Image & runtime
    image: str = Field(..., description="e.g., raedbari/node.js")
    tag: str = Field(..., description="immutable image tag (e.g., short SHA)")
    port: int = Field(..., ge=1, le=65535)

    # HTTP paths
    health_path: str = Field("/healthz")
    readiness_path: str = Field("/ready")
    metrics_path: str = Field("/metrics")

    # Scaling & env
    replicas: int = Field(1, ge=1, le=50)
    env: List[EnvVar] = Field(default_factory=list)

    # Resources (pass-through to Kubernetes client)
    resources: Optional[Dict[str, Dict[str, str]]] = Field(
        default_factory=lambda: {
            "requests": {"cpu": "100m", "memory": "128Mi"},
            "limits":   {"cpu": "500m", "memory": "512Mi"},
        }
    )

    # --------- convenience computed properties ---------
    @property
    def full_image(self) -> str:
        """Returns 'image:tag'."""
        return f"{self.image}:{self.tag}"

    @property
    def effective_app_label(self) -> str:
        """Label value used in selectors; falls back to resource name."""
        return self.app_label or self.name

    @property
    def effective_service_name(self) -> str:
        """Service name to manage; falls back to resource name."""
        return self.service_name or self.name

    @property
    def effective_container_name(self) -> str:
        """Container name to patch; falls back to resource name."""
        return self.container_name or self.name


class ScaleRequest(BaseModel):
    """Scaling request for an already-deployed application."""
    name: str
    replicas: int = Field(..., ge=1, le=100)


class StatusItem(BaseModel):
    """Describes the status of a managed Deployment."""
    name: str
    image: str
    desired: int
    current: int
    available: int
    updated: int
    conditions: Dict[str, str] = Field(default_factory=dict)


class StatusResponse(BaseModel):
    """List of managed application statuses."""
    items: List[StatusItem]


class KPIQuery(BaseModel):
    """
    Simplified query for Prometheus KPIs (used later):
    - app: optional app filter.
    - window: PromQL range, e.g., 1m or 5m.
    """
    app: Optional[str] = None
    window: str = Field("1m", description="e.g., 1m or 5m")
