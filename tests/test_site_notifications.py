from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.chain.site import SiteChain
from app.schemas import SiteUserData


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


@pytest.mark.parametrize("domain, url, ratio, user_level, expected_warning", [
    ("colapt.com", "https://www.colapt.com/", 0.13293872859055073, "User", False),
    ("https://www.colapt.com/", "https://www.colapt.com/", 0.5, "User", False),
    (None, "https://www.colapt.com/", 0.5, "User", False),
    ("m-team.cc", "https://kp.m-team.cc/", 0.5, "User", True),
    ("colapt.com.invalid", "https://colapt.com.invalid/", 0.5, "User", True),
    ("m-team.cc", "https://kp.m-team.cc/", 1.0, "User", False),
    ("m-team.cc", "https://kp.m-team.cc/", 0.5, "VIP", False),
    ("m-team.cc", "https://kp.m-team.cc/", None, "User", False),
])
def test_site_ratio_warning_respects_traffic_basis(
    monkeypatch, domain, url, ratio, user_level, expected_warning,
):
    """Cola 原始分享率不触发计费预警，其他站预警、数据保存与站内信保持正常。"""
    userdata = SiteUserData(
        upload=1821130752, download=13699023387, ratio=ratio, user_level=user_level,
        message_unread=1, message_unread_contents=[("新消息", "2026-10-19", "正文")],
    )
    site = {"id": 42, "name": "Cola", "domain": domain, "url": url}
    oper = Mock()
    monkeypatch.setattr("app.chain.site.SiteOper", lambda: oper)
    monkeypatch.setattr("app.chain.site.eventmanager.send_event", Mock())
    chain = object.__new__(SiteChain)
    monkeypatch.setattr(chain, "run_module", Mock(return_value=userdata))
    sent = []
    monkeypatch.setattr(chain, "post_message", sent.append)

    assert chain.refresh_userdata(site) is userdata

    oper.update_userdata.assert_called_once()
    assert oper.update_userdata.call_args.kwargs["payload"] == userdata.model_dump()
    assert sent[0].title == "【站点 Cola 消息】"
    assert sent[0].text == "时间：2026-10-19\n标题：新消息\n内容：\n正文"
    warnings = [message for message in sent if message.title == "【站点分享率低预警】"]
    assert bool(warnings) is expected_warning
    assert len(sent) == 1 + int(expected_warning)
