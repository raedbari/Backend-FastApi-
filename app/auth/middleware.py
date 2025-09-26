# from fastapi import Request, HTTPException
# from .security import decode_jwt

# async def require_auth(req: Request) -> dict:
#     token = req.cookies.get("auth")
#     if not token: raise HTTPException(401, "unauthenticated")
#     try:
#         payload = decode_jwt(token)
#     except Exception:
#         raise HTTPException(401, "invalid token")
#     req.state.tenant_id = payload["tenant_id"]
#     req.state.role = payload["role"]
#     return payload
