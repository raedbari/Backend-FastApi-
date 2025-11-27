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
from typing import Dict, List

from kubernetes import client
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
    ns   = spec.namespace or get_namespace()
    apps = get_api_clients()["apps"]

    name   = spec.effective_app_label
    port   = spec.effective_port
    path   = spec.effective_health_path
    labels = platform_labels({"app": name, "role": "active"})

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

# ============================================================
# 🌐 Automatically create Ingress (supports TLS + port discovery + privacy protection)
# ============================================================

from kubernetes import client
from kubernetes.client.rest import ApiException
from .k8s_client import get_api_clients
from .auth import get_current_context


def create_ingress_for_app(app_name: str, namespace: str , ctx=None):
    
    clients = get_api_clients()
    net_api = clients["networking"]
    core_api = clients["core"]

    if ctx is None:
       ctx = get_current_context()
    role = getattr(ctx, "role", "")

    # 🚫 Prevent platform_admin from creating any resource inside customer namespaces
    if role == "platform_admin" and namespace != "default":
        print(f"🚫 platform_admin is not allowed to create Ingress inside customer namespaces ({namespace})")
        return

    #  Prevent any other user from deploying inside default namespace
    if role != "platform_admin" and namespace == "default":
        print(f"🚫 User '{role}' is not allowed to deploy inside 'default' namespace")
        return

    host = f"{app_name}.{namespace}.apps.smartdevops.lat"
    ingress_name = f"{app_name}-ingress"
    tls_secret = f"{app_name}-tls"

    #  Detect port from Service
    try:
        svc = core_api.read_namespaced_service(app_name, namespace)
        port_number = svc.spec.ports[0].port if svc.spec.ports else 8080
    except ApiException:
        print(f"⚠️ Service {app_name} not found in {namespace}, using default port 8080.")
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
                                        name=app_name,
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
        existing = net_api.read_namespaced_ingress(ingress_name, namespace)
        net_api.delete_namespaced_ingress(ingress_name, namespace)
        print(f"♻️ Old Ingress {ingress_name} deleted in {namespace} — it will be recreated.")
    except ApiException as e:
        if getattr(e, "status", None) != 404:
            print(f"⚠️ Failed to check existing Ingress: {e}")

    try:
        net_api.create_namespaced_ingress(namespace=namespace, body=ingress_manifest)
        print(f"✅ Ingress {ingress_name} created successfully in {namespace}")
        print(f"🌍 URL: https://{host}")
    except ApiException as e:
        print(f"❌ Failed to create Ingress: {e}")
        raise



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
    selector = {"app": app_label, "role": "active"}

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
        selector=client.V1LabelSelector(match_labels={"app": app_label}),  # ثابت ومهم
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

def bg_promote(name: str, namespace: str):
    ns = namespace or get_namespace()
    apps = get_api_clients()["apps"]

    deps = _find_deployments_by_app(apps, ns, name)
    preview = None
    active = None

    for d in deps:
        role = d.metadata.labels.get("role", "")
        if role == "preview":
            preview = d
        elif role == "active":
            active = d

    if not preview:
        raise ApiException(status=404, reason="No preview deployment found")

    # 1) Preview becomes active + scale to 1
    _patch_deploy_labels(apps, ns, preview.metadata.name, "active")
    apps.patch_namespaced_deployment_scale(
        preview.metadata.name, ns, {"spec": {"replicas": 1}}
    )

    # 2) Old active becomes idle + scale to 0

    if active:
        _patch_deploy_labels(apps, ns, active.metadata.name, "idle")
        apps.patch_namespaced_deployment_scale(
            active.metadata.name, ns, {"spec": {"replicas": 0}}
        )

    return {"ok": True}

def bg_rollback(name: str, namespace: str):
    ns = namespace or get_namespace()
    apps = get_api_clients()["apps"]

    deps = _find_deployments_by_app(apps, ns, name)
    active = None
    idle = None

    for d in deps:
        role = d.metadata.labels.get("role", "")
        if role == "active":
            active = d
        elif role == "idle":
            idle = d

    if not idle:
        return {"note": "No idle version to rollback to"}

    # 1) idle → active (turn on)
    _patch_deploy_labels(apps, ns, idle.metadata.name, "active")
    apps.patch_namespaced_deployment_scale(
        idle.metadata.name, ns, {"spec": {"replicas": 1}}
    )

    # 2) active → idle (turn off)
    if active:
        _patch_deploy_labels(apps, ns, active.metadata.name, "idle")
        apps.patch_namespaced_deployment_scale(
            active.metadata.name, ns, {"spec": {"replicas": 0}}
        )

    return {"ok": True}

from kubernetes import config
try:
    # In some versions, ApiException import path differs, so we keep both imports
    from kubernetes.client.exceptions import ApiException as K8sApiException  # k8s >= 28
except Exception:  # pragma: no cover
    from kubernetes.client.rest import ApiException as K8sApiException        # k8s < 28

def _ensure_k8s_config():
    """Load in-cluster config if running inside k8s, otherwise fall back to local kubeconfig."""
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

def create_tenant_namespace(ns: str) -> dict:
   
    apis = get_api_clients()
    v1   = apis["core"]     # CoreV1Api
    rbac = apis["rbac"]     # RbacAuthorizationV1Api

    created = {"namespace": False, "serviceaccount": False, "role": False, "rolebinding": False}

    # 0) Try to read the Namespace; if 404 then try to create it (may fail with 403 if no cluster-level permission)
    try:
        v1.read_namespace(ns)
    except ApiException as e:
        if getattr(e, "status", None) == 404:
            try:
                body = client.V1Namespace(metadata=client.V1ObjectMeta(name=ns))
                v1.create_namespace(body)
                created["namespace"] = True
            except ApiException as e2:
                # No cluster permission? Don’t break execution — continue with RBAC setup assuming namespace exists
                if getattr(e2, "status", None) != 409:  # 409 = already exists
                    pass
        elif getattr(e, "status", None) != 200:
            pass

    # 1) ServiceAccount
    sa_name = "tenant-app-sa"
    try:
        v1.read_namespaced_service_account(sa_name, ns)
    except ApiException as e:
        if getattr(e, "status", None) == 404:
            sa = client.V1ServiceAccount(
                metadata=client.V1ObjectMeta(name=sa_name, namespace=ns)
            )
            v1.create_namespaced_service_account(ns, sa)
            created["serviceaccount"] = True
        else:
            raise

    # 2) Role (permissions limited to this namespace)
    role_name = "tenant-app-role"
    try:
        rbac.read_namespaced_role(role_name, ns)
    except ApiException as e:
        if getattr(e, "status", None) == 404:
            rules = [
                # Deployments under apps
                client.V1PolicyRule(
                    api_groups=["apps"],
                    resources=["deployments"],
                    verbs=["get", "list", "watch", "create", "update", "patch", "delete"],
                ),
                # Services under core
                client.V1PolicyRule(
                    api_groups=[""],
                    resources=["services"],
                    verbs=["get", "list", "watch", "create", "update", "patch", "delete"],
                ),
                # Ingresses under networking.k8s.io
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

    # 3) RoleBinding (use RbacV1Subject instead of V1Subject)
    rb_name = "tenant-app-binding"
    try:
        rbac.read_namespaced_role_binding(rb_name, ns)
    except ApiException as e:
        if getattr(e, "status", None) == 404:
            rb = client.V1RoleBinding(
                metadata=client.V1ObjectMeta(name=rb_name, namespace=ns),
                subjects=[
                    client.RbacV1Subject(  # <-- This is the correct type
                        kind="ServiceAccount", name=sa_name, namespace=ns
                    )
                ],
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

    return {"ok": True, "namespace": ns, "created": created}


def delete_app(namespace: str, name: str):
    apps = client.AppsV1Api()
    core = client.CoreV1Api()
    net = client.NetworkingV1Api()

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
        net.delete_namespaced_ingress(name, namespace)
    except:
        pass

    # Delete preview (blue/green)
    try:
        apps.delete_namespaced_deployment(name + "-preview", namespace)
    except:
        pass
    try:
        core.delete_namespaced_service(name + "-preview", namespace)
    except:
        pass

    return {"ok": True, "deleted": name}
