from typing import Optional

from pydantic import BaseModel, computed_field


class Token(BaseModel):
    """登录响应，保留第三方客户端需要的只读权限信息。"""

    # 令牌
    access_token: str
    # 令牌类型
    token_type: str
    # 用户ID
    user_id: int
    # 用户名
    user_name: str
    # 兼容旧客户端的管理员标记，由认证结果赋值
    super_user: bool = False
    # 头像
    avatar: Optional[str] = None
    # 权限级别
    level: int = 1
    # 是否显示配置向导
    wizard: Optional[bool] = None

    @computed_field
    @property
    def permissions(self) -> dict[str, bool]:
        """根据管理员身份提供客户端功能标记，不参与服务端鉴权。"""
        return {key: self.super_user for key in ("discovery", "search", "subscribe", "manage")}


class TokenPayload(BaseModel):
    # 用户ID
    sub: Optional[int] = None
    # 用户名
    username: Optional[str] = None
    # 超级用户
    super_user: Optional[bool] = None
    # 权限级别
    level: Optional[int] = None
    # 令牌用途 authentication\resource
    purpose: Optional[str] = None
