from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.security import create_access_token, decode_access_token, hash_password, verify_password
from app.core.config import settings
from app.db.store import create_user, get_user_by_id, get_user_by_username
from app.schemas.user import TokenResponse, UserLogin, UserRegister, UserResponse

router = APIRouter(tags=["auth"])
security = HTTPBearer(auto_error=False)

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}
_DEBUG_USER = {"id": 0, "username": "本机调试", "created_at": None}


def _can_bypass_auth(request: Request) -> bool:
    """Allow password-free debugging only in non-production loopback sessions."""
    if settings.env.lower() in {"prod", "production"} or not settings.local_debug_auth_bypass:
        return False
    client_host = request.client.host.lower() if request.client else ""
    return client_host in _LOOPBACK_HOSTS


def _current_user_id(credentials: HTTPAuthorizationCredentials | None = None) -> int | None:
    if not credentials or credentials.scheme.lower() != "bearer":
        return None
    payload = decode_access_token(credentials.credentials)
    if not payload or "sub" not in payload:
        return None
    try:
        return int(payload["sub"])
    except (TypeError, ValueError):
        return None


def require_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """Resolve the bearer token and reject unauthenticated business calls."""
    if _can_bypass_auth(request):
        return _DEBUG_USER.copy()
    uid = _current_user_id(credentials)
    if not uid:
        raise HTTPException(status_code=401, detail="登录状态无效或已过期")
    user = get_user_by_id(uid)
    if not user:
        raise HTTPException(status_code=401, detail="用户不存在")
    return user


@router.post("/auth/register", response_model=TokenResponse)
def register(body: UserRegister) -> TokenResponse:
    """注册新用户，成功后返回 token 与用户信息。"""
    if get_user_by_username(body.username):
        raise HTTPException(status_code=400, detail="用户名已存在")
    user = create_user(body.username, hash_password(body.password))
    token = create_access_token(user["id"])
    return TokenResponse(
        access_token=token,
        user=UserResponse(id=user["id"], username=user["username"], created_at=user.get("created_at")),
    )


@router.post("/auth/login", response_model=TokenResponse)
def login(body: UserLogin) -> TokenResponse:
    """登录，返回 token 与用户信息。"""
    row = get_user_by_username(body.username)
    if not row or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = create_access_token(row["id"])
    return TokenResponse(
        access_token=token,
        user=UserResponse(id=row["id"], username=row["username"], created_at=row.get("created_at")),
    )


@router.get("/auth/me", response_model=UserResponse)
def me(user: dict = Depends(require_current_user)) -> UserResponse:
    """Return the current user or an explicit 401 for an invalid session."""
    return UserResponse(id=user["id"], username=user["username"], created_at=user.get("created_at"))


@router.post("/auth/logout")
def logout() -> dict:
    """登出（客户端丢弃 token 即可，服务端无状态）。"""
    return {"ok": True}
