"""资源描述在抓取、识别和缓存读写边界的纯文本契约。"""

import asyncio
import pickle
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock, Mock

import pytest

from app.api.endpoints import torrent as torrent_endpoint
from app.chain.torrents import TorrentsChain
from app.chain import torrents as torrents_module
from app.core.context import Context, MediaInfo, TorrentInfo
from app.core.metainfo import MetaInfo
from app.helper.rss import RssHelper
from app.modules.indexer import IndexerModule
from app.utils import rust_accel
from app.utils.http import RequestUtils
from app.utils.string import StringUtils


@pytest.mark.parametrize("description,expected", [
    ('<p><b>动画标题</b></p><p>字幕：<span>简繁日内封</span><br/>1080p HEVC</p>',
     '动画标题\n字幕：简繁日内封\n1080p HEVC'),
    ('<div>Anime &amp; Friends</div><ul><li>字幕：CHS / CHT</li><li>AAC 2.0</li></ul>',
     'Anime & Friends\n字幕：CHS / CHT\nAAC 2.0'),
    ('<script>第99集 720p</script><style>CHT</style><!--广告--><p>简中 1080p</p>'
     '<img src="https://invalid.test/x" onerror="alert(1)"/><iframe>广告</iframe>', '简中 1080p'),
    ('&lt;p&gt;简&amp;amp;繁字幕&lt;br/&gt;第12集&lt;/p&gt;', '简&繁字幕\n第12集'),
    ('<p>片名 <CHS> &amp; <CHT></p>', '片名 <CHS> & <CHT>'),
    ('<p>动画标题<br>字幕：简中', '动画标题\n字幕：简中'),
    ('<table><tr><td>视频</td><td>1080p</td></tr><tr><td>音频</td><td>AAC</td></tr></table>',
     '视频 1080p\n音频 AAC'),
    ('PT中文副标题 [国英双语] <CHS>  S01E12  1080p', 'PT中文副标题 [国英双语] <CHS>  S01E12  1080p'),
    ('', ''), (None, None),
])
def test_description_normalization_preserves_resource_text(
    description: Optional[str], expected: Optional[str],
) -> None:
    """去掉 HTML 包装与主动内容，保留正文、段落和 PT 副标题。"""
    assert StringUtils.strip_html(description) == expected
    assert StringUtils.strip_html(expected) == expected


def test_html_description_is_not_truncated() -> None:
    """长正文末尾的字幕信息必须仍可用于识别和过滤。"""
    description = '<p>' + '发布说明' * 2000 + '</p><p>字幕：简繁日内封 / 1080p HEVC</p>'
    text = StringUtils.strip_html(description)
    assert text == '发布说明' * 2000 + '\n字幕：简繁日内封 / 1080p HEVC'


def test_indexer_result_is_plain_text_before_recognition() -> None:
    """索引器返回资源时已清理描述，搜索和爬虫缓存共用这一入口。"""
    result = IndexerModule._IndexerModule__parse_result(
        {"id": 1, "name": "Nyaa", "domain": "https://nyaa.si/"},
        [{"title": "Example S01E12 1080p", "description": "<p>简繁内封字幕</p>"}], 0,
    )[0]
    assert result.description == '简繁内封字幕'


@pytest.mark.parametrize("stype", ['rss', 'spider'])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_old_cache_is_normalized_before_listing_and_deleting(
    monkeypatch: pytest.MonkeyPatch, stype: str, asynchronous: bool,
) -> None:
    """旧 pickle 缓存读出即清理，列表和删除使用相同指纹且不丢识别信息。"""
    context = Context(
        torrent_info=TorrentInfo(title='Example S01E12 1080p', description='<p>字幕：简中</p>'),
        meta_info=MetaInfo(title='Example S01E12 1080p', subtitle='<p>字幕：简中</p>'),
        media_info=MediaInfo(title='Example', tmdb_id=123),
    )
    cache = pickle.dumps({'nyaa.si': [context]})
    chain = object.__new__(TorrentsChain)
    monkeypatch.setattr(chain, 'load_cache', lambda _: pickle.loads(cache))
    monkeypatch.setattr(chain, 'async_load_cache', AsyncMock(side_effect=lambda _: pickle.loads(cache)))
    saved = AsyncMock()
    monkeypatch.setattr(chain, 'async_save_cache', saved)
    monkeypatch.setattr(torrent_endpoint, 'TorrentsChain', lambda: chain)
    monkeypatch.setattr(torrent_endpoint.settings, 'SUBSCRIBE_MODE', stype)
    result = asyncio.run(chain.async_get_torrents(stype)) if asynchronous else chain.get_torrents(stype)
    assert result['nyaa.si'][0].torrent_info.description == '字幕：简中'
    assert result['nyaa.si'][0].meta_info.subtitle == '字幕：简中'
    assert result['nyaa.si'][0].media_info.tmdb_id == 123
    response = asyncio.run(torrent_endpoint.torrents_cache())
    item = response.data['data'][0]
    assert item['description'] == '字幕：简中'
    response = asyncio.run(torrent_endpoint.delete_cache('nyaa.si', item['hash']))
    assert response.success is True
    assert saved.call_args.args[0] == {'nyaa.si': []}


@pytest.mark.parametrize("stype", ['rss', 'spider'])
def test_refresh_persists_clean_old_cache_without_duplicate_resources(
    monkeypatch: pytest.MonkeyPatch, stype: str,
) -> None:
    """刷新旧缓存时写回纯文本，HTML 包装变化不应触发重复识别或新增资源。"""
    title = 'Example S01E12 1080p'
    old_context = Context(
        torrent_info=TorrentInfo(title=title, description='<p>字幕：简中</p>', enclosure='magnet:test'),
        meta_info=MetaInfo(title=title, subtitle='<p>字幕：简中</p>'),
        media_info=MediaInfo(title='Example', tmdb_id=123),
    )
    chain = object.__new__(TorrentsChain)
    monkeypatch.setattr(chain, 'load_cache', lambda _: {'nyaa.si': [old_context]})
    fresh = lambda **_: [TorrentInfo(title=title, description='字幕：简中', enclosure='magnet:test')]
    monkeypatch.setattr(chain, 'rss', fresh)
    monkeypatch.setattr(chain, 'browse', fresh)
    saved = Mock()
    monkeypatch.setattr(chain, 'save_cache', saved)
    monkeypatch.setattr(torrents_module, 'SitesHelper', lambda: SimpleNamespace(get_indexers=lambda: [
        {'id': 1, 'name': 'Nyaa', 'domain': 'https://nyaa.si/'},
    ]))
    monkeypatch.setattr(torrents_module, 'TorrentHelper', lambda: SimpleNamespace(is_invalid=lambda _: False))
    recognize = Mock(side_effect=AssertionError('旧资源不应重复识别'))
    monkeypatch.setattr(torrents_module, 'MediaChain', recognize)
    monkeypatch.setattr(torrents_module.settings, 'SUBSCRIBE_MODE', stype)
    monkeypatch.setattr(torrents_module.settings, 'NO_CACHE_SITE_KEY', 'unrelated.example')
    monkeypatch.setattr(torrents_module, 'global_vars', SimpleNamespace(is_system_stopped=False))
    chain.refresh(stype=stype, sites=[1])
    saved_cache, filename = saved.call_args.args
    assert filename == ('__rss_cache__' if stype == 'rss' else '__torrents_cache__')
    assert len(saved_cache['nyaa.si']) == 1
    assert saved_cache['nyaa.si'][0].torrent_info.description == '字幕：简中'
    assert b'<p>' not in pickle.dumps(saved_cache)
    recognize.assert_not_called()


@pytest.mark.parametrize("accelerated", [False, True])
def test_real_rss_parsers_produce_the_same_clean_description(
    monkeypatch: pytest.MonkeyPatch, accelerated: bool,
) -> None:
    """真实 Rust 扩展和 Python 兜底解析均应返回相同的完整纯文本。"""
    if accelerated and not rust_accel.is_available():
        pytest.skip('Rust 扩展未安装')
    xml = ('<rss><channel><item><title>Example S01E12 1080p</title>'
           '<description><![CDATA[<p>动画标题</p><p>字幕：简繁日内封<br/>HEVC &amp; AAC</p>]]></description>'
           '<link>https://nyaa.si/view/1</link><enclosure url="https://nyaa.si/download/1.torrent" length="1024"/>'
           '</item></channel></rss>')
    monkeypatch.setattr(RequestUtils, 'get_res', lambda *_args, **_kwargs: SimpleNamespace(
        status_code=200, content=xml.encode(), text=xml,
    ))
    monkeypatch.setattr(rust_accel.settings, 'RUST_ACCEL', accelerated)
    original_parser = rust_accel.parse_rss_items
    parsed_results = []

    def _parse(*args, **kwargs):
        result = original_parser(*args, **kwargs)
        parsed_results.append(result)
        return result

    monkeypatch.setattr(rust_accel, 'parse_rss_items', _parse)
    item = RssHelper().parse('https://nyaa.si/?page=rss')[0]
    assert parsed_results
    assert bool(parsed_results[0]) is accelerated
    assert item['description'] == '动画标题\n字幕：简繁日内封\nHEVC & AAC'
    assert item['enclosure'] == 'https://nyaa.si/download/1.torrent'
    assert item['size'] == 1024
