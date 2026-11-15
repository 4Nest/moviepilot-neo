import secrets
from dataclasses import dataclass
from typing import Literal, Optional, Tuple, Union

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.chain import ChainBase
from app.core.config import settings
from app.core.security import get_password_hash, verify_password
from app.db import db_update
from app.db.models.subscribe import Subscribe
from app.db.models.subscribehistory import SubscribeHistory
from app.db.models.user import User
from app.db.user_oper import UserOper
from app.log import logger
from app.schemas import AuthCredentials, AuthInterceptCredentials
from app.schemas.types import ChainEventType
from app.utils.otp import OtpUtils

PASSWORD_INVALID_CREDENTIALS_MESSAGE = "用户名、密码或验证码错误"


MfaMethod = Literal["otp"]


@dataclass(frozen=True)
class MfaRequired:
    """密码验证通过后，当前账号仍需完成的二次验证要求。"""

    methods: Tuple[MfaMethod, ...]


@db_update
def _migrate_emby_user_identity(
    db: Session,
    user_id: int,
    old_username: str,
    new_username: str,
    emby_user_id: str,
) -> bool:
    """在同一事务中迁移 Emby 改名用户的用户名、个性化绑定与订阅归属。"""
    target_user = db.query(User).filter(User.id == user_id).first()
    if not target_user:
        return False
    conflict_user = (
        db.query(User)
        .filter(User.name == new_username, User.id != user_id)
        .first()
    )
    if conflict_user:
        return False
    merged_settings = dict(target_user.settings or {}) if isinstance(target_user.settings, dict) else {}
    merged_settings["emby_user_id"] = emby_user_id
    target_user.name = new_username
    target_user.settings = merged_settings
    if old_username and old_username != new_username:
        db.query(Subscribe).filter(Subscribe.username == old_username).update(
            {Subscribe.username: new_username},
            synchronize_session=False,
        )
        db.query(SubscribeHistory).filter(SubscribeHistory.username == old_username).update(
            {SubscribeHistory.username: new_username},
            synchronize_session=False,
        )
    return True


class UserChain(ChainBase):
    """
    用户链，处理本地密码与媒体服务器辅助认证协议
    """

    @staticmethod
    def _has_local_password_auth(user: Optional[User]) -> bool:
        """判断账号是否允许通过本地密码哈希认证（排除 Emby 自动创建的随机密码账号）。"""
        if not user:
            return False
        if user.name == settings.SUPERUSER:
            return True
        user_settings = user.settings if isinstance(user.settings, dict) else {}
        if user_settings.get("auth_source") == "emby":
            return False
        return bool(user.hashed_password)

    @staticmethod
    def _find_user_by_emby_id(user_oper: UserOper, emby_user_id: str) -> Optional[User]:
        """根据持久化的不可变 emby_user_id 查找本地绑定用户。"""
        if not emby_user_id:
            return None
        for item in user_oper.list():
            if not item:
                continue
            user_settings = item.settings if isinstance(item.settings, dict) else {}
            if str(user_settings.get("emby_user_id") or "").strip() == emby_user_id:
                return item
        return None

    @staticmethod
    def _persist_emby_user_id(user_oper: UserOper, user: User, emby_user_id: Optional[str]) -> User:
        """将不可变 emby_user_id 记录到用户 settings，保留原有密码与非 Emby 认证来源标记。"""
        if not user or not emby_user_id:
            return user
        current_settings = dict(user.settings or {}) if isinstance(user.settings, dict) else {}
        if current_settings.get("emby_user_id") == emby_user_id:
            return user
        current_settings["emby_user_id"] = emby_user_id
        user.update(db=user_oper._db, payload={"settings": current_settings})
        refreshed = user_oper.get_by_name(name=user.name)
        return refreshed or user

    def user_authenticate(
            self,
            username: Optional[str] = None,
            password: Optional[str] = None,
            mfa_code: Optional[str] = None,
            grant_type: Optional[str] = "password"
    ) -> Tuple[bool, Union[str, User, MfaRequired, None]]:
        """
        认证用户，支持超级管理员、本地有密码账号及 Emby 辅助认证账号

        :param username: 用户名
        :param password: 用户密码
        :param mfa_code: 一次性密码
        :param grant_type: 认证类型，仅支持 "password"
        :return:
            - 对于成功的认证，返回 (True, User)
            - 对于失败的认证，返回 (False, "错误信息")
        """
        credentials = AuthCredentials(
            username=username,
            password=password,
            mfa_code=mfa_code,
            grant_type=grant_type
        )
        logger.debug(f"认证类型：{grant_type}，开始准备对用户 {username} 进行身份校验")
        if credentials.grant_type != "password":
            logger.debug(f"认证类型 {grant_type} 未实现")
            return False, "不支持的认证类型"
        if credentials.username == settings.SUPERUSER:
            success, user_or_message = self.password_authenticate(credentials=credentials)
        else:
            existing_user = UserOper().get_by_name(name=credentials.username)
            if existing_user and not existing_user.is_active:
                return False, PASSWORD_INVALID_CREDENTIALS_MESSAGE
            if self._has_local_password_auth(existing_user):
                success, user_or_message = self.password_authenticate(credentials=credentials)
                if not success and not existing_user.is_superuser:
                    success, user_or_message = self.emby_authenticate(credentials=credentials)
            else:
                success, user_or_message = self.emby_authenticate(credentials=credentials)
        if not success:
            return False, PASSWORD_INVALID_CREDENTIALS_MESSAGE
        mfa_result = self._verify_mfa(user_or_message, credentials.mfa_code)
        if isinstance(mfa_result, MfaRequired):
            return False, mfa_result
        if not mfa_result:
            return False, PASSWORD_INVALID_CREDENTIALS_MESSAGE
        logger.info(f"用户 {getattr(user_or_message, 'name', username)} 认证成功")
        return True, user_or_message

    @staticmethod
    def password_authenticate(credentials: AuthCredentials) -> Tuple[bool, Union[User, str]]:
        """
        密码认证

        :param credentials: 认证凭证，包含用户名、密码以及可选的 MFA 认证码
        :return:
            - 成功时返回 (True, User)，其中 User 是认证通过的用户对象
            - 失败时返回 (False, "错误信息")
        """
        if not credentials or credentials.grant_type != "password":
            logger.info("密码认证失败，认证类型不匹配")
            return False, PASSWORD_INVALID_CREDENTIALS_MESSAGE

        user = UserOper().get_by_name(name=credentials.username)
        if not user or not UserChain._has_local_password_auth(user):
            logger.info(f"密码认证拒绝：用户 {credentials.username} 未启用本地密码认证")
            return False, PASSWORD_INVALID_CREDENTIALS_MESSAGE
        if not user.is_active:
            logger.info(f"密码认证失败，用户 {credentials.username} 已被禁用")
            return False, PASSWORD_INVALID_CREDENTIALS_MESSAGE
        try:
            verified = verify_password(credentials.password, str(user.hashed_password))
        except (ValueError, TypeError):
            verified = False
        if not verified:
            logger.info(f"密码认证失败，用户 {credentials.username} 的密码验证不通过")
            return False, PASSWORD_INVALID_CREDENTIALS_MESSAGE
        return True, user

    def emby_authenticate(self, credentials: AuthCredentials) -> Tuple[bool, Union[User, str]]:
        """
        使用 Emby 媒体服务器进行辅助认证并在首次登录时自动创建普通用户。

        :param credentials: 认证凭证
        :return:
            - 成功时返回 (True, User)
            - 失败时返回 (False, "错误信息")
        """
        if (
            not credentials
            or credentials.grant_type != "password"
            or not credentials.username
            or not credentials.password
        ):
            return False, PASSWORD_INVALID_CREDENTIALS_MESSAGE
        input_username = credentials.username.strip()
        if not input_username or input_username.lower() == settings.SUPERUSER.lower():
            return False, PASSWORD_INVALID_CREDENTIALS_MESSAGE

        user_oper = UserOper()
        pre_user = user_oper.get_by_name(name=credentials.username)
        if pre_user and (not pre_user.is_active or pre_user.is_superuser):
            logger.info(f"Emby 辅助认证拒绝：用户 {credentials.username} 已禁用或角色不允许")
            return False, PASSWORD_INVALID_CREDENTIALS_MESSAGE

        auth_result: Optional[AuthCredentials] = self.run_module(
            "user_authenticate", credentials=credentials
        )
        if not auth_result or not auth_result.token:
            logger.info(f"Emby 辅助认证失败：用户 {credentials.username} 凭证校验未通过")
            return False, PASSWORD_INVALID_CREDENTIALS_MESSAGE

        canonical_username = (auth_result.canonical_username or credentials.username or "").strip()
        emby_user_id = (auth_result.emby_user_id or "").strip() or None
        if not canonical_username or canonical_username.lower() == settings.SUPERUSER.lower():
            logger.warning(f"Emby 辅助认证拒绝：规范用户名 {canonical_username!r} 非法或与管理员冲突")
            return False, PASSWORD_INVALID_CREDENTIALS_MESSAGE

        intercept_data = AuthInterceptCredentials(
            username=canonical_username,
            channel=auth_result.channel or "Emby",
            service=auth_result.service or "",
            token=auth_result.token,
            status="completed",
        )
        event = self.eventmanager.send_event(ChainEventType.AuthIntercept, intercept_data)
        event_data = getattr(event, "event_data", event) or intercept_data
        if getattr(event_data, "cancel", False):
            logger.info(
                f"Emby 辅助认证完成后被拦截：user={canonical_username}, "
                f"source={getattr(event_data, 'source', None)}, reason={getattr(event_data, 'reason', None)}"
            )
            return False, PASSWORD_INVALID_CREDENTIALS_MESSAGE

        user: Optional[User] = None
        if emby_user_id:
            bound_user = self._find_user_by_emby_id(user_oper, emby_user_id)
            if bound_user:
                if not bound_user.is_active or bound_user.is_superuser:
                    logger.info(f"Emby 辅助认证拒绝：绑定用户 {bound_user.name} 已禁用或角色不允许")
                    return False, PASSWORD_INVALID_CREDENTIALS_MESSAGE
                if bound_user.name != canonical_username:
                    old_name = bound_user.name
                    migrated = _migrate_emby_user_identity(
                        db=user_oper._db,
                        user_id=bound_user.id,
                        old_username=old_name,
                        new_username=canonical_username,
                        emby_user_id=emby_user_id,
                    )
                    if not migrated:
                        logger.warning(
                            f"Emby 用户改名迁移失败：{old_name} -> {canonical_username}（目标用户名冲突或记录不存在）"
                        )
                        return False, PASSWORD_INVALID_CREDENTIALS_MESSAGE
                    logger.info(f"Emby 用户改名已自动对齐迁移：{old_name} -> {canonical_username}")
                    user = user_oper.get_by_name(name=canonical_username)
                else:
                    user = bound_user

        if not user:
            user = user_oper.get_by_name(name=canonical_username)
            if user:
                if not user.is_active or user.is_superuser:
                    logger.info(f"Emby 辅助认证拒绝：用户 {canonical_username} 已禁用或角色不允许")
                    return False, PASSWORD_INVALID_CREDENTIALS_MESSAGE
                user = self._persist_emby_user_id(user_oper, user, emby_user_id)

        if not user:
            initial_settings = {"auth_source": "emby"}
            if emby_user_id:
                initial_settings["emby_user_id"] = emby_user_id
            try:
                user_oper.add(
                    name=canonical_username,
                    is_active=True,
                    is_superuser=False,
                    hashed_password=get_password_hash(secrets.token_urlsafe(16)),
                    settings=initial_settings,
                )
            except IntegrityError:
                logger.info(f"Emby 用户 {canonical_username} 并发建号冲突，回查已创建记录")
            user = user_oper.get_by_name(name=canonical_username)
            if user and emby_user_id:
                user = self._persist_emby_user_id(user_oper, user, emby_user_id)

        if not user or not user.is_active or user.is_superuser:
            return False, PASSWORD_INVALID_CREDENTIALS_MESSAGE
        return True, user

    @staticmethod
    def _verify_mfa(user: User, mfa_code: Optional[str]) -> Union[bool, MfaRequired]:
        """
        验证密码登录后的 6 位验证码。

        :param user: 用户对象
        :param mfa_code: 身份验证器生成的 6 位验证码
        :return:
            - 如果验证成功返回 True
            - 如果需要 MFA 但未提供，返回当前账号实际可用的验证方式
            - 如果MFA验证失败，返回 False
        """
        if not user.is_otp:
            return True

        if not mfa_code:
            logger.info(f"用户 {user.name} 已启用二次验证，需要提供验证码")
            return MfaRequired(methods=("otp",))

        if not OtpUtils.check(str(user.otp_secret), mfa_code):
            logger.info(f"用户 {user.name} 的 MFA 认证失败")
            return False
        return True
