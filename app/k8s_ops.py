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

from typing import Optional, Dict, Any
from kubernetes import client, config


try:
    from kubernetes.client.exceptions import ApiException  # kubernetes >= 28
except Exception:
    from kubernetes.client.rest import ApiException  # kubernetes < 28

from .k8s_client import get_api_clients, get_namespace, platform_labels
from .models import AppSpec, StatusItem, StatusResponse

# ============================================================
# 🧩  Create or Update the Deployment
# ============================================================

def upsert_deployment(spec: AppSpec) -> dict:
    ns = spec.namespace or get_namespace()
    apis = get_api_clients()
    apps = apis["apps"]
    v1   = apis["core"]  # ✅ CoreV1Api

    name   = spec.effective_app_label
    port   = spec.effective_port
    path   = spec.effective_health_path
    labels = platform_labels({"app": name, "role": "active"})

    # ----------------------------
    # ✅ Ensure PVC exists FIRST
    # ----------------------------
    pvc_name   = getattr(spec, "pvc_name", None) or "tenant-storage"
    pvc_size   = getattr(spec, "pvc_size", None) or "500Mi"
    # اتركها None ليستخدم default StorageClass (عندك local-path default)
    storage_class = getattr(spec, "storage_class", None)

    ensure_tenant_pvc(
        v1=v1,
        ns=ns,
        pvc_name=pvc_name,
        size=pvc_size,
        storage_class=storage_class,
    )

    # ----------------------------
    # Security context + resources
    # ----------------------------
    sc = client.V1SecurityContext(allow_privilege_escalation=False)
    if not getattr(spec, "compat_mode", False) and getattr(spec, "run_as_non_root", True):
        sc.run_as_non_root = True
        sc.run_as_user = getattr(spec, "run_as_user", None) or 1001

    default_resources = {
        "requests": {"cpu": "20m", "memory": "64Mi"},
        "limits":   {"cpu": "200m", "memory": "256Mi"},
    }
    res = spec.resources or default_resources
    resources = client.V1ResourceRequirements(
        requests=res.get("requests", default_resources["requests"]),
        limits=res.get("limits",   default_resources["limits"]),
    )

    # ----------------------------
    # PVC mount
    # ----------------------------
    mount_path = getattr(spec, "pvc_mount_path", None) or "/data"

    volume_mounts = [
        client.V1VolumeMount(name="tenant-data", mount_path=mount_path)
    ]

    volumes = [
        client.V1Volume(
            name="tenant-data",
            persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                claim_name=pvc_name
            ),
        )
    ]

    # ----------------------------
    # Container
    # ----------------------------
    container = client.V1Container(
        name=name,
        image=(f"{spec.image}:{spec.tag}" if getattr(spec, "tag", None) else spec.image),
        image_pull_policy="Always",
        ports=[client.V1ContainerPort(container_port=port, name="http")],
        security_context=sc,
        resources=resources,
        volume_mounts=volume_mounts,
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
        spec=client.V1PodSpec(containers=[container], volumes=volumes),
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


# ============================================================
# 🌐 Automatically create Ingress (supports TLS + port discovery + privacy protection)
# ============================================================

from kubernetes import client
from kubernetes.client.rest import ApiException
from .k8s_client import get_api_clients
from .auth import get_current_context


def create_ingress_for_app(
    app_name: str,
    namespace: str,
    ctx=None,
    *,
    host: str | None = None,
    ingress_name: str | None = None,
    tls_secret: str | None = None,
    service_name: str | None = None,
):
    clients = get_api_clients()
    net_api = clients["networking"]
    core_api = clients["core"]

    if ctx is None:
        ctx = get_current_context()
    role = getattr(ctx, "role", "")

    if role == "platform_admin" and namespace != "default":
        print(f"🚫 platform_admin is not allowed to create Ingress inside customer namespaces ({namespace})")
        return
    if role != "platform_admin" and namespace == "default":
        print(f"🚫 User '{role}' is not allowed to deploy inside 'default' namespace")
        return

    # ✅ defaults (كما كان عندك)
    service_name = service_name or app_name
    ingress_name = ingress_name or f"{app_name}-ingress"
    tls_secret   = tls_secret   or f"{app_name}-tls"
    host         = host         or f"{app_name}.{namespace}.apps.smartdevops.lat"

    # Detect port from Service
    try:
        svc = core_api.read_namespaced_service(service_name, namespace)
        port_number = svc.spec.ports[0].port if svc.spec.ports else 8080
    except ApiException:
        print(f"⚠️ Service {service_name} not found in {namespace}, using default port 8080.")
        port_number = 8080

    ingress_manifest = client.V1Ingress(
        api_version="networking.k8s.io/v1",
        kind="Ingress",
        metadata=client.V1ObjectMeta(
            name=ingress_name,
            annotations={
                "kubernetes.io/ingress.class": "nginx",
                "cert-manager.io/cluster-issuer": "letsencrypt-prod",
            },
        ),
        spec=client.V1IngressSpec(
            tls=[client.V1IngressTLS(hosts=[host], secret_name=tls_secret)],
            rules=[
                client.V1IngressRule(
                    host=host,
                    http=client.V1HTTPIngressRuleValue(
                        paths=[
                            client.V1HTTPIngressPath(
                                path="/",
                                path_type="Prefix",
                                backend=client.V1IngressBackend(
                                    service=client.V1IngressServiceBackend(
                                        name=service_name,
                                        port=client.V1ServiceBackendPort(number=port_number),
                                    )
                                ),
                            )
                        ]
                    ),
                )
            ],
        ),
    )

    try:
        net_api.read_namespaced_ingress(ingress_name, namespace)
        net_api.delete_namespaced_ingress(ingress_name, namespace)
        print(f"♻️ Old Ingress {ingress_name} deleted in {namespace} — it will be recreated.")
    except ApiException as e:
        if getattr(e, "status", None) != 404:
            print(f"⚠️ Failed to check existing Ingress: {e}")

    net_api.create_namespaced_ingress(namespace=namespace, body=ingress_manifest)
    print(f"✅ Ingress {ingress_name} created successfully in {namespace}")
    print(f"🌍 URL: https://{host}")


def upsert_preview_deployment(spec: "AppSpec") -> dict:
    ns = spec.namespace or get_namespace()
    apis = get_api_clients()
    apps = apis["apps"]
    v1   = apis["core"]

    app_label = spec.effective_app_label
    dep_name  = f"{app_label}-preview"
    port      = spec.effective_port
    path      = spec.effective_health_path

    match_labels = {"app": app_label, "role": "preview"}
    labels = platform_labels(match_labels)

    # PVC مثل deploy
    pvc_name = getattr(spec, "pvc_name", None) or "tenant-storage"
    pvc_size = getattr(spec, "pvc_size", None) or "500Mi"
    storage_class = getattr(spec, "storage_class", None)
    ensure_tenant_pvc(v1=v1, ns=ns, pvc_name=pvc_name, size=pvc_size, storage_class=storage_class)

    mount_path = getattr(spec, "pvc_mount_path", None) or "/data"
    volume_mounts = [client.V1VolumeMount(name="tenant-data", mount_path=mount_path)]
    volumes = [client.V1Volume(
        name="tenant-data",
        persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(claim_name=pvc_name),
    )]

    container = client.V1Container(
        name=dep_name,
        image=f"{spec.image}:{spec.tag}",
        image_pull_policy="Always",
        ports=[client.V1ContainerPort(container_port=port, name="http")],
        volume_mounts=volume_mounts,
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
        spec=client.V1PodSpec(containers=[container], volumes=volumes),
    )

    # ⚠️ selector في Kubernetes immutable، فلا نحاول نعدله في patch إذا كان موجود
    create_body = client.V1Deployment(
        api_version="apps/v1",
        kind="Deployment",
        metadata=client.V1ObjectMeta(name=dep_name, namespace=ns, labels=labels),
        spec=client.V1DeploymentSpec(
            replicas=1,
            selector=client.V1LabelSelector(match_labels=match_labels),
            template=pod_template,
        ),
    )

    try:
        # موجود؟ اعمل patch فقط للـ template/spec اللي نحتاجه
        apps.read_namespaced_deployment(name=dep_name, namespace=ns)

        patch_body = {
            "metadata": {"labels": labels},
            "spec": {
                "replicas": 1,
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "containers": [{
                            "name": dep_name,
                            "image": f"{spec.image}:{spec.tag}",
                            "imagePullPolicy": "Always",
                            "ports": [{"containerPort": port, "name": "http"}],
                            "readinessProbe": {
                                "httpGet": {"path": path, "port": port},
                                "initialDelaySeconds": 5,
                                "periodSeconds": 5,
                                "timeoutSeconds": 2,
                                "failureThreshold": 3,
                            },
                            "livenessProbe": {
                                "httpGet": {"path": path, "port": port},
                                "initialDelaySeconds": 10,
                                "periodSeconds": 10,
                                "timeoutSeconds": 2,
                                "failureThreshold": 3,
                            },
                            "volumeMounts": [{"name": "tenant-data", "mountPath": mount_path}],
                        }],
                        "volumes": [{
                            "name": "tenant-data",
                            "persistentVolumeClaim": {"claimName": pvc_name}
                        }],
                    },
                },
            },
        }
        resp = apps.patch_namespaced_deployment(name=dep_name, namespace=ns, body=patch_body)

    except ApiException as e:
        if getattr(e, "status", None) == 404:
            resp = apps.create_namespaced_deployment(namespace=ns, body=create_body)
        else:
            raise

    return resp.to_dict()


# def bg_prepare_full(spec: AppSpec, ctx=None) -> dict:
#     preview_dep = upsert_preview_deployment(spec)
#     preview_svc = upsert_service_preview(spec, ctx)
#     return {"preview_deployment": preview_dep, "preview_service": preview_svc}

def bg_prepare_full(spec: AppSpec, ctx=None) -> dict:
    preview_dep = upsert_preview_deployment(spec)
    preview_svc = upsert_service_preview(spec, ctx)

    ns = spec.namespace or (getattr(ctx, "k8s_namespace", None) if ctx else None) or "default"
    app_label = spec.effective_app_label
    preview_host = f"preview-{app_label}.{ns}.apps.smartdevops.lat"

    return {
        "preview_deployment": preview_dep,
        "preview_service": preview_svc,
        "preview_host": preview_host,
        "preview_url": f"https://{preview_host}",
    }


# ============================================================
# ⚙️ Create or Update Service + Ingress (Secure and Role-Based Version)
# ============================================================
def upsert_service(spec: "AppSpec", ctx: "CurrentContext" = None) -> dict:
  
    current_ctx = ctx or get_current_context()
    role = getattr(current_ctx, "role", "")
    ns = getattr(current_ctx, "k8s_namespace", None) or getattr(spec, "namespace", None) or "default"

    # 🚫 Privacy protection: platform_admin is not allowed to access customer namespaces
    if role == "platform_admin" and ns != "default":
        raise PermissionError(f"🚫 platform_admin is not allowed to deploy inside customer namespaces ({ns}).")

    # 🚫 No other user is allowed to deploy inside the default namespace
    if role != "platform_admin" and ns == "default":
        raise PermissionError(f"🚫 User '{role}' is not allowed to deploy inside namespace 'default'.")

    print(f"🧭 User '{role}' is working within namespace: {ns}")

    core = get_api_clients()["core"]
    app_label = spec.effective_app_label
    svc_name = spec.effective_service_name
    port = spec.effective_port

    labels = platform_labels({"app": app_label, "role": "active"})
    selector = {"app": app_label, "slot": "blue"}  


    try:
        existing = core.read_namespaced_service(name=svc_name, namespace=ns)
        svc_type = existing.spec.type or "ClusterIP"
        cluster_port = existing.spec.ports[0].port if existing.spec.ports else port
        node_port = existing.spec.ports[0].node_port if svc_type == "NodePort" else None

        patch_body = client.V1Service(
            api_version="v1",
            metadata=client.V1ObjectMeta(labels=labels),
            spec=client.V1ServiceSpec(
                selector=selector,
                type=svc_type,
                ports=[
                    client.V1ServicePort(
                        name="http",
                        port=cluster_port,
                        target_port=port,
                        protocol="TCP",
                        node_port=node_port,
                    )
                ],
            ),
        )
        resp = core.patch_namespaced_service(name=svc_name, namespace=ns, body=patch_body)
        print(f"🔄 Service {svc_name} updated in {ns}")
    except ApiException as e:
        if getattr(e, "status", None) == 404:
            create_body = client.V1Service(
                api_version="v1",
                kind="Service",
                metadata=client.V1ObjectMeta(name=svc_name, namespace=ns, labels=labels),
                spec=client.V1ServiceSpec(
                    type="ClusterIP",
                    selector=selector,
                    ports=[
                        client.V1ServicePort(
                            name="http",
                            port=port,
                            target_port=port,
                            protocol="TCP",
                        )
                    ],
                ),
            )
            resp = core.create_namespaced_service(namespace=ns, body=create_body)
            print(f"✅ Service {svc_name} created in {ns}")
        else:
            raise

    try:
        print(f"🚀 Creating Ingress for app {app_label} in {ns}")
        create_ingress_for_app(app_label, ns, ctx=current_ctx)
    except Exception as e:
        print(f"⚠️ Failed to create or update Ingress for app {app_label} in {ns}: {e}")

    return resp.to_dict()

from kubernetes import client
from kubernetes.client.rest import ApiException

def upsert_service_preview(spec: "AppSpec", ctx: "CurrentContext" = None) -> dict:
    current_ctx = ctx or get_current_context()
    role = getattr(current_ctx, "role", "")
    ns = getattr(current_ctx, "k8s_namespace", None) or getattr(spec, "namespace", None) or "default"

    # نفس قواعد الأمان
    if role == "platform_admin" and ns != "default":
        raise PermissionError(f"🚫 platform_admin is not allowed to deploy inside customer namespaces ({ns}).")
    if role != "platform_admin" and ns == "default":
        raise PermissionError(f"🚫 User '{role}' is not allowed to deploy inside namespace 'default'.")

    core = get_api_clients()["core"]

    app_label = spec.effective_app_label
    svc_name = f"{app_label}-preview"
    port = spec.effective_port

    # preview selectors/labels
    selector = {"app": app_label, "role": "preview"}
    labels   = platform_labels({"app": app_label, "role": "preview"})

    try:
        existing = core.read_namespaced_service(name=svc_name, namespace=ns)
        svc_type = existing.spec.type or "ClusterIP"
        cluster_port = existing.spec.ports[0].port if existing.spec.ports else port

        patch_body = client.V1Service(
            api_version="v1",
            metadata=client.V1ObjectMeta(labels=labels),
            spec=client.V1ServiceSpec(
                selector=selector,
                type=svc_type,
                ports=[client.V1ServicePort(name="http", port=cluster_port, target_port=port, protocol="TCP")],
            ),
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
                    selector=selector,
                    ports=[client.V1ServicePort(name="http", port=port, target_port=port, protocol="TCP")],
                ),
            )
            resp = core.create_namespaced_service(namespace=ns, body=create_body)
        else:
            raise

    # ✅ Ingress preview (hostname الصحيح)
    host = f"preview-{app_label}.{ns}.apps.smartdevops.lat"
    create_ingress_for_app(
        app_name=app_label,
        namespace=ns,
        ctx=current_ctx,
        host=host,
        ingress_name=f"{app_label}-preview-ingress",
        tls_secret=f"{app_label}-preview-tls",
        service_name=svc_name,
    )

    return resp.to_dict()

# ---- Status / Scale / Blue-Green (Part 2/3) ----

def list_status(name: Optional[str] = None, namespace: Optional[str] = None) -> StatusResponse:
    ns = namespace or get_namespace()
    apps = get_api_clients()["apps"]

    # Get the list of Deployments
    if name:
        try:
            d = apps.read_namespaced_deployment(name=name, namespace=ns)
            deployments = [d]
        except ApiException:
            # Don't break the API: return an empty list
            return StatusResponse(items=[])
    else:
        deployments = apps.list_namespaced_deployment(
            namespace=ns,
            label_selector="app.kubernetes.io/managed-by=cloud-devops-platform",
        ).items

    items: List[StatusItem] = []

    for d in deployments:
        spec = d.spec or client.V1DeploymentSpec()
        status = d.status or client.V1DeploymentStatus()

        # Image
        image = ""
        try:
            containers = (spec.template.spec.containers or [])
            if containers:
                image = containers[0].image or ""
        except Exception:
            pass

        # Conditions
        conds = {c.type: c.status for c in (status.conditions or [])}

        # App name/label
        d_name = d.metadata.name
        d_labels = d.metadata.labels or {}
        app_label = d_labels.get("app", d_name)

        # Service selector (optional)
        try:
            svc_sel = get_service_selector(app_label, ns)
        except Exception:
            svc_sel = {}

        # Preview status (optional)
        try:
            prev_ok = get_preview_ready(app_label, ns)
        except Exception:
            prev_ok = False

        items.append(
            StatusItem(
                name=d_name,
                image=image,
                desired=spec.replicas or 0,
                current=status.replicas or 0,
                available=status.available_replicas or 0,
                updated=status.updated_replicas or 0,
                conditions=conds,
                svc_selector=svc_sel,
                preview_ready=prev_ok,
                host=get_app_host(ns, d_name),
            )
        )

    return StatusResponse(items=items)


def scale(name: str, replicas: int, namespace: Optional[str] = None) -> Dict:
    ns = namespace or get_namespace()
    apps = get_api_clients()["apps"]
    body = {"spec": {"replicas": replicas}}
    resp = apps.patch_namespaced_deployment_scale(name=name, namespace=ns, body=body)
    return resp.to_dict()


# ----------------------------- Blue/Green helpers -----------------------------
def _labels_for(app_label: str, role: str) -> dict:
    return platform_labels({"app": app_label, "role": role})

def _find_deployments_by_app(apps, ns: str, app_label: str):
    # Returns all deployments that have label app=<name>
    resp = apps.list_namespaced_deployment(
        namespace=ns, label_selector=f"app={app_label}"
    )
    return resp.items

def _patch_deploy_labels(apps, ns: str, dep_name: str, role: str):
    patch_body = {
        "metadata": {"labels": {"role": role}},
        "spec": {"template": {"metadata": {"labels": {"role": role}}}},
    }
    return apps.patch_namespaced_deployment(
        name=dep_name, namespace=ns, body=patch_body
    )

def _scale_deploy(apps, ns: str, dep_name: str, replicas: int):
    body = {"spec": {"replicas": replicas}}
    return apps.patch_namespaced_deployment_scale(
        name=dep_name, namespace=ns, body=body
    )

# ---- Helpers previously used but not defined in the original code ----
def get_service_selector(app_label: str, ns: str) -> dict:
    core = get_api_clients()["core"]
    # Attempt 1: Service with the same name as the app
    try:
        svc = core.read_namespaced_service(name=app_label, namespace=ns)
        return (svc.spec.selector or {}) if svc and svc.spec else {}
    except ApiException as e:
        if getattr(e, "status", None) != 404:
            raise
    # Attempt 2: First service with label app=<name>
    svcs = core.list_namespaced_service(namespace=ns, label_selector=f"app={app_label}").items
    if svcs:
        s = svcs[0]
        return (s.spec.selector or {}) if s and s.spec else {}
    return {}

def get_preview_ready(app_label: str, ns: str) -> bool:
    """Considers the preview ready if a Deployment with role=preview exists and is available."""
    apps = get_api_clients()["apps"]
    deps = apps.list_namespaced_deployment(namespace=ns, label_selector=f"app={app_label},role=preview").items
    if not deps:
        return False
    d = deps[0]
    st = d.status or client.V1DeploymentStatus()
    # Simple practical check: at least one available replica
    return (st.available_replicas or 0) > 0


# ----------------------------- Blue/Green ops -----------------------------
def bg_prepare(spec: AppSpec):
    ns = spec.namespace or get_namespace()
    apps = get_api_clients()["apps"]

    app_label = spec.effective_app_label
    preview_name = f"{app_label}-preview"

    labels = {
        "app": app_label,
        "role": "preview",
        "app.kubernetes.io/managed-by": "cloud-devops-platform"
    }

    container = client.V1Container(
        name=app_label,
        image=f"{spec.image}:{spec.tag}",
        ports=[client.V1ContainerPort(container_port=spec.effective_port)],
    )

    pod_template = client.V1PodTemplateSpec(
        metadata=client.V1ObjectMeta(labels=labels),
        spec=client.V1PodSpec(containers=[container])
    )

    dep_spec = client.V1DeploymentSpec(
        replicas=1,
        selector=client.V1LabelSelector(match_labels={"app": app_label}),  
        template=pod_template
    )

    body = client.V1Deployment(
        metadata=client.V1ObjectMeta(name=preview_name, labels=labels),
        spec=dep_spec
    )

    try:
        apps.read_namespaced_deployment(preview_name, ns)
        resp = apps.patch_namespaced_deployment(preview_name, ns, body)
    except ApiException as e:
        if e.status == 404:
            resp = apps.create_namespaced_deployment(ns, body)
        else:
            raise

    return {"ok": True, "preview": resp.to_dict()}


from kubernetes.client.rest import ApiException

def bg_promote(name: str, namespace: str):
    ns = namespace or get_namespace()
    apis = get_api_clients()
    apps = apis["apps"]
    core = apis["core"]

    svc_name = name  # service name = x
    svc = core.read_namespaced_service(svc_name, ns)
    sel = svc.spec.selector or {}

    current_slot = sel.get("slot", "blue")  # الافتراضي blue
    new_slot = "green" if current_slot == "blue" else "blue"

    # تأكد إن deployment الهدف موجود
    target_dep = f"{name}-{new_slot}"
    apps.read_namespaced_deployment(target_dep, ns)

    # 1) بدّل الترافيك: patch service selector
    core.patch_namespaced_service(
        name=svc_name,
        namespace=ns,
        body={"spec": {"selector": {"app": name, "slot": new_slot}}}
    )

    # 2) شغّل الجديد
    apps.patch_namespaced_deployment_scale(
        target_dep, ns, {"spec": {"replicas": 1}}
    )

    # 3) طفّي القديم (يبقى موجود idle)
    old_dep = f"{name}-{current_slot}"
    try:
        apps.patch_namespaced_deployment_scale(
            old_dep, ns, {"spec": {"replicas": 0}}
        )
    except ApiException:
        pass

    return {"ok": True, "active_slot": new_slot}

def bg_rollback(name: str, namespace: str):
    ns = namespace or get_namespace()
    apis = get_api_clients()
    apps = apis["apps"]
    core = apis["core"]

    svc = core.read_namespaced_service(name, ns)
    sel = svc.spec.selector or {}
    current_slot = sel.get("slot", "blue")

    rollback_slot = "blue" if current_slot == "green" else "green"

    rollback_dep = f"{name}-{rollback_slot}"
    apps.read_namespaced_deployment(rollback_dep, ns)

    # رجّع الترافيك
    core.patch_namespaced_service(
        name=name,
        namespace=ns,
        body={"spec": {"selector": {"app": name, "slot": rollback_slot}}}
    )

    # شغّل القديم
    apps.patch_namespaced_deployment_scale(
        rollback_dep, ns, {"spec": {"replicas": 1}}
    )

    # طفّي الحالي
    current_dep = f"{name}-{current_slot}"
    try:
        apps.patch_namespaced_deployment_scale(
            current_dep, ns, {"spec": {"replicas": 0}}
        )
    except ApiException:
        pass

    return {"ok": True, "active_slot": rollback_slot}



def _ensure_k8s_config() -> None:
    """Load in-cluster config if running inside k8s, otherwise fall back to local kubeconfig."""
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


def ensure_tenant_pvc(
    v1: client.CoreV1Api,
    ns: str,
    pvc_name: str = "tenant-storage",
    size: str = "500Mi",
    storage_class: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Create a default PVC for the tenant namespace if it doesn't exist.
    """
    try:
        v1.read_namespaced_persistent_volume_claim(pvc_name, ns)
        return {"pvc": pvc_name, "created": False}
    except ApiException as e:
        if e.status != 404:
            raise

    pvc_spec = client.V1PersistentVolumeClaimSpec(
        access_modes=["ReadWriteOnce"],
        resources=client.V1ResourceRequirements(requests={"storage": size}),
        storage_class_name=storage_class,  # None => use default StorageClass if exists
    )

    pvc = client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(name=pvc_name, namespace=ns),
        spec=pvc_spec,
    )

    v1.create_namespaced_persistent_volume_claim(ns, pvc)
    return {"pvc": pvc_name, "created": True, "size": size, "storageClass": storage_class}


def ensure_storage_quota(
    v1: client.CoreV1Api,
    ns: str,
    quota_name: str = "storage-quota",
    storage_limit: str = "500Mi",
) -> Dict[str, Any]:
    try:
        v1.read_namespaced_resource_quota(quota_name, ns)
        return {"quota": quota_name, "created": False}
    except ApiException as e:
        if e.status != 404:
            raise

    quota = client.V1ResourceQuota(
        metadata=client.V1ObjectMeta(name=quota_name, namespace=ns),
        spec=client.V1ResourceQuotaSpec(
            hard={
                "requests.storage": storage_limit,
                "persistentvolumeclaims": "1",
            }
        ),
    )

    v1.create_namespaced_resource_quota(ns, quota)
    return {"quota": quota_name, "created": True, "limit": storage_limit}


def create_tenant_namespace(
    ns: str,
    storage_size: str = "500Mi",
    storage_class: Optional[str] = None,
) -> dict:
    """
    Create tenant namespace + RBAC + default PVC + storage quota.
    """

    _ensure_k8s_config()

    apis = get_api_clients()
    v1 = apis["core"]   # CoreV1Api
    rbac = apis["rbac"] # RbacAuthorizationV1Api

    created: Dict[str, Any] = {
        "namespace": False,
        "serviceaccount": False,
        "role": False,
        "rolebinding": False,
        "pvc": None,
        "storage_quota": None,
    }

    # 0) Namespace
    try:
        v1.read_namespace(ns)
    except ApiException as e:
        if e.status == 404:
            body = client.V1Namespace(metadata=client.V1ObjectMeta(name=ns))
            v1.create_namespace(body)
            created["namespace"] = True
        elif e.status != 409:
            # 409 = already exists (rare here), anything else should raise
            raise

    # 1) ServiceAccount
    sa_name = "tenant-app-sa"
    try:
        v1.read_namespaced_service_account(sa_name, ns)
    except ApiException as e:
        if e.status == 404:
            sa = client.V1ServiceAccount(metadata=client.V1ObjectMeta(name=sa_name, namespace=ns))
            v1.create_namespaced_service_account(ns, sa)
            created["serviceaccount"] = True
        else:
            raise

    # 2) Role
    role_name = "tenant-app-role"
    try:
        rbac.read_namespaced_role(role_name, ns)
    except ApiException as e:
        if e.status == 404:
            rules = [
                client.V1PolicyRule(
                    api_groups=["apps"],
                    resources=["deployments"],
                    verbs=["get", "list", "watch", "create", "update", "patch", "delete"],
                ),
                client.V1PolicyRule(
                    api_groups=[""],
                    resources=["services"],
                    verbs=["get", "list", "watch", "create", "update", "patch", "delete"],
                ),
                client.V1PolicyRule(
                    api_groups=["networking.k8s.io"],
                    resources=["ingresses"],
                    verbs=["get", "list", "watch", "create", "update", "patch", "delete"],
                ),
            ]
            role = client.V1Role(
                metadata=client.V1ObjectMeta(name=role_name, namespace=ns),
                rules=rules,
            )
            rbac.create_namespaced_role(ns, role)
            created["role"] = True
        else:
            raise

    # 3) RoleBinding
    rb_name = "tenant-app-binding"
    try:
        rbac.read_namespaced_role_binding(rb_name, ns)
    except ApiException as e:
        if e.status == 404:
            rb = client.V1RoleBinding(
                metadata=client.V1ObjectMeta(name=rb_name, namespace=ns),
                subjects=[client.RbacV1Subject(kind="ServiceAccount", name=sa_name, namespace=ns)],
                role_ref=client.V1RoleRef(
                    api_group="rbac.authorization.k8s.io",
                    kind="Role",
                    name=role_name,
                ),
            )
            rbac.create_namespaced_role_binding(ns, rb)
            created["rolebinding"] = True
        else:
            raise

    # 4) PVC + Storage Quota (هذا كان ناقص عندك)
    created["pvc"] = ensure_tenant_pvc(
        v1=v1,
        ns=ns,
        pvc_name="tenant-storage",
        size=storage_size,
        storage_class=storage_class,
    )
    created["storage_quota"] = ensure_storage_quota(
        v1=v1,
        ns=ns,
        quota_name="storage-quota",
        storage_limit=storage_size,
    )

    return {"ok": True, "namespace": ns, "created": created}

def delete_app(namespace: str, name: str):
    apps = client.AppsV1Api()
    core = client.CoreV1Api()
    net = client.NetworkingV1Api()
    cert = client.CustomObjectsApi()   # ← مهم جداً

    # Delete Deployment
    try:
        apps.delete_namespaced_deployment(name, namespace)
    except:
        pass

    # Delete Service
    try:
        core.delete_namespaced_service(name, namespace)
    except:
        pass

    # Delete Ingress
    try:
        net.delete_namespaced_ingress(f"{name}-ingress", namespace)
    except:
        pass

    # ==============================
    # DELETE Certificate CRD
    # ==============================
    try:
        cert.delete_namespaced_custom_object(
            group="cert-manager.io",
            version="v1",
            namespace=namespace,
            plural="certificates",
            name=f"{name}-tls"
        )
    except:
        pass

    # DELETE TLS Secret
    try:
        core.delete_namespaced_secret(f"{name}-tls", namespace)
    except:
        pass

    # Delete Blue/Green resources
    try:
        apps.delete_namespaced_deployment(f"{name}-preview", namespace)
    except:
        pass

    try:
        core.delete_namespaced_service(f"{name}-preview", namespace)
    except:
        pass

    return {"ok": True, "deleted": name}



def get_app_host(namespace: str, app_name: str) -> str | None:
    net = client.NetworkingV1Api()
    ing_name = f"{app_name}-ingress"
    try:
        ing = net.read_namespaced_ingress(name=ing_name, namespace=namespace)
        rules = ing.spec.rules or []
        if rules and rules[0].host:
            return rules[0].host
    except Exception:
        return None
    return None
