import os, requests

PROM_URL = os.getenv("PROM_URL", "").rstrip("/")

def prom_storage_gb(namespace: str) -> float:
    if not PROM_URL:
        return 0.0

    promql = f'sum(kube_persistentvolumeclaim_resource_requests_storage_bytes{{namespace="{namespace}"}}) / 1024 / 1024 / 1024'

    r = requests.get(f"{PROM_URL}/api/v1/query", params={"query": promql}, timeout=5)
    r.raise_for_status()
    data = r.json()

    try:
        # result[0].value = [timestamp, "123.45"]
        result = data["data"]["result"]
        if not result:
            return 0.0
        return float(result[0]["value"][1])
    except Exception:
        return 0.0