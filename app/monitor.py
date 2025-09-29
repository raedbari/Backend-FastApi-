# app/monitor.py
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
import os, re, time
import httpx
from typing import List, Optional
from kubernetes import client, config

PROM_URL = os.environ["PROM_URL"].rstrip("/")
LOKI_URL = os.environ["LOKI_URL"].rstrip("/")
ALLOWED_NS = set([s.strip() for s in os.getenv("ALLOWED_NAMESPACES","").split(",") if s.strip()])

router = APIRouter(prefix="/monitor", tags=["monitor"])

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

LOKI_URL = os.environ.get("LOKI_URL", "").rstrip("/")

@router.get("/logs")
def get_logs(
    ns: str = Query(..., alias="ns"),
    app: str = Query(..., alias="app"),
    q: str | None = Query(None, alias="q"),
    limit: int = Query(200),
    since: int = Query(900),  # آخر 15 دقيقة افتراضيًا
):
    now = time.time()
    start_ns = int((now - since) * 1e9)
    end_ns = int(now * 1e9)

    # فلتر المحتوى (إن وجد)
    content = f' |~ "{q}"' if q else ""

    # الاستعلامين: بالـ app وبـ pod regex
    sel_app = f'{{namespace="{ns}", app="{app}"}}'
    sel_pod = f'{{namespace="{ns}", pod=~"^{app}.*"}}'

    # اتّحاد (OR) بين التعبيرين، ونفس فلتر المحتوى يطبق على الاثنين
    query = f'({sel_app}{content}) or ({sel_pod}{content})'

    params = {
        "query": query,
        "start": str(start_ns),
        "end": str(end_ns),
        "limit": str(limit),
        "direction": "backward",
    }
    r = requests.get(f"{LOKI_URL}/loki/api/v1/query_range", params=params, timeout=15)
    r.raise_for_status()
    data = r.json()

    items = []
    for stream in data.get("data", {}).get("result", []):
        labels = stream.get("stream", {})
        for ts, line in stream.get("values", []):
            items.append({
                "ts": ts,          # نانو ثانية من Loki
                "line": line,
                "labels": labels,
            })

    # رجّع نفس الشكل اللي تتوقعه الواجهة
    return JSONResponse({"items": items})

@router.get("/events")
async def k8s_events(ns: str, app: str, since: Optional[int]=3600):
    ns_guard(ns)
    if not k8s: raise HTTPException(500, "k8s client not initialized")
    # fieldSelector by involvedObject labels is limited; filter client-sideEee
    evs = k8s.list_namespaced_event(ns)
    cutoff = time.time() - since
    out=[]
    for e in evs.items:
        if e.event_time and e.event_time.timestamp() < cutoff: 
            continue
        if e.regarding and e.regarding.namespace == ns and app in (e.regarding.name or ""):
            out.append({
                "type": e.type,
                "reason": e.reason,
                "note": e.note,
                "at": (e.event_time or e.last_timestamp).isoformat() if (e.event_time or e.last_timestamp) else None,
                "obj": {"kind": e.regarding.kind, "name": e.regarding.name}
            })
    return {"items": out}
