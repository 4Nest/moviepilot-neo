"""
词表远程同步:从多个远程源拉取词表配置,与本地词表合并生效(追加,不覆盖本地编辑)。

支持两类链接:
  仓库链接  https://github.com/<owner>/<repo>[/tree/<branch>]
            -> 拉取 Words/ 下 4 个标准词表文件(受源的词表开关过滤)
  文件链接  https://github.com/<o>/<r>/blob/<branch>/Words/CustomIdentifiers.txt
            或 https://raw.githubusercontent.com/<o>/<r>/<branch>/Words/CustomIdentifiers.txt
            -> 按文件名识别词表,仅同步该词表

同步到的内容按源存放在 SyncedWords(远程词表),与本地词表(用户手编辑)
在消费方读取时合并,远程内容不出现在主编辑框。
"""
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.chain import ChainBase
from app.db.systemconfig_oper import SystemConfigOper
from app.log import logger
from app.schemas.types import SystemConfigKey
from app.utils.http import RequestUtils

# 标准词表文件名 -> 系统设置键
WORDS_SYNC_FILES: Dict[str, SystemConfigKey] = {
    "CustomIdentifiers.txt": SystemConfigKey.CustomIdentifiers,
    "CustomReleaseGroups.txt": SystemConfigKey.CustomReleaseGroups,
    "Customization.txt": SystemConfigKey.Customization,
    "TransferExcludeWords.txt": SystemConfigKey.TransferExcludeWords,
}
# 仓库内固定目录
WORDS_SYNC_DIR = "Words"

DEFAULT_SYNC_INTERVAL_DAYS = 7

# 词表键 -> 前端词表标识(用于 tables 开关与前端展示)
WORDS_TABLE_IDS: Dict[str, SystemConfigKey] = {
    "identifiers": SystemConfigKey.CustomIdentifiers,
    "releaseGroups": SystemConfigKey.CustomReleaseGroups,
    "customization": SystemConfigKey.Customization,
    "excludeWords": SystemConfigKey.TransferExcludeWords,
}


class WordsSyncChain(ChainBase):
    """词表远程同步链(多源、追加模式)。"""

    # ---------- 链接解析 ----------

    @staticmethod
    def _resolve_repo_base(url: str) -> Optional[str]:
        """仓库链接 -> raw 根地址;非仓库链接返回 None。"""
        match = re.match(r"^https?://github\.com/([^/]+)/([^/]+?)(?:/tree/([^/]+))?/?$", url)
        if not match:
            return None
        owner, repo, branch = match.group(1), match.group(2), match.group(3) or "main"
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}"

    @staticmethod
    def _resolve_file_url(url: str) -> Optional[Tuple[str, str]]:
        """
        单文件链接 -> (raw 文件地址, 词表键值);无法识别返回 None。
        支持 github blob 链接与 raw 链接,文件名须为标准词表文件名。
        """
        match = re.match(r"^https?://github\.com/([^/]+)/([^/]+?)/blob/([^/]+)/(.+)$", url)
        if match:
            owner, repo, branch, path = match.groups()
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
        elif re.match(r"^https?://raw\.githubusercontent\.com/", url):
            raw_url = url
        else:
            return None
        filename = raw_url.rsplit("/", 1)[-1]
        config_key = WORDS_SYNC_FILES.get(filename)
        if not config_key:
            return None
        return raw_url, config_key.value

    # ---------- 设置存取 ----------

    @classmethod
    def get_sources(cls) -> List[Dict[str, Any]]:
        """读取同步源列表;兼容迁移旧的单链接设置(WordsSyncSettings)。"""
        oper = SystemConfigOper()
        sources = oper.get(SystemConfigKey.WordsSyncSources)
        if sources is not None:
            return [cls._normalize_source(item) for item in sources if isinstance(item, dict)]
        # 旧版单链接设置迁移
        legacy = oper.get("WordsSyncSettings") or {}
        if legacy.get("url"):
            migrated = cls._normalize_source({
                "url": legacy["url"],
                "enabled": legacy.get("enabled", False),
                "interval_days": legacy.get("interval_days"),
                "tables": list(WORDS_TABLE_IDS),
                "last_sync": legacy.get("last_sync"),
                "last_status": legacy.get("last_status"),
                "last_message": legacy.get("last_message"),
            })
            oper.set(SystemConfigKey.WordsSyncSources, [migrated])
            oper.delete("WordsSyncSettings")
            logger.info(f"词表同步设置已迁移为多源结构:{legacy['url']}")
            return [migrated]
        return []

    @staticmethod
    def _normalize_source(item: Dict[str, Any]) -> Dict[str, Any]:
        """补全源默认值。"""
        return {
            "url": (item.get("url") or "").strip(),
            "enabled": bool(item.get("enabled")),
            "interval_days": int(item.get("interval_days") or DEFAULT_SYNC_INTERVAL_DAYS),
            "tables": [t for t in (item.get("tables") or list(WORDS_TABLE_IDS)) if t in WORDS_TABLE_IDS],
            "last_sync": item.get("last_sync"),
            "last_status": item.get("last_status"),
            "last_message": item.get("last_message"),
        }

    @classmethod
    def save_sources(cls, sources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """保存源列表,返回规范化后的内容。"""
        normalized = [cls._normalize_source(item) for item in sources if isinstance(item, dict)]
        SystemConfigOper().set(SystemConfigKey.WordsSyncSources, normalized)
        return normalized

    # ---------- 远程词表(追加部分) ----------

    @classmethod
    def get_synced_words(cls) -> Dict[str, Dict[str, List[str]]]:
        """远程词表:{ 源url: { 词表键值: [行...] } }"""
        return SystemConfigOper().get(SystemConfigKey.SyncedWords) or {}

    @classmethod
    def get_synced_lines(cls, config_key: SystemConfigKey) -> List[Tuple[str, List[str]]]:
        """返回指定词表的远程内容:[(源url, 行列表)],供前端分源展示。"""
        result = []
        for source_url, tables in cls.get_synced_words().items():
            lines = tables.get(config_key.value)
            if lines:
                result.append((source_url, lines))
        return result

    # ---------- 同步 ----------

    @staticmethod
    def should_sync(source: Dict[str, Any]) -> bool:
        """单个源是否到达自动同步时间。"""
        if not source.get("enabled") or not source.get("url"):
            return False
        last_sync = source.get("last_sync")
        if not last_sync:
            return True
        try:
            last_time = datetime.strptime(last_sync, "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            return True
        return datetime.now() >= last_time + timedelta(days=source.get("interval_days") or DEFAULT_SYNC_INTERVAL_DAYS)

    @classmethod
    def _fetch_lines(cls, raw_url: str) -> Tuple[Optional[List[str]], str]:
        """拉取单个词表文件;返回 (行列表, 状态消息),行列表为 None 表示失败。"""
        try:
            res = RequestUtils(timeout=20).get_res(raw_url)
        except Exception as err:
            return None, f"请求异常: {err}"
        if res is None:
            return None, "请求失败"
        if res.status_code == 404:
            return None, "文件不存在(404),已跳过"
        if res.status_code != 200:
            return None, f"HTTP {res.status_code}"
        lines = res.text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        while lines and lines[-1] == "":
            lines.pop()
        return lines, "OK"

    @classmethod
    def sync_source(cls, source: Dict[str, Any]) -> Dict[str, Any]:
        """
        同步单个源:拉取其启用的词表文件,更新该源在远程词表中的内容。
        远程词表与本地词表追加合并,不触碰本地编辑内容。
        """
        source = cls._normalize_source(source)
        url = source["url"]
        if not url:
            return {"success": False, "message": "链接为空", "source": source}

        enabled_tables = {
            WORDS_TABLE_IDS[t] for t in source["tables"] if t in WORDS_TABLE_IDS
        }
        # 解析需要拉取的文件:词表键值 -> raw 文件地址
        targets: Dict[str, str] = {}
        file_link = cls._resolve_file_url(url)
        if file_link:
            raw_url, key_value = file_link
            targets[key_value] = raw_url
        else:
            raw_base = cls._resolve_repo_base(url)
            if raw_base is None:
                # 无法识别的地址形态,按 raw 根目录处理
                raw_base = url.rstrip("/")
            for filename, config_key in WORDS_SYNC_FILES.items():
                targets[config_key.value] = f"{raw_base}/{WORDS_SYNC_DIR}/{filename}"

        # 词表开关过滤(单文件链接只同步其对应词表,若该词表未勾选则跳过)
        targets = {k: v for k, v in targets.items() if SystemConfigKey(k) in enabled_tables}
        if not targets:
            source["last_status"] = "skipped"
            source["last_message"] = "没有启用同步的词表"
            return {"success": True, "message": source["last_message"], "source": source, "results": {}}

        logger.info(f"开始同步远程词表源:{url},目标词表 {list(targets)}")
        results: Dict[str, str] = {}
        fetched: Dict[str, List[str]] = {}
        for key_value, raw_url in targets.items():
            lines, status = cls._fetch_lines(raw_url)
            results[key_value] = status
            if lines is None:
                logger.warn(f"词表 {key_value} 拉取失败:{status}({raw_url})")
                continue
            fetched[key_value] = lines
            logger.info(f"词表 {key_value} 同步完成:{len(lines)} 行 <- {raw_url}")

        # 写回远程词表:仅更新成功拉取的词表;拉取失败保留旧值
        synced_words = cls.get_synced_words()
        source_words = synced_words.get(url, {})
        for key_value, lines in fetched.items():
            if lines:
                source_words[key_value] = lines
            else:
                source_words.pop(key_value, None)
        if source_words:
            synced_words[url] = source_words
        else:
            synced_words.pop(url, None)
        SystemConfigOper().set(SystemConfigKey.SyncedWords, synced_words)

        all_ok = len(fetched) == len(targets)
        source["last_sync"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        source["last_status"] = "success" if all_ok else "partial"
        source["last_message"] = f"成功 {len(fetched)}/{len(targets)} 个词表"
        logger.info(f"远程词表源同步结束:{url} - {source['last_message']}")
        return {"success": all_ok, "message": source["last_message"], "source": source, "results": results}

    @classmethod
    def sync(cls, source_url: Optional[str] = None) -> Dict[str, Any]:
        """
        同步入口:指定 source_url 仅同步该源,否则同步全部源。
        同步后把各源状态写回源列表。
        """
        sources = cls.get_sources()
        if not sources:
            return {"success": False, "message": "未配置同步源"}
        targets = [s for s in sources if source_url is None or s["url"] == source_url]
        if not targets:
            return {"success": False, "message": f"同步源不存在: {source_url}"}

        overall_ok = True
        messages = []
        for source in targets:
            result = cls.sync_source(source)
            overall_ok = overall_ok and result.get("success", False)
            messages.append(f"{source['url']}: {result.get('message')}")
        cls.save_sources(sources)
        if source_url is None:
            # 全量同步时清理已从配置中删除的源残留,避免UI重复显示旧词表
            synced_words = cls.get_synced_words()
            stale_urls = [u for u in synced_words if u not in {s["url"] for s in sources}]
            if stale_urls:
                for u in stale_urls:
                    synced_words.pop(u, None)
                SystemConfigOper().set(SystemConfigKey.SyncedWords, synced_words)
        return {
            "success": overall_ok,
            "message": ";".join(messages),
            "sources": sources,
        }

    @classmethod
    def auto_sync(cls) -> None:
        """调度入口:逐源检查到期才同步。"""
        sources = cls.get_sources()
        changed = False
        for source in sources:
            if not cls.should_sync(source):
                continue
            cls.sync_source(source)
            changed = True
        if changed:
            cls.save_sources(sources)

    @classmethod
    def status(cls) -> Dict[str, Any]:
        """API 状态输出:源列表 + 各词表远程行数。"""
        synced_words = cls.get_synced_words()
        table_counts: Dict[str, int] = {}
        for tables in synced_words.values():
            for key_value, lines in tables.items():
                table_counts[key_value] = table_counts.get(key_value, 0) + len(lines)
        return {
            "sources": cls.get_sources(),
            "synced_counts": table_counts,
        }
