import builtins
import importlib.util

import pytest


def test_legacy_plex_import_returns_existing_client():
    """旧插件的导入路径应返回现有 Plex 客户端类。"""
    from app.modules.plex import Plex
    from app.modules.plex.plex import Plex as PlexClient

    assert Plex is PlexClient


def test_plex_module_discovery_does_not_import_client(monkeypatch):
    """模块发现和未知属性查询不应提前加载 Plex 客户端。"""
    import app.modules.plex as plex_module

    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "app.modules.plex.plex" or name == "plexapi" or name.startswith("plexapi."):
            raise AssertionError(f"模块发现时不应加载 {name}")
        return original_import(name, *args, **kwargs)

    # 在独立模块对象中执行入口，避免测试顺序和已有导入缓存掩盖提前加载。
    spec = importlib.util.spec_from_file_location("plex_import_probe", plex_module.__file__)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    spec.loader.exec_module(module)

    assert module.PlexModule.get_name() == "Plex"
    assert "Plex" not in vars(module)
    with pytest.raises(AttributeError, match="missing_client"):
        getattr(module, "missing_client")
