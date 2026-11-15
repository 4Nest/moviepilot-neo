import io
from pathlib import Path
from typing import Optional, List

from PIL import Image

from app.chain.mediaserver import MediaServerChain
from app.chain.tmdb import TmdbChain
from app.core.cache import cached, FileCache, AsyncFileCache
from app.core.config import settings
from app.log import logger
from app.utils.http import RequestUtils, AsyncRequestUtils
from app.utils.ip import IpUtils
from app.utils.security import SecurityUtils
from app.utils.singleton import Singleton
from app.utils.url import UrlUtils
from urllib.parse import parse_qsl, urlsplit

_GITHUB_IMAGE_MIRROR_TIMEOUT = 15


class WallpaperHelper(metaclass=Singleton):
    """
    壁纸帮助类
    """

    def get_wallpaper(self) -> Optional[str]:
        """
        获取登录页面壁纸
        """
        if settings.WALLPAPER == "bing":
            return self.get_bing_wallpaper()
        elif settings.WALLPAPER == "mediaserver":
            return self.get_mediaserver_wallpaper()
        elif settings.WALLPAPER == "customize":
            return self.get_customize_wallpaper()
        elif settings.WALLPAPER == "tmdb":
            return self.get_tmdb_wallpaper()
        return ''

    def get_wallpapers(self, num: int = 10) -> List[str]:
        """
        获取登录页面壁纸列表
        """
        if settings.WALLPAPER == "bing":
            return self.get_bing_wallpapers(num)
        elif settings.WALLPAPER == "mediaserver":
            return self.get_mediaserver_wallpapers(num)
        elif settings.WALLPAPER == "customize":
            return self.get_customize_wallpapers()
        elif settings.WALLPAPER == "tmdb":
            return self.get_tmdb_wallpapers(num)
        return []

    @cached(maxsize=1, ttl=3600)
    def get_tmdb_wallpaper(self) -> Optional[str]:
        """
        获取TMDB每日壁纸
        """
        return TmdbChain().get_random_wallpager()

    @cached(maxsize=1, ttl=3600, skip_empty=True)
    def get_tmdb_wallpapers(self, num: int = 10) -> List[str]:
        """
        获取7天的TMDB每日壁纸
        """
        return TmdbChain().get_trending_wallpapers(num)

    @cached(maxsize=1, ttl=3600)
    def get_bing_wallpaper(self) -> Optional[str]:
        """
        获取Bing每日壁纸
        """
        url = "https://cn.bing.com/HPImageArchive.aspx?format=js&idx=0&n=1"
        resp = RequestUtils(timeout=5).get_res(url)
        if resp and resp.status_code == 200:
            try:
                result = resp.json()
                if isinstance(result, dict):
                    for image in result.get('images') or []:
                        return f"https://cn.bing.com{image.get('url')}" if 'url' in image else ''
            except Exception as err:
                print(str(err))
        return None

    @cached(maxsize=1, ttl=3600, skip_empty=True)
    def get_bing_wallpapers(self, num: int = 7) -> List[str]:
        """
        获取7天的Bing每日壁纸
        """
        url = f"https://cn.bing.com/HPImageArchive.aspx?format=js&idx=0&n={num}"
        resp = RequestUtils(timeout=5).get_res(url)
        if resp and resp.status_code == 200:
            try:
                result = resp.json()
                if isinstance(result, dict):
                    return [f"https://cn.bing.com{image.get('url')}" for image in result.get('images') or []]
            except Exception as err:
                print(str(err))
        return []

    @cached(maxsize=1, ttl=3600)
    def get_mediaserver_wallpaper(self) -> Optional[str]:
        """
        获取媒体服务器壁纸
        """
        return MediaServerChain().get_latest_wallpaper()

    @cached(maxsize=1, ttl=3600, skip_empty=True)
    def get_mediaserver_wallpapers(self, num: int = 10) -> List[str]:
        """
        获取媒体服务器壁纸列表
        """
        return MediaServerChain().get_latest_wallpapers(count=num)

    @cached(maxsize=1, ttl=3600)
    def get_customize_wallpaper(self) -> Optional[str]:
        """
        获取自定义壁纸api壁纸
        """
        wallpaper_list = self.get_customize_wallpapers()
        if wallpaper_list:
            return wallpaper_list[0]
        return None

    @cached(maxsize=1, ttl=3600, skip_empty=True)
    def get_customize_wallpapers(self) -> List[str]:
        """
        获取自定义壁纸api壁纸
        """

        def find_files_with_suffixes(obj, suffixes: List[str]) -> List[str]:
            """
            递归查找对象中所有包含特定后缀的文件，返回匹配的字符串列表
            支持输入：字典、列表、字符串
            """
            _result = []

            # 处理字符串
            if isinstance(obj, str):
                if obj.endswith(tuple(suffixes)):
                    _result.append(obj)

            # 处理字典
            elif isinstance(obj, dict):
                for value in obj.values():
                    _result.extend(find_files_with_suffixes(value, suffixes))

            # 处理列表
            elif isinstance(obj, list):
                for item in obj:
                    _result.extend(find_files_with_suffixes(item, suffixes))

            return _result

        # 判断是否存在自定义壁纸api
        if settings.CUSTOMIZE_WALLPAPER_API_URL:
            wallpaper_list = []
            resp = RequestUtils(timeout=15).get_res(settings.CUSTOMIZE_WALLPAPER_API_URL)
            if resp and resp.status_code == 200:
                # 如果返回的是图片格式
                content_type = resp.headers.get('Content-Type')
                if content_type and content_type.lower().startswith('image/'):
                    wallpaper_list.append(settings.CUSTOMIZE_WALLPAPER_API_URL)
                else:
                    try:
                        result = resp.json()
                        if isinstance(result, list) or isinstance(result, dict) or isinstance(result, str):
                            wallpaper_list = find_files_with_suffixes(result, settings.SECURITY_IMAGE_SUFFIXES)
                    except Exception as err:
                        print(str(err))
            return wallpaper_list
        else:
            return []


class ImageHelper(metaclass=Singleton):
    """
    外部图片抓取、内容校验与本地文件缓存辅助类。
    """

    def __init__(self):
        _base_path = settings.CACHE_PATH
        _ttl = settings.GLOBAL_IMAGE_CACHE_DAYS * 24 * 3600
        self.file_cache = FileCache(base=_base_path, ttl=_ttl)
        self.async_file_cache = AsyncFileCache(base=_base_path, ttl=_ttl)

    @staticmethod
    def _prepare_cache_path(url: str) -> str:
        """缓存路径"""
        sanitized_path = SecurityUtils.sanitize_url_path(url)
        cache_path = Path(sanitized_path)
        if not cache_path.suffix:
            cache_path = cache_path.with_suffix(".jpg")
        return cache_path.as_posix()

    @staticmethod
    def _build_github_mirror_url(url: str, cookies: Optional[str | dict]) -> Optional[str]:
        """
        为公开的 GitHub 静态图片资源构造 GITHUB_PROXY 镜像地址。
        """
        prefix = str(getattr(settings, "GITHUB_PROXY", "") or "").strip()
        if not prefix or cookies:
            return None
        try:
            parts = urlsplit(url)
            static_host = parts.hostname in {
                "raw.githubusercontent.com",
                "avatars.githubusercontent.com",
                "user-images.githubusercontent.com",
            }
            blob_image = parts.hostname == "github.com" and "/blob/" in parts.path and (
                ("raw", "true") in parse_qsl(parts.query)
            )
            if (
                parts.scheme != "https"
                or parts.port not in (None, 443)
                or parts.username
                or parts.password
                or not (static_host or blob_image)
                or any(key.lower() != "raw" for key, _ in parse_qsl(parts.query))
            ):
                return None
        except ValueError:
            return None
        return f"{UrlUtils.standardize_base_url(prefix)}{url}"

    @staticmethod
    def _mirror_request_params(params: dict) -> dict:
        """
        生成镜像站请求参数，不透传原始代理、Cookie 与 Referer。
        """
        return {
            **params,
            "proxies": None,
            "cookies": None,
            "referer": None,
            "timeout": _GITHUB_IMAGE_MIRROR_TIMEOUT,
        }

    @staticmethod
    def get_image_mime_type(content: bytes, verify: bool = True) -> Optional[str]:
        """
        根据图片内容返回 Pillow 识别的图片 MIME 类型。

        外部响应在写入缓存前需要完整校验；已校验的缓存只需读取格式头。
        非图片或可脚本化的 MIME 类型不作为图片代理响应。
        """
        if not content:
            return None
        try:
            with Image.open(io.BytesIO(content)) as image:
                image_format = (image.format or "").upper()
                if verify:
                    image.verify()
            mime_type = Image.MIME.get(image_format)
            if (
                not mime_type
                or not mime_type.startswith("image/")
                or mime_type == "image/svg+xml"
            ):
                return None
            return mime_type
        except Exception as err:
            logger.warning(f"Invalid image format: {err}")
            return None

    @staticmethod
    def _get_request_params(url: str, proxy: Optional[bool], cookies: Optional[str | dict]) -> dict:
        """获取参数"""
        referer = "https://movie.douban.com/" if "doubanio.com" in url else None
        if proxy is None:
            proxies = settings.PROXY if not (referer or IpUtils.is_internal(url)) else None
        else:
            proxies = settings.PROXY if proxy else None
        return {
            "ua": settings.NORMAL_USER_AGENT,
            "proxies": proxies,
            "referer": referer,
            "cookies": cookies,
            "accept_type": "image/avif,image/webp,image/apng,*/*",
        }

    def _validate_fetched_response(
        self,
        url: str,
        response,
        *,
        warn_on_failure: bool = True,
    ) -> Optional[tuple[bytes, str]]:
        """
        统一校验远端图片响应状态码与图片格式。
        """
        if response is None or response.status_code != 200:
            if warn_on_failure:
                logger.warn(f"Failed to fetch image from URL: {url}")
            return None
        content = response.content
        mime_type = self.get_image_mime_type(content)
        if not mime_type:
            return None
        return content, mime_type

    def fetch_image(
        self,
        url: str,
        proxy: Optional[bool] = None,
        use_cache: bool = True,
        cookies: Optional[str | dict] = None) -> Optional[bytes]:
        """
        获取图片（同步版本）
        """
        result = self.fetch_image_with_mime_type(
            url=url,
            proxy=proxy,
            use_cache=use_cache,
            cookies=cookies,
        )
        return result[0] if result else None

    def fetch_image_with_mime_type(
        self,
        url: str,
        proxy: Optional[bool] = None,
        use_cache: bool = True,
        cookies: Optional[str | dict] = None,
    ) -> Optional[tuple[bytes, str]]:
        """
        同步获取图片及其内容识别 MIME 类型。

        网络响应在写入缓存前完整验证一次；缓存命中仅重新识别格式头。
        """
        if not url:
            return None

        cache_path = self._prepare_cache_path(url)

        # 检查缓存
        if use_cache:
            content = self.file_cache.get(cache_path, region="images")
            if content:
                mime_type = self.get_image_mime_type(content, verify=False)
                if mime_type:
                    return content, mime_type

        # 请求远程图片（GitHub 静态资源优先走 GITHUB_PROXY，失败回退直连/原配置出口）
        params = self._get_request_params(url, proxy, cookies)
        validated = None
        mirror_url = self._build_github_mirror_url(url, cookies)
        if mirror_url:
            try:
                mirror_resp = RequestUtils(**self._mirror_request_params(params)).get_res(url=mirror_url)
                validated = self._validate_fetched_response(url, mirror_resp, warn_on_failure=False)
            except Exception as err:
                logger.debug(f"GitHub 图片镜像请求失败：{err}")

        if validated is None:
            response = RequestUtils(**params).get_res(url=url)
            validated = self._validate_fetched_response(url, response, warn_on_failure=True)
        if validated is None:
            return None

        content, mime_type = validated
        # 保存缓存
        self.file_cache.set(cache_path, content, region="images")
        return content, mime_type

    async def async_fetch_image(
        self,
        url: str,
        proxy: Optional[bool] = None,
        use_cache: bool = True,
        cookies: Optional[str | dict] = None) -> Optional[bytes]:
        """
        获取图片（异步版本）
        """
        result = await self.async_fetch_image_with_mime_type(
            url=url,
            proxy=proxy,
            use_cache=use_cache,
            cookies=cookies,
        )
        return result[0] if result else None

    async def async_fetch_image_with_mime_type(
        self,
        url: str,
        proxy: Optional[bool] = None,
        use_cache: bool = True,
        cookies: Optional[str | dict] = None,
    ) -> Optional[tuple[bytes, str]]:
        """
        异步获取图片及其内容识别 MIME 类型。

        网络响应在写入缓存前完整验证一次；缓存命中仅重新识别格式头。
        """
        if not url:
            return None

        cache_path = self._prepare_cache_path(url)

        # 检查缓存
        if use_cache:
            content = await self.async_file_cache.get(cache_path, region="images")
            if content:
                mime_type = self.get_image_mime_type(content, verify=False)
                if mime_type:
                    return content, mime_type

        # 请求远程图片（GitHub 静态资源优先走 GITHUB_PROXY，失败回退直连/原配置出口）
        params = self._get_request_params(url, proxy, cookies)
        validated = None
        mirror_url = self._build_github_mirror_url(url, cookies)
        if mirror_url:
            try:
                mirror_resp = await AsyncRequestUtils(**self._mirror_request_params(params)).get_res(url=mirror_url)
                validated = self._validate_fetched_response(url, mirror_resp, warn_on_failure=False)
            except Exception as err:
                logger.debug(f"GitHub 图片镜像请求失败：{err}")

        if validated is None:
            response = await AsyncRequestUtils(**params).get_res(url=url)
            validated = self._validate_fetched_response(url, response, warn_on_failure=True)
        if validated is None:
            return None

        content, mime_type = validated
        # 保存缓存
        await self.async_file_cache.set(cache_path, content, region="images")
        return content, mime_type
