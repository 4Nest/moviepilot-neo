# -*- coding: utf-8 -*-
import pytest

from app.modules.indexer.parser.nexus_audiences import NexusAudiencesSiteUserInfo
from app.utils.string import StringUtils


def test_audiences_userbar_metrics_override_generic_nexus_regex():
    parser = NexusAudiencesSiteUserInfo(
        site_name="Audiences",
        url="https://audiences.me/",
        site_cookie="",
        apikey=None,
        token=None,
    )
    html_text = """
    <html>
      <body>
        <div
          data-uploader-label="jxxghp"
          data-uploader-url="userdetails.php?id=18978"
          data-uploader-badge="(江湖儿女)Elite User"
          data-uploader-stats='[
            {"label":"上传量：","value":"10.150 TB","tone":"uploaded"},
            {"label":"爆米花：","value":"1,973,896.2","tone":"bonus"},
            {"label":"下载量：","value":"3.624 TB","tone":"downloaded"},
            {"label":"活跃","value":"↑ 355 / ↓ 7","tone":"active"}
          ]'>
        </div>
        <span class="site-userbar__compact-metric site-userbar__compact-metric--ratio">
          <i></i><span>2.801</span>
        </span>
      </body>
    </html>
    """

    # Audiences 新版用户栏把流量数据放在 data 属性中，通用 NexusPHP 正则无法稳定识别。
    parser._parse_user_traffic_info(html_text)

    assert parser.userid == "18978"
    assert parser.username == "jxxghp"
    assert parser.user_level == "(江湖儿女)Elite User"
    assert parser.upload == StringUtils.num_filesize("10.150 TB")
    assert parser.download == StringUtils.num_filesize("3.624 TB")
    assert parser.ratio == 2.801
    assert parser.bonus == 1973896.2
    assert parser.seeding == 355
    assert parser.leeching == 7


def test_audiences_inbox_total_unread_badge_uses_unread_part():
    parser = NexusAudiencesSiteUserInfo(
        site_name="Audiences",
        url="https://audiences.me/",
        site_cookie="",
        apikey=None,
        token=None,
    )
    html_text = """
    <html>
      <body>
        <div class="site-userbar__compact-actions">
          <a class="site-userbar__compact-tool site-userbar__compact-tool--has-unread"
             href="messages.php"
             title="收件箱 1749/172"
             aria-label="收件箱 1749/172">
            <i class="fas fa-inbox" aria-hidden="true"></i>
            <strong>收件箱</strong>
            <span class="site-userbar__compact-tool-badge site-userbar__compact-tool-badge--unread">1749/172</span>
          </a>
          <a class="site-userbar__compact-tool"
             href="messages.php?action=viewmailbox&amp;box=-1"
             title="发件箱 0"
             aria-label="发件箱 0">
            <strong>发件箱</strong>
            <span class="site-userbar__compact-tool-badge">0</span>
          </a>
        </div>
      </body>
    </html>
    """

    parser._parse_message_unread(html_text)

    assert parser.message_unread == 172


def test_audiences_inbox_total_only_is_not_unread_count():
    parser = NexusAudiencesSiteUserInfo(
        site_name="Audiences",
        url="https://audiences.me/",
        site_cookie="",
        apikey=None,
        token=None,
    )
    html_text = """
    <html>
      <body>
        <div class="site-userbar__compact-actions">
          <a class="site-userbar__compact-tool"
             href="messages.php"
             title="收件箱 1749"
             aria-label="收件箱 1749">
            <strong>收件箱</strong>
            <span class="site-userbar__compact-tool-badge">1749</span>
          </a>
        </div>
      </body>
    </html>
    """

    parser._parse_message_unread(html_text)

    assert parser.message_unread == 0


def test_audiences_unread_badge_plain_count_is_unread_count():
    parser = NexusAudiencesSiteUserInfo(
        site_name="Audiences",
        url="https://audiences.me/",
        site_cookie="",
        apikey=None,
        token=None,
    )
    html_text = """
    <html>
      <body>
        <div class="site-userbar__compact-actions">
          <a class="site-userbar__compact-tool site-userbar__compact-tool--has-unread"
             href="messages.php"
             title="收件箱 1749"
             aria-label="收件箱 1749">
            <strong>收件箱</strong>
            <span class="site-userbar__compact-tool-badge site-userbar__compact-tool-badge--unread">172</span>
          </a>
        </div>
      </body>
    </html>
    """

    parser._parse_message_unread(html_text)

    assert parser.message_unread == 172


def test_audiences_unread_marker_without_count_uses_unknown_count():
    parser = NexusAudiencesSiteUserInfo(
        site_name="Audiences",
        url="https://audiences.me/",
        site_cookie="",
        apikey=None,
        token=None,
    )
    html_text = """
    <html>
      <body>
        <div class="site-userbar__compact-actions">
          <a class="site-userbar__compact-tool site-userbar__compact-tool--has-unread"
             href="messages.php"
             title="收件箱"
             aria-label="收件箱">
            <strong>收件箱</strong>
          </a>
        </div>
      </body>
    </html>
    """

    parser._parse_message_unread(html_text)

    assert parser.message_unread == 99999


def test_audiences_table_unread_links_ignore_content_rows():
    parser = NexusAudiencesSiteUserInfo(
        site_name="Audiences",
        url="https://audiences.me/",
        site_cookie="",
        apikey=None,
        token=None,
    )
    html_text = """
    <html>
      <body>
        <table>
          <tr>
            <td class="rowfollow" align="center">
              <img class="unreadpm" src="pic/trans.gif" alt="Unread" title="未读">
            </td>
            <td class="rowfollow" align="left">
              <a href="messages.php?action=viewmessage&amp;id=4318225">种子被删除</a>
            </td>
            <td class="rowfollow" align="left">系统</td>
            <td class="rowfollow" nowrap=""><span title="2026-05-07 23:01:58">8天17时前</span></td>
            <td class="rowfollow"><input class="checkbox" type="checkbox" name="messages[]" value="4318225"></td>
          </tr>
          <tr>
            <td colspan="5" style="padding: 8px;">消息摘要内容</td>
          </tr>
          <tr>
            <td class="rowfollow" align="center">
              <img class="readpm" src="pic/trans.gif" alt="Read" title="已读">
            </td>
            <td class="rowfollow" align="left">
              <a href="messages.php?action=viewmessage&amp;id=4318000">已读消息</a>
            </td>
            <td class="rowfollow" align="left">系统</td>
            <td class="rowfollow" nowrap=""><span title="2026-05-07 23:01:58">8天17时前</span></td>
            <td class="rowfollow"><input class="checkbox" type="checkbox" name="messages[]" value="4318000"></td>
          </tr>
          <tr>
            <td class="rowfollow" align="center">
              <img class="readpm" src="pic/trans.gif" title="已读">
            </td>
            <td class="rowfollow" align="left">
              <a href="messages.php?action=viewmessage&amp;id=4317999">无英文 alt 的已读消息</a>
            </td>
            <td class="rowfollow" align="left">系统</td>
            <td class="rowfollow" nowrap=""><span title="2026-05-07 23:01:58">8天17时前</span></td>
            <td class="rowfollow"><input class="checkbox" type="checkbox" name="messages[]" value="4317999"></td>
          </tr>
          <tr>
            <td class="rowfollow" align="center"></td>
            <td class="rowfollow" align="left">
              <a href="messages.php?action=viewmessage&amp;id=4317998">无状态图标消息</a>
            </td>
            <td class="rowfollow" align="left">系统</td>
            <td class="rowfollow" nowrap=""><span title="2026-05-07 23:01:58">8天17时前</span></td>
            <td class="rowfollow"><input class="checkbox" type="checkbox" name="messages[]" value="4317998"></td>
          </tr>
        </table>
      </body>
    </html>
    """
    msg_links = []

    next_page = parser._parse_message_unread_links(html_text, msg_links)

    assert msg_links == ["messages.php?action=viewmessage&id=4318225"]
    assert next_page == "messages.php?action=viewmailbox&box=1&unread=yes&page=1"


def test_audiences_readpm_row_is_not_unread_message():
    parser = NexusAudiencesSiteUserInfo(
        site_name="Audiences",
        url="https://audiences.me/",
        site_cookie="",
        apikey=None,
        token=None,
    )
    html_text = """
    <html>
      <body>
        <table>
          <tr>
            <td class="rowfollow" align="center">
              <img class="readpm" src="pic/trans.gif" alt="Read" title="已读">
            </td>
            <td class="rowfollow" align="left">
              <a href="messages.php?action=viewmessage&amp;id=4318000">已读消息</a>
            </td>
          </tr>
        </table>
      </body>
    </html>
    """
    msg_links = []

    parser._parse_message_unread_links(html_text, msg_links)

    assert msg_links == []


def test_audiences_pm_item_unread_links_use_list_preview_when_detail_empty():
    """
    Audiences 新版 div 私信列表应能识别未读行，并在详情页不可解析时使用列表预览通知。
    """
    parser = NexusAudiencesSiteUserInfo(
        site_name="Audiences",
        url="https://audiences.me/",
        site_cookie="",
        apikey=None,
        token=None,
    )
    parser.message_unread = 7
    unread_rows = "".join(
        f"""
        <div class="pm-item-row is-unread">
          <div class="pm-item">
            <input class="pm-item__check" type="checkbox" name="messages[]" value="{4495900 + index}">
            <span class="pm-item__status pm-item__status--unread" title="未读"></span>
            <a class="pm-item__subject"
               href="messages.php?action=viewmessage&amp;id={4495900 + index}">种子被删除</a>
            <span class="pm-item__user"><i class="fas fa-user" aria-hidden="true"></i>系统</span>
            <span class="pm-item__time">2026-06-22 22:32:11</span>
          </div>
          <div class="pm-item__preview">
            你下载的种子'Wonder Wall S01E{index:02d} 2026 1080p WEB-DL H265 AAC-ADWeb'被管理员删除。
          </div>
        </div>
        """
        for index in range(1, 8)
    )
    list_html = f"""
    <html>
      <body>
        <form action="messages.php" method="post">
          <div class="pm-list">
            {unread_rows}
            <div class="pm-item-row">
              <div class="pm-item">
                <span class="pm-item__status" title="已读"></span>
                <a class="pm-item__subject"
                   href="messages.php?action=viewmessage&amp;id=4419171">已读消息</a>
                <span class="pm-item__time">2026-06-07 14:27:45</span>
              </div>
            </div>
          </div>
        </form>
      </body>
    </html>
    """
    requested_urls = []

    def fake_get_page_content(url, params=None, headers=None):
        """
        模拟新版列表页可读，但详情页结构暂不兼容导致解析为空。
        """
        requested_urls.append(url)
        return "<html></html>" if "viewmessage" in url else list_html

    parser._get_page_content = fake_get_page_content

    parser._pase_unread_msgs()

    detail_requests = [url for url in requested_urls if "viewmessage" in url]
    assert len(detail_requests) == 7
    assert len(parser.message_unread_contents) == 7
    assert parser.message_unread_contents[0] == (
        "种子被删除",
        "2026-06-22 22:32:11",
        "你下载的种子'Wonder Wall S01E01 2026 1080p WEB-DL H265 AAC-ADWeb'被管理员删除。",
    )
    assert "已读消息" not in [item[0] for item in parser.message_unread_contents]


def test_audiences_pm_view_message_content_is_parsed():
    """
    Audiences 新版短消息详情页应解析 pm-view 中的标题、日期和正文。
    """
    parser = NexusAudiencesSiteUserInfo(
        site_name="Audiences",
        url="https://audiences.me/",
        site_cookie="",
        apikey=None,
        token=None,
    )
    html_text = """
    <html>
      <body>
        <td class="embedded">
          <div class="pm-page">
            <div class="pm-hero">
              <div class="pm-hero__text">
                <h1 class="pm-hero__title">种子被删除</h1>
                <p class="pm-hero__sub">自 系统</p>
              </div>
            </div>
            <article class="pm-view">
              <header class="pm-view__head">
                <div class="pm-view__meta">
                  <span class="pm-view__label">自</span>
                  <span class="pm-view__value">系统</span>
                </div>
                <div class="pm-view__meta">
                  <span class="pm-view__label">日期</span>
                  <span class="pm-view__value">2026-06-22 22:32:11 </span>
                </div>
              </header>
              <div class="pm-view__body">
                你下载的种子'Wonder Wall S01E20 2026 1080p WEB-DL H265 AAC-ADWeb'被管理员删除。原因：已完结剧集，清理单集。
              </div>
            </article>
          </div>
        </td>
      </body>
    </html>
    """

    head, date, content = parser._parse_message_content(html_text)

    assert head == "种子被删除"
    assert date == "2026-06-22 22:32:11"
    assert content == "你下载的种子'Wonder Wall S01E20 2026 1080p WEB-DL H265 AAC-ADWeb'被管理员删除。原因：已完结剧集，清理单集。"


def test_audiences_unread_mailbox_only_uses_user_box():
    """
    Audiences 只使用用户消息箱，首页不传 page，page=1 实际表示第二页。
    """
    parser = NexusAudiencesSiteUserInfo(
        site_name="Audiences",
        url="https://audiences.me/",
        site_cookie="",
        apikey=None,
        token=None,
    )

    assert parser._user_mail_unread_page == "messages.php?action=viewmailbox&box=1&unread=yes"
    assert parser._sys_mail_unread_page is None


def test_audiences_unread_links_increment_page_until_empty():
    """
    Audiences 每页固定 10 条，有未读行时按 page 参数自增继续翻页。
    """
    parser = NexusAudiencesSiteUserInfo(
        site_name="Audiences",
        url="https://audiences.me/",
        site_cookie="",
        apikey=None,
        token=None,
    )
    html_text = """
    <html>
      <body>
        <table>
          <tr>
            <td class="rowfollow" align="center">
              <img class="unreadpm" src="pic/trans.gif" alt="Unread" title="未读">
            </td>
            <td class="rowfollow" align="left">
              <a href="messages.php?action=viewmessage&amp;id=4318225">种子被删除</a>
            </td>
          </tr>
        </table>
      </body>
    </html>
    """
    next_html_text = html_text.replace("4318225", "4318226").replace("种子被删除", "系统通知")
    msg_links = []

    next_page = parser._parse_message_unread_links(html_text, msg_links)
    next_next_page = parser._parse_message_unread_links(next_html_text, msg_links)
    stop_page = parser._parse_message_unread_links("<html><body><table></table></body></html>", msg_links)

    assert msg_links == [
        "messages.php?action=viewmessage&id=4318225",
        "messages.php?action=viewmessage&id=4318226",
    ]
    assert next_page == "messages.php?action=viewmailbox&box=1&unread=yes&page=1"
    assert next_next_page == "messages.php?action=viewmailbox&box=1&unread=yes&page=2"
    assert stop_page is None


def test_audiences_unread_messages_stop_when_pages_repeat():
    """
    Audiences 异常分页重复返回同一批消息时，应停止翻页并只通知一次。
    """
    parser = NexusAudiencesSiteUserInfo(
        site_name="Audiences",
        url="https://audiences.me/",
        site_cookie="",
        apikey=None,
        token=None,
    )
    parser.message_unread = 172
    list_html = """
    <html>
      <body>
        <table>
          <tr>
            <td class="rowfollow" align="center">
              <img class="unreadpm" src="pic/trans.gif" alt="Unread" title="未读">
            </td>
            <td class="rowfollow" align="left">
              <a href="messages.php?action=viewmessage&amp;id=4318225">种子被删除</a>
            </td>
          </tr>
        </table>
      </body>
    </html>
    """
    requested_urls = []

    def fake_get_page_content(url, params=None, headers=None):
        """
        模拟观众分页异常：每个未读列表页都返回同一个消息链接。
        """
        requested_urls.append(url)
        return "<html></html>" if "viewmessage" in url else list_html

    def fake_parse_message_content(_):
        """
        返回可识别的消息详情，便于验证重复链接没有被重复通知。
        """
        return "种子被删除", "2026-05-07 23:01:58", "消息摘要内容"

    parser._get_page_content = fake_get_page_content
    parser._parse_message_content = fake_parse_message_content

    parser._pase_unread_msgs()

    mailbox_requests = [url for url in requested_urls if "viewmailbox" in url]
    detail_requests = [url for url in requested_urls if "viewmessage" in url]
    assert mailbox_requests == [
        "https://audiences.me/messages.php?action=viewmailbox&box=1&unread=yes",
        "https://audiences.me/messages.php?action=viewmailbox&box=1&unread=yes&page=1",
    ]
    assert detail_requests == [
        "https://audiences.me/messages.php?action=viewmessage&id=4318225"
    ]
    assert parser.message_unread_contents == [
        ("种子被删除", "2026-05-07 23:01:58", "消息摘要内容")
    ]


def test_audiences_unread_messages_skip_empty_detail():
    """
    详情页解析失败时，不应把全 None 的消息写入通知列表。
    """
    parser = NexusAudiencesSiteUserInfo(
        site_name="Audiences",
        url="https://audiences.me/",
        site_cookie="",
        apikey=None,
        token=None,
    )
    parser.message_unread = 1
    list_html = """
    <html>
      <body>
        <table>
          <tr>
            <td class="rowfollow" align="center">
              <img class="unreadpm" src="pic/trans.gif" alt="Unread" title="未读">
            </td>
            <td class="rowfollow" align="left">
              <a href="messages.php?action=viewmessage&amp;id=4318225">种子被删除</a>
            </td>
          </tr>
        </table>
      </body>
    </html>
    """

    def fake_get_page_content(url, params=None, headers=None):
        """
        未读列表正常返回链接，消息详情页返回无法解析的空页面。
        """
        return "<html></html>" if "viewmessage" in url else list_html

    def fake_parse_message_content(_):
        """
        模拟详情页解析不到标题、时间和内容。
        """
        return None, None, None

    parser._get_page_content = fake_get_page_content
    parser._parse_message_content = fake_parse_message_content

    parser._pase_unread_msgs()

    assert parser.message_unread_contents == []


def test_audiences_unknown_unread_count_updates_from_collected_links():
    """
    只有未读状态没有可靠数量时，最终用实际抓到的未读链接数回填。
    """
    parser = NexusAudiencesSiteUserInfo(
        site_name="Audiences",
        url="https://audiences.me/",
        site_cookie="",
        apikey=None,
        token=None,
    )
    parser.message_unread = 99999
    first_list_html = """
    <html>
      <body>
        <table>
          <tr>
            <td class="rowfollow" align="center">
              <img class="unreadpm" src="pic/trans.gif" alt="Unread" title="未读">
            </td>
            <td class="rowfollow" align="left">
              <a href="messages.php?action=viewmessage&amp;id=4318225">种子被删除</a>
            </td>
          </tr>
        </table>
      </body>
    </html>
    """
    second_list_html = first_list_html.replace("4318225", "4318226").replace("种子被删除", "系统通知")

    def fake_get_page_content(url, params=None, headers=None):
        """
        模拟未读数量未知时正常翻到空页为止。
        """
        if "viewmessage" in url:
            return "<html></html>"
        if "page=1" in url:
            return second_list_html
        if "page=2" in url:
            return "<html><body><table></table></body></html>"
        return first_list_html

    def fake_parse_message_content(html_text):
        """
        返回固定消息详情，测试重点是未知数量回填。
        """
        return "标题", "2026-05-07 23:01:58", "内容"

    parser._get_page_content = fake_get_page_content
    parser._parse_message_content = fake_parse_message_content

    parser._pase_unread_msgs()

    assert parser.message_unread == 2
    assert len(parser.message_unread_contents) == 2


def test_audiences_unknown_unread_count_resets_when_no_links():
    """
    未知未读数量但列表为空时，不保留 99999 作为通知数量。
    """
    parser = NexusAudiencesSiteUserInfo(
        site_name="Audiences",
        url="https://audiences.me/",
        site_cookie="",
        apikey=None,
        token=None,
    )
    parser.message_unread = 99999

    def fake_get_page_content(url, params=None, headers=None):
        """
        模拟站点标记有未读但未读列表为空。
        """
        return "<html><body><table></table></body></html>"

    parser._get_page_content = fake_get_page_content

    parser._pase_unread_msgs()

    assert parser.message_unread == 0
    assert parser.message_unread_contents == []


@pytest.mark.parametrize("comments, expected", [
    ('<div class="torrent-comment-shell" id="22431"><span class="torrent-comment-head__time">'
     '2026-10-09 08:58:44</span><div class="torrent-comment-head__meta">'
     '<a href="userdetails.php?id=33590">shaob8</a></div>'
     '<div class="torrent-comment-body__content"><fieldset><legend>引用: mzy1996</legend>'
     '<fieldset>历史回复</fieldset>原始的IMAX流媒体就没有DV层好吧</fieldset><br>iTunes版的dv层</div></div>'
     '<div class="torrent-comment-shell" id="22432"><span class="torrent-comment-head__time">'
     '2026-10-09 09:00:00</span></div>', "https://audiences.me/details.php?id=729762#22431"),
    ('<div class="torrent-comment-shell" id="22432"><span class="torrent-comment-head__time">'
     '2026-10-09 09:00:00</span></div>', "https://audiences.me/details.php?id=729762"),
    ('<div class="torrent-comment-shell" id="22431"><span class="torrent-comment-head__time">'
     '2026-10-09 08:58:44</span></div>'
     '<div class="torrent-comment-shell" id="22432"><span class="torrent-comment-head__time">'
     '2026-10-09 08:58:44</span></div>', "https://audiences.me/details.php?id=729762"),
    ("<html></html>", "https://audiences.me/details.php?id=729762"),
])
def test_audiences_comment_notification_includes_matching_body(monkeypatch, comments, expected):
    """新评论通知补充对应正文和链接，重复或缺失匹配时保留原通知。"""
    parser = NexusAudiencesSiteUserInfo(
        site_name="Audiences", url="https://audiences.me/", site_cookie="", apikey=None, token=None,
    )
    parser.message_unread = 1
    list_html = '<tr><td><img alt="Unread"></td><td><a href="messages.php?action=viewmessage&amp;id=1">新评论</a></td></tr>'
    message_html = '''<h1 class="pm-hero__title">新评论</h1>
        <div class="pm-view__meta"><span class="pm-view__label">日期</span>
        <span class="pm-view__value">2026-10-09 08:58:44</span></div>
        <div class="pm-view__body">你发布的种子收到了新评论 <a href="/details.php?id=729762">Spider-Man</a>.</div>'''
    requests = []

    def fake_get_page_content(url, **_kwargs):
        """用离线 HTML 模拟收件箱、短消息和种子详情页。"""
        requests.append(url)
        if "viewmailbox" in url:
            return list_html
        if "viewmessage" in url:
            return message_html
        assert url == "https://audiences.me/details.php?id=729762"
        return comments

    monkeypatch.setattr(parser, "_get_page_content", fake_get_page_content)
    parser._pase_unread_msgs()

    expected_content = "你发布的种子收到了新评论 Spider-Man."
    if expected.endswith("#22431"):
        expected_content += "\n\n评论者：shaob8\n评论内容：\niTunes版的dv层"
    assert parser.message_unread_contents == [(
        "新评论", "2026-10-09 08:58:44", expected_content, None, expected,
    )]
    parser._resolve_comment_message(message_html, *parser._parse_message_content(message_html))
    assert requests.count("https://audiences.me/details.php?id=729762") == 1


@pytest.mark.parametrize("href", [
    "https://other.example/details.php?id=729762", "//other.example/details.php?id=729762",
    "/details.php?id=invalid", "/details.php?id=1&amp;id=2", "/comment.php?action=delete&amp;cid=22431",
])
def test_audiences_comment_link_ignores_untrusted_targets(monkeypatch, href):
    """评论链接解析只访问当前站点的有效种子详情地址。"""
    parser = NexusAudiencesSiteUserInfo(
        site_name="Audiences", url="https://audiences.me/", site_cookie="", apikey=None, token=None,
    )

    def unexpected_request(*_args, **_kwargs):
        """禁止针对无效链接发起请求。"""
        pytest.fail("不应请求无效评论链接")

    monkeypatch.setattr(parser, "_get_page_content", unexpected_request)
    assert parser._resolve_comment_message(
        f'<div class="pm-view__body"><a href="{href}">种子</a></div>',
        "新评论", "2026-10-09 08:58:44", "你发布的种子收到了新评论 种子.",
    ) == ("你发布的种子收到了新评论 种子.", None)


def test_audiences_comment_request_failure_keeps_torrent_link(monkeypatch):
    """评论页请求异常时仍能发送原通知并跳转种子页。"""
    parser = NexusAudiencesSiteUserInfo(
        site_name="Audiences", url="https://audiences.me/", site_cookie="", apikey=None, token=None,
    )

    def failed_request(*_args, **_kwargs):
        """模拟站点请求超时。"""
        raise TimeoutError("测试超时")

    monkeypatch.setattr(parser, "_get_page_content", failed_request)
    assert parser._resolve_comment_message(
        '<div class="pm-view__body"><a href="/details.php?id=729762">种子</a></div>',
        "新评论", "2026-10-09 08:58:44", "你发布的种子收到了新评论 种子.",
    ) == ("你发布的种子收到了新评论 种子.", "https://audiences.me/details.php?id=729762")


def test_audiences_anonymous_comment_does_not_reveal_admin_identity():
    """匿名评论保留匿名身份，正文过滤引用并保留表情与特殊字符。"""
    parser = NexusAudiencesSiteUserInfo(
        site_name="Audiences", url="https://audiences.me/", site_cookie="", apikey=None, token=None,
    )
    comment = parser._match_torrent_comment('''
        <div class="torrent-comment-shell" id="22431">
          <span class="torrent-comment-head__time">2026-10-09 08:58:44</span>
          <div class="torrent-comment-head__meta"><i class="anon-name">匿名</i>
            <span class="anon-admin-reveal"><a href="userdetails.php?id=1">真实用户名</a></span>
          </div>
          <div class="torrent-comment-body__content"><blockquote>历史评论</blockquote>
            A &amp; B &lt;test&gt;<br><img alt="[微笑]" src="smile.gif"> 谢谢
          </div>
        </div>''', "2026-10-09 08:58:44")
    assert comment == ("22431", "匿名", "A & B <test> [微笑] 谢谢")


def test_audiences_comment_without_body_keeps_exact_link(monkeypatch):
    """正文节点缺失时保留原通知和已匹配的评论直达链接。"""
    parser = NexusAudiencesSiteUserInfo(
        site_name="Audiences", url="https://audiences.me/", site_cookie="", apikey=None, token=None,
    )
    monkeypatch.setattr(parser, "_get_page_content", lambda *_args: '''
        <div class="torrent-comment-shell" id="22431">
          <span class="torrent-comment-head__time">2026-10-09 08:58:44</span>
        </div>''')
    content = "你发布的种子收到了新评论 种子."
    assert parser._resolve_comment_message(
        '<div class="pm-view__body"><a href="/details.php?id=729762">种子</a></div>',
        "新评论", "2026-10-09 08:58:44", content,
    ) == (content, "https://audiences.me/details.php?id=729762#22431")
