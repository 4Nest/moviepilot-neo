from typing import Optional

from pydantic import BaseModel, Field, ConfigDict


# Shared properties
class UserBase(BaseModel):
    # 用户名
    name: str
    # 邮箱，未启用
    email: Optional[str] = None
    # 头像
    avatar: Optional[str] = None
    # 是否开启二次验证
    is_otp: Optional[bool] = False
    # 个性化设置
    settings: Optional[dict] = Field(default_factory=dict)

    model_config = ConfigDict(from_attributes=True)


# Properties to receive via API on profile update（仅允许自助修改的字段）
class UserProfileUpdate(BaseModel):
    # 邮箱
    email: Optional[str] = None
    # 新密码
    password: Optional[str] = None
    # 头像
    avatar: Optional[str] = None
    # 昵称（写入 settings.nickname）
    nickname: Optional[str] = None
    # 个性化设置
    settings: Optional[dict] = None

    # name/is_active/is_superuser/permissions/id 等字段一律拒绝
    model_config = ConfigDict(extra="forbid")


class UserInDBBase(UserBase):
    id: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)


# Additional properties to return via API
class User(UserInDBBase):
    name: str
    email: Optional[str] = None


# Additional properties stored in DB
class UserInDB(UserInDBBase):
    hashed_password: str
