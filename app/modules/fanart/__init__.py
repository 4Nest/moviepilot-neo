import asyncio
import hashlib
import threading
from pathlib import Path
from typing import Any, Optional, Tuple, Union

from cachetools import TTLCache

from app.core.cache import cached
from app.core.context import MediaInfo, settings
from app.log import logger
from app.modules import _ModuleBase
from app.schemas.types import MediaType, ModuleType, OtherModulesType
from app.utils.http import RequestUtils, AsyncRequestUtils


class FanartModule(_ModuleBase):
    """
    {
        "name": "The Wheel of Time",
        "thetvdb_id": "355730",
        "tvposter": [
            {
                "id": "174068",
                "url": "http://assets.fanart.tv/fanart/tv/355730/tvposter/the-wheel-of-time-64b009de9548d.jpg",
                "lang": "en",
                "likes": "3"
            },
            {
                "id": "176424",
                "url": "http://assets.fanart.tv/fanart/tv/355730/tvposter/the-wheel-of-time-64de44fe42073.jpg",
                "lang": "00",
                "likes": "3"
            },
            {
                "id": "176407",
                "url": "http://assets.fanart.tv/fanart/tv/355730/tvposter/the-wheel-of-time-64dde63c7c941.jpg",
                "lang": "en",
                "likes": "0"
            },
            {
                "id": "177321",
                "url": "http://assets.fanart.tv/fanart/tv/355730/tvposter/the-wheel-of-time-64eda10599c3d.jpg",
                "lang": "cz",
                "likes": "0"
            },
            {
                "id": "155050",
                "url": "http://assets.fanart.tv/fanart/tv/355730/tvposter/the-wheel-of-time-6313adbd1fd58.jpg",
                "lang": "pl",
                "likes": "0"
            },
            {
                "id": "140198",
                "url": "http://assets.fanart.tv/fanart/tv/355730/tvposter/the-wheel-of-time-61a0d7b11952e.jpg",
                "lang": "en",
                "likes": "0"
            },
            {
                "id": "140034",
                "url": "http://assets.fanart.tv/fanart/tv/355730/tvposter/the-wheel-of-time-619e65b73871d.jpg",
                "lang": "en",
                "likes": "0"
            }
        ],
        "hdtvlogo": [
            {
                "id": "139835",
                "url": "http://assets.fanart.tv/fanart/tv/355730/hdtvlogo/the-wheel-of-time-6197d9392faba.png",
                "lang": "en",
                "likes": "3"
            },
            {
                "id": "140039",
                "url": "http://assets.fanart.tv/fanart/tv/355730/hdtvlogo/the-wheel-of-time-619e87941a128.png",
                "lang": "pt",
                "likes": "3"
            },
            {
                "id": "140092",
                "url": "http://assets.fanart.tv/fanart/tv/355730/hdtvlogo/the-wheel-of-time-619fa2347bada.png",
                "lang": "en",
                "likes": "3"
            },
            {
                "id": "164312",
                "url": "http://assets.fanart.tv/fanart/tv/355730/hdtvlogo/the-wheel-of-time-63c8185cb8824.png",
                "lang": "hu",
                "likes": "1"
            },
            {
                "id": "139827",
                "url": "http://assets.fanart.tv/fanart/tv/355730/hdtvlogo/the-wheel-of-time-6197539658a9e.png",
                "lang": "en",
                "likes": "1"
            },
            {
                "id": "177214",
                "url": "http://assets.fanart.tv/fanart/tv/355730/hdtvlogo/the-wheel-of-time-64ebae44c23a6.png",
                "lang": "cz",
                "likes": "0"
            },
            {
                "id": "177215",
                "url": "http://assets.fanart.tv/fanart/tv/355730/hdtvlogo/the-wheel-of-time-64ebae472deef.png",
                "lang": "cz",
                "likes": "0"
            },
            {
                "id": "156163",
                "url": "http://assets.fanart.tv/fanart/tv/355730/hdtvlogo/the-wheel-of-time-63316bef1ff9d.png",
                "lang": "cz",
                "likes": "0"
            },
            {
                "id": "155051",
                "url": "http://assets.fanart.tv/fanart/tv/355730/hdtvlogo/the-wheel-of-time-6313add04ca92.png",
                "lang": "pl",
                "likes": "0"
            },
            {
                "id": "152668",
                "url": "http://assets.fanart.tv/fanart/tv/355730/hdtvlogo/the-wheel-of-time-62ced3775a40a.png",
                "lang": "pl",
                "likes": "0"
            },
            {
                "id": "142266",
                "url": "http://assets.fanart.tv/fanart/tv/355730/hdtvlogo/the-wheel-of-time-61ccd93eeac2b.png",
                "lang": "de",
                "likes": "0"
            }
        ],
        "hdclearart": [
            {
                "id": "164313",
                "url": "http://assets.fanart.tv/fanart/tv/355730/hdclearart/the-wheel-of-time-63c81871c982c.png",
                "lang": "en",
                "likes": "3"
            },
            {
                "id": "140284",
                "url": "http://assets.fanart.tv/fanart/tv/355730/hdclearart/the-wheel-of-time-61a2128ed1df2.png",
                "lang": "pt",
                "likes": "3"
            },
            {
                "id": "139828",
                "url": "http://assets.fanart.tv/fanart/tv/355730/hdclearart/the-wheel-of-time-61975401e894c.png",
                "lang": "en",
                "likes": "1"
            },
            {
                "id": "164314",
                "url": "http://assets.fanart.tv/fanart/tv/355730/hdclearart/the-wheel-of-time-63c8188488a5f.png",
                "lang": "hu",
                "likes": "1"
            },
            {
                "id": "177322",
                "url": "http://assets.fanart.tv/fanart/tv/355730/hdclearart/the-wheel-of-time-64eda135933b6.png",
                "lang": "cz",
                "likes": "0"
            },
            {
                "id": "142267",
                "url": "http://assets.fanart.tv/fanart/tv/355730/hdclearart/the-wheel-of-time-61ccda9918c5c.png",
                "lang": "de",
                "likes": "0"
            }
        ],
        "seasonposter": [
            {
                "id": "140199",
                "url": "http://assets.fanart.tv/fanart/tv/355730/seasonposter/the-wheel-of-time-61a0d7c2976de.jpg",
                "lang": "en",
                "likes": "1",
                "season": "1"
            },
            {
                "id": "176395",
                "url": "http://assets.fanart.tv/fanart/tv/355730/seasonposter/the-wheel-of-time-64dd80b3d79a9.jpg",
                "lang": "en",
                "likes": "0",
                "season": "1"
            },
            {
                "id": "140035",
                "url": "http://assets.fanart.tv/fanart/tv/355730/seasonposter/the-wheel-of-time-619e65c4d5357.jpg",
                "lang": "en",
                "likes": "0",
                "season": "1"
            }
        ],
        "tvthumb": [
            {
                "id": "140242",
                "url": "http://assets.fanart.tv/fanart/tv/355730/tvthumb/the-wheel-of-time-61a1813035506.jpg",
                "lang": "en",
                "likes": "1"
            },
            {
                "id": "177323",
                "url": "http://assets.fanart.tv/fanart/tv/355730/tvthumb/the-wheel-of-time-64eda15b6dce6.jpg",
                "lang": "cz",
                "likes": "0"
            },
            {
                "id": "176399",
                "url": "http://assets.fanart.tv/fanart/tv/355730/tvthumb/the-wheel-of-time-64dd85c9b618c.jpg",
                "lang": "en",
                "likes": "0"
            },
            {
                "id": "152669",
                "url": "http://assets.fanart.tv/fanart/tv/355730/tvthumb/the-wheel-of-time-62ced53d16574.jpg",
                "lang": "pl",
                "likes": "0"
            },
            {
                "id": "141983",
                "url": "http://assets.fanart.tv/fanart/tv/355730/tvthumb/the-wheel-of-time-61c6d04a6d701.jpg",
                "lang": "en",
                "likes": "0"
            }
        ],
        "showbackground": [
            {
                "id": "177324",
                "url": "http://assets.fanart.tv/fanart/tv/355730/showbackground/the-wheel-of-time-64eda1833ccb1.jpg",
                "lang": "",
                "likes": "0",
                "season": "all"
            },
            {
                "id": "141986",
                "url": "http://assets.fanart.tv/fanart/tv/355730/showbackground/the-wheel-of-time-61c6d08f7c7e2.jpg",
                "lang": "",
                "likes": "0",
                "season": "all"
            },
            {
                "id": "139868",
                "url": "http://assets.fanart.tv/fanart/tv/355730/showbackground/the-wheel-of-time-6198ce358b98a.jpg",
                "lang": "",
                "likes": "0",
                "season": "all"
            }
        ],
        "seasonthumb": [
            {
                "id": "176396",
                "url": "http://assets.fanart.tv/fanart/tv/355730/seasonthumb/the-wheel-of-time-64dd80c8593f9.jpg",
                "lang": "en",
                "likes": "0",
                "season": "1"
            },
            {
                "id": "176400",
                "url": "http://assets.fanart.tv/fanart/tv/355730/seasonthumb/the-wheel-of-time-64dd85da7c5e9.jpg",
                "lang": "en",
                "likes": "0",
                "season": "0"
            }
        ],
        "tvbanner": [
            {
                "id": "176397",
                "url": "http://assets.fanart.tv/fanart/tv/355730/tvbanner/the-wheel-of-time-64dd80da9a255.jpg",
                "lang": "en",
                "likes": "0"
            },
            {
                "id": "176401",
                "url": "http://assets.fanart.tv/fanart/tv/355730/tvbanner/the-wheel-of-time-64dd85e8904ea.jpg",
                "lang": "en",
                "likes": "0"
            },
            {
                "id": "141988",
                "url": "http://assets.fanart.tv/fanart/tv/355730/tvbanner/the-wheel-of-time-61c6d34bceb5f.jpg",
                "lang": "en",
                "likes": "0"
            },
            {
                "id": "141984",
                "url": "http://assets.fanart.tv/fanart/tv/355730/tvbanner/the-wheel-of-time-61c6d06c1c21c.jpg",
                "lang": "en",
                "likes": "0"
            }
        ],
        "seasonbanner": [
            {
                "id": "176398",
                "url": "http://assets.fanart.tv/fanart/tv/355730/seasonbanner/the-wheel-of-time-64dd80e7dbd9f.jpg",
                "lang": "en",
                "likes": "0",
                "season": "1"
            },
            {
                "id": "176402",
                "url": "http://assets.fanart.tv/fanart/tv/355730/seasonbanner/the-wheel-of-time-64dd85fb4f1b1.jpg",
                "lang": "en",
                "likes": "0",
                "season": "0"
            }
        ]
    }
    """

    # 保护缺图告警去重缓存的并发读写：`cachetools.TTLCache` 非线程安全，
    # 容量与过期时间对齐 Fanart 请求缓存，避免长驻进程内存无限增长。
    _missing_warning_lock = threading.Lock()
    _missing_warning_cache: TTLCache[Tuple[str, str, str, str, str], bool] = TTLCache(
        maxsize=max(settings.CONF.fanart, 1),
        ttl=max(settings.CONF.meta, 1),
    )

    def init_module(self) -> None:
        pass

    def stop(self):
        pass

    def test(self) -> Tuple[bool, str]:
        """
        测试模块连接性
        """
        ret = RequestUtils().get_res("https://webservice.fanart.tv")
        if ret and ret.status_code == 200:
            return True, ""
        elif ret:
            return False, f"无法连接fanart，错误码：{ret.status_code}"
        return False, "fanart网络连接失败"

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        return "FANART_API_KEY", True

    @staticmethod
    def get_name() -> str:
        return "Fanart"

    @staticmethod
    def get_type() -> ModuleType:
        """
        获取模块类型
        """
        return ModuleType.Other

    @staticmethod
    def get_subtype() -> OtherModulesType:
        """
        获取模块子类型
        """
        return OtherModulesType.Fanart

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效
        """
        return 0

    def obtain_images(self, mediainfo: MediaInfo) -> Optional[MediaInfo]:
        """
        获取图片
        :param mediainfo:  识别的媒体信息
        :return: 更新后的媒体信息
        """
        images = self.__obtain_fanart_images(mediainfo=mediainfo)
        if not images:
            return None

        self.__set_mediainfo_images(mediainfo=mediainfo, images=images)
        return mediainfo

    async def async_obtain_images(self, mediainfo: MediaInfo) -> Optional[MediaInfo]:
        """
        获取图片（异步版本）
        :param mediainfo:  识别的媒体信息
        :return: 更新后的媒体信息
        """
        images = await self.__async_obtain_fanart_images(mediainfo=mediainfo)
        if not images:
            return None

        self.__set_mediainfo_images(mediainfo=mediainfo, images=images)
        return mediainfo

    @classmethod
    def __set_mediainfo_images(cls, mediainfo: MediaInfo, images: dict) -> None:
        """
        显式回填 MediaInfo 支持的展示图片字段
        """
        for image_name, image_url in images.items():
            image_attr = cls.__mediainfo_image_attr(image_name)
            if image_attr and not getattr(mediainfo, image_attr, None):
                setattr(mediainfo, image_attr, image_url)
                logger.debug(f"{mediainfo.title_year} 使用 Fanart 图片回填 {image_attr}：{image_name}")

    def metadata_img(
        self,
        mediainfo: MediaInfo,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> Optional[dict]:
        """
        获取图片名称和url
        :param mediainfo: 媒体信息
        :param season: 季号
        :param episode: 集号
        """
        if episode is not None:
            # Fanart 没有集图片
            return None
        return self.__obtain_fanart_images(mediainfo=mediainfo, season=season)

    def __obtain_fanart_images(
        self, mediainfo: MediaInfo, season: Optional[int] = None
    ) -> Optional[dict]:
        """
        获取 Fanart 图片并转换为刮削图片名称
        """
        query = self.__fanart_query(mediainfo=mediainfo)
        if not query:
            return None
        key_token = self.__key_token()
        result = self.__request_fanart(*query, _key_token=key_token)
        return self.__extract_images(
            mediainfo=mediainfo,
            result=result,
            season=season,
            query=query,
            key_token=key_token,
        )

    async def __async_obtain_fanart_images(
        self,
        mediainfo: MediaInfo,
        season: Optional[int] = None,
    ) -> Optional[dict]:
        """
        异步获取 Fanart 图片并转换为刮削图片名称
        """
        query = self.__fanart_query(mediainfo=mediainfo)
        if not query:
            return None
        key_token = self.__key_token()
        result = await self.__async_request_fanart(*query, _key_token=key_token)
        return self.__extract_images(
            mediainfo=mediainfo,
            result=result,
            season=season,
            query=query,
            key_token=key_token,
        )

    @staticmethod
    def __fanart_query(
        mediainfo: MediaInfo,
    ) -> Optional[Tuple[MediaType, Union[str, int], str]]:
        """
        获取 Fanart 查询参数。

        电影使用 TMDB 标识；电视剧优先使用 TVDB 标识，缺失 TVDB 标识时回退使用
        TMDB 标识并标记 ``tmdb`` 来源，避免服务端按默认 TVDB 命名空间返回错误剧集。

        :param mediainfo: 包含媒体类型与外部标识的媒体对象
        :return: ``(media_type, queryid, source)`` 三元组；模块关闭或缺少可用标识时返回 None
        """
        if not settings.FANART_ENABLE:
            return None
        if not mediainfo.tmdb_id and not mediainfo.tvdb_id:
            return None
        if mediainfo.type == MediaType.MOVIE and mediainfo.tmdb_id is not None:
            return MediaType.MOVIE, mediainfo.tmdb_id, "tmdb"
        if mediainfo.tvdb_id:
            return MediaType.TV, mediainfo.tvdb_id, "tvdb"
        if mediainfo.type == MediaType.TV and mediainfo.tmdb_id:
            return MediaType.TV, mediainfo.tmdb_id, "tmdb"
        logger.info(f"{mediainfo.title_year} 没有tvdbid，无法获取fanart图片")
        return None

    @classmethod
    def __missing_warning_key(
        cls,
        mediainfo: MediaInfo,
        query: Optional[Tuple[MediaType, Union[str, int], str]] = None,
        key_token: Optional[str] = None,
    ) -> Tuple[str, str, str, str, str]:
        """
        构造 Fanart 缺图告警的去重键。

        结合媒体类型、标识来源、查询标识、标题年份与当前 API Key 指纹去重；
        区分 TVDB 与 TMDB 来源避免同号碰撞，更换 API Key 后指纹随之变化。

        :param mediainfo: 待提取图片的媒体对象
        :param query: 已解析的 ``(media_type, queryid, source)`` 查询参数；直接调用提取方法时可缺省
        :param key_token: API Key 指纹；缺省时按当前运行时配置计算
        :return: 用于缺图告警去重的稳定五元组键
        """
        if query is not None:
            media_type, queryid, source_key = query
            media_type_key = media_type.value
            query_key = str(queryid)
        elif mediainfo.type == MediaType.MOVIE and mediainfo.tmdb_id is not None:
            media_type_key = MediaType.MOVIE.value
            source_key = "tmdb"
            query_key = str(mediainfo.tmdb_id)
        elif mediainfo.tvdb_id:
            media_type_key = MediaType.TV.value
            source_key = "tvdb"
            query_key = str(mediainfo.tvdb_id)
        elif mediainfo.type == MediaType.TV and mediainfo.tmdb_id:
            media_type_key = MediaType.TV.value
            source_key = "tmdb"
            query_key = str(mediainfo.tmdb_id)
        else:
            media_type_key = mediainfo.type.value if mediainfo.type else ""
            source_key = ""
            query_key = str(mediainfo.tmdb_id or mediainfo.tvdb_id or "")
        title_year = mediainfo.title_year or ""
        token = cls.__key_token() if key_token is None else key_token
        return media_type_key, source_key, query_key, title_year, token

    @classmethod
    def __should_warn_missing(cls, warning_key: Tuple[str, str, str, str, str]) -> bool:
        """
        判断是否应为指定媒体输出 Fanart 缺图告警，并原子登记已告警状态。

        :param warning_key: 由 ``__missing_warning_key`` 生成的去重键
        :return: 缓存有效期内首次命中返回 True，重复命中返回 False
        """
        with cls._missing_warning_lock:
            if warning_key in cls._missing_warning_cache:
                return False
            cls._missing_warning_cache[warning_key] = True
            return True

    @classmethod
    def __clear_missing_warning(cls, warning_key: Tuple[str, str, str, str, str]) -> None:
        """
        在获取到有效 Fanart 响应后移除对应媒体的缺图告警记录。

        :param warning_key: 由 ``__missing_warning_key`` 生成的去重键
        """
        with cls._missing_warning_lock:
            cls._missing_warning_cache.pop(warning_key, None)

    @classmethod
    def __clear_missing_warnings(cls) -> None:
        """
        清空全部 Fanart 缺图告警去重记录。
        """
        with cls._missing_warning_lock:
            cls._missing_warning_cache.clear()

    def __extract_images(
        self,
        mediainfo: MediaInfo,
        result: Optional[dict[str, Any]],
        season: Optional[int] = None,
        *,
        query: Optional[Tuple[MediaType, Union[str, int], str]] = None,
        key_token: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """
        从 Fanart 响应中提取图片名称和地址。

        同一媒体在缺图缓存有效期内仅输出一次告警，避免逐集整理或刮削时重复刷屏。
        """
        warning_key = self.__missing_warning_key(
            mediainfo=mediainfo,
            query=query,
            key_token=key_token,
        )
        if not result or result.get("status") == "error":
            if self.__should_warn_missing(warning_key):
                logger.warning(f"没有获取到 {mediainfo.title_year} 的fanart图片数据")
            return None
        self.__clear_missing_warning(warning_key)

        ret = {}
        # 获取所有图片
        for name, images in result.items():
            if not images:
                continue
            if not isinstance(images, list):
                continue

            # 图片属性xx_path
            image_name = self.__name(name)
            if image_name.startswith("season"):
                image_type = image_name[6:]
                # 季图片，图片格式seasonxx-xxxx/season-specials-xxxx
                for image_obj in images:
                    image_season = image_obj.get("season")
                    if image_season is not None:
                        if season is not None and str(image_season) != str(season):
                            continue
                        # 包括poster,thumb,banner
                        if image_season == "0":
                            season_image = f"season-specials-{image_type}"
                        else:
                            season_image = (
                                f"season{str(image_season).rjust(2, '0')}-{image_type}"
                            )
                        if image_url := image_obj.get("url"):
                            ret.setdefault(
                                f"{season_image}{Path(image_url).suffix}", image_url
                            )
            else:
                if season is not None:
                    continue
                image_obj = self.__pick_best_image(images)
                if image_url := image_obj.get("url"):
                    ret[f"{image_name}{Path(image_url).suffix}"] = image_url

        return ret or None

    @staticmethod
    def __mediainfo_image_attr(image_name: str) -> Optional[str]:
        """
        将 Fanart 刮削图片名映射为 MediaInfo 的显式图片字段
        """
        image_key = Path(image_name).stem
        if image_key == "poster":
            return "poster_path"
        if image_key in ("background", "fanart", "backdrop"):
            return "backdrop_path"
        if image_key == "logo":
            return "logo_path"
        return None

    @staticmethod
    def __pick_best_image(_images: list):
        """
        其他图片，优先环境变量指定语言，再like最多
        """
        lang_env = settings.FANART_LANG
        if lang_env:
            langs = [lang.strip() for lang in lang_env.split(",") if lang.strip()]
            for lang in langs:
                lang_images = [img for img in _images if img.get("lang") == lang]
                if lang_images:
                    lang_images.sort(key=lambda x: int(x.get("likes", 0)), reverse=True)
                    return lang_images[0]
        # 没设置或没找到，按原逻辑 zh、en、like最多
        zh_images = [img for img in _images if img.get("lang") == "zh"]
        if zh_images:
            zh_images.sort(key=lambda x: int(x.get("likes", 0)), reverse=True)
            return zh_images[0]
        en_images = [img for img in _images if img.get("lang") == "en"]
        if en_images:
            en_images.sort(key=lambda x: int(x.get("likes", 0)), reverse=True)
            return en_images[0]
        _images.sort(key=lambda x: int(x.get("likes", 0)), reverse=True)
        return _images[0]

    _FANART_NAME_MAP = {
        "showbackground": "fanart",
        "moviebackground": "fanart",
        "hdtvlogo": "logo",
        "hdmovielogo": "logo",
        "movielogo": "logo",
        "tvposter": "poster",
        "movieposter": "poster",
        "tvthumb": "thumb",
        "moviethumb": "thumb",
        "tvbanner": "banner",
        "moviebanner": "banner",
        "hdclearart": "clearart",
        "movieart": "clearart",
        "hdmovieclearart": "clearart",
        "cdart": "cdart",
        "moviedisc": "disc",
        "seasonposter": "seasonposter",
        "seasonthumb": "seasonthumb",
        "seasonbanner": "seasonbanner",
    }

    @classmethod
    def __name(cls, fanart_name: str) -> str:
        """
        转换Fanart图片的名字为媒体服务器兼容名称
        """
        return cls._FANART_NAME_MAP.get(fanart_name.lower(), fanart_name)

    @classmethod
    @cached(maxsize=settings.CONF.fanart, ttl=settings.CONF.meta, shared_key="get")
    def __request_fanart(
        cls,
        media_type: MediaType,
        queryid: Union[str, int],
        source: str = "",
        _key_token: str = "",
    ) -> Optional[dict[str, Any]]:
        """
        查询 Fanart 图集并使用标识来源与密钥指纹隔离缓存。
        """
        image_url = cls.__fanart_url(
            media_type=media_type, queryid=queryid, source=source
        )
        try:
            ret = RequestUtils(proxies=settings.PROXY, timeout=10).get_res(
                image_url, raise_exception=True
            )
            if ret:
                return ret.json()
            logger.debug(f"未能获取到 {queryid} 的Fanart图片")
            return {}
        except Exception as err:
            logger.error(f"获取{queryid}的Fanart图片失败：{str(err)}")
            return None

    @classmethod
    @cached(maxsize=settings.CONF.fanart, ttl=settings.CONF.meta, shared_key="get")
    async def __async_request_fanart(
        cls,
        media_type: MediaType,
        queryid: Union[str, int],
        source: str = "",
        _key_token: str = "",
    ) -> Optional[dict[str, Any]]:
        """
        异步查询 Fanart 图集并使用标识来源与密钥指纹隔离缓存。
        """
        image_url = cls.__fanart_url(
            media_type=media_type, queryid=queryid, source=source
        )
        try:
            ret = await AsyncRequestUtils(proxies=settings.PROXY, timeout=10).get_json(
                image_url
            )
            if ret:
                return ret
            logger.debug(f"未能获取到 {queryid} 的Fanart图片")
            return {}
        except Exception as err:
            logger.error(f"获取{queryid}的Fanart图片失败：{str(err)}")
            return None

    @staticmethod
    def __key_token() -> str:
        """
        计算当前 Fanart API Key 的稳定摘要指纹，避免在缓存键中明文存储密钥。
        """
        api_key = str(settings.FANART_API_KEY or "")
        return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def __fanart_url(
        cls,
        media_type: MediaType,
        queryid: Union[str, int],
        source: str = "",
    ) -> str:
        """
        生成 Fanart 请求地址。

        电视剧回退使用 TMDB 标识时附带 ``source=tmdb``，避免与同号 TVDB 剧集冲突。
        """
        api_key = settings.FANART_API_KEY
        if media_type == MediaType.MOVIE:
            return f"https://webservice.fanart.tv/v3/movies/{queryid}?api_key={api_key}"
        url = f"https://webservice.fanart.tv/v3/tv/{queryid}?api_key={api_key}"
        if source == "tmdb":
            return f"{url}&source=tmdb"
        return url

    def clear_cache(self):
        """
        清除缓存与缺图告警去重记录
        """
        logger.info(f"开始清除{self.get_name()}缓存 ...")
        self.__clear_missing_warnings()
        self.__request_fanart.cache_clear()
        async_cache_clear = self.__async_request_fanart.cache_clear()
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(async_cache_clear)
        except RuntimeError:
            asyncio.run(async_cache_clear)
        logger.info(f"{self.get_name()}缓存清除完成")
