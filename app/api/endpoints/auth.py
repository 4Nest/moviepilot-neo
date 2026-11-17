from typing import Any

from fastapi import APIRouter

from app.core.config import settings
from app.db.models.passkey import PassKey
from app.db.models.user import User

router = APIRouter()


def _system_auth_providers() -> list[dict[str, Any]]:
    """
    获取系统内建的匿名登录方式摘要。

    :return: 系统认证提供方列表
    """
    admin_user = User.get_by_name(db=None, name=settings.SUPERUSER)
    has_passkey = bool(
        PassKey.get_by_user_id(db=None, user_id=admin_user.id)
        if admin_user
        else False
    )
    return [
        {
            "id": "system:passkey",
            "type": "system",
            "method": "passkey",
            "name": "通行密钥",
            "icon": "material-symbols:passkey",
            "enabled": has_passkey,
        }
    ]


@router.get("/providers", summary="查询登录认证提供方", response_model=list[dict])
def auth_providers() -> list[dict[str, Any]]:
    """
    查询系统提供的登录认证入口，仅保留系统 Passkey。

    :return: 认证提供方摘要列表
    """
    return [provider for provider in _system_auth_providers() if provider.get("enabled", True)]
