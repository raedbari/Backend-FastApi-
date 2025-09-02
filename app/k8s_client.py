# app/k8s_client.py
from __future__ import annotations

import os
from typing import Dict, Optional

from kubernetes import client as k8s_client, config as k8s_config

# Cache a single ApiClient instance to avoid recreating it on each call
_api_client: Optional[k8s_client.ApiClient] = None


def _load_config() -> k8s_client.ApiClient:
    """
    Load Kubernetes configuration in this order:
    1) In-cluster config (when running inside Kubernetes)
    2) Local kubeconfig (~/.kube/config) for development
    Returns a cached ApiClient instance.
    """
    global _api_client
    if _api_client is not None:
        return _api_client
    try:
        k8s_config.load_incluster_config()
    except Exception:
        k8s_config.load_kube_config()
    _api_client = k8s_client.ApiClient()
    return _api_client


def get_api_clients() -> Dict[str, object]:
    """
    Return commonly used Kubernetes API clients:
      - 'apps'   -> AppsV1Api
      - 'core'   -> CoreV1Api
      - 'custom' -> CustomObjectsApi
    """
    api = _load_config()
    return {
        "apps": k8s_client.AppsV1Api(api),
        "core": k8s_client.CoreV1Api(api),
        "custom": k8s_client.CustomObjectsApi(api),
    }


def get_namespace() -> str:
    """
    Resolve the working namespace in this order:
      1) NAMESPACE env var (or PLATFORM_NAMESPACE)
      2) In-cluster service account namespace file
      3) Active context namespace from kubeconfig
      4) 'default'
    """
    ns = os.environ.get("NAMESPACE") or os.environ.get("PLATFORM_NAMESPACE")
    if ns:
        return ns

    # In-cluster namespace (mounted by ServiceAccount)
    try:
        with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace", "r", encoding="utf-8") as f:
            v = f.read().strip()
            if v:
                return v
    except Exception:
        pass

    # Active kubeconfig context namespace (for local dev)
    try:
        contexts, active = k8s_config.list_kube_config_contexts()
        if active:
            return active.get("context", {}).get("namespace", "default") or "default"
    except Exception:
        pass

    return "default"


def platform_labels(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """
    Standard labels to mark resources as managed by this platform.
    You can pass extra labels to merge.
    """
    base = {
        "managed-by": "cloud-devops-platform",
        "app.kubernetes.io/managed-by": "cloud-devops-platform",
    }
    if extra:
        base.update(extra)
    return base
