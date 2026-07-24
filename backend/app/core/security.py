"""密码哈希与 JWT 签发/校验。"""

from datetime import datetime, timedelta
from typing import Any, Optional
import hashlib

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings

# 这里改为使用 pbkdf2_sha256，避免 Windows 环境下 bcrypt 的 72 字节限制兼容性问题
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


def _normalize_password(password: str) -> str:
    """将任意长度密码转换为 bcrypt 可接受的输入。

    bcrypt 只会使用前 72 字节；这里统一对超过 72 字节的密码做一次 SHA256，
    保证：
    - 不再触发 passlib 的 72 字节限制错误
    - 同一明文密码在 hash 与 verify 中走相同流程
    """
    data = password.encode("utf-8")
    if len(data) <= 72:
        return password
    # 先做一次 SHA256，再把十六进制结果作为 bcrypt 的输入
    return hashlib.sha256(data).hexdigest()


def hash_password(password: str) -> str:
    return pwd_context.hash(_normalize_password(password))


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(_normalize_password(plain), hashed)


def create_access_token(subject: str | int, extra: Optional[dict[str, Any]] = None) -> str:
    now = datetime.now()
    expire = now + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {"sub": str(subject), "exp": expire, "iat": now}
    if extra:
        payload.update(extra)
    return jwt.encode(
        payload,
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def decode_access_token(token: str) -> Optional[dict[str, Any]]:
    try:
        return jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
    except JWTError:
        return None
