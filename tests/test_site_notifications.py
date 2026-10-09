from types import SimpleNamespace

import pytest

from app.chain.site import SiteChain


@pytest.mark.parametrize("metadata, expected_link", [
    ((), "https://audiences.me/"),
    (("message:1",), "https://audiences.me/"),
    ((None, "https://audiences.me/details.php?id=729762#22431"),
     "https://audiences.me/details.php?id=729762#22431"),
])
def test_site_notification_uses_optional_link_without_changing_body(monkeypatch, metadata, expected_link):
    """站点通知透传可选跳转链接，同时兼容原有三元和四元消息。"""
    sent = []
    chain = object.__new__(SiteChain)
    chain.messageoper = SimpleNamespace(exists_by_source=lambda _source: False)
    monkeypatch.setattr(chain, "post_message", sent.append)
    chain._post_site_messages(
        site={"name": "Audiences", "url": "https://audiences.me/"},
        userdata=SimpleNamespace(message_unread=1, message_unread_contents=[(
            "新评论", "2026-10-09 08:58:44", "原通知正文", *metadata,
        )]),
    )
    assert len(sent) == 1
    assert sent[0].link == expected_link
    assert sent[0].source == (metadata[0] if metadata else None)
    assert sent[0].text == "时间：2026-10-09 08:58:44\n标题：新评论\n内容：\n原通知正文"
