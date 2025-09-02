# app/k8s_ops.py
"""
Kubernetes operations for our platform:
- Build a V1Container from AppSpec.
- Upsert (create or patch) a Deployment (adopt existing by name).
- Upsert a Service (adopt existing by name) without breaking NodePort settings.
- Scale a Deployment via the Scale subresource.
- List status of managed Deployments.
"""

from __future__ import annotations
from typing import Dict, List, Optional

from kubernetes import client
try:
    # kubernetes >= 28
    from kubernetes.client.exceptions import ApiException  # type: ignore
except Exception:  # pragma: no cover
    # kubernetes < 28
    from kubernetes.client.rest import ApiException  # type: ignore

from .k8s_client import get_api_clients, get_namespace, platform_labels
from .models import AppSpec, StatusItem, StatusResponse


def _container_from_spec(spec: AppSpec) -> client.V1Container:
    """
    Convert AppSpec into a V1Container with ports, probes, resources, env, and security context.
    Uses spec.effective_container_name so we don't break existing CI commands that reference
    the container by name (e.g., 'nodejs').
    """
    env_list = [client.V1EnvVar(name=e.name, value=e.value) for e in (spec.env or [])]

    liveness = client.V1Probe(
        http_get=client.V1HTTPGetAction(path=spec.health_path, port=spec.port),
        initial_delay_seconds=10,
        period_seconds=10,
        timeout_seconds=2,
        failure_threshold=3,
    )
    readiness = client.V1Probe(
        http_get=client.V1HTTPGetAction(
            path=spec.readiness_path or spec.health_path, port=spec.port
        ),
        initial_delay_seconds=5,
        period_seconds=5,
        timeout_seconds=2,
        failure_threshold=3,
    )

    resources = client.V1ResourceRequirements(**(spec.resources or {}))

    return client.V1Container(
        name=spec.effective_container_name,   # <<< important
        image=spec.full_image,
        image_pull_policy="Always",
        ports=[client.V1ContainerPort(container_port=spec.port)],
        env=env_list,
        liveness_probe=liveness,
        readiness_probe=readiness,
        resources=resources,
        security_context=client.V1SecurityContext(
            run_as_user=1001,
            run_as_non_root=True,
            allow_privilege_escalation=False,
        ),
    )


def upsert_deployment(spec: AppSpec) -> Dict:
    """
    Create or patch a Deployment named <spec.name> in the working namespace.
    Selector remains immutable for existing Deployments; we patch only replicas and template.
    """
    ns = get_namespace()
    apps = get_api_clients()["apps"]

    app_label = spec.effective_app_label
    labels = platform_labels({"app": app_label})

    pod_template = client.V1PodTemplateSpec(
        metadata=client.V1ObjectMeta(labels=labels),
        spec=client.V1PodSpec(containers=[_container_from_spec(spec)]),
    )

    body = client.V1Deployment(
        api_version="apps/v1",
        kind="Deployment",
        metadata=client.V1ObjectMeta(name=spec.name, labels=labels),
        spec=client.V1DeploymentSpec(
            replicas=spec.replicas,
            selector=client.V1LabelSelector(match_labels={"app": app_label}),
            template=pod_template,
        ),
    )

    try:
        # Exists → patch only labels, replicas, and template
        _ = apps.read_namespaced_deployment(name=spec.name, namespace=ns)
        patch_body = {
            "metadata": {"labels": labels},
            "spec": {
                "replicas": spec.replicas,
                "template": pod_template.to_dict(),
            },
        }
        resp = apps.patch_namespaced_deployment(
            name=spec.name, namespace=ns, body=patch_body
        )
    except ApiException as e:
        if getattr(e, "status", None) == 404:
            resp = apps.create_namespaced_deployment(namespace=ns, body=body)
        else:
            raise

    return resp.to_dict()


def upsert_service(spec: AppSpec) -> Dict:
    """
    Create or patch a Service exposing the app.
    Adoption rules:
      - If Service exists, DO NOT change .spec.type or .spec.ports (to keep NodePort).
        Only update metadata.labels and .spec.selector.
      - If it does not exist, create a ClusterIP Service with the given port.
    """
    ns = get_namespace()
    core = get_api_clients()["core"]

    app_label = spec.effective_app_label
    svc_name = spec.effective_service_name
    labels = platform_labels({"app": app_label})

    desired_ports = [
        client.V1ServicePort(name="http", port=spec.port, target_port=spec.port)
    ]

    try:
        _existing = core.read_namespaced_service(name=svc_name, namespace=ns)
        patch_body = {
            "metadata": {"labels": labels},
            "spec": {"selector": {"app": app_label}},
        }
        resp = core.patch_namespaced_service(
            name=svc_name, namespace=ns, body=patch_body
        )
    except ApiException as e:
        if getattr(e, "status", None) == 404:
            body = client.V1Service(
                api_version="v1",
                kind="Service",
                metadata=client.V1ObjectMeta(name=svc_name, labels=labels),
                spec=client.V1ServiceSpec(
                    type="ClusterIP",
                    selector={"app": app_label},
                    ports=desired_ports,
                ),
            )
            resp = core.create_namespaced_service(namespace=ns, body=body)
        else:
            raise

    return resp.to_dict()


def list_status(name: Optional[str] = None) -> StatusResponse:
    """
    Return status for either a single Deployment (if `name` is provided)
    or all Deployments labeled as managed-by our platform.
    """
    ns = get_namespace()
    apps = get_api_clients()["apps"]

    if name:
        deployments = [apps.read_namespaced_deployment(name=name, namespace=ns)]
    else:
        deployments = apps.list_namespaced_deployment(
            namespace=ns, label_selector="managed-by=cloud-devops-platform"
        ).items

    items: List[StatusItem] = []
    for d in deployments:
        spec = d.spec or client.V1DeploymentSpec()
        status = d.status or client.V1DeploymentStatus()

        image = ""
        try:
            image = (spec.template.spec.containers or [])[0].image  # type: ignore[assignment]
        except Exception:
            pass

        conds = {c.type: c.status for c in (status.conditions or [])}
        items.append(
            StatusItem(
                name=d.metadata.name,
                image=image,
                desired=spec.replicas or 0,
                current=status.replicas or 0,
                available=status.available_replicas or 0,
                updated=status.updated_replicas or 0,
                conditions=conds,
            )
        )

    return StatusResponse(items=items)


def scale(name: str, replicas: int) -> Dict:
    """
    Patch the Scale subresource of a Deployment to set a new replica count.
    """
    ns = get_namespace()
    apps = get_api_clients()["apps"]
    body = {"spec": {"replicas": replicas}}
    resp = apps.patch_namespaced_deployment_scale(name=name, namespace=ns, body=body)
    return resp.to_dict()
