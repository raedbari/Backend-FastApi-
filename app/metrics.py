# metrics.py
from prometheus_client import Counter

OPEN_APP_TOTAL = Counter(
    "smartdevops_open_app_total",
    "Number of times users clicked Open App",
    ["namespace", "app", "host"]
)