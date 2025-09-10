# app/k8s_ops.py
"""
Kubernetes operations for our platform:
- Build a V1Container from AppSpec.
- Upsert (create or patch) a Deployment (adopt existing by name).
- Upsert a Service (adopt existing by name) without breaking NodePort.
- Scale a Deployment via the Scale subresource.
- List status of managed Deployments.

NOTE: For patch operations we pass typed Kubernetes objects (V1Deployment / V1Service)
not raw dicts, so the serializer emits proper camelCase (containerPort, targetPort, …).
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


# def _container_from_spec(spec: AppSpec) -> client.V1Container:
#     """Convert AppSpec into a V1Container (ports, probes, resources, env, security)."""
#     # استخدم القيم الفعّالة المطبَّعة
#     port = spec.effective_port
#     path = spec.effective_health_path

#     env_list = [client.V1EnvVar(name=e.name, value=e.value) for e in (spec.env or [])]

#     # موارد افتراضية متواضعة إذا لم يمرّر المستخدم شيئًا
#     default_resources = {
#         "requests": {"cpu": "20m", "memory": "64Mi"},
#         "limits":   {"cpu": "200m", "memory": "256Mi"},
#     }
#     res = spec.resources or default_resources
#     resources = client.V1ResourceRequirements(
#         requests=res.get("requests", default_resources["requests"]),
#         limits=res.get("limits", default_resources["limits"]),
#     )

#     return client.V1Container(
#         name=spec.effective_container_name,
#         image=spec.full_image,
#         image_pull_policy="Always",
#         ports=[client.V1ContainerPort(container_port=port, name="http", protocol="TCP")],
#         env=env_list,
#         # بروبس موحّدة على "/" والمنفذ الفعّال
#         readiness_probe=client.V1Probe(
#             http_get=client.V1HTTPGetAction(path=path, port=port),
#             initial_delay_seconds=5, period_seconds=5, timeout_seconds=2, failure_threshold=3
#         ),
#         liveness_probe=client.V1Probe(
#             http_get=client.V1HTTPGetAction(path=path, port=port),
#             initial_delay_seconds=10, period_seconds=10, timeout_seconds=2, failure_threshold=3
#         ),
#         # startup_probe=client.V1Probe(
#         #     http_get=client.V1HTTPGetAction(path=path, port=port),
#         #     failure_threshold=30, period_seconds=2
#         # ),
#         security_context=client.V1SecurityContext(
#             run_as_user=1001,
#             run_as_non_root=True,
#             allow_privilege_escalation=False,
#         ),
#         resources=resources,
#     )



# أعلى الملف (للتوافق مع kubernetes >=28 و <28)


def upsert_deployment(spec: AppSpec) -> dict:
    ns   = spec.namespace or get_namespace()
    apps = get_api_clients()["apps"]

    name   = spec.effective_app_label
    port   = spec.effective_port
    path   = spec.effective_health_path
    labels = platform_labels({"app": name})

    # ---- SecurityContext ديناميكي (compat_mode / run_as_non_root / run_as_user) ----
    sc = client.V1SecurityContext(allow_privilege_escalation=False)
    if not getattr(spec, "compat_mode", False) and getattr(spec, "run_as_non_root", True):
        sc.run_as_non_root = True
        sc.run_as_user = getattr(spec, "run_as_user", None) or 1001
    # else: نترك الصورة تعمل بإعداداتها (قد تكون root)

    # ---- موارد افتراضية خفيفة (وتُستبدَل إن مرّر المستخدم موارد) ----
    default_resources = {
        "requests": {"cpu": "20m", "memory": "64Mi"},
        "limits":   {"cpu": "200m", "memory": "256Mi"},
    }
    res = spec.resources or default_resources
    resources = client.V1ResourceRequirements(
        requests=res.get("requests", default_resources["requests"]),
        limits=res.get("limits",   default_resources["limits"]),
    )

    # ---- الحاوية (بدون startupProbe افتراضيًا) ----
    container = client.V1Container(
        name=name,
        image=(f"{spec.image}:{spec.tag}" if getattr(spec, "tag", None) else spec.image),
        image_pull_policy="Always",
        ports=[client.V1ContainerPort(container_port=port, name="http")],
        security_context=sc,
        resources=resources,
        readiness_probe=client.V1Probe(
            http_get=client.V1HTTPGetAction(path=path, port=port),
            initial_delay_seconds=5, period_seconds=5, timeout_seconds=2, failure_threshold=3,
        ),
        liveness_probe=client.V1Probe(
            http_get=client.V1HTTPGetAction(path=path, port=port),
            initial_delay_seconds=10, period_seconds=10, timeout_seconds=2, failure_threshold=3,
        ),
    )

    pod_template = client.V1PodTemplateSpec(
        metadata=client.V1ObjectMeta(labels=labels),
        spec=client.V1PodSpec(containers=[container]),
    )

    dep_spec = client.V1DeploymentSpec(
        replicas=spec.replicas or 1,
        selector=client.V1LabelSelector(match_labels={"app": name}),
        template=pod_template,
        strategy=client.V1DeploymentStrategy(
            type="RollingUpdate",
            rolling_update=client.V1RollingUpdateDeployment(max_surge=1, max_unavailable=0),
        ),
    )

    body = client.V1Deployment(
        api_version="apps/v1",
        kind="Deployment",
        metadata=client.V1ObjectMeta(name=name, namespace=ns, labels=labels),
        spec=dep_spec,
    )

    try:
        apps.read_namespaced_deployment(name=name, namespace=ns)
        resp = apps.patch_namespaced_deployment(name=name, namespace=ns, body=body)
    except ApiException as e:
        if getattr(e, "status", None) == 404:
            resp = apps.create_namespaced_deployment(namespace=ns, body=body)
        else:
            raise
    return resp.to_dict()


def upsert_service(spec: AppSpec) -> dict:
    """
    Create or patch a Service named <spec.service_name> في الـnamespace المحسوم.
    - إذا كانت الخدمة موجودة ونوعها NodePort، نُبقي nodePort/type كما هما.
    - نضبط دائمًا targetPort ليطابق المنفذ الفعّال للحاوية (effective_port).
    - إذا لم توجد، ننشئ ClusterIP افتراضيًا.
    """
    ns   = spec.namespace or get_namespace()
    core = get_api_clients()["core"]

    app_label = spec.effective_app_label
    svc_name  = spec.effective_service_name
    labels    = platform_labels({"app": app_label})
    port      = spec.effective_port

    try:
        existing = core.read_namespaced_service(name=svc_name, namespace=ns)

        svc_type     = existing.spec.type or "ClusterIP"
        cluster_port = (existing.spec.ports[0].port if existing.spec.ports else port)
        node_port    = None
        if svc_type == "NodePort" and existing.spec.ports:
            node_port = existing.spec.ports[0].node_port

        patch_body = client.V1Service(
            api_version="v1",
            metadata=client.V1ObjectMeta(labels=labels),
            spec=client.V1ServiceSpec(
                selector={"app": app_label},
                type=svc_type,
                ports=[client.V1ServicePort(
                    name="http",
                    port=cluster_port,      # نُبقي Port الخدمة كما كان
                    target_port=port,       # نربطه بمنفذ الحاوية الفعّال
                    protocol="TCP",
                    node_port=node_port     # فقط إن كان NodePort
                )]
            )
        )
        resp = core.patch_namespaced_service(name=svc_name, namespace=ns, body=patch_body)

    except ApiException as e:
        if getattr(e, "status", None) == 404:
            create_body = client.V1Service(
                api_version="v1",
                kind="Service",
                metadata=client.V1ObjectMeta(name=svc_name, namespace=ns, labels=labels),
                spec=client.V1ServiceSpec(
                    type="ClusterIP",
                    selector={"app": app_label},
                    ports=[client.V1ServicePort(name="http", port=port, target_port=port, protocol="TCP")]
                )
            )
            resp = core.create_namespaced_service(namespace=ns, body=create_body)
        else:
            raise

    return resp.to_dict()



def list_status(name: Optional[str] = None, namespace: Optional[str] = None) -> StatusResponse:
    """Status for one/all managed Deployments in the resolved namespace."""
    ns = namespace or get_namespace()
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


def scale(name: str, replicas: int, namespace: Optional[str] = None) -> Dict:
    """Patch the Scale subresource of a Deployment in the resolved namespace."""
    ns = namespace or get_namespace()
    apps = get_api_clients()["apps"]
    body = {"spec": {"replicas": replicas}}
    resp = apps.patch_namespaced_deployment_scale(name=name, namespace=ns, body=body)
    return resp.to_dict()
