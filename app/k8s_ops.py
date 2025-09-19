# app/k8s_ops.py d
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
   #labels = platform_labels({"app": name})
    labels = platform_labels({"app": name, "role": "active"})


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
                selector=selector,   # ← لا تستخدم V1LabelSelector هنا
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
            image = (spec.template.spec.containers or [])[0].image  
        except Exception:
            pass

        conds = {c.type: c.status for c in (status.conditions or [])}

        d_name = d.metadata.name
        ns = namespace or get_namespace()
        try:
            svc_sel = get_service_selector(app_label, ns) 
        except Exception:
            svc_sel = {}

        app_label = (d.metadata.labels or {}).get("app", d_name)
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



def bg_prepare(spec: AppSpec) -> dict:
    """
    ينشئ/يحدّث Deployment جديد بعلامة role=preview
    ويترك الـService يشير لـ role=active.
    الاسم يكون name-<color> (blue/green) بالتناوب.
    """


def bg_promote(name: str, namespace: str) -> dict:
    """
    يجعل الـpreview هو active بعكس labels:
    - preview -> role=active
    - active -> role=idle (أو preview سابقاً)
    الService لا يتغير selector تبعه (role=active)،
    لذا التحويل فوري.
    """


def bg_rollback(name: str, namespace: str) -> dict:
    """
    يعيد الـlabels بحيث يعود الـactive السابق هو active
    ويحذف/يعطل الـpreview الحالي (اختياري: scale=0 أو delete).
    """
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
        "spec": {
            "template": {"metadata": {"labels": {"role": role}}}
        },
    }
    return apps.patch_namespaced_deployment(
        name=dep_name, namespace=ns, body=patch_body
    )

def _scale_deploy(apps, ns: str, dep_name: str, replicas: int):
    body = {"spec": {"replicas": replicas}}
    return apps.patch_namespaced_deployment_scale(
        name=dep_name, namespace=ns, body=body
    )

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
       #selector=client.V1LabelSelector(match_labels={"app": app_label, "role": "preview"}),
        selector=client.V1LabelSelector(match_labels={"app": app_label}),
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
    تحويل المرور إلى نسخة preview دون لمس Labels الدبلويمِنتات:
    - تأكيد وجود deploy/<name> و deploy/<name>-preview و svc/<name>.
    - Patch لselector الخدمة ليصبح {app: <name>, role: preview}.
    - Scale بالاسم: القديم -> 0 ، المعاينة -> (>=1).
    """
    ns     = namespace or get_namespace()
    apis   = get_api_clients()
    apps   = apis["apps"]
    core   = apis["core"]
    active = name
    preview= f"{name}-preview"
    svc_nm = name

    # تحقق من وجود الموارد
    try:
        dep_active  = apps.read_namespaced_deployment(name=active,  namespace=ns)
    except ApiException as e:
        if getattr(e, "status", None) == 404:
            raise RuntimeError(f"Active deployment '{active}' not found in '{ns}'.")
        raise
    try:
        dep_preview = apps.read_namespaced_deployment(name=preview, namespace=ns)
    except ApiException as e:
        if getattr(e, "status", None) == 404:
            raise RuntimeError(f"Preview deployment '{preview}' not found in '{ns}'.")
        raise
    try:
        svc = _read_service(core, ns, svc_nm)
    except ApiException as e:
        if getattr(e, "status", None) == 404:
            raise RuntimeError(f"Service '{svc_nm}' not found in '{ns}'.")
        raise

    # بدّل Selector الخدمة إلى preview (Idempotent)
    desired_selector = {"app": name, "role": "preview"}
    if _current_svc_selector(svc) != desired_selector:
        _patch_service_selector(core, ns, svc_nm, desired_selector)

    # Scale بالاسم (Idempotent)
    # القديم -> 0
    _scale_deploy(apps, ns, active, 0)
    # المعاينة -> لا تقل عن 1
    preview_replicas = max((dep_preview.spec.replicas or 1), 1)
    _scale_deploy(apps, ns, preview, preview_replicas)

    return {
        "status": "promoted",
        "service_selector": desired_selector,
        "scaled": {active: 0, preview: preview_replicas},
    }


def bg_rollback(name: str, namespace: str) -> dict:
    """
    إعادة المرور إلى النسخة النشطة (active):
    - Patch لselector الخدمة ليصبح {app: <name>, role: active}.
    - Scale بالاسم: active -> (>=1) ، preview -> 0.
    """
    ns     = namespace or get_namespace()
    apis   = get_api_clients()
    apps   = apis["apps"]
    core   = apis["core"]
    active = name
    preview= f"{name}-preview"
    svc_nm = name

    # اختياريًا نتحقق من وجود الـDeployments (لرسائل أوضح)
    try:
        dep_active  = apps.read_namespaced_deployment(name=active,  namespace=ns)
    except ApiException as e:
        if getattr(e, "status", None) == 404:
            raise RuntimeError(f"Active deployment '{active}' not found in '{ns}'.")
        raise
    try:
        dep_preview = apps.read_namespaced_deployment(name=preview, namespace=ns)
    except ApiException as e:
        if getattr(e, "status", None) == 404:
            # في بعض الحالات قد لا يوجد preview (لا بأس)، نرجّع فقط تعديل الخدمة وتسكيل active
            dep_preview = None
        else:
            raise

    # بدّل Selector الخدمة إلى active (Idempotent)
    desired_selector = {"app": name, "role": "active"}
    try:
        svc = _read_service(core, ns, svc_nm)
    except ApiException as e:
        if getattr(e, "status", None) == 404:
            raise RuntimeError(f"Service '{svc_nm}' not found in '{ns}'.")
        raise
    if _current_svc_selector(svc) != desired_selector:
        _patch_service_selector(core, ns, svc_nm, desired_selector)

    # Scale بالاسم
    active_replicas = max((dep_active.spec.replicas or 1), 1)
    _scale_deploy(apps, ns, active, active_replicas)
    if dep_preview is not None:
        _scale_deploy(apps, ns, preview, 0)

    return {
        "status": "rolled-back",
        "service_selector": desired_selector,
        "scaled": {active: active_replicas, preview: 0 if dep_preview else "n/a"},
    }

def _read_service(core, ns: str, svc_name: str):
    return core.read_namespaced_service(name=svc_name, namespace=ns)

def _current_svc_selector(svc) -> dict:
    return (svc.spec.selector or {}) if getattr(svc.spec, "selector", None) else {}

def _patch_service_selector(core, ns: str, svc_name: str, selector: dict):
    return core.patch_namespaced_service(name=svc_name, namespace=ns, body={"spec": {"selector": selector}})

def is_deploy_ready(apps, ns: str, name: str, min_available: int = 1) -> bool:
    try:
        d = apps.read_namespaced_deployment(name=name, namespace=ns)
    except ApiException as e:
        if getattr(e, "status", None) == 404:
            return False
        raise
    st = d.status or client.V1DeploymentStatus()
    return (st.available_replicas or 0) >= min_available

def get_service_selector(name: str, ns: str) -> dict:
    core = get_api_clients()["core"]
    try:
        svc = core.read_namespaced_service(name=name, namespace=ns)
        return svc.spec.selector or {}
    except ApiException as e:
        if getattr(e, "status", None) == 404:
            return {}
        raise

def get_preview_ready(app_label: str, ns: str) -> bool:
    apps = get_api_clients()["apps"]
    preview_name = f"{app_label}-preview"
    return is_deploy_ready(apps, ns, preview_name, 1)
