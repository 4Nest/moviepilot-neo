"""
提供 App 私有推送设备管理与实例凭据轮换接口。
"""

from fastapi import APIRouter, Depends, HTTPException

from app import schemas
from app.chain.notification import NotificationChain
from app.db.models.user import User
from app.db.user_oper import get_current_admin, get_current_user

router = APIRouter()


def get_current_active_user(current_user: User = Depends(get_current_user)) -> User:
    """
    获取当前已启用登录用户。
    """
    if not getattr(current_user, "is_active", True):
        raise HTTPException(status_code=403, detail="用户未启用")
    return current_user


def get_current_active_superuser(current_user: User = Depends(get_current_admin)) -> User:
    """
    获取当前已启用超级管理员用户。
    """
    return current_user


@router.post("/app/instance", summary="轮换 App 通知实例凭据", response_model=schemas.Response)
def manage_app_instance(
    request: schemas.AppPushInstanceRequest,
    current_user: User = Depends(get_current_active_superuser),
) -> schemas.Response:
    """
    仅容器管理员能轮换整个实例的发送凭据，响应只包含结果，不下发密钥。
    """
    result = NotificationChain().manage_channel(
        channel="AppPush",
        action="rotate_instance",
        source=request.source,
        owner=current_user.notification_identity,
        administrative=True,
    )
    return schemas.Response(
        success=bool(result.get("success")),
        message=result.get("message") or "",
        data=result.get("data"),
    )


@router.post("/app", summary="管理当前用户的 App 通知", response_model=schemas.Response)
def manage_app_notification(
    request: schemas.AppPushRequest,
    current_user: User = Depends(get_current_active_user),
) -> schemas.Response:
    """
    复用现有用户认证并强制注入账号通知身份，客户端不能操作其他用户设备。
    """
    if not getattr(current_user, "notification_identity", None):
        return schemas.Response(success=False, message="请完成数据库升级后重新登录")
    params = request.model_dump(exclude={"action"})
    result = NotificationChain().manage_channel(
        channel="AppPush",
        action=request.action,
        owner=current_user.notification_identity,
        **params,
    )
    return schemas.Response(
        success=bool(result.get("success")),
        message=result.get("message") or "",
        data=result.get("data"),
    )
