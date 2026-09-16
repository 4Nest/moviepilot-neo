import base64
import re
from typing import Annotated, Any, Union

from fastapi import APIRouter, Body, Depends, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession

from app import schemas
from app.core.security import get_password_hash
from app.db import get_async_db
from app.db.models.user import User
from app.db.user_oper import get_current_admin, get_current_admin_async
from app.db.userconfig_oper import UserConfigOper

router = APIRouter()


@router.get("/current", summary="当前登录用户信息", response_model=schemas.User)
async def read_current_user(
    current_user: User = Depends(get_current_admin_async),
) -> Any:
    """
    当前登录用户信息
    """
    return current_user


@router.put("/current", summary="更新当前用户资料", response_model=schemas.Response)
async def update_current_user(
    *,
    db: AsyncSession = Depends(get_async_db),
    user_in: schemas.UserProfileUpdate,
    current_user: User = Depends(get_current_admin_async),
) -> Any:
    """
    更新当前登录用户（canonical admin）的资料。

    只接受 email/password/avatar/nickname/settings，请求体夹带
    name/is_active/is_superuser/permissions/id 等字段将被请求校验拒绝。
    """
    user_info = user_in.model_dump(exclude_unset=True)
    if user_info.get("password"):
        # 正则表达式匹配密码包含字母、数字、特殊字符中的至少两项
        pattern = r"^(?![a-zA-Z]+$)(?!\d+$)(?![^\da-zA-Z\s]+$).{6,50}$"
        if not re.match(pattern, user_info["password"]):
            return schemas.Response(
                success=False,
                message="密码需要同时包含字母、数字、特殊字符中的至少两项，且长度大于6位",
            )
        user_info["hashed_password"] = get_password_hash(user_info["password"])
    user_info.pop("password", None)
    # 昵称写入个性化设置，与既有 settings 合并
    nickname = user_info.pop("nickname", None)
    if nickname is not None:
        settings_value = dict(user_info.get("settings") or current_user.settings or {})
        settings_value["nickname"] = nickname
        user_info["settings"] = settings_value
    if not user_info:
        return schemas.Response(success=True)
    await current_user.async_update(db, user_info)
    return schemas.Response(success=True)


@router.post("/current/avatar", summary="上传当前用户头像", response_model=schemas.Response)
async def upload_avatar(
    db: AsyncSession = Depends(get_async_db),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_admin_async),
) -> schemas.Response:
    """
    上传当前登录用户头像
    """
    # 将文件转换为Base64
    file_base64 = base64.b64encode(file.file.read())
    await current_user.async_update(db, {"avatar": f"data:image/ico;base64,{file_base64}"})
    return schemas.Response(success=True, data={"filename": file.filename})


@router.get("/config/{key}", summary="查询用户配置", response_model=schemas.Response)
def get_config(key: str, current_user: User = Depends(get_current_admin)):
    """
    查询用户配置
    """
    value = UserConfigOper().get(username=current_user.name, key=key)
    return schemas.Response(success=True, data={"value": value})


@router.post("/config/{key}", summary="更新用户配置", response_model=schemas.Response)
def set_config(
    key: str,
    value: Annotated[Union[list, dict, bool, int, str] | None, Body()] = None,
    current_user: User = Depends(get_current_admin),
):
    """
    更新用户配置
    """
    UserConfigOper().set(username=current_user.name, key=key, value=value)
    return schemas.Response(success=True)
