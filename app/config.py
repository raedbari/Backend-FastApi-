# app/config.py
import os

# 🔐 مدة صلاحية التوكنات (بالساعات)
JWT_EXP_HOURS = int(os.getenv("JWT_EXP_HOURS", "24"))

# 🧩 البريد الإلكتروني للإشعارات (اختياري)
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "")

# 🔑 المفتاح السري لـ JWT
JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-key")

# ⚙️ خوارزمية التوقيع
JWT_ALG = os.getenv("JWT_ALG", "HS256")

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", "admin@smartdevops.lat")

# Grafana settings
GRAFANA_URL = os.getenv("GRAFANA_URL", "https://grafana.smartdevops.lat").rstrip("/")
GRAFANA_API_TOKEN = os.getenv("GRAFANA_API_TOKEN", "")  # لا تضع القيمة هنا، ستكون Secret
GRAFANA_ORG_ID = int(os.getenv("GRAFANA_ORG_ID", "1"))

# Frontend CORS (موجود عندك كـ FRONTEND_ORIGIN في .env)
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "https://smartdevops.lat")
