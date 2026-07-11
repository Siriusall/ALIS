from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.security import create_access_token, decode_access_token, hash_password, verify_password
from app.db.store import create_user, get_user_by_id, get_user_by_username
from app.schemas.user import TokenResponse, UserLogin, UserRegister, UserResponse

router = APIRouter(tags=["auth"])
security = HTTPBearer(auto_error=False)


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


@router.get("/auth/me", response_model=UserResponse | None)
def me(credentials: HTTPAuthorizationCredentials | None = Depends(security)) -> UserResponse | None:
    """获取当前登录用户，未登录返回 null。"""
    uid = _current_user_id(credentials)
    if not uid:
        return None
    user = get_user_by_id(uid)
    if not user:
        return None
    return UserResponse(id=user["id"], username=user["username"], created_at=user.get("created_at"))


@router.post("/auth/logout")
def logout() -> dict:
    """登出（客户端丢弃 token 即可，服务端无状态）。"""
    return {"ok": True}
