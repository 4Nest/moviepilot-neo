"""
词表读取辅助:本地词表(用户手编辑)与远程同步词表(追加)合并。
"""
from typing import Dict, List, Union

from app.db.systemconfig_oper import SystemConfigOper
from app.schemas.types import SystemConfigKey


class WordsHelper:
    """词表合并读取:本地在前,远程同步内容追加在后,按行去重。"""

    @staticmethod
    def get_synced_words() -> Dict[str, Dict[str, List[str]]]:
        """只读取仍在同步源列表中的远程词表，防止旧版本残留继续生效。"""
        oper = SystemConfigOper()
        sources = oper.get(SystemConfigKey.WordsSyncSources) or []
        source_urls = {
            (source.get("url") or "").strip()
            for source in sources if isinstance(source, dict)
        }
        synced_words = oper.get(SystemConfigKey.SyncedWords) or {}
        return {
            url: tables for url, tables in synced_words.items()
            if url in source_urls and isinstance(tables, dict)
        }

    @staticmethod
    def get_merged_words(key: Union[str, SystemConfigKey]) -> List[str]:
        """
        读取指定词表:本地配置 + 各远程同步源追加(去重,保序)。
        """
        key_value = key.value if isinstance(key, SystemConfigKey) else str(key)
        oper = SystemConfigOper()
        local = oper.get(key) or []
        if not isinstance(local, list):
            local = [local]
        merged: List[str] = list(local)
        seen = set(local)
        synced_words = WordsHelper.get_synced_words()
        for tables in synced_words.values():
            if not isinstance(tables, dict):
                continue
            for line in tables.get(key_value) or []:
                if line not in seen:
                    seen.add(line)
                    merged.append(line)
        return merged
