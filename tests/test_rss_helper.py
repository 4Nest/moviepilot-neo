from types import SimpleNamespace
from xml.sax.saxutils import escape

import pytest

from app.helper import rss as rss_module
from app.helper.rss import RssHelper
from app.utils import rust_accel
from app.utils.http import RequestUtils


@pytest.fixture(params=["python", "rust"])
def parse_rss(monkeypatch, request):
    """模拟 RSS 响应，分别验证 Python 和 Rust 解析后的统一处理。"""
    def _parse(title, description="", query="ismalldescr=1", feed_url="https://example.com/torrentrss.php"):
        xml = (
            f"<rss><channel><item><title>{escape(title)}</title>"
            f"<description>{escape(description)}</description>"
            "<link>https://example.com/details.php?id=1</link>"
            '<enclosure url="https://example.com/download.php?id=1" length="1024" />'
            "</item></channel></rss>"
        )
        response = SimpleNamespace(status_code=200, content=xml.encode(), text=xml)
        monkeypatch.setattr(rss_module.RequestUtils, "get_res", lambda *_args, **_kwargs: response)
        monkeypatch.setattr(rss_module.rust_accel, "parse_rss_items", lambda *_args, **_kwargs: (
            [{"title": title, "description": description, "size": 1024,
              "link": "https://example.com/details.php?id=1",
              "enclosure": "https://example.com/download.php?id=1", "pubdate": ""}]
            if request.param == "rust" else None
        ))
        return RssHelper().parse(f"{feed_url}?{query}")[0]
    return _parse


@pytest.mark.parametrize("feed_url", [
    "https://mikanani.me/RSS/Classic",
    "https://mikanani.tv/RSS/Classic",
    "https://nyaa.si/?page=rss",
    "https://anibt.net/rss/magnets.xml",
    "https://share.dmhy.org/topics/rss/rss.xml",
    "https://bangumi.moe/rss/latest",
    "https://www.comicat.org/rss.xml",
    "https://www.kisssub.org/rss.xml",
    "https://www.miobt.com/rss.xml",
])
def test_bt_rss_preserves_release_body_for_recognition(parse_rss, feed_url):
    """清理 RSS 的 HTML 包装但保留完整正文，字幕等识别和过滤条件不能截断。"""
    title = "[三明治摆烂组&LoliHouse] Example S02 - 02 [WebRip 1080p][简繁日内封字幕]"
    description = '<p>资源摘要</p><hr/><p>字幕：简繁日内封</p><p>发布说明和播放器推荐</p>'
    item = parse_rss(title, description, query="", feed_url=feed_url)
    assert item["title"] == title
    assert item["description"] == "资源摘要\n字幕：简繁日内封\n发布说明和播放器推荐"
    assert item["size"] == 1024
    assert item["enclosure"] == "https://example.com/download.php?id=1"


def test_rss_splits_nexus_subtitle_with_nested_brackets(parse_rss):
    """截图中的长副标题应独立输出，内部嵌套括号不能被截断。"""
    title = "Beyond Time's Gaze S01E43 2025 2160p HQ WEB-DL H265 HDR AAC-ADWeb"
    description = "国创连载 光阴之外 第43集 [玄机科技 动画 奇幻武侠] 主演：陈张太康"
    item = parse_rss(f"{title}[{description}]", "<p>种子正文与 MediaInfo</p>")
    assert item["title"] == title
    assert item["description"] == description
    assert item["size"] == 1024
    assert item["enclosure"] == "https://example.com/download.php?id=1"


@pytest.mark.parametrize("query", ["ismalldescr=1", "itemsmalldescr=1"])
def test_rss_retains_joint_group_before_subtitle(parse_rss, query):
    """拆分副标题时，名称中的制作组和连接符必须原样保留。"""
    title = "[三明治摆烂组&LoliHouse] Example - 01 [1080p]"
    item = parse_rss(f"{title}[中文字幕 合作发布]", query=query)
    assert item["title"] == title
    assert item["description"] == "中文字幕 合作发布"


def test_rss_locates_subtitle_before_size_and_uploader(parse_rss):
    """RSS 尾部大小和发布者不能被误当成描述。"""
    item = parse_rss("Movie 2160p[导演剪辑版 [双语字幕]][3.32 GB][Uploader]",
                     query="ismalldescr=1&isize=1&iuplder=1")
    assert item["title"] == "Movie 2160p[3.32 GB][Uploader]"
    assert item["description"] == "导演剪辑版 [双语字幕]"


@pytest.mark.parametrize("title", [
    "[三明治摆烂组&LoliHouse] Example - 01 [1080p]",
    "Example - 01 [三明治摆烂组&LoliHouse]",
    "Example - 01 [三明治摆烂组 & LoliHouse]",
    "Example - 01 [幻樱字幕组]",
    "Example - 01 [DMG&VCB-Studio]",
    "Example - 01 [CHS&CHT]",
    "Example - 01 [1080p 10bit HEVC AAC]",
    "Movie 2160p[副标题没有闭合",
    "Movie 2160p[]",
])
def test_rss_keeps_ambiguous_tags_and_malformed_suffix(parse_rss, title):
    """无有效副标题时，保留制作组、分辨率、字幕标签和不完整名称。"""
    item = parse_rss(title)
    assert item["title"] == title
    assert item["description"] == ""


@pytest.mark.parametrize("query", ["", "ismalldescr=0"])
def test_rss_does_not_guess_subtitle_without_feed_flag(parse_rss, query):
    """没有副标题开关或明确重复描述时，普通 RSS 的名称不能被改写。"""
    title = "Movie 2160p[中文片名标签]"
    item = parse_rss(title, "正常 RSS 简介", query=query)
    assert item["title"] == title
    assert item["description"] == "正常 RSS 简介"


def test_rss_removes_only_an_exact_duplicate_description(parse_rss):
    """独立描述与标题末尾完全相同时，即使无站点开关也能去掉重复部分。"""
    item = parse_rss("Movie 2160p[中文简介]", "中文简介", query="")
    assert item["title"] == "Movie 2160p"
    assert item["description"] == "中文简介"


@pytest.mark.parametrize("query", ["", "ismalldescr=1"])
def test_rss_keeps_joint_group_even_when_description_duplicates_it(parse_rss, query):
    """普通 RSS 或带副标题开关的 RSS 都不能删除制作组标签。"""
    title = "Example - 01 [三明治摆烂组&LoliHouse]"
    item = parse_rss(title, "三明治摆烂组&LoliHouse", query=query)
    assert item["title"] == title
    assert item["description"] == "三明治摆烂组&LoliHouse"


def test_rss_helper_decodes_utf8_xml_before_python_parser(monkeypatch):
    """
    RSS 解码应先修正 XML 文本，再交给 Python 解析兜底路径处理。
    """
    xml = """
    <?xml version="1.0" encoding="UTF-8"?>
    <rss>
      <channel>
        <item>
          <title><![CDATA[警察故事4：简单任务 2160p]]></title>
          <description><![CDATA[中文简介]]></description>
          <link>https://example.com/details/4</link>
          <pubDate>2026-06-25T10:30:00Z</pubDate>
        </item>
      </channel>
    </rss>
    """.strip()

    class FakeRequestUtils:
        """
        测试用 RequestUtils，避免真实网络请求。
        """

        get_decoded_xml_content = staticmethod(RequestUtils.get_decoded_xml_content)

        def __init__(self, **_kwargs):
            """
            保存构造参数占位，兼容 RssHelper 的调用方式。
            """

        def get_res(self, _url):
            """
            返回带错误 HTTP 默认编码的 RSS 响应对象。
            """
            return SimpleNamespace(
                status_code=200,
                content=xml.encode("utf-8"),
                text=xml.encode("utf-8").decode("ISO-8859-1"),
                apparent_encoding="utf-8",
                encoding="ISO-8859-1",
            )

    monkeypatch.setattr(rss_module, "RequestUtils", FakeRequestUtils)
    monkeypatch.setattr(rss_module.rust_accel, "parse_rss_items", lambda *_args, **_kwargs: None)

    result = RssHelper().parse("https://example.com/rss")

    assert result[0]["title"] == "警察故事4：简单任务 2160p"
    assert result[0]["description"] == "中文简介"


@pytest.mark.skipif(
    not rust_accel.is_available(),
    reason="moviepilot_rust 扩展未安装",
)
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("closed", [False, True])
def test_atom_enclosure_matches_between_rust_and_python(monkeypatch, reverse, closed):
    """真实 Rust 与 Python 兜底经 RssHelper 应得到相同的 Atom 详情、下载链接和大小。"""
    alternate = '<link rel="alternate" href="https://example.com/details/1"/>'
    enclosure = '<link rel="enclosure" href="https://example.com/file.torrent" length="1234"/>'
    links = [enclosure, alternate] if reverse else [alternate, enclosure]
    if closed:
        links = [link.replace('/>', '></link>') for link in links]
    xml = '<feed><entry><title>Review</title>' + ''.join(links) + '</entry></feed>'
    response = SimpleNamespace(status_code=200, content=xml.encode(), text=xml)
    monkeypatch.setattr(rss_module.RequestUtils, "get_res", lambda *_args, **_kwargs: response)
    monkeypatch.setattr(rss_module.settings, "RUST_ACCEL", True)
    original_parse = rss_module.rust_accel.parse_rss_items
    rust_returns = []

    def spy_real_rust(*args, **kwargs):
        """调用真实 Rust RSS 入口并记录返回值，不改写结果。"""
        result = original_parse(*args, **kwargs)
        rust_returns.append(result)
        return result

    with monkeypatch.context() as patch_context:
        patch_context.setattr(rss_module.rust_accel, "parse_rss_items", spy_real_rust)
        rust_result = RssHelper().parse("https://example.com/rss")
    with monkeypatch.context() as patch_context:
        patch_context.setattr(rss_module.rust_accel, "parse_rss_items", lambda *_args, **_kwargs: None)
        python_result = RssHelper().parse("https://example.com/rss")

    assert len(rust_returns) == 1 and rust_returns[0] is not None
    assert rust_result == python_result
    assert python_result == [{
        "title": "Review",
        "enclosure": "https://example.com/file.torrent",
        "size": 1234,
        "description": "",
        "link": "https://example.com/details/1",
        "pubdate": "",
    }]
