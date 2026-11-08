"""企业微信带图通知轻量重试与文本兜底的离线回归测试。"""

from unittest.mock import Mock

from app.modules.wechat.wechat import WeChat
from app.modules.wechat.wechatbot import WeChatBot


def _wechat():
    """构造不连接真实服务的企业微信发送器。"""
    client = WeChat.__new__(WeChat)
    client._appid = 1
    client._send_msg_url = "https://example.test/send?access_token={access_token}"
    return client


def test_wechat_image_network_failure_retries_then_sends_text(monkeypatch):
    """自建应用图片连续失败三次后应重试并发送同一正文。"""
    client = _wechat()
    monkeypatch.setattr(client, "_WeChat__get_access_token", lambda force=False: "token")
    post_request = Mock(side_effect=[ConnectionError("network"), ConnectionError("network"),
                                    ConnectionError("network"), True])
    monkeypatch.setattr(client, "_WeChat__post_request", post_request)

    result = client.send_msg(title="标题", text="正文", image="https://example.test/poster.jpg")

    assert result is True
    assert post_request.call_count == 4
    assert [call.args[1]["msgtype"] for call in post_request.call_args_list] == [
        "news", "news", "news", "text",
    ]
    assert "正文" in post_request.call_args.args[1]["text"]["content"]


def test_wechat_image_retry_recovers_without_text_fallback(monkeypatch):
    """自建应用图片重试恢复后不再补发文本。"""
    client = _wechat()
    monkeypatch.setattr(client, "_WeChat__get_access_token", lambda force=False: "token")
    post_request = Mock(side_effect=[ConnectionError("network"), True])
    monkeypatch.setattr(client, "_WeChat__post_request", post_request)

    result = client.send_msg(title="标题", text="正文", image="https://example.test/poster.jpg")

    assert result is True
    assert post_request.call_count == 2
    assert all(call.args[1]["msgtype"] == "news" for call in post_request.call_args_list)


def test_wechat_bot_image_failure_retries_then_removes_image(monkeypatch):
    """智能机器人 Markdown 图片发送失败三次后应移除图片并降级。"""
    client = WeChatBot.__new__(WeChatBot)
    client._config_name = "test-bot"
    send_markdown = Mock(side_effect=[False, False, False, True])
    monkeypatch.setattr(client, "_send_markdown", send_markdown)

    result = client.send_msg(title="标题", text="正文", image="https://example.test/poster.jpg")

    assert result is True
    assert send_markdown.call_count == 4
    assert "![](" in send_markdown.call_args_list[0].kwargs["content"]
    assert "![](" not in send_markdown.call_args.kwargs["content"]
    assert "正文" in send_markdown.call_args.kwargs["content"]
