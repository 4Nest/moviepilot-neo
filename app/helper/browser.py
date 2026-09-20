import uuid
from typing import Any, Callable, Optional, Protocol

from app.core.config import settings
from app.log import logger
from app.utils.http import RequestUtils, cookie_parse


class BrowserElement(Protocol):
    """
    页面元素的最小接口，避免为了类型标注直接导入 Playwright。
    """

    def is_visible(self) -> bool:
        """判断元素是否可见。"""
        ...

    def fill(self, value: str) -> None:
        """向元素输入文本。"""
        ...

    def inner_text(self) -> str:
        """获取元素可见文本。"""
        ...


class BrowserContext(Protocol):
    """
    CloakBrowser 返回的上下文只需要满足这些能力即可。
    """

    def new_page(self) -> "BrowserPage":
        """创建新的浏览器页面。"""
        ...

    def cookies(self) -> list[dict[str, Any]]:
        """返回当前上下文 Cookie。"""
        ...

    def close(self) -> None:
        """关闭浏览器上下文。"""
        ...


class BrowserPage(Protocol):
    """
    CloakBrowser 页面对象的最小接口，覆盖 helper 和登录流程当前用到的方法。
    """

    context: BrowserContext
    url: str

    def set_extra_http_headers(self, headers: dict[str, str]) -> None:
        """设置页面额外请求头。"""
        ...

    def set_default_timeout(self, timeout: int) -> None:
        """设置页面默认超时时间。"""
        ...

    def goto(self, url: str, *args: Any, **kwargs: Any) -> Any:
        """导航到指定 URL。"""
        ...

    def wait_for_load_state(self, state: str, *args: Any, **kwargs: Any) -> Any:
        """等待页面加载状态。"""
        ...

    def wait_for_selector(self, selector: str, *args: Any, **kwargs: Any) -> Any:
        """等待指定选择器出现。"""
        ...

    def fill(self, selector: str, value: str, *args: Any, **kwargs: Any) -> Any:
        """向指定选择器输入文本。"""
        ...

    def click(self, selector: str, *args: Any, **kwargs: Any) -> Any:
        """点击指定选择器。"""
        ...

    def select_option(self, selector: str, *args: Any, **kwargs: Any) -> Any:
        """选择下拉框选项。"""
        ...

    def query_selector(self, selector: str) -> Optional[BrowserElement]:
        """查询指定选择器元素。"""
        ...

    def title(self) -> str:
        """返回页面标题。"""
        ...

    def inner_text(self, selector: str) -> str:
        """返回指定选择器的可见文本。"""
        ...

    def content(self) -> str:
        """返回页面 HTML 内容。"""
        ...

    def evaluate(self, expression: str, *args: Any, **kwargs: Any) -> Any:
        """执行页面 JavaScript 表达式。"""
        ...

    def screenshot(self, *args: Any, **kwargs: Any) -> bytes:
        """截取页面截图。"""
        ...

    def close(self) -> None:
        """关闭浏览器页面。"""
        ...



class PlaywrightHelper:
    def __init__(self):
        """初始化网页渲染辅助类。"""

    @staticmethod
    def __browser_emulation() -> str:
        """
        当前浏览器仿真类型。
        """
        return (settings.BROWSER_EMULATION or "cloakbrowser").lower()

    @staticmethod
    def __launch_cloakbrowser_context(headless: bool,
                                      user_agent: Optional[str] = None,
                                      proxies: Optional[dict] = None) -> BrowserContext:
        """
        启动 CloakBrowser 上下文。
        """
        from cloakbrowser import launch_context

        return launch_context(headless=headless,
                              proxy=proxies,
                              user_agent=user_agent,
                              humanize=settings.CLOAKBROWSER_HUMANIZE,
                              human_preset=settings.CLOAKBROWSER_HUMAN_PRESET)

    @staticmethod
    def __fs_cookie_str(cookies: list) -> str:
        if not cookies:
            return ""
        return "; ".join([f"{c.get('name')}={c.get('value')}" for c in cookies if c and c.get('name') is not None])

    @staticmethod
    def __flaresolverr_request(url: str,
                               cookies: Optional[str] = None,
                               proxy_config: Optional[dict] = None,
                               timeout: Optional[int] = 60) -> Optional[dict]:
        """
        调用 FlareSolverr 解决 Cloudflare 并返回 solution 结果
        参考: https://github.com/FlareSolverr/FlareSolverr
        """
        if not settings.FLARESOLVERR_URL:
            logger.warn("未配置 FLARESOLVERR_URL，无法使用 FlareSolverr")
            return None

        fs_api = settings.FLARESOLVERR_URL.rstrip("/") + "/v1"
        session_id = None

        try:
            # 检查是否需要代理认证
            need_proxy_auth = (proxy_config and proxy_config.get("server") and
                               (proxy_config.get("username") or proxy_config.get("password")))

            if need_proxy_auth:
                # 使用 session 模式支持代理认证
                logger.debug("检测到flaresolverr代理需要认证，使用 session 模式")

                # 1. 创建会话
                session_id = str(uuid.uuid4())
                create_payload: dict = {
                    "cmd": "sessions.create",
                    "session": session_id
                }

                # 添加代理配置到会话创建请求
                if proxy_config and proxy_config.get("server"):
                    proxy_payload: dict = {"url": proxy_config["server"]}
                    if proxy_config.get("username"):
                        proxy_payload["username"] = proxy_config["username"]
                    if proxy_config.get("password"):
                        proxy_payload["password"] = proxy_config["password"]
                    create_payload["proxy"] = proxy_payload

                # 创建会话
                create_result = RequestUtils(content_type="application/json",
                                             timeout=timeout or 60).post_json(url=fs_api, json=create_payload)
                if not create_result or create_result.get("status") != "ok":
                    logger.error(
                        f"创建 FlareSolverr 会话失败: {create_result.get('message') if create_result else '无响应'}")
                    return None

                # 2. 使用会话发送请求
                request_payload = {
                    "cmd": "request.get",
                    "url": url,
                    "session": session_id,
                    "maxTimeout": int(timeout or 60) * 1000,
                }
            else:
                # 使用普通模式（无代理认证）
                request_payload = {
                    "cmd": "request.get",
                    "url": url,
                    "maxTimeout": int(timeout or 60) * 1000,
                }
                # 添加代理配置（仅 URL，无认证）
                if proxy_config and proxy_config.get("server"):
                    request_payload["proxy"] = {"url": proxy_config["server"]}

            # 将 cookies 以数组形式传递给 FlareSolverr
            if cookies:
                try:
                    request_payload["cookies"] = cookie_parse(cookies, array=True)
                except Exception as e:
                    logger.debug(f"解析 cookies 失败，忽略: {str(e)}")

            # 发送请求
            data = RequestUtils(content_type="application/json",
                                timeout=timeout or 60).post_json(url=fs_api, json=request_payload)
            if not data:
                logger.error("FlareSolverr 返回空响应")
                return None
            if data.get("status") != "ok":
                logger.error(f"FlareSolverr 调用失败: {data.get('message')}")
                return None
            return data.get("solution")
        except Exception as e:
            logger.error(f"调用 FlareSolverr 失败: {str(e)}")
            return None
        finally:
            # 清理会话
            if session_id:
                try:
                    destroy_payload = {
                        "cmd": "sessions.destroy",
                        "session": session_id
                    }
                    RequestUtils(content_type="application/json",
                                 timeout=10).post_json(url=fs_api, json=destroy_payload)
                    logger.debug(f"已清理 FlareSolverr 会话: {session_id}")
                except Exception as e:
                    logger.warning(f"清理 FlareSolverr 会话失败: {str(e)}")

    def action(self, url: str,
               callback: Callable[[BrowserPage], Any],
               cookies: Optional[str] = None,
               ua: Optional[str] = None,
               proxies: Optional[dict] = None,
               headless: Optional[bool] = False,
               timeout: Optional[int] = 60) -> Any:
        """
        访问网页，接收Page对象并执行操作
        :param url: 网页地址
        :param callback: 回调函数，需要接收page对象
        :param cookies: cookies
        :param ua: user-agent
        :param proxies: 代理
        :param headless: 是否无头模式
        :param timeout: 超时时间
        """
        result = None
        try:
            context = None
            page = None
            try:
                # 如果配置使用 FlareSolverr，先通过其获取清除后的 cookies 与 UA
                fs_cookie_header = None
                fs_ua = None
                if self.__browser_emulation() == "flaresolverr":
                    solution = self.__flaresolverr_request(url=url, cookies=cookies,
                                                           proxy_config=proxies, timeout=timeout)
                    if solution:
                        fs_cookie_header = self.__fs_cookie_str(solution.get("cookies", []))
                        fs_ua = solution.get("userAgent")

                context = self.__launch_cloakbrowser_context(headless=headless,
                                                             user_agent=fs_ua or ua,
                                                             proxies=proxies)
                page = context.new_page()

                # 优先使用 FlareSolverr 返回，其次使用入参
                merged_cookie = fs_cookie_header or cookies
                if merged_cookie:
                    page.set_extra_http_headers({"cookie": merged_cookie})

                page.goto(url)
                page.wait_for_load_state("networkidle", timeout=timeout * 1000)

                # 回调函数
                result = callback(page)

            except Exception as e:
                logger.error(f"网页操作失败: {str(e)}")
            finally:
                if page:
                    page.close()
                if context:
                    context.close()
        except Exception as e:
            logger.error(f"CloakBrowser初始化失败: {str(e)}")

        return result

    def get_page_source(self, url: str,
                        cookies: Optional[str] = None,
                        ua: Optional[str] = None,
                        proxies: Optional[dict] = None,
                        headless: Optional[bool] = False,
                        timeout: Optional[int] = 60) -> Optional[str]:
        """
        获取网页源码
        :param url: 网页地址
        :param cookies: cookies
        :param ua: user-agent
        :param proxies: 代理
        :param headless: 是否无头模式
        :param timeout: 超时时间
        """
        source = None
        # 如果配置为 FlareSolverr，则直接调用获取页面源码
        if self.__browser_emulation() == "flaresolverr":
            try:
                solution = self.__flaresolverr_request(url=url, cookies=cookies,
                                                       proxy_config=proxies, timeout=timeout)
                if solution:
                    return solution.get("response")
            except Exception as e:
                logger.error(f"FlareSolverr 获取源码失败: {str(e)}")
        try:
            context = None
            page = None
            try:
                context = self.__launch_cloakbrowser_context(headless=headless,
                                                             user_agent=ua,
                                                             proxies=proxies)
                page = context.new_page()

                if cookies:
                    page.set_extra_http_headers({"cookie": cookies})

                page.goto(url)
                page.wait_for_load_state("networkidle", timeout=timeout * 1000)

                source = page.content()

            except Exception as e:
                logger.error(f"获取网页源码失败: {str(e)}")
                source = None
            finally:
                # 确保资源被正确清理
                if page:
                    page.close()
                if context:
                    context.close()
        except Exception as e:
            logger.error(f"CloakBrowser初始化失败: {str(e)}")

        return source
