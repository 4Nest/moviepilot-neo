from typing import Optional, List

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app import schemas
from app.core.security import verify_token
from app.db import DbOper, get_db, get_async_db
from app.db.models.user import User


def get_current_user(
        db: Session = Depends(get_db),
        token_data: schemas.TokenPayload = Depends(verify_token)
) -> User:
    """
    获取当前用户
    """
    user = User.get(db, rid=token_data.sub)
    if not user:
        raise HTTPException(status_code=403, detail="用户不存在")
    return user


async def get_current_user_async(
        db: AsyncSession = Depends(get_async_db),
        token_data: schemas.TokenPayload = Depends(verify_token)
) -> User:
    """
    异步获取当前用户
    """
    user = await User.async_get(db, rid=token_data.sub)
    if not user:
        raise HTTPException(status_code=403, detail="用户不存在")
    return user


def get_current_admin(current_user: User = Depends(get_current_user)) -> User:
    """只允许 canonical admin 账号访问业务接口。"""
    from app.core.config import settings
    if not current_user.is_active or not current_user.is_superuser or current_user.name != settings.SUPERUSER:
        raise HTTPException(status_code=403, detail="仅管理员账号可访问")
    return current_user


async def get_current_admin_async(current_user: User = Depends(get_current_user_async)) -> User:
    """异步版本的 canonical admin guard。"""
    from app.core.config import settings
    if not current_user.is_active or not current_user.is_superuser or current_user.name != settings.SUPERUSER:
        raise HTTPException(status_code=403, detail="仅管理员账号可访问")
    return current_user


class UserOper(DbOper):
    """
    用户管理
    """

    def list(self) -> List[User]:
        """
        获取用户列表
        """
        return User.list(self._db)

    def add(self, **kwargs):
        """
        新增用户
        """
        user = User(**kwargs)
        user.create(self._db)

    def get_by_name(self, name: str) -> User:
        """
        根据用户名获取用户
        """
        return User.get_by_name(self._db, name)

    async def async_get_by_name(self, name: str) -> User:
        """
        异步根据用户名获取用户。
        """
        return await User.async_get_by_name(self._db, name)

    async def async_get_by_id(self, user_id: int) -> User:
        """
        异步根据用户 ID 获取用户。
        """
        return await User.async_get_by_id(self._db, user_id)

    def get_settings(self, name: str) -> Optional[dict]:
        """
        获取用户个性化设置，返回None表示用户不存在
        """
        user = User.get_by_name(self._db, name)
        if user:
            return user.settings or {}
        return None

    def get_setting(self, name: str, key: str) -> Optional[str]:
        """
        获取用户个性化设置
        """
        settings = self.get_settings(name)
        if settings:
            return settings.get(key)
        return None

    def get_name(self, **kwargs) -> Optional[str]:
        """
        根据绑定账号获取用户名称
        """
        users = self.list()
        for user in users:
            user_setting = user.settings
            if user_setting:
                for k, v in kwargs.items():
                    if user_setting.get(k) == str(v):
                        return user.name
        return None
