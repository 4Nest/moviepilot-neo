from pathlib import Path
from unittest.mock import MagicMock, patch

from app import schemas
from app.modules.emby.emby import Emby
from app.modules.plex.plex import Plex
from app.modules.zspace.zspace import ZSpace


def _make_item(title: str, path: str) -> schemas.RefreshMediaItem:
    return schemas.RefreshMediaItem(
        title=title,
        year="2026",
        type="电视剧",
        category="剧集",
        target_path=Path(path),
    )


def test_emby_refresh_library_by_items_refreshes_all_ids() -> None:
    """Emby 同批次命中多个媒体 ID 时应逐个全部刷新后再汇总结果。"""
    emby = Emby.__new__(Emby)
    items = [_make_item("剧集A", "/media/tv/a/e01.mkv"), _make_item("剧集B", "/media/tv/b/e01.mkv")]

    with (
        patch.object(Emby, "_Emby__get_emby_library_id_by_item", side_effect=["id-1", "id-2"]),
        patch.object(Emby, "_Emby__refresh_emby_library_by_id", side_effect=[False, True]) as mock_refresh,
    ):
        result = emby.refresh_library_by_items(items)

    assert result is False
    assert [call.args[0] for call in mock_refresh.call_args_list] == ["id-1", "id-2"]


def test_plex_refresh_library_by_items_refreshes_all_paths() -> None:
    """Plex 同批次命中多个路径时应逐个触发 refresh 请求而不是首个即返回。"""
    plex = Plex.__new__(Plex)
    mock_client = MagicMock()
    plex._plex = mock_client  # pylint: disable=protected-access
    plex._libraries = []  # pylint: disable=protected-access
    items = [_make_item("剧集A", "/media/tv/a/e01.mkv"), _make_item("剧集B", "/media/tv/b/e01.mkv")]

    with patch.object(
        Plex,
        "_Plex__find_librarie",
        side_effect=[("1", Path("/media/tv/a/e01.mkv")), ("2", Path("/media/tv/b/e01.mkv"))],
    ):
        plex.refresh_library_by_items(items)

    assert mock_client.query.call_count == 2
    assert "/library/sections/1/refresh" in mock_client.query.call_args_list[0].args[0]
    assert "/library/sections/2/refresh" in mock_client.query.call_args_list[1].args[0]


def test_zspace_refresh_library_by_items_refreshes_all_ids() -> None:
    """极影视同批次命中多个媒体 ID 时应逐个全部刷新后再返回 True。"""
    zspace = ZSpace.__new__(ZSpace)
    items = [_make_item("剧集A", "/media/tv/a/e01.mkv"), _make_item("剧集B", "/media/tv/b/e01.mkv")]

    with (
        patch.object(ZSpace, "_ZSpace__get_library_id_by_item", side_effect=["lib-1", "lib-2"]),
        patch.object(ZSpace, "_ZSpace__refresh_library_by_id", return_value=True) as mock_refresh,
    ):
        result = zspace.refresh_library_by_items(items)

    assert result is True
    assert [call.args[0] for call in mock_refresh.call_args_list] == ["lib-1", "lib-2"]
