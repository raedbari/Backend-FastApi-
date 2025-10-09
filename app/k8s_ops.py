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
from typing import Dict, List  # Optional محذوف لأنه غير مستخدم في هذا الجزء

from kubernetes import client

# توحيد استيراد ApiException بما يدعم kubernetes >= 28 و < 28
try:
    # kubernetes >= 28
    from kubernetes.client.exceptions import ApiException  # type: ignore
except Exception:  # pragma: no cover
    # kubernetes < 28
    from kubernetes.client.rest import ApiException  # type: ignore

from .k8s_client import get_api_clients, get_namespace, platform_labels
# استيراد Status* محذوف لأنه غير مستخدم في هذا الجزء
# from .models import AppSpec, StatusItem, StatusResponse
from .models import AppSpec, StatusItem, StatusResponse

# ملاحظة: تمت إزالة الكتلة الكبيرة _container_from_spec لأنها كانت مُعلقة بالكامل (commented-out)
# وغير مستخدمة فعليًا. إذا احتجناها مستقبلًا سنعيد تقديم نسخة مبسطة ومستخدمة.

def upsert_deployment(spec: AppSpec) -> dict:
    ns   = spec.namespace or get_namespace()
    apps = get_api_clients()["apps"]

    name   = spec.effective_app_label
    port   = spec.effective_port
    path   = spec.effective_health_path
    labels = platform_labels({"app": name, "role": "active"})  # توحيد اللّيبلز

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
    ns   = spec.namespace or get_namespace()
    core = get_api_clients()["core"]

    # عرّف القيم أولاً
    app_label = spec.effective_app_label
    svc_name  = spec.effective_service_name
    port      = spec.effective_port

    # الخدمة دائمًا توجه الـ active
    labels   = platform_labels({"app": app_label, "role": "active"})
    selector = {"app": app_label, "role": "active"}

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
                selector=selector,   # ملاحظة: لا تستخدم V1LabelSelector هنا
                type=svc_type,
                ports=[client.V1ServicePort(
                    name="http",
                    port=cluster_port,
                    target_port=port,
                    protocol="TCP",
                    node_port=node_port
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
                    selector=selector,
                    ports=[client.V1ServicePort(name="http", port=port, target_port=port, protocol="TCP")]
                )
            )
            resp = core.create_namespaced_service(namespace=ns, body=create_body)
        else:
            raise

    return resp.to_dict()

# ---- Status / Scale / Blue-Green (Part 2/3) ----

def list_status(name: Optional[str] = None, namespace: Optional[str] = None) -> StatusResponse:
    """Status for one/all managed Deployments in the resolved namespace."""
    ns = namespace or get_namespace()
    apps = get_api_clients()["apps"]

    # احصل على قائمة الـ Deployments
    if name:
        try:
            d = apps.read_namespaced_deployment(name=name, namespace=ns)
            deployments = [d]
        except ApiException:
            # لا نكسر الـAPI: نرجّع قائمة فارغة
            return StatusResponse(items=[])
    else:
        deployments = apps.list_namespaced_deployment(
            namespace=ns,
            label_selector="managed-by=cloud-devops-platform",
        ).items

    items: List[StatusItem] = []

    for d in deployments:
        spec = d.spec or client.V1DeploymentSpec()
        status = d.status or client.V1DeploymentStatus()

        # الصورة
        image = ""
        try:
            containers = (spec.template.spec.containers or [])
            if containers:
                image = containers[0].image or ""
        except Exception:
            pass

        # الشروط
        conds = {c.type: c.status for c in (status.conditions or [])}

        # اسم/ليبل التطبيق
        d_name = d.metadata.name
        d_labels = d.metadata.labels or {}
        app_label = d_labels.get("app", d_name)

        # الـ Service selector (اختياري)
        try:
            svc_sel = get_service_selector(app_label, ns)
        except Exception:
            svc_sel = {}

        # حالة الـ preview (اختياري)
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
    """Patch the Scale subresource of a Deployment in the resolved namespace."""
    ns = namespace or get_namespace()
    apps = get_api_clients()["apps"]
    body = {"spec": {"replicas": replicas}}
    resp = apps.patch_namespaced_deployment_scale(name=name, namespace=ns, body=body)
    return resp.to_dict()


# ----------------------------- Blue/Green helpers -----------------------------

def _labels_for(app_label: str, role: str) -> dict:
    return platform_labels({"app": app_label, "role": role})

def _find_deployments_by_app(apps, ns: str, app_label: str):
    # يرجع كل الدبلويمِنتات التي تحمل label app=<name>
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

# ---- Helpers كانت مُستخدمة وغير معرّفة في الكود الأصلي ----
def get_service_selector(app_label: str, ns: str) -> dict:
    """يحاول قراءة Service باسم التطبيق، وإلا يبحث عن أول Service تابعة له."""
    core = get_api_clients()["core"]
    # المحاولة 1: خدمة بنفس اسم التطبيق
    try:
        svc = core.read_namespaced_service(name=app_label, namespace=ns)
        return (svc.spec.selector or {}) if svc and svc.spec else {}
    except ApiException as e:
        if getattr(e, "status", None) != 404:
            raise
    # المحاولة 2: أول خدمة تحمل label app=<name>
    svcs = core.list_namespaced_service(namespace=ns, label_selector=f"app={app_label}").items
    if svcs:
        s = svcs[0]
        return (s.spec.selector or {}) if s and s.spec else {}
    return {}

def get_preview_ready(app_label: str, ns: str) -> bool:
    """يعتبر الـpreview جاهزًا إذا وجدنا Deployment role=preview وبحالة متاحة."""
    apps = get_api_clients()["apps"]
    deps = apps.list_namespaced_deployment(namespace=ns, label_selector=f"app={app_label},role=preview").items
    if not deps:
        return False
    d = deps[0]
    st = d.status or client.V1DeploymentStatus()
    # معيار بسيط وعملي: وجود متاحين على الأقل
    return (st.available_replicas or 0) > 0


# ----------------------------- Blue/Green ops -----------------------------

def bg_prepare(spec: AppSpec) -> dict:
    """
    ينشئ/يحدّث Deployment موازي باسم <name>-preview بعلامة role=preview
    ولا يلمس الـService (ما زالت تشير إلى role=active).
    """
    ns   = spec.namespace or get_namespace()
    apps = get_api_clients()["apps"]

    app_label = spec.effective_app_label
    preview_name = f"{app_label}-preview"

    # بناء الحاوية والمواصفات كما في upsert_deployment لكن role=preview
    name   = preview_name
    port   = spec.effective_port
    path   = spec.effective_health_path
    labels = _labels_for(app_label, "preview")

    # أمن الموارد/الأمان كما في upsert_deployment
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
        name=app_label,
        image=(f"{spec.image}:{spec.tag}" if getattr(spec, "tag", None) else spec.image),
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
        selector=client.V1LabelSelector(match_labels={"app": app_label}),  # لا نثبت role هنا
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

    return {"preview": resp.to_dict()}


def bg_promote(name: str, namespace: str) -> dict:
    """
    يجعل الـpreview هو active:
    - role=preview  -> role=active
    - role=active   -> role=idle
    Service selector ثابت على role=active → التحويل فوري.
    """
    ns = namespace or get_namespace()
    apps = get_api_clients()["apps"]

    # ابحث عن كل Deployments الخاصة بالتطبيق
    deps = _find_deployments_by_app(apps, ns, name)
    preview = None
    active  = None

    for d in deps:
        role = (d.metadata.labels or {}).get("role", "")
        if role == "preview":
            preview = d
        elif role == "active":
            active = d

    if not preview:
        raise ApiException(status=404, reason="No preview deployment found")

    # روّج الـpreview ليصبح active
    _patch_deploy_labels(apps, ns, preview.metadata.name, "active")

    # اجعل الـactive الحالي idle (إن وجد)
    if active:
        _patch_deploy_labels(apps, ns, active.metadata.name, "idle")

    return {"ok": True, "promoted": preview.metadata.name, "demoted": getattr(active, "metadata", {}).get("name")}


def bg_rollback(name: str, namespace: str) -> dict:
    """
    يعيد الـactive السابق ليكون active ويجعل الحالي preview/idle حسب الحاجة.
    استراتيجية بسيطة:
      - إن وُجد active و preview: بدّل الأدوار (active↔preview).
      - إن وُجد active فقط: لا شيء يُفعل.
      - إن وُجد preview فقط: اجعله idle (لا ترجع للخلف لعدم وجود مرجع).
    """
    ns = namespace or get_namespace()
    apps = get_api_clients()["apps"]

    deps = _find_deployments_by_app(apps, ns, name)
    preview = None
    active  = None
    idle    = []

    for d in deps:
        role = (d.metadata.labels or {}).get("role", "")
        if role == "preview":
            preview = d
        elif role == "active":
            active = d
        elif role == "idle":
            idle.append(d)

    if active and preview:
        _patch_deploy_labels(apps, ns, active.metadata.name, "preview")
        _patch_deploy_labels(apps, ns, preview.metadata.name, "active")
        return {"ok": True, "swapped": [active.metadata.name, preview.metadata.name]}

    if not active and preview:
        # لا يوجد active؛ preview يصبح active
        _patch_deploy_labels(apps, ns, preview.metadata.name, "active")
        return {"ok": True, "promoted_from_preview": preview.metadata.name}

    # لا إجراء واضح
    return {"ok": True, "note": "No rollback action performed"}









# --- Blue/Green (Part 3/3) — cleaned ---
# نعتمد تصميم التحويل عبر تبديل Labels فقط:
# - preview -> active
# - active  -> idle
# مع بقاء Service selector ثابتًا على role=active لضمان تحويل فوري وآمن.
# لذلك لا نحتاج دوال التلاعب بالـService selector ولا ترقيع template.labels هنا.
# (التعريفات الموثوقة لـ bg_prepare/bg_promote/bg_rollback موجودة أعلاه في Part 2/3)

# ----------------------------- Tenant Provisioning -----------------------------
# Creates/ensures Namespace + ServiceAccount + Role + RoleBinding for a tenant.
# Idempotent: safe to call multiple times.

from kubernetes import config
try:
    # على بعض الإصدارات قد يختلف مسار ApiException، لذا نُحافظ على الاستيرادين
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
    """
    Creates/ensures:
      - Namespace <ns>
      - ServiceAccount tenant-app-sa
      - Role tenant-app-role  (CRUD on Deployments/Services/Ingress in ns)
      - RoleBinding tenant-app-rb  (bind SA->Role)
    Returns summary dict of created/existing resources.
    """
    _ensure_k8s_config()
    v1   = client.CoreV1Api()
    rbac = client.RbacAuthorizationV1Api()

    summary = {"namespace": ns, "created": [], "existing": []}

    # 1) Namespace
    try:
        v1.read_namespace(ns)
        summary["existing"].append("Namespace")
    except (ApiException, K8sApiException) as e:
        if getattr(e, "status", None) == 404:
            body = client.V1Namespace(metadata=client.V1ObjectMeta(name=ns))
            v1.create_namespace(body)
            summary["created"].append("Namespace")
        else:
            raise

    # 2) ServiceAccount
    sa_name = "tenant-app-sa"
    try:
        v1.read_namespaced_service_account(sa_name, ns)
        summary["existing"].append("ServiceAccount")
    except (ApiException, K8sApiException) as e:
        if getattr(e, "status", None) == 404:
            sa = client.V1ServiceAccount(metadata=client.V1ObjectMeta(name=sa_name, namespace=ns))
            v1.create_namespaced_service_account(ns, sa)
            summary["created"].append("ServiceAccount")
        else:
            raise

    # 3) Role
    role_name = "tenant-app-role"
    rules = [
        client.V1PolicyRule(
            api_groups=["apps"],
            resources=["deployments"],
            verbs=["get","list","watch","create","update","patch","delete"],
        ),
        client.V1PolicyRule(
            api_groups=[""],
            resources=["services"],
            verbs=["get","list","watch","create","update","patch","delete"],
        ),
        client.V1PolicyRule(
            api_groups=["networking.k8s.io"],
            resources=["ingresses"],
            verbs=["get","list","watch","create","update","patch","delete"],
        ),
    ]
    try:
        rbac.read_namespaced_role(role_name, ns)
        summary["existing"].append("Role")
    except (ApiException, K8sApiException) as e:
        if getattr(e, "status", None) == 404:
            role = client.V1Role(metadata=client.V1ObjectMeta(name=role_name, namespace=ns), rules=rules)
            rbac.create_namespaced_role(ns, role)
            summary["created"].append("Role")
        else:
            raise

    # 4) RoleBinding
    rb_name = "tenant-app-rb"
    rb = client.V1RoleBinding(
        metadata=client.V1ObjectMeta(name=rb_name, namespace=ns),
        role_ref=client.V1RoleRef(api_group="rbac.authorization.k8s.io", kind="Role", name=role_name),
        subjects=[client.V1Subject(kind="ServiceAccount", name=sa_name, namespace=ns)],
    )
    try:
        rbac.read_namespaced_role_binding(rb_name, ns)
        summary["existing"].append("RoleBinding")
    except (ApiException, K8sApiException) as e:
        if getattr(e, "status", None) == 404:
            rbac.create_namespaced_role_binding(ns, rb)
            summary["created"].append("RoleBinding")
        else:
            raise

    # (اختياري لاحقًا) NetworkPolicy / ResourceQuota / LimitRange

    return summary
