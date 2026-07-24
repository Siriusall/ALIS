from pydantic import BaseModel, Field


class UserRegister(BaseModel):
    username: str = Field(..., min_length=2, max_length=32, description="用户名")
    password: str = Field(..., min_length=6, max_length=64, description="密码")


class UserLogin(BaseModel):
    username: str = Field(..., description="用户名")
    password: str = Field(..., description="密码")


class UserResponse(BaseModel):
    id: int
    username: str
    created_at: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse
