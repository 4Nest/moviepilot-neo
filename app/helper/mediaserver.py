from typing import Any, Dict, List, Optional

from app.helper.service import ServiceBaseHelper
from app.schemas import MediaServerConf, MediaServerQueryError, ServiceInfo
from app.schemas.types import SystemConfigKey, ModuleType


def read_emby_query_items(
    request: Any,
    url: str,
    params: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    读取 Emby/Jellyfin 存在性查询的条目列表，区分成功空列表与不可确认的响应。

    :param request: 保留当前媒体服务器认证配置的 HTTP 工具
    :param url: 标题搜索或分集查询地址
    :param params: 当前请求的查询条件
    :return: 已验证的原始条目列表，成功查询无条目时为空列表
    :raises MediaServerQueryError: 无响应、非 200 状态或条目结构无效
    """
    response = request.get_res(url, params)
    if response is None:
        raise MediaServerQueryError("媒体库查询失败：未收到响应")
    try:
        if getattr(response, "status_code", None) != 200:
            raise MediaServerQueryError(
                f"媒体库查询失败：HTTP {getattr(response, 'status_code', None)}"
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise MediaServerQueryError("媒体库查询失败：响应格式无效")
        items = payload.get("Items")
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise MediaServerQueryError("媒体库查询失败：条目列表格式无效")
        return items
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            try:
                close()
            except AttributeError:
                pass


class MediaServerHelper(ServiceBaseHelper[MediaServerConf]):
    """
    媒体服务器帮助类
    """

    def __init__(self):
        super().__init__(
            config_key=SystemConfigKey.MediaServers,
            conf_type=MediaServerConf,
            module_type=ModuleType.MediaServer
        )

    def is_media_server(
            self,
            service_type: Optional[str] = None,
            service: Optional[ServiceInfo] = None,
            name: Optional[str] = None,
    ) -> bool:
        """
        通用的媒体服务器类型判断方法
        :param service_type: 媒体服务器的类型名称（如 'plex', 'emby', 'jellyfin'）
        :param service: 要判断的服务信息
        :param name: 服务的名称
        :return: 如果服务类型或实例为指定类型，返回 True；否则返回 False
        """
        # 如果未提供 service 则通过 name 获取服务
        service = service or self.get_service(name=name)

        # 判断服务类型是否为指定类型
        return bool(service and service.type == service_type)
