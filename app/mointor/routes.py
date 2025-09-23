from fastapi import APIRouter, Depends
from . import __init__  # لا شيء فعلياً، فقط للتصدير
from app.auth.middleware import require_auth
import os, httpx

PROM_URL = os.getenv("PROM_URL")  # مثل http://prometheus:9090

r = APIRouter(prefix="/api/monitor", tags=["monitor"])

@r.get("/metrics")
async def metrics(payload=Depends(require_auth)):
    ns = f'tenant-{payload["tenant_id"]}'
    q = f'sum(rate(http_requests_total{{namespace="{ns}"}}[5m]))'
    async with httpx.AsyncClient(timeout=5) as c:
        res = await c.get(f"{PROM_URL}/api/v1/query", params={"query": q})
    return res.json()
