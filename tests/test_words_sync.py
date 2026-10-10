"""词表同步源删除回归：清理远程残留，同时保留本地及其他来源的词条。"""
import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from app.api.endpoints import system as system_endpoint
from app.chain.words import WordsSyncChain
from app.core.meta.words import WordsMatcher
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.words import WordsHelper
from app.modules.filter import FilterModule
from app.schemas.types import SystemConfigKey


@pytest.fixture
def word_settings(monkeypatch):
    """以独立内存配置替代持久化边界，避免改动数据库或全局配置。"""
    values = {}

    def get_value(_self, key):
        return deepcopy(values.get(key))

    def set_value(_self, key, value):
        values[key] = deepcopy(value)
        return True

    monkeypatch.setattr(SystemConfigOper, "get", get_value)
    monkeypatch.setattr(SystemConfigOper, "set", set_value)
    monkeypatch.setattr(system_endpoint.eventmanager, "async_send_event", AsyncMock())
    return values


@pytest.mark.parametrize("remaining", [[], [{"url": "https://example.com/keep", "enabled": False}]])
def test_save_sources_removes_only_deleted_remote_words(word_settings, remaining):
    """删最后一个源或多个源中的一个，都只清理该来源；停用自动同步不等于删除。"""
    local_words = ["本地 => 保留", "共享 => 保留"]
    keep_words = {SystemConfigKey.CustomIdentifiers.value: ["其他来源 => 保留", "共享 => 保留"]}
    word_settings[SystemConfigKey.CustomIdentifiers] = local_words
    word_settings[SystemConfigKey.SyncedWords] = {
        "https://example.com/remove": {key.value: ["删除词条"] for key in (
            SystemConfigKey.CustomIdentifiers, SystemConfigKey.CustomReleaseGroups,
            SystemConfigKey.Customization, SystemConfigKey.TransferExcludeWords,
        )},
        "https://example.com/keep": keep_words,
    }

    WordsSyncChain.save_sources(remaining)

    assert word_settings[SystemConfigKey.CustomIdentifiers] == local_words
    assert word_settings[SystemConfigKey.SyncedWords] == (
        {"https://example.com/keep": keep_words} if remaining else {}
    )
    assert WordsHelper.get_merged_words(SystemConfigKey.CustomIdentifiers) == (
        local_words + ["其他来源 => 保留"] if remaining else local_words
    )


@pytest.mark.parametrize("sources", [None, [], [{"url": "https://example.com/keep"}]])
def test_legacy_orphan_words_are_hidden_and_do_not_affect_recognition(word_settings, sources):
    """已删除来源的历史残留不再展示、统计或改写资源标题，无需重新同步。"""
    word_settings[SystemConfigKey.WordsSyncSources] = sources
    word_settings[SystemConfigKey.CustomIdentifiers] = ["Local => 本地"]
    word_settings[SystemConfigKey.SyncedWords] = {
        "https://example.com/deleted": {SystemConfigKey.CustomIdentifiers.value: ["Blue Box => 错误名称"]},
    }

    assert WordsSyncChain.get_synced_lines(SystemConfigKey.CustomIdentifiers) == []
    assert WordsSyncChain.status()["synced_counts"] == {}
    assert WordsMatcher().prepare("Blue Box S01E26")[0] == "Blue Box S01E26"
    assert WordsMatcher().prepare("Local")[0] == "本地"


@pytest.mark.parametrize("payload", [[], None])
def test_settings_endpoint_cleans_words_when_last_source_is_removed(word_settings, payload):
    """实际设置入口不能只保存源列表，空列表和清空请求都要清理词条并通知重载。"""
    word_settings[SystemConfigKey.WordsSyncSources] = [{"url": "https://example.com/deleted"}]
    word_settings[SystemConfigKey.SyncedWords] = {
        "https://example.com/deleted": {SystemConfigKey.CustomIdentifiers.value: ["旧词条"]},
    }

    result = asyncio.run(system_endpoint.set_setting(SystemConfigKey.WordsSyncSources.value, payload, None))

    assert result.success
    assert word_settings[SystemConfigKey.WordsSyncSources] == []
    assert word_settings[SystemConfigKey.SyncedWords] == {}
    event_kwargs = system_endpoint.eventmanager.async_send_event.call_args.kwargs
    assert event_kwargs["data"].key == {SystemConfigKey.WordsSyncSources.value}
    assert SystemConfigKey.WordsSyncSources.value in FilterModule.CONFIG_WATCH


def test_saving_changed_url_removes_previous_source_content(word_settings):
    """修改源地址不能继续沿用原地址拉取的内容。"""
    word_settings[SystemConfigKey.SyncedWords] = {
        "https://example.com/old": {SystemConfigKey.CustomIdentifiers.value: ["旧词条"]},
    }
    WordsSyncChain.save_sources([{"url": " https://example.com/new "}])
    assert word_settings[SystemConfigKey.WordsSyncSources][0]["url"] == "https://example.com/new"
    assert word_settings[SystemConfigKey.SyncedWords] == {}


def test_configured_remote_source_still_merges_and_deduplicates(word_settings):
    """正常来源继续参与合并，按本地、远程顺序去重。"""
    word_settings[SystemConfigKey.WordsSyncSources] = [{"url": "https://example.com/keep", "enabled": True}]
    word_settings[SystemConfigKey.CustomIdentifiers] = ["本地", "重复"]
    word_settings[SystemConfigKey.SyncedWords] = {
        "https://example.com/keep": {SystemConfigKey.CustomIdentifiers.value: ["重复", "远程"]},
    }
    assert WordsHelper.get_merged_words(SystemConfigKey.CustomIdentifiers) == ["本地", "重复", "远程"]
    assert WordsSyncChain.status()["synced_counts"] == {SystemConfigKey.CustomIdentifiers.value: 2}
