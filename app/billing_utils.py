import os, requests

PROM_URL = os.getenv("PROM_URL", "").rstrip("/")

def prom_storage_gb(namespace: str) -> float:
    if not PROM_URL:
        print("PROM_URL missing")
        return 0.0

    promql = f'sum(kube_persistentvolumeclaim_resource_requests_storage_bytes{{namespace="{namespace}"}}) / 1024 / 1024 / 1024'
    try:
        r = requests.get(f"{PROM_URL}/api/v1/query", params={"query": promql}, timeout=5)
        r.raise_for_status()
        data = r.json()
        result = data.get("data", {}).get("result", [])
        if not result:
            print("PROM empty result for ns:", namespace)
            return 0.0
        val = float(result[0]["value"][1])
        return val
    except Exception as e:
        print("PROM ERROR ns=", namespace, "err=", repr(e))
        return 0.0
