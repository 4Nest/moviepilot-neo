import os
import threading
from unittest.mock import Mock

import pytest

from app.api.endpoints import media as media_endpoint
from app.core.config import settings
from app.modules.themoviedb.category import CategoryHelper
from app.schemas.category import CategoryRawConfig

TEMPLATE_PATH = CategoryHelper.DEFAULT_TEMPLATE_PATH
TEMPLATE_CONTENT = TEMPLATE_PATH.read_text(encoding="utf-8")

VALID_RAW = "movie:\n  测试电影:\n    genre_ids: '28'\ntv: {}\n"

BROKEN_YAML = "movie: [未闭合\n"



def _make_helper(tmp_path) -> CategoryHelper:
    """构造指向临时目录的 CategoryHelper（绕过单例与 __init__）。"""
    helper = object.__new__(CategoryHelper)
    helper._category_path = tmp_path / "category.yaml"
    helper._lock = threading.RLock()
    helper._categorys = {}
    helper._movie_categorys = {}
    helper._tv_categorys = {}
    return helper


@pytest.fixture
def helper(tmp_path, monkeypatch) -> CategoryHelper:
    helper = _make_helper(tmp_path)
    monkeypatch.setattr(media_endpoint, "CategoryHelper", lambda: helper)
    return helper


def test_get_raw_auto_initializes_from_template(helper) -> None:
    """category.yaml 不存在时应先按现有逻辑拷贝模板再返回原文。"""
    assert not helper._category_path.exists()

    result = media_endpoint.get_category_config_raw(_=Mock())

    assert result.success is True
    assert result.data["content"] == TEMPLATE_CONTENT
    assert helper._category_path.exists()


def test_get_raw_returns_existing_content(helper) -> None:
    """已存在的 category.yaml 应原样返回，不覆盖既有内容。"""
    helper._category_path.write_text(VALID_RAW, encoding="utf-8")

    result = media_endpoint.get_category_config_raw(_=Mock())

    assert result.success is True
    assert result.data["content"] == VALID_RAW
    assert helper._category_path.read_text(encoding="utf-8") == VALID_RAW


def test_get_raw_read_failure(helper) -> None:
    """读取失败应返回业务错误而非抛异常。"""
    helper._category_path.mkdir()

    result = media_endpoint.get_category_config_raw(_=Mock())

    assert result.success is False
    assert result.message


def test_put_raw_success_writes_backup_and_reloads(helper) -> None:
    """保存原文应滚动备份旧文件、落盘新内容并重新加载生效。"""
    old_content = "movie: {}\ntv: {}\n"
    helper._category_path.write_text(old_content, encoding="utf-8")

    result = media_endpoint.save_category_config_raw(
        config=CategoryRawConfig(content=VALID_RAW), _=Mock()
    )

    assert result.success is True
    # 原文（含注释）原样落盘
    assert helper._category_path.read_text(encoding="utf-8") == VALID_RAW
    # 旧内容滚动备份为 category.yaml.bak
    backup = helper._category_path.parent / "category.yaml.bak"
    assert backup.read_text(encoding="utf-8") == old_content
    # init() 重新加载后新分类即时生效
    assert "测试电影" in helper._movie_categorys


def test_put_raw_rejects_yaml_syntax_error(helper) -> None:
    """YAML 语法错误应返回带定位信息的业务错误且不写盘、不备份。"""
    old_content = "movie: {}\ntv: {}\n"
    helper._category_path.write_text(old_content, encoding="utf-8")

    result = media_endpoint.save_category_config_raw(
        config=CategoryRawConfig(content="movie: [未闭合\n"), _=Mock()
    )

    assert result.success is False
    assert result.message.startswith("YAML 语法错误")
    assert helper._category_path.read_text(encoding="utf-8") == old_content
    assert not (helper._category_path.parent / "category.yaml.bak").exists()


def test_put_raw_rejects_structure_error(helper) -> None:
    """movie 值非映射等结构错误应返回业务错误且不写盘。"""
    old_content = "movie: {}\ntv: {}\n"
    helper._category_path.write_text(old_content, encoding="utf-8")

    result = media_endpoint.save_category_config_raw(
        config=CategoryRawConfig(content="movie: not-a-map\n"), _=Mock()
    )

    assert result.success is False
    assert result.message.startswith("配置结构错误")
    assert helper._category_path.read_text(encoding="utf-8") == old_content


def test_put_raw_rejects_non_mapping_root(helper) -> None:
    """顶层非映射的合法 YAML 也应归为结构错误。"""
    result = media_endpoint.save_category_config_raw(
        config=CategoryRawConfig(content="just a string\n"), _=Mock()
    )

    assert result.success is False
    assert result.message.startswith("配置结构错误")


def test_get_raw_template_is_readonly(helper) -> None:
    """模板端点只读：返回内置模板原文且不在配置目录落任何文件。"""
    current_content = "movie: {}\ntv: {}\n"
    helper._category_path.write_text(current_content, encoding="utf-8")
    result = media_endpoint.get_category_config_raw_template(_=Mock())

    assert result.success is True
    assert result.data["content"] == TEMPLATE_CONTENT
    assert helper._category_path.read_text(encoding="utf-8") == current_content
    assert not (helper._category_path.parent / "category.yaml.bak").exists()


def test_put_raw_rejects_empty_content(helper) -> None:
    """空内容应被拒绝且不写盘、不备份，避免清空 category.yaml。"""
    old_content = VALID_RAW
    helper._category_path.write_text(old_content, encoding="utf-8")

    result = media_endpoint.save_category_config_raw(
        config=CategoryRawConfig(content="  \n\t \n"), _=Mock()
    )

    assert result.success is False
    assert result.message == "内容不能为空"
    assert helper._category_path.read_text(encoding="utf-8") == old_content
    assert not (helper._category_path.parent / "category.yaml.bak").exists()


def test_put_raw_rejects_unknown_top_level_key(helper) -> None:
    """未知顶层键（如 moive 笔误）应拒绝且不写盘，避免静默覆写全部配置。"""
    old_content = VALID_RAW
    helper._category_path.write_text(old_content, encoding="utf-8")

    result = media_endpoint.save_category_config_raw(
        config=CategoryRawConfig(content="moive:\n  测试电影:\n    genre_ids: '28'\ntv: {}\n"),
        _=Mock(),
    )

    assert result.success is False
    assert result.message.startswith("配置结构错误")
    assert helper._category_path.read_text(encoding="utf-8") == old_content


def test_put_raw_rejects_unknown_rule_key(helper) -> None:
    """规则内未知键（如 genre_id 笔误）应拒绝且不写盘。"""
    old_content = VALID_RAW
    helper._category_path.write_text(old_content, encoding="utf-8")

    result = media_endpoint.save_category_config_raw(
        config=CategoryRawConfig(content="movie:\n  测试电影:\n    genre_id: '28'\ntv: {}\n"),
        _=Mock(),
    )

    assert result.success is False
    assert result.message.startswith("配置结构错误")
    assert helper._category_path.read_text(encoding="utf-8") == old_content


def test_put_raw_surfaces_write_failure_reason(helper, monkeypatch) -> None:
    """写盘失败时应透出异常原因，且原子替换失败不损坏原文件。"""
    old_content = "movie: {}\ntv: {}\n"
    helper._category_path.write_text(old_content, encoding="utf-8")

    def _raise_replace(*args, **kwargs):
        raise OSError("磁盘只读")

    monkeypatch.setattr(os, "replace", _raise_replace)

    result = media_endpoint.save_category_config_raw(
        config=CategoryRawConfig(content=VALID_RAW), _=Mock()
    )

    assert result.success is False
    assert "磁盘只读" in result.message
    assert helper._category_path.read_text(encoding="utf-8") == old_content
    # 残留的临时文件应被清理
    assert not (helper._category_path.parent / "category.yaml.tmp").exists()


def test_init_resets_state_on_broken_yaml(helper) -> None:
    """解析失败时应显式重置全部分类状态，不残留上一次的旧值。"""
    helper._category_path.write_text(BROKEN_YAML, encoding="utf-8")
    helper._categorys = {"movie": {"旧分类": {}}}
    helper._movie_categorys = {"旧分类": {}}
    helper._tv_categorys = {"旧剧集": {}}

    helper.init()

    assert helper._categorys == {}
    assert helper._movie_categorys == {}
    assert helper._tv_categorys == {}


def test_init_resets_state_on_non_mapping_root(helper) -> None:
    """顶层非映射（如 YAML 列表）时应显式重置全部分类状态而非抛异常。"""
    helper._category_path.write_text("- 电影\n- 电视剧\n", encoding="utf-8")
    helper._movie_categorys = {"旧分类": {}}
    helper._tv_categorys = {"旧剧集": {}}

    helper.init()

    assert helper._categorys == {}
    assert helper._movie_categorys == {}
    assert helper._tv_categorys == {}
