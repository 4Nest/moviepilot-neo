"""
词表读取辅助:本地词表(用户手编辑)与远程同步词表(追加)合并。
"""
from typing import List, Union

from app.db.systemconfig_oper import SystemConfigOper
from app.schemas.types import SystemConfigKey


class WordsHelper:
    """词表合并读取:本地在前,远程同步内容追加在后,按行去重。"""

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
        synced_words = oper.get(SystemConfigKey.SyncedWords) or {}
        for tables in synced_words.values():
            if not isinstance(tables, dict):
                continue
            for line in tables.get(key_value) or []:
                if line not in seen:
                    seen.add(line)
                    merged.append(line)
        return merged
