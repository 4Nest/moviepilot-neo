"""补充 v2 资源目录尚未收录的站点，认证和流控仍由原资源帮助器负责。"""

import hashlib
import importlib.util
import json
import sys
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Optional, Tuple

from app.core.config import settings
from app.db.models.site import Site
from app.db.site_oper import SiteOper
from app.helper.sites import SitesHelper as ResourceSitesHelper
from app.log import logger
from app.utils.string import StringUtils

_TEMPLATE_KEYS = (
    "category",
    "torrents",
    "search",
    "batch",
    "browse",
    "subtitles",
    "result_num",
)

SITE_SUPPLEMENTS = {
    "colapt.com": {
        "id": "colapt", "name": "Cola", "url": "https://www.colapt.com/",
        "domain": "https://www.colapt.com/", "public": False,
        "parser": "mTorrent", "schema": "MTorrent", "language": "zh",
        "subtitles": True,
    },
    "uhddolby.com": {
        "id": "hddolby", "name": "高清杜比", "url": "https://www.uhddolby.com/",
        "domain": "https://www.uhddolby.com/", "public": False,
        "parser": "HDDolby", "schema": "HDDolby", "language": "zh",
        "subtitles": True,
        "alias_of": "hddolby.com",
    },
    "hdwing.org": {
        "id": "hdwing", "name": "HDWinG", "url": "https://hdwing.org/",
        "domain": "https://hdwing.org/", "public": False,
        "parser": "HDWing", "schema": "NexusPhp", "language": "zh",
        "encoding": "UTF-8",
        "search": {
            "paths": [{"path": "torrents.php", "method": "get"}],
            "params": {"search": "{keyword}", "search_area": 4},
            "batch": {"delimiter": " ", "space_replace": "_"},
        },
        "category": {
            "movie": [
                {"id": 20, "cat": "Movies/BluRay", "desc": "Movie Full BD"},
                {"id": 17, "cat": "Movies/HD", "desc": "Movie 1080p"},
                {"id": 16, "cat": "Movies/HD", "desc": "Movie 1080i"},
                {"id": 9, "cat": "Movies/HD", "desc": "Movie 720p"},
                {"id": 27, "cat": "Movies/SD", "desc": "iPad Video"},
                {"id": 410, "cat": "Movies/UHD", "desc": "4K UltraHD"},
                {"id": 14, "cat": "Anime", "desc": "Animation"},
                {"id": 5, "cat": "Documentary", "desc": "Documentary"},
            ],
            "tv": [
                {"id": 13, "cat": "TV", "desc": "EU/US TV series"},
                {"id": 25, "cat": "TV", "desc": "Chinese TV series"},
                {"id": 26, "cat": "TV", "desc": "Kor Drama"},
                {"id": 24, "cat": "TV", "desc": "Jpn Drama"},
                {"id": 21, "cat": "TV", "desc": "EU/US TV series pack"},
                {"id": 22, "cat": "TV", "desc": "Chinese TV series pack"},
                {"id": 23, "cat": "TV", "desc": "JPN/KOR drama pack"},
                {"id": 5, "cat": "Documentary", "desc": "Documentary"},
                {"id": 14, "cat": "Anime", "desc": "Animation"},
                {"id": 401, "cat": "TV", "desc": "TV Shows"},
            ],
        },
        "subtitles": {
            "search": {
                "paths": [{"path": "subtitles.php?search={keyword}&lang_id=0", "method": "get"}],
            },
            "list": {"selector": "tr:has(td.rowfollow)"},
            "fields": {
                "language": {"selector": "td:nth-child(1) img", "attribute": "title"},
                "language_icon": {"selector": "td:nth-child(1) img", "attribute": "src"},
                "title": {"selector": "td:nth-child(2) a"},
                "download": {"selector": "td:nth-child(2) a", "attribute": "href"},
                "date_added": {"selector": "td:nth-child(3) span", "attribute": "title", "optional": True},
                "date_elapsed": {"selector": "td:nth-child(3) span", "optional": True},
                "size": {"selector": "td:nth-child(4)"},
                "grabs": {"selector": "td:nth-child(5)"},
                "uploader": {"selector": "td:nth-child(6)"},
                "report": {"selector": "td:nth-child(7) a", "attribute": "href"},
            },
        },
        "torrents": {
            "list": {"selector": 'table.torrent_list tr:has("td.t_cat")'},
            "fields": {
                "id": {
                    "selector": 'a[href*="details.php?id="]',
                    "attribute": "href",
                    "filters": [{"name": "re_search", "args": ["\\d+", 0]}],
                },
                "title": {"selector": 'a[href*="details.php?id="]', "attribute": "title"},
                "description": {"selector": "table.tbname tr > td:nth-child(2) > h4"},
                "labels": {"selector": "table.tbname tr > td:nth-child(2) > h4 > span"},
                "category": {
                    "selector": 'a[href*="?cat="]',
                    "attribute": "href",
                    "filters": [{"name": "querystring", "args": "cat"}],
                },
                "details": {"selector": 'a[href*="details.php?id="]', "attribute": "href"},
                "download": {"selector": 'a[href*="download.php?hash="]', "attribute": "href"},
                "size": {"selector": "td.t_size"},
                "grabs": {"selector": "td.t_completed"},
                "seeders": {"selector": "td.t_torrents"},
                "leechers": {"selector": "td.t_leech"},
                "date_elapsed": {"selector": "td.t_time > span"},
                "date_added": {"selector": "td.t_time > span", "attribute": "title"},
                "csrf": {"selector": 'meta[name="x-csrf"]', "attribute": "content"},
                "downloadvolumefactor": {
                    "case": {
                        "img.pro_free": 0,
                        "img.pro_free2up": 0,
                        "img.pro_50pctdown": 0.5,
                        "img.pro_50pctdown2up": 0.5,
                        "img.pro_30pctdown": 0.3,
                        "*": 1,
                    }
                },
                "uploadvolumefactor": {
                    "case": {
                        "img.pro_50pctdown2up": 2,
                        "img.pro_free2up": 2,
                        "img.pro_2up": 2,
                        "*": 1,
                    }
                },
                "freedate": {
                    "selector": "span.sp_state_placeholder",
                    "optional": True,
                },
                "hr": {"selector": "img.hitandrun"},
            },
        },
    },
    "piggo.me": {
        "id": "piggo", "name": "猪猪", "url": "https://piggo.me/",
        "domain": "https://piggo.me/", "public": False,
        "parser": "Piggo", "schema": "Piggo", "language": "zh",
        "api_url": "https://api.piggo.me/v1/api",
    },
    "hxpt.org": {
        "id": "hxpt", "name": "好学", "url": "https://www.hxpt.org/",
        "domain": "https://www.hxpt.org/", "public": False,
        "parser": "FluxTorrent", "schema": "FluxTorrent", "language": "zh",
    },
}

_OVERRIDE_PROTOCOL_KEYS = ("parser", "schema", "language", "subtitles")

SITE_OVERRIDES = {
    "piggo.me": {
        "id": "piggo", "name": "猪猪", "url": "https://piggo.me/",
        "domain": "https://piggo.me/", "public": False,
        "parser": "Piggo", "schema": "Piggo", "language": "zh",
        "api_url": "https://api.piggo.me/v1/api",
    },
    "hxpt.org": {
        "id": "hxpt", "name": "好学", "url": "https://www.hxpt.org/",
        "domain": "https://www.hxpt.org/", "public": False,
        "parser": "FluxTorrent", "schema": "FluxTorrent", "language": "zh",
    },
    "hdarea.club": {
        "id": "HDarea",
        "name": "高清视界",
        "url": "https://hdarea.club/",
        "domain": "https://hdarea.club/",
        "public": False,
        "parser": "HDArea",
        "schema": "HDArea",
        "language": "zh",
        "subtitles": True,
        "category": {
            "movie": [
                {"id": 300, "cat": "Movies/UHD", "desc": "Movies UHD-4K"},
                {"id": 401, "cat": "Movies/BluRay", "desc": "Movies Blu-ray"},
                {"id": 415, "cat": "Movies/HD", "desc": "Movies REMUX"},
                {"id": 416, "cat": "Movies/3D", "desc": "Movies 3D"},
                {"id": 410, "cat": "Movies/HD", "desc": "Movies 1080p"},
                {"id": 411, "cat": "Movies/HD", "desc": "Movies 720p"},
                {"id": 414, "cat": "Movies/DVD", "desc": "Movies DVD"},
                {"id": 412, "cat": "Movies/WEB-DL", "desc": "Movies WEB-DL"},
                {"id": 413, "cat": "Movies/HD", "desc": "Movies HDTV"},
                {"id": 417, "cat": "Movies/Other", "desc": "Movies iPad"},
                {"id": 405, "cat": "Anime", "desc": "Animations"},
                {"id": 404, "cat": "Documentary", "desc": "Documentaries"},
            ],
            "tv": [
                {"id": 404, "cat": "Documentary", "desc": "Documentaries"},
                {"id": 405, "cat": "Anime", "desc": "Animations"},
                {"id": 402, "cat": "TV", "desc": "TV Series"},
                {"id": 403, "cat": "TV", "desc": "TV Shows"},
            ],
        },
    },
}


CIRCUIT_BREAKER_THRESHOLD = 3
CIRCUIT_BREAKER_COOLDOWN_SECONDS = 1800


class SiteHealthSentinel:
    """以规范域名维护线程安全的站点连续失败、熔断窗口和告警状态。"""

    CIRCUIT_BREAKER_THRESHOLD = CIRCUIT_BREAKER_THRESHOLD
    CIRCUIT_BREAKER_COOLDOWN_SECONDS = CIRCUIT_BREAKER_COOLDOWN_SECONDS
    _states: dict[str, dict[str, Any]] = {}
    _lock = RLock()

    @classmethod
    def _normalize_domain(cls, domain: str) -> str:
        """将站点地址统一为熔断状态使用的注册域名。"""
        return StringUtils.get_url_domain(domain).lower() if domain else ""

    @staticmethod
    def _format_timestamp(timestamp: Optional[float]) -> Optional[str]:
        """将熔断截止时间转换为前端可直接展示的本地时间。"""
        if timestamp is None:
            return None
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")

    @classmethod
    def record_success(cls, domain: str) -> None:
        """清除站点全部运行时异常状态，表示熔断探测或站点配置已恢复。"""
        normalized_domain = cls._normalize_domain(domain)
        if not normalized_domain:
            return
        with cls._lock:
            cls._states.pop(normalized_domain, None)

    @classmethod
    def record_failure(
        cls,
        domain: str,
        error_reason: Optional[str] = None,
        auth_failed: bool = False,
        count_failure: bool = True,
        alert_requested: bool = False,
    ) -> bool:
        """
        记录站点失败并按阈值开启熔断，返回本次是否应发送健康告警。

        :param domain: 站点地址或规范域名
        :param error_reason: 可供用户排查的最近异常原因
        :param auth_failed: 是否属于 Cookie、API Key 或权限等鉴权异常
        :param count_failure: 数据库统计入口已计数时可关闭重复递增
        :param alert_requested: 调用方将实际发送告警时记录告警时间
        :return: 满足告警条件且本轮连续故障尚未发送过告警时返回 True
        """
        normalized_domain = cls._normalize_domain(domain)
        if not normalized_domain:
            return False
        now = time.time()
        with cls._lock:
            state = cls._states.setdefault(normalized_domain, {
                "consecutive_fails": 0,
                "circuit_until_ts": None,
                "last_error": None,
                "alerted": False,
            })
            if count_failure:
                state["consecutive_fails"] += 1
            if error_reason:
                state["last_error"] = str(error_reason)

            consecutive_fails = state["consecutive_fails"]
            circuit_until_ts = state["circuit_until_ts"]
            if consecutive_fails >= cls.CIRCUIT_BREAKER_THRESHOLD:
                if count_failure or not circuit_until_ts or now >= circuit_until_ts:
                    state["circuit_until_ts"] = now + cls.CIRCUIT_BREAKER_COOLDOWN_SECONDS

            should_alert = auth_failed or consecutive_fails >= cls.CIRCUIT_BREAKER_THRESHOLD
            alert_due = should_alert and not state["alerted"]
            if alert_due and alert_requested:
                state["alerted"] = True
            return alert_due

    @classmethod
    def is_circuit_open(cls, domain: str) -> Tuple[bool, Optional[str]]:
        """
        判断站点是否处于熔断期；冷却到期后清除窗口并保留一次半开试探机会。

        :param domain: 站点地址或规范域名
        :return: 是否拦截请求，以及可供展示的熔断恢复时间
        """
        normalized_domain = cls._normalize_domain(domain)
        if not normalized_domain:
            return False, None
        now = time.time()
        with cls._lock:
            state = cls._states.get(normalized_domain)
            if not state:
                return False, None
            circuit_until_ts = state["circuit_until_ts"]
            if circuit_until_ts is None:
                return False, None
            if now < circuit_until_ts:
                return True, cls._format_timestamp(circuit_until_ts)
            state["circuit_until_ts"] = None
            state["consecutive_fails"] = cls.CIRCUIT_BREAKER_THRESHOLD - 1
            return False, None

    @classmethod
    def get_status(cls, domain: str) -> dict[str, Any]:
        """返回可合并到站点统计响应的实时熔断快照，不修改数据库耗时记录。"""
        normalized_domain = cls._normalize_domain(domain)
        now = time.time()
        with cls._lock:
            state = cls._states.get(normalized_domain)
            if not state:
                return {
                    "consecutive_fails": 0,
                    "circuit_open": False,
                    "circuit_until": None,
                    "last_error": None,
                }
            circuit_until_ts = state["circuit_until_ts"]
            circuit_open = circuit_until_ts is not None and now < circuit_until_ts
            return {
                "consecutive_fails": state["consecutive_fails"],
                "circuit_open": circuit_open,
                "circuit_until": cls._format_timestamp(circuit_until_ts) if circuit_open else None,
                "last_error": state["last_error"],
            }

    @classmethod
    def active_domains(cls) -> list[str]:
        """返回当前存在内存异常或熔断记录的所有规范域名。"""
        with cls._lock:
            return list(cls._states.keys())

    @classmethod
    def reset_for_tests(cls) -> None:
        """清空全部站点状态，为隔离单元测试提供确定的初始环境。"""
        with cls._lock:
            cls._states.clear()


class SiteResourceRegistry:
    """管理 NEO 站点热更新 JSON 配置与经 SHA-256 校验的动态适配器模块。"""

    BUILTIN_RESOURCE_VERSION = "1.0.1"
    _lock = RLock()
    _cached_signature: Optional[Tuple[Optional[int], Optional[int]]] = None
    _manifest: dict[str, Any] = {}
    _config: dict[str, Any] = {}
    _loaded_modules: dict[str, Tuple[str, Any]] = {}

    @classmethod
    def resources_dir(cls) -> Path:
        """返回本地热更站点资源根目录。"""
        return Path(settings.CONFIG_PATH) / "sites" / "resources"

    @classmethod
    def manifest_path(cls) -> Path:
        """返回本地热更清单文件路径。"""
        return cls.resources_dir() / "manifest.json"

    @classmethod
    def sites_json_path(cls) -> Path:
        """返回本地热更站点定义 JSON 路径。"""
        return cls.resources_dir() / "neo.sites.json"

    @staticmethod
    def _file_sha256(path: Path) -> str:
        """计算本地文件的 SHA-256 摘要。"""
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @classmethod
    def _safe_resolve(cls, rel_target: str) -> Optional[Path]:
        """将清单相对路径解析到受控资源目录内，拒绝目录穿越。"""
        if not rel_target or ".." in rel_target or rel_target.startswith(("/", "\\")):
            return None
        base_dir = cls.resources_dir().resolve()
        candidate = (base_dir / rel_target).resolve()
        if base_dir not in candidate.parents:
            return None
        return candidate

    @classmethod
    def _ensure_loaded_locked(cls) -> None:
        """按清单与配置文件修改时间惰性刷新内存缓存。"""
        m_path = cls.manifest_path()
        s_path = cls.sites_json_path()
        m_mtime = m_path.stat().st_mtime_ns if m_path.is_file() else None
        s_mtime = s_path.stat().st_mtime_ns if s_path.is_file() else None
        sig = (m_mtime, s_mtime)
        if cls._cached_signature == sig:
            return
        cls._cached_signature = sig
        cls._manifest = {}
        cls._config = {}
        if not m_path.is_file() or not s_path.is_file():
            return
        try:
            manifest = json.loads(m_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                return
            manifest_ver = str(manifest.get("version") or "").strip()
            if not manifest_ver or StringUtils.compare_version(
                manifest_ver, "<", cls.BUILTIN_RESOURCE_VERSION
            ):
                return
            files_map = manifest.get("files") or {}
            expected_json_hash = files_map.get("neo.sites.json")
            if not expected_json_hash or cls._file_sha256(s_path) != expected_json_hash:
                logger.warning("本地 neo.sites.json 校验和不匹配，忽略该热更配置并回退内置配置")
                return
            config = json.loads(s_path.read_text(encoding="utf-8"))
            if not isinstance(config, dict):
                return
            cls._manifest = manifest
            cls._config = config
        except Exception as err:
            logger.warning(f"加载本地热更站点配置失败，回退内置配置：{err}")

    @classmethod
    def reload(cls) -> None:
        """强制清空缓存并重新加载磁盘上的最新热更站点配置与适配器。"""
        with cls._lock:
            cls._cached_signature = None
            cls._manifest = {}
            cls._config = {}
            cls._loaded_modules.clear()
            cls._ensure_loaded_locked()

    @classmethod
    def reset_for_tests(cls) -> None:
        """在单元测试间隔离动态资源缓存。"""
        with cls._lock:
            cls._cached_signature = None
            cls._manifest = {}
            cls._config = {}
            cls._loaded_modules.clear()

    @classmethod
    def current_version(cls) -> Optional[str]:
        """返回当前已生效的本地热更资源包版本号。"""
        with cls._lock:
            cls._ensure_loaded_locked()
            version = cls._manifest.get("version") or cls._config.get("version")
            return str(version) if version else None

    @classmethod
    def _load_verified_attr_locked(cls, rel_module: str, attr_name: str) -> Any:
        """校验模块文件 SHA-256 与 manifest 一致后动态导入并返回指定属性。"""
        if not rel_module or not attr_name:
            return None
        files_map = cls._manifest.get("files") or {}
        expected_hash = files_map.get(rel_module)
        if not expected_hash:
            logger.warning(f"热更适配器模块未在 manifest.json 中登记：{rel_module}")
            return None
        mod_path = cls._safe_resolve(rel_module)
        if not mod_path or not mod_path.is_file():
            return None
        try:
            actual_hash = cls._file_sha256(mod_path)
        except OSError:
            return None
        if actual_hash != expected_hash:
            logger.warning(f"热更适配器模块 SHA-256 校验失败，拒绝加载：{rel_module}")
            return None

        cached = cls._loaded_modules.get(rel_module)
        if cached and cached[0] == expected_hash:
            return getattr(cached[1], attr_name, None)

        safe_slug = "".join(ch if ch.isalnum() else "_" for ch in rel_module)
        mod_name = f"_mp_neo_site_adapter_{safe_slug}_{expected_hash[:12]}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, str(mod_path))
            if not spec or not spec.loader:
                return None
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
            cls._loaded_modules[rel_module] = (expected_hash, module)
            return getattr(module, attr_name, None)
        except Exception as err:
            sys.modules.pop(mod_name, None)
            logger.error(f"加载热更站点适配器失败 ({rel_module}:{attr_name})：{err}")
            return None

    @classmethod
    def get_supplements(cls) -> dict:
        """返回合并热更补充站点后的字典（热更优先，内置兜底）。"""
        with cls._lock:
            cls._ensure_loaded_locked()
            hot = cls._config.get("supplements")
            if isinstance(hot, dict) and hot:
                return {**SITE_SUPPLEMENTS, **hot}
            return SITE_SUPPLEMENTS

    @classmethod
    def get_overrides(cls) -> dict:
        """返回合并热更协议覆盖后的字典（热更优先，内置兜底）。"""
        with cls._lock:
            cls._ensure_loaded_locked()
            hot = cls._config.get("overrides")
            if isinstance(hot, dict) and hot:
                return {**SITE_OVERRIDES, **hot}
            return SITE_OVERRIDES

    @classmethod
    def get_rss_link_conf(cls) -> dict:
        """返回热更配置中的自定义站点 RSS 页面提取规则。"""
        with cls._lock:
            cls._ensure_loaded_locked()
            hot = cls._config.get("rss_link_conf")
            return deepcopy(hot) if isinstance(hot, dict) else {}

    @classmethod
    def get_apikey_rss_templates(cls) -> dict[str, str]:
        """返回支持通过 API Key 直接拼接 RSS 地址的站点模板映射。"""
        defaults = {"hdarea.club": "torrentrss.php?rows=50&linktype=dl&apikey={apikey}"}
        with cls._lock:
            cls._ensure_loaded_locked()
            hot = cls._config.get("apikey_rss_templates")
            if isinstance(hot, dict) and hot:
                return {**defaults, **{str(k): str(v) for k, v in hot.items() if k and v}}
            return defaults

    @classmethod
    def get_special_site_test_aliases(cls) -> dict[str, str]:
        """返回热更配置声明的特殊连通性测试复用域名别名映射。"""
        with cls._lock:
            cls._ensure_loaded_locked()
            hot = cls._config.get("special_site_tests")
            if isinstance(hot, dict):
                return {str(k).lower(): str(v).lower() for k, v in hot.items() if k and v}
            return {}

    @classmethod
    def get_spider_class(cls, parser_name: Optional[str]) -> Any:
        """根据站点 parser 标识获取已通过校验的热更 Spider 类。"""
        if not parser_name:
            return None
        with cls._lock:
            cls._ensure_loaded_locked()
            adapters = cls._config.get("adapters")
            if not isinstance(adapters, dict):
                return None
            spec = adapters.get(parser_name)
            if not isinstance(spec, dict):
                return None
            return cls._load_verified_attr_locked(
                spec.get("spider_module") or "",
                spec.get("spider_class") or "",
            )

    @classmethod
    def get_parser_class(cls, schema_name: Optional[str]) -> Any:
        """根据站点 schema 标识获取已通过校验的热更用户数据解析器类。"""
        if not schema_name:
            return None
        with cls._lock:
            cls._ensure_loaded_locked()
            adapters = cls._config.get("adapters")
            if not isinstance(adapters, dict):
                return None
            spec = adapters.get(schema_name)
            if not isinstance(spec, dict):
                return None
            return cls._load_verified_attr_locked(
                spec.get("parser_module") or "",
                spec.get("parser_class") or "",
            )

    @classmethod
    def get_site_test_func(cls, domain: Optional[str]) -> Optional[Callable]:
        """根据规范域名获取热更适配器提供的站点连通性测试函数。"""
        if not domain:
            return None
        norm_domain = StringUtils.get_url_domain(domain).lower()
        with cls._lock:
            cls._ensure_loaded_locked()
            adapters = cls._config.get("adapters")
            if not isinstance(adapters, dict):
                return None
            for spec in adapters.values():
                if not isinstance(spec, dict):
                    continue
                domains = [str(d).lower() for d in (spec.get("domains") or [])]
                if norm_domain in domains and spec.get("test_module") and spec.get("test_func"):
                    return cls._load_verified_attr_locked(
                        spec["test_module"],
                        spec["test_func"],
                    )
            return None

class SitesHelper:
    """合并原站点目录与 NEO 补充目录，不修改随资源更新覆盖的二进制文件。"""

    def __init__(self) -> None:
        self._resource = ResourceSitesHelper()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resource, name)

    def get_indexsites(self) -> dict:
        """返回可添加站点目录，上游已收录的条目优先。"""
        sites = dict(self._resource.get_indexsites())
        overrides_map = SiteResourceRegistry.get_overrides()
        supplements_map = SiteResourceRegistry.get_supplements()
        overrides = {
            domain: {key: value for key, value in item.items() if key in {"id", "name", "url", "public"}}
            for domain, item in overrides_map.items()
        }
        supplements = {
            domain: {key: value for key, value in item.items() if key in {"id", "name", "url", "public"}}
            for domain, item in supplements_map.items()
        }
        return {**overrides, **supplements, **sites}

    @staticmethod
    def _apply_override(domain: str, entry: dict) -> dict:
        """对资源目录中已改版为 API 的旧站点应用协议字段覆盖，保留数据库实时配置。"""
        override = SiteResourceRegistry.get_overrides().get(domain)
        if not override:
            return entry
        merged = deepcopy(entry)
        for key in _OVERRIDE_PROTOCOL_KEYS:
            if key in override:
                merged[key] = deepcopy(override[key])
        if not merged.get("category") and override.get("category"):
            merged["category"] = deepcopy(override["category"])
        return merged

    @staticmethod
    def _build_indexer(
        domain: str,
        site: Optional[Site] = None,
        base: Optional[dict] = None,
        source_map: Optional[dict] = None,
    ) -> dict:
        catalog_map = source_map if source_map is not None else SiteResourceRegistry.get_supplements()
        entry = deepcopy(catalog_map[domain])
        entry.pop("alias_of", None)
        if isinstance(base, dict):
            for key in _TEMPLATE_KEYS:
                if base.get(key):
                    entry[key] = deepcopy(base[key])
        if site:
            entry.update(site.to_dict())
            # 搜索器的 domain 是完整站点地址，数据库 domain 则是注册域名。
            entry["domain"] = site.url
        return entry

    def get_indexer(self, domain: str) -> dict:
        """获取站点定义及实时配置；补充与覆盖目录同样要求通过用户认证。"""
        norm_domain = StringUtils.get_url_domain(domain)
        indexer = self._resource.get_indexer(domain)
        if indexer:
            return self._apply_override(norm_domain, indexer)
        if self._resource.auth_level < 2:
            return {}
        overrides_map = SiteResourceRegistry.get_overrides()
        if norm_domain in overrides_map:
            return self._build_indexer(
                norm_domain,
                SiteOper().get_by_domain(norm_domain),
                source_map=overrides_map,
            )
        supplements_map = SiteResourceRegistry.get_supplements()
        if norm_domain not in supplements_map:
            return {}
        alias_of = supplements_map[norm_domain].get("alias_of")
        base = self._resource.get_indexer(alias_of) if alias_of else None
        return self._build_indexer(
            norm_domain,
            SiteOper().get_by_domain(norm_domain),
            base=base,
            source_map=supplements_map,
        )

    async def async_get_indexer(self, domain: str) -> dict:
        """异步获取站点定义及实时配置。"""
        norm_domain = StringUtils.get_url_domain(domain)
        indexer = await self._resource.async_get_indexer(domain)
        if indexer:
            return self._apply_override(norm_domain, indexer)
        if self._resource.auth_level < 2:
            return {}
        overrides_map = SiteResourceRegistry.get_overrides()
        if norm_domain in overrides_map:
            return self._build_indexer(
                norm_domain,
                await SiteOper().async_get_by_domain(norm_domain),
                source_map=overrides_map,
            )
        supplements_map = SiteResourceRegistry.get_supplements()
        if norm_domain not in supplements_map:
            return {}
        alias_of = supplements_map[norm_domain].get("alias_of")
        base = await self._resource.async_get_indexer(alias_of) if alias_of else None
        return self._build_indexer(
            norm_domain,
            await SiteOper().async_get_by_domain(norm_domain),
            base=base,
            source_map=supplements_map,
        )

    def _merge_indexers(
        self,
        indexers: list[dict],
        sites: list[Site],
        bases: Optional[dict[str, dict]] = None,
    ) -> list[dict]:
        overrides_map = SiteResourceRegistry.get_overrides()
        supplements_map = SiteResourceRegistry.get_supplements()
        upgraded = [
            self._apply_override(
                StringUtils.get_url_domain(item.get("domain") or item.get("url")),
                item,
            )
            for item in indexers
        ]
        domains = {StringUtils.get_url_domain(item.get("domain") or item.get("url")) for item in upgraded}
        extra_entries = []
        for site in sites:
            if not site.is_active or site.domain in domains:
                continue
            if site.domain in overrides_map:
                extra_entries.append(
                    self._build_indexer(site.domain, site, source_map=overrides_map)
                )
            elif site.domain in supplements_map:
                extra_entries.append(
                    self._build_indexer(
                        site.domain,
                        site,
                        base=(bases or {}).get(site.domain),
                        source_map=supplements_map,
                    )
                )
        return sorted([*upgraded, *extra_entries], key=lambda item: item.get("pri") or 0)

    def get_indexers(self) -> list[dict]:
        """返回已启用站点，未添加或已停用的补充站点不参与搜索和订阅。"""
        indexers = self._resource.get_indexers()
        if self._resource.auth_level < 2:
            return indexers
        supplements_map = SiteResourceRegistry.get_supplements()
        sites = SiteOper().list_active()
        bases = {
            site.domain: self._resource.get_indexer(supplements_map[site.domain]["alias_of"])
            for site in sites
            if site.domain in supplements_map and supplements_map[site.domain].get("alias_of")
        }
        return self._merge_indexers(indexers, sites, bases=bases)

    async def async_get_indexers(self) -> list[dict]:
        """异步返回已启用站点。"""
        indexers = await self._resource.async_get_indexers()
        if self._resource.auth_level < 2:
            return indexers
        supplements_map = SiteResourceRegistry.get_supplements()
        sites = await SiteOper().async_list_active()
        bases = {
            site.domain: await self._resource.async_get_indexer(supplements_map[site.domain]["alias_of"])
            for site in sites
            if site.domain in supplements_map and supplements_map[site.domain].get("alias_of")
        }
        return self._merge_indexers(indexers, sites, bases=bases)
