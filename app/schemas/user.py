from typing import Optional

from pydantic import BaseModel, Field, ConfigDict, computed_field, field_validator


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
    """用户资料响应，兼容第三方客户端的只读账号状态。"""

    name: str
    email: Optional[str] = None
    is_active: bool = False
    is_superuser: bool = False

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value: Optional[str]) -> str:
        """未设置邮箱时返回空字符串，兼容客户端的非空字符串模型。"""
        return value or ""

    @computed_field
    @property
    def permissions(self) -> dict[str, bool]:
        """仅从账号实际状态派生客户端功能标记。"""
        return {key: self.is_active and self.is_superuser for key in ("discovery", "search", "subscribe", "manage")}


# Additional properties stored in DB
class UserInDB(UserInDBBase):
    hashed_password: str
