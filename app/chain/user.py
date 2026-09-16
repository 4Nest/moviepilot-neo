from dataclasses import dataclass
from typing import Literal, Optional, Tuple, Union

from app.chain import ChainBase
from app.core.config import settings
from app.core.security import verify_password
from app.db.models.user import User
from app.db.user_oper import UserOper
from app.log import logger
from app.schemas import AuthCredentials
from app.utils.otp import OtpUtils

PASSWORD_INVALID_CREDENTIALS_MESSAGE = "用户名、密码或验证码错误"


MfaMethod = Literal["otp"]


@dataclass(frozen=True)
class MfaRequired:
    """密码验证通过后，当前账号仍需完成的二次验证要求。"""

    methods: Tuple[MfaMethod, ...]


class UserChain(ChainBase):
    """
    用户链，处理多种认证协议
    """

    def user_authenticate(
            self,
            username: Optional[str] = None,
            password: Optional[str] = None,
            mfa_code: Optional[str] = None,
            grant_type: Optional[str] = "password"
    ) -> Tuple[bool, Union[str, User, MfaRequired, None]]:
        """
        认证用户，唯一账号为 canonical admin，仅支持密码+OTP 认证

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
        success, user_or_message = self.password_authenticate(credentials=credentials)
        if not success:
            return False, PASSWORD_INVALID_CREDENTIALS_MESSAGE
        mfa_result = self._verify_mfa(user_or_message, credentials.mfa_code)
        if isinstance(mfa_result, MfaRequired):
            return False, mfa_result
        if not mfa_result:
            return False, PASSWORD_INVALID_CREDENTIALS_MESSAGE
        logger.info(f"用户 {username} 通过密码认证成功")
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
        if not user or user.name != settings.SUPERUSER:
            logger.info(f"密码认证拒绝：非 canonical admin 用户 {credentials.username}")
            return False, PASSWORD_INVALID_CREDENTIALS_MESSAGE
        if not user.is_active:
            logger.info(f"密码认证失败，用户 {credentials.username} 已被禁用")
            return False, PASSWORD_INVALID_CREDENTIALS_MESSAGE
        if not verify_password(credentials.password, str(user.hashed_password)):
            logger.info(f"密码认证失败，用户 {credentials.username} 的密码验证不通过")
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
