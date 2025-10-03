# app/grafana_client.py
import os
import httpx
from typing import Optional, Dict, Any

GRAFANA_URL = os.getenv("GRAFANA_URL", "https://rango-project.duckdns.org/grafana").rstrip("/")
GRAFANA_TOKEN = os.getenv("GRAFANA_TOKEN")  # قد يكون None لو لم نُفعل الProvisioning

_HEADERS = {"Content-Type": "application/json"}
if GRAFANA_TOKEN:
    _HEADERS["Authorization"] = f"Bearer {GRAFANA_TOKEN}"

# UID الافتراضي للدashboard الذي ستفتحه من الواجهة
DEFAULT_DASH_UID = os.getenv("GRAFANA_DASH_UID", "app-observability")

def build_dashboard_url(namespace: str, app: str,
                        uid: str = DEFAULT_DASH_UID,
                        time_from: str = "now-6h", time_to: str = "now") -> str:
    """
    يبني رابط فتح الداشبورد مع تمرير المتغيرات:
    var_namespace, var_app, والفترة الزمنية.
    """
    return (
        f"{GRAFANA_URL}/d/{uid}/app-observability"
        f"?var_namespace={namespace}&var_app={app}&from={time_from}&to={time_to}"
    )

# ------- (اختياري) دوال Provisioning عبر API -------
# تستخدم التوكن لو أردت عمل Dashboard/Folder أو تحديثهما تلقائياً.

def grafana_get(path: str) -> httpx.Response:
    if not GRAFANA_TOKEN:
        raise RuntimeError("GRAFANA_TOKEN غير مضبوط، لا يمكن استدعاء Grafana API.")
    with httpx.Client(timeout=15) as s:
        return s.get(f"{GRAFANA_URL}{path}", headers=_HEADERS)

def grafana_post(path: str, json: Dict[str, Any]) -> httpx.Response:
    if not GRAFANA_TOKEN:
        raise RuntimeError("GRAFANA_TOKEN غير مضبوط، لا يمكن استدعاء Grafana API.")
    with httpx.Client(timeout=30) as s:
        return s.post(f"{GRAFANA_URL}{path}", json=json, headers=_HEADERS)

def ensure_folder(folder_title: str = "Apps", uid: str = "apps-folder") -> Dict[str, Any]:
    """
    ينشئ Folder إن لم يكن موجوداً (Idempotent).
    """
    # جرّب تجيبه أولاً
    r = grafana_get(f"/api/folders/{uid}")
    if r.status_code == 200:
        return r.json()

    # غير موجود => أنشئه
    payload = {"uid": uid, "title": folder_title}
    r = grafana_post("/api/folders", payload)
    r.raise_for_status()
    return r.json()

def upsert_dashboard(dashboard: Dict[str, Any], folder_id: Optional[int] = None) -> Dict[str, Any]:
    """
    ينشئ/يحدّث داشبورد (PUT /api/dashboards/db).
    يجب أن يحتوي dashboard["dashboard"] على الحقول القياسية (uid, title, panels, templating...).
    """
    payload = {
        "dashboard": dashboard,
        "overwrite": True
    }
    if folder_id is not None:
        payload["folderId"] = folder_id

    r = grafana_post("/api/dashboards/db", payload)
    r.raise_for_status()
    return r.json()
