"""
PassKey WebAuthn 辅助工具类
"""
import base64
import binascii
import ipaddress
import json
import secrets
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Tuple
from urllib.parse import urlparse

from webauthn import (
    generate_registration_options,
    verify_registration_response,
    generate_authentication_options,
    verify_authentication_response,
    options_to_json
)
from webauthn.helpers import (
    parse_registration_credential_json,
    parse_authentication_credential_json
)
from webauthn.helpers.structs import (
    PublicKeyCredentialDescriptor,
    AuthenticatorTransport,
    UserVerificationRequirement,
    ResidentKeyRequirement,
    AuthenticatorSelectionCriteria
)
from webauthn.helpers.cose import COSEAlgorithmIdentifier
from webauthn.helpers.exceptions import InvalidRegistrationResponse

from app.core.cache import TTLCache
from app.core.config import settings
from app.helper.redis import RedisHelper
from app.log import logger

PASSKEY_CHALLENGE_TTL_SECONDS = 5 * 60
PasskeyChallengePurpose = Literal["authentication", "registration"]


@dataclass(frozen=True)
class PasskeyChallenge:
    """服务端保存的一次性 Passkey challenge 及其认证边界。"""

    challenge: str
    purpose: PasskeyChallengePurpose
    user_id: Optional[int]
    expected_origin: Optional[str] = None
    expected_rp_id: Optional[str] = None


class PasskeyChallengeStore:
    """使用当前缓存后端签发并原子消费短时 Passkey challenge。"""

    _cache = TTLCache(
        region="passkey_challenge",
        maxsize=4096,
        ttl=PASSKEY_CHALLENGE_TTL_SECONDS,
    )
    _memory_consume_lock = threading.Lock()

    @classmethod
    def issue(
        cls,
        *,
        challenge: str,
        purpose: PasskeyChallengePurpose,
        user_id: Optional[int],
        expected_origin: Optional[str] = None,
        expected_rp_id: Optional[str] = None,
    ) -> str:
        """保存 challenge 并返回不携带认证事实的随机事务 token。"""
        transaction_token = secrets.token_urlsafe(32)
        cls._cache.set(
            transaction_token,
            PasskeyChallenge(
                challenge=challenge,
                purpose=purpose,
                user_id=user_id,
                expected_origin=expected_origin,
                expected_rp_id=expected_rp_id,
            ),
        )
        return transaction_token

    @classmethod
    def consume(
        cls,
        *,
        transaction_token: str,
        purpose: PasskeyChallengePurpose,
    ) -> Optional[PasskeyChallenge]:
        """原子领取 challenge；任何完成尝试都会使事务失效。"""
        if not transaction_token:
            return None

        if cls._cache.is_redis():
            challenge = RedisHelper().pop(
                transaction_token,
                region="passkey_challenge",
            )
        else:
            with cls._memory_consume_lock:
                try:
                    challenge = cls._cache.pop(transaction_token)
                except KeyError:
                    challenge = None

        if not isinstance(challenge, PasskeyChallenge):
            return None
        if challenge.purpose != purpose:
            return None
        return challenge


class PassKeyRegistrationVerificationError(Exception):
    """Passkey 注册响应未通过 WebAuthn 安全校验。"""


class PassKeyRegistrationOriginMismatchError(PassKeyRegistrationVerificationError):
    """浏览器来源与系统配置的 Passkey 注册来源不一致。"""


class PassKeyHelper:
    """
    PassKey WebAuthn 辅助类
    """

    @staticmethod
    def _is_ip_hostname(hostname: str) -> bool:
        """判断主机名是否为裸 IPv4/IPv6 地址（WebAuthn 规范不支持裸 IP 作为 RP ID）。"""
        if not hostname:
            return False
        try:
            ipaddress.ip_address(hostname.strip("[]"))
            return True
        except ValueError:
            return False

    @staticmethod
    def _normalize_origin_and_rp_id(raw_url: str) -> Optional[Tuple[str, str]]:
        """
        将配置的域名或请求来源规范化为标准 WebAuthn (origin, rp_id)。
        自动补齐缺失协议、剔除默认端口(:80/:443)与子路径，确保与浏览器 window.location.origin 一致。
        """
        raw = (raw_url or "").strip()
        if not raw:
            return None
        if "://" not in raw:
            lower_head = raw.split("/", 1)[0].split(":", 1)[0].lower()
            default_scheme = "http" if lower_head in ("localhost", "127.0.0.1") else "https"
            raw = f"{default_scheme}://{raw}"
        parsed = urlparse(raw)
        scheme = (parsed.scheme or "").lower()
        hostname = (parsed.hostname or "").lower()
        if scheme not in ("http", "https") or not hostname:
            return None
        try:
            port = parsed.port
        except ValueError:
            return None
        default_port = 80 if scheme == "http" else 443
        host_expr = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
        if port and port != default_port:
            origin = f"{scheme}://{host_expr}:{port}"
        else:
            origin = f"{scheme}://{host_expr}"
        return origin, hostname

    @staticmethod
    def get_rp_id() -> str:
        """
        获取 Relying Party ID
        """
        if settings.APP_DOMAIN:
            normalized = PassKeyHelper._normalize_origin_and_rp_id(settings.APP_DOMAIN)
            if normalized:
                return normalized[1]
        # 只有在未配置 APP_DOMAIN 时，才默认为 localhost
        return "localhost"

    @staticmethod
    def get_rp_name() -> str:
        """
        获取 Relying Party 名称
        """
        return "MoviePilot"

    @staticmethod
    def get_origin() -> str:
        """
        获取源地址
        """
        if settings.APP_DOMAIN:
            normalized = PassKeyHelper._normalize_origin_and_rp_id(settings.APP_DOMAIN)
            if normalized:
                return normalized[0]
            return settings.APP_DOMAIN.strip().rstrip("/")
        # 如果未配置APP_DOMAIN，使用默认的localhost地址
        normalized_local = PassKeyHelper._normalize_origin_and_rp_id(
            f"http://localhost:{settings.NGINX_PORT}"
        )
        return normalized_local[0] if normalized_local else f"http://localhost:{settings.NGINX_PORT}"

    @staticmethod
    def resolve_request_context(request: Any = None) -> Tuple[Optional[str], Optional[str]]:
        """
        当系统未显式配置 APP_DOMAIN 时，从当前 HTTP 请求头安全推导 (expected_origin, expected_rp_id)，
        以支持反向代理域名或非默认本地端口开箱即用；一旦配置了 APP_DOMAIN，始终以系统配置为准。
        """
        if settings.APP_DOMAIN and settings.APP_DOMAIN.strip():
            return None, None
        if request is None or not hasattr(request, "headers"):
            return None, None

        origin_header = (request.headers.get("origin") or "").strip()
        candidate = origin_header
        if not candidate:
            forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip()
            forwarded_host = (request.headers.get("x-forwarded-host") or "").split(",", 1)[0].strip()
            host = forwarded_host or (request.headers.get("host") or "").strip()
            scheme = forwarded_proto or getattr(getattr(request, "url", None), "scheme", "") or "http"
            if host:
                candidate = f"{scheme}://{host}"

        normalized = PassKeyHelper._normalize_origin_and_rp_id(candidate)
        if not normalized:
            return None, None
        origin, rp_id = normalized
        if rp_id == "testserver" or PassKeyHelper._is_ip_hostname(rp_id):
            return None, None
        return origin, rp_id

    @staticmethod
    def standardize_credential_id(credential_id: str) -> str:
        """
        标准化凭证ID（Base64 URL Safe）
        """
        try:
            # Base64解码并重新编码以标准化格式
            decoded = base64.urlsafe_b64decode(credential_id + '==')
            return base64.urlsafe_b64encode(decoded).decode('utf-8').rstrip('=')
        except (binascii.Error, TypeError, ValueError) as e:
            logger.error(f"标准化凭证ID失败: {e}")
            return credential_id

    @staticmethod
    def _base64_encode_urlsafe(data: bytes) -> str:
        """
        Base64 URL Safe 编码（不带填充）

        :param data: 要编码的字节数据
        :return: Base64 URL Safe 编码的字符串
        """
        return base64.urlsafe_b64encode(data).decode('utf-8').rstrip('=')

    @staticmethod
    def _base64_decode_urlsafe(data: str) -> bytes:
        """
        Base64 URL Safe 解码（自动添加填充）

        :param data: Base64 URL Safe 编码的字符串
        :return: 解码后的字节数据
        """
        return base64.urlsafe_b64decode(data + '==')

    @staticmethod
    def _parse_credential_list(credentials: List[Dict[str, Any]]) -> List[PublicKeyCredentialDescriptor]:
        """
        解析凭证列表为 PublicKeyCredentialDescriptor 列表

        :param credentials: 凭证字典列表
        :return: PublicKeyCredentialDescriptor 列表
        """
        result = []
        for cred in credentials:
            try:
                valid_transports = []
                for raw_transport in (cred.get("transports") or "").split(","):
                    item = raw_transport.strip()
                    if not item:
                        continue
                    try:
                        valid_transports.append(AuthenticatorTransport(item))
                    except ValueError:
                        logger.debug(f"忽略未知的PassKey传输类型: {item}")
                result.append(
                    PublicKeyCredentialDescriptor(
                        id=PassKeyHelper._base64_decode_urlsafe(cred["credential_id"]),
                        transports=valid_transports or None,
                    )
                )
            except Exception as e:
                logger.warning(f"解析凭证失败: {e}")
                continue
        return result

    @staticmethod
    def _get_user_verification_requirement(user_verification: Optional[str] = None) -> UserVerificationRequirement:
        """
        获取用户验证要求

        :param user_verification: 指定的用户验证要求，如果不指定则从配置中读取
        :return: UserVerificationRequirement
        """
        if user_verification:
            return UserVerificationRequirement(user_verification)
        return UserVerificationRequirement.REQUIRED if settings.PASSKEY_REQUIRE_UV \
            else UserVerificationRequirement.PREFERRED

    @staticmethod
    def _get_verification_params(
        expected_origin: Optional[str] = None,
        expected_rp_id: Optional[str] = None
    ) -> Tuple[str, str]:
        """
        获取验证参数（origin 和 rp_id）

        :param expected_origin: 期望的源地址
        :param expected_rp_id: 期望的RP ID
        :return: (origin, rp_id)
        """
        origin = expected_origin or PassKeyHelper.get_origin()
        rp_id = expected_rp_id or PassKeyHelper.get_rp_id()
        return origin, rp_id

    @staticmethod
    def generate_registration_options(
        user_id: int,
        username: str,
        display_name: Optional[str] = None,
        existing_credentials: Optional[List[Dict[str, Any]]] = None,
        rp_id: Optional[str] = None,
    ) -> Tuple[str, str]:
        """
        生成注册选项

        :param user_id: 用户ID
        :param username: 用户名
        :param display_name: 显示名称
        :param existing_credentials: 已存在的凭证列表
        :param rp_id: 可选覆盖的 RP ID
        :return: (options_json, challenge)
        """
        try:
            # 用户信息
            user_id_bytes = str(user_id).encode('utf-8')

            # 排除已有的凭证
            exclude_credentials = PassKeyHelper._parse_credential_list(existing_credentials) \
                if existing_credentials else None

            # 用户验证要求
            uv_requirement = PassKeyHelper._get_user_verification_requirement()

            # 生成注册选项
            options = generate_registration_options(
                rp_id=rp_id or PassKeyHelper.get_rp_id(),
                rp_name=PassKeyHelper.get_rp_name(),
                user_id=user_id_bytes,
                user_name=username,
                user_display_name=display_name or username,
                exclude_credentials=exclude_credentials,
                authenticator_selection=AuthenticatorSelectionCriteria(
                    authenticator_attachment=None,
                    resident_key=ResidentKeyRequirement.REQUIRED,
                    user_verification=uv_requirement,
                ),
                supported_pub_key_algs=[
                    COSEAlgorithmIdentifier.ECDSA_SHA_256,
                    COSEAlgorithmIdentifier.EDDSA,
                    COSEAlgorithmIdentifier.RSASSA_PKCS1_v1_5_SHA_256,
                ]
            )

            # 转换为JSON
            options_json = options_to_json(options)

            # 提取challenge（用于后续验证）
            challenge = PassKeyHelper._base64_encode_urlsafe(options.challenge)

            return options_json, challenge

        except Exception as e:
            logger.error(f"生成注册选项失败: {e}")
            raise

    @staticmethod
    def verify_registration_response(
        credential: Dict[str, Any],
        expected_challenge: str,
        expected_origin: Optional[str] = None,
        expected_rp_id: Optional[str] = None
    ) -> Tuple[str, str, int, Optional[str]]:
        """
        验证注册响应
        
        :param credential: 客户端返回的凭证
        :param expected_challenge: 期望的challenge
        :param expected_origin: 期望的源地址
        :param expected_rp_id: 期望的RP ID
        :return: (credential_id, public_key, sign_count, aaguid)
        """
        try:
            # 准备验证参数
            origin, rp_id = PassKeyHelper._get_verification_params(expected_origin, expected_rp_id)
            # 解码challenge
            challenge_bytes = PassKeyHelper._base64_decode_urlsafe(expected_challenge)

            # 构建RegistrationCredential对象
            registration_credential = parse_registration_credential_json(json.dumps(credential))

            # 验证注册响应
            verification = verify_registration_response(
                credential=registration_credential,
                expected_challenge=challenge_bytes,
                expected_rp_id=rp_id,
                expected_origin=origin,
                require_user_verification=settings.PASSKEY_REQUIRE_UV
            )

            # 提取信息
            credential_id = PassKeyHelper._base64_encode_urlsafe(verification.credential_id)
            public_key = PassKeyHelper._base64_encode_urlsafe(verification.credential_public_key)
            sign_count = verification.sign_count
            # aaguid 可能已经是字符串格式，也可能是bytes
            if verification.aaguid:
                if isinstance(verification.aaguid, bytes):
                    aaguid = verification.aaguid.hex()
                else:
                    aaguid = str(verification.aaguid)
            else:
                aaguid = None

            return credential_id, public_key, sign_count, aaguid

        except InvalidRegistrationResponse as e:
            logger.error(f"验证注册响应失败: {e}")
            if str(e).startswith("Unexpected client data origin "):
                raise PassKeyRegistrationOriginMismatchError() from e
            raise PassKeyRegistrationVerificationError() from e
        except Exception as e:
            logger.error(f"验证注册响应失败: {e}")
            raise

    @staticmethod
    def generate_authentication_options(
        existing_credentials: Optional[List[Dict[str, Any]]] = None,
        user_verification: Optional[str] = None,
        rp_id: Optional[str] = None,
    ) -> Tuple[str, str]:
        """
        生成认证选项

        :param existing_credentials: 已存在的凭证列表（用于限制可用凭证）
        :param user_verification: 用户验证要求，如果不指定则从配置中读取
        :param rp_id: 可选覆盖的 RP ID
        :return: (options_json, challenge)
        """
        try:
            # 允许的凭证
            allow_credentials = PassKeyHelper._parse_credential_list(existing_credentials) \
                if existing_credentials else None

            # 用户验证要求
            uv_requirement = PassKeyHelper._get_user_verification_requirement(user_verification)

            # 生成认证选项
            options = generate_authentication_options(
                rp_id=rp_id or PassKeyHelper.get_rp_id(),
                allow_credentials=allow_credentials,
                user_verification=uv_requirement
            )

            # 转换为JSON
            options_json = options_to_json(options)

            # 提取challenge
            challenge = PassKeyHelper._base64_encode_urlsafe(options.challenge)

            return options_json, challenge

        except Exception as e:
            logger.error(f"生成认证选项失败: {e}")
            raise

    @staticmethod
    def verify_authentication_response(
        credential: Dict[str, Any],
        expected_challenge: str,
        credential_public_key: str,
        credential_current_sign_count: int,
        expected_origin: Optional[str] = None,
        expected_rp_id: Optional[str] = None
    ) -> Tuple[bool, int]:
        """
        验证认证响应
        
        :param credential: 客户端返回的凭证
        :param expected_challenge: 期望的challenge
        :param credential_public_key: 凭证公钥
        :param credential_current_sign_count: 当前签名计数
        :param expected_origin: 期望的源地址
        :param expected_rp_id: 期望的RP ID
        :return: (验证成功, 新的签名计数)
        """
        try:
            # 准备验证参数
            origin, rp_id = PassKeyHelper._get_verification_params(expected_origin, expected_rp_id)
            # 解码
            challenge_bytes = PassKeyHelper._base64_decode_urlsafe(expected_challenge)
            public_key_bytes = PassKeyHelper._base64_decode_urlsafe(credential_public_key)

            # 构建AuthenticationCredential对象
            authentication_credential = parse_authentication_credential_json(json.dumps(credential))

            # 验证认证响应
            verification = verify_authentication_response(
                credential=authentication_credential,
                expected_challenge=challenge_bytes,
                expected_rp_id=rp_id,
                expected_origin=origin,
                credential_public_key=public_key_bytes,
                credential_current_sign_count=credential_current_sign_count,
                require_user_verification=settings.PASSKEY_REQUIRE_UV
            )

            return True, verification.new_sign_count

        except Exception as e:
            logger.error(f"验证认证响应失败: {e}")
            return False, credential_current_sign_count
