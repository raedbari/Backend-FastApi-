# app/monitor.py
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
import os, re, time
import httpx
from typing import List, Optional , Dict, Any

from kubernetes import client, config

PROM_URL = os.environ["PROM_URL"].rstrip("/")
LOKI_URL = os.environ["LOKI_URL"].rstrip("/")
ALLOWED_NS = set([s.strip() for s in os.getenv("ALLOWED_NAMESPACES","").split(",") if s.strip()])

router = APIRouter(prefix="/monitor", tags=["monitor"])

def _load_k8s():
    """Load in-cluster config (or kubeconfig if running locally)."""
    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()
    return client.AppsV1Api(), client.CoreV1Api()


# ---- Utils ----
def ns_guard(ns: str):
    if ALLOWED_NS and ns not in ALLOWED_NS:
        raise HTTPException(status_code=403, detail="namespace not allowed")

_prom = httpx.AsyncClient(base_url=PROM_URL, timeout=10)
_loki = httpx.AsyncClient(base_url=LOKI_URL, timeout=30)

def promq(expr: str, rng: str = "5m"):
    # range query window used by /query_range endpoints
    now = int(time.time())
    start = now - 60*5 if rng.endswith("m") else now - 900
    step = "15s"
    return {"query": expr, "start": start, "end": now, "step": step}

def _ns_to_iso(nanos: str) -> str:
    """Convert Loki nanoseconds string to ISO8601."""
    # Loki returns a string like "1759129248269352048"
    try:
        ts = int(nanos)
        return datetime.fromtimestamp(ts / 1_000_000_000, tz=timezone.utc).isoformat()
    except Exception:
        return ""


def _safe_regex(s: str) -> str:
    """Escape user strings for regex usage in Loki filters."""
    return re.escape(s)


def _loki_query_range(query: str, start_ns: int, end_ns: int, limit: int = 200, direction: str = "backward") -> Dict[str, Any]:
    url = f"{LOKI_URL}/loki/api/v1/query_range"
    params = {
        "query": query,
        "start": str(start_ns),
        "end": str(end_ns),
        "limit": str(limit),
        "direction": direction,
    }
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()  # surface 4xx/5xx to caller
    return r.json()

# ---- Schemas ----
class AppItem(BaseModel):
    namespace: str
    app: str
    image: str
    tag: str
    replicas_desired: int
    replicas_available: int

class PodItem(BaseModel):
    name: str
    phase: str
    ready: bool
    age_seconds: int
    image: str

class Overview(BaseModel):
    namespace: str
    app: str
    replicas: dict
    cpu_mcores: List[dict]
    mem_bytes: List[dict]
    http: Optional[dict] = None

# ---- K8s init ----
try:
    # in-cluster first, fallback to kubeconfig for local dev
    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()
    k8s = client.CoreV1Api()
    apps = client.AppsV1Api()
    events = client.EventsV1Api()
except Exception as e:
    k8s = apps = events = None

# ---- Endpoints ----

@router.get("/apps", response_model=List[AppItem])
async def list_apps():
    if not apps: raise HTTPException(500, "k8s client not initialized")
    out: List[AppItem] = []
    dps = apps.list_deployment_for_all_namespaces()
    for d in dps.items:
        ns = d.metadata.namespace
        labels = d.metadata.labels or {}
        app = labels.get("app") or d.metadata.name
        if ALLOWED_NS and ns not in ALLOWED_NS:
            continue
        img = ""
        tag = ""
        try:
            c = d.spec.template.spec.containers[0]
            img = c.image
            if ":" in img:
                tag = img.split(":")[-1]
        except Exception:
            pass
        out.append(AppItem(
            namespace=ns,
            app=app,
            image=img,
            tag=tag,
            replicas_desired=d.spec.replicas or 0,
            replicas_available=(d.status.available_replicas or 0)
        ))
    return out

@router.get("/pods", response_model=List[PodItem])
async def pods(ns: str = Query(..., alias="ns"), app: str = Query(..., alias="app")):
    ns_guard(ns)
    if not k8s: raise HTTPException(500, "k8s client not initialized")
    lbl = f"app={app}"
    pls = k8s.list_namespaced_pod(namespace=ns, label_selector=lbl)
    out = []
    now = time.time()
    for p in pls.items:
        st = p.status
        cs = (st.container_statuses or [None])[0]
        ready = bool(cs and cs.ready)
        image = cs.image if cs else ""
        age = int(now - p.metadata.creation_timestamp.timestamp())
        out.append(PodItem(name=p.metadata.name, phase=st.phase or "Unknown",
                           ready=ready, age_seconds=age, image=image))
    return out

@router.get("/overview", response_model=Overview)
async def overview(ns: str, app: str):
    ns_guard(ns)

    # Replicas from kube-state-metrics
    q_des = f'kube_deployment_status_replicas{{namespace="{ns}",deployment="{app}"}}'
    q_av  = f'kube_deployment_status_replicas_available{{namespace="{ns}",deployment="{app}"}}'

    # CPU / Memory per pod
    q_cpu = f'sum by(pod) (rate(container_cpu_usage_seconds_total{{namespace="{ns}", pod=~"{app}.*", image!=""}}[5m]))'
    q_mem = f'max by(pod) (container_memory_working_set_bytes{{namespace="{ns}", pod=~"{app}.*", image!=""}})'

    async with httpx.AsyncClient(timeout=10) as s:
        r1 = await s.get(f"{PROM_URL}/api/v1/query", params={"query": q_des})
        r2 = await s.get(f"{PROM_URL}/api/v1/query", params={"query": q_av})
        r3 = await s.get(f"{PROM_URL}/api/v1/query", params={"query": q_cpu})
        r4 = await s.get(f"{PROM_URL}/api/v1/query", params={"query": q_mem})

    def one(res): 
        try: return int(float(res.json()["data"]["result"][0]["value"][1]))
        except: return 0

    def vec(res, key):
        out=[]
        for it in res.json()["data"]["result"]:
            out.append({"pod": it["metric"].get("pod",""), key: float(it["value"][1])})
        return out

    replicas = {"desired": one(r1), "available": one(r2)}
    cpu = [{"pod": v["pod"], "mcores": round(v.get("value", v.get("mcores",0))*1000, 1)} 
           for v in [{"pod": x["pod"], "value": x["value"]} for x in vec(r3, "value")] ]
    mem = vec(r4, "bytes")
    # Optional HTTP metrics if app exposes them
    http = None
    q_err = f'sum(rate(http_requests_total{{namespace="{ns}", app="{app}", status=~"5.."}}[5m]))'
    q_lat = f'histogram_quantile(0.95, sum by(le) (rate(http_request_duration_seconds_bucket{{namespace="{ns}", app="{app}"}}[5m])))'
    try:
        rr = await _prom.get("/api/v1/query", params={"query": q_err})
        rl = await _prom.get("/api/v1/query", params={"query": q_lat})
        err = float(rr.json()["data"]["result"][0]["value"][1]) if rr.json()["data"]["result"] else 0.0
        p95 = float(rl.json()["data"]["result"][0]["value"][1]) * 1000 if rl.json()["data"]["result"] else None
        http = {"errors_rate": err, "p95_ms": p95}
    except Exception:
        http = None

    return Overview(namespace=ns, app=app, replicas=replicas, cpu_mcores=cpu, mem_bytes=mem, http=http)

# app/monitor.py (أو نفس الملف الذي فيه /monitor/logs)
import time, requests, os
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/monitor", tags=["monitor"])

LOKI_URL = os.getenv("LOKI_URL", "http://loki.monitoring.svc:3100")

def _nanos(ts_sec: int) -> int:
    return ts_sec * 1_000_000_000

@router.get("/logs")
def get_logs(
    ns: str = Query(..., alias="ns"),
    app: str = Query(..., alias="app"),
    q: str | None = Query(None),
    limit: int = Query(200)
):
    now = int(time.time())
    start_ns = _nanos(now - 900)     # آخر 15 دقيقة
    end_ns   = _nanos(now)

    # الاستعلام الأساسي: حسب الـlabels
    primary_query = f'({{namespace="{ns}", app="{app}"}}) or ({{namespace="{ns}", pod=~"^{app}.*"}})'

    # لو فيه q من المستخدم، خليها فلتر نصّي
    # (ممكن لاحقًا نضيف |= أو |~ حسب الحاجة)
    if q:
        primary_query += f' |= "{q}"'

    params = {
        "query": primary_query,
        "start": str(start_ns),
        "end": str(end_ns),
        "limit": str(limit),
        "direction": "backward",
    }

    try:
        r = requests.get(f"{LOKI_URL}/loki/api/v1/query_range", params=params, timeout=10)
        if r.status_code == 200:
            data = r.json()
            items = []
            # رجّع أسطر اللوج منresult type streams
            for stream in data.get("data", {}).get("result", []):
                labels = stream.get("stream", {})
                for ts, line in stream.get("values", []):
                    items.append({"ts": ts, "line": line, "labels": labels})
            return {"items": items}

        # لو رجع 400 أو غيره: جرّب fallback أبسط
        # fallback: بحث نصّي داخل namespace
        fallback_query = f'{{namespace="{ns}"}} |= "{app}"'
        if q:
            fallback_query += f' |= "{q}"'

        fb_params = {
            "query": fallback_query,
            "start": str(start_ns),
            "end": str(end_ns),
            "limit": str(limit),
            "direction": "backward",
        }
        r2 = requests.get(f"{LOKI_URL}/loki/api/v1/query_range", params=fb_params, timeout=10)
        if r2.status_code == 200:
            data = r2.json()
            items = []
            for stream in data.get("data", {}).get("result", []):
                labels = stream.get("stream", {})
                for ts, line in stream.get("values", []):
                    items.append({"ts": ts, "line": line, "labels": labels})
            return {"items": items}

        # لو فشل الاثنين، أرجع رسالة Loki بدل 500 غامضة
        try:
            msg = r.json().get("error", r.text)
        except Exception:
            msg = r.text
        raise HTTPException(status_code=502, detail=f"Loki error: {msg}")

    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Loki request failed: {e}")

def k8s_events(
    ns: str = Query(..., alias="ns"),
    app: str = Query(..., alias="app"),
    since: int = Query(3600),
):
    # رجعنا لِـ CoreV1Api لأن EventsV1Api يلزم event_time != None
    v1 = client.CoreV1Api()
    evs = v1.list_namespaced_event(ns)

    items = []
    for e in evs.items:
        obj = getattr(e, "involved_object", None)
        name = getattr(obj, "name", "") if obj else ""
        if app and app not in (name or ""):
            continue

        # اختَر زمن مناسب متوفر
        ts = getattr(e, "last_timestamp", None) or getattr(e, "first_timestamp", None) or getattr(e.metadata, "creation_timestamp", None)

        items.append({
            "type": getattr(e, "type", None),
            "reason": getattr(e, "reason", None),
            "message": getattr(e, "message", None),
            "ts": str(ts) if ts else None,
            "regarding": {
                "kind": getattr(obj, "kind", None) if obj else None,
                "name": name,
                "uid": getattr(obj, "uid", None) if obj else None,
            }
        })

    return JSONResponse({"items": items})