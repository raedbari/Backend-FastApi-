import datetime as dt, os, jwt
from passlib.hash import bcrypt

JWT_SECRET = os.getenv("JWT_SECRET","change-me")
JWT_ALG = "HS256"
JWT_EXP_HOURS = int(os.getenv("JWT_EXP_HOURS","12"))

def hash_pw(p: str) -> str: return bcrypt.hash(p)
def verify_pw(p: str, h: str) -> bool: return bcrypt.verify(p, h)

def make_jwt(sub: str, tenant_id: str, role: str) -> str:
    payload = {
        "sub": sub, "tenant_id": tenant_id, "role": role,
        "exp": dt.datetime.utcnow()+dt.timedelta(hours=JWT_EXP_HOURS)
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)

def decode_jwt(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
