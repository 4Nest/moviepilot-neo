from unittest.mock import MagicMock

import pytest

import app.chain.download as download_module
import app.chain.message as message_module
from app.chain.download import DownloadChain, download_interaction_manager
from app.chain.message import MessageChain
from app.schemas import DownloaderTorrent
from app.schemas.types import MessageChannel


@pytest.fixture(autouse=True)
def _isolate_download_interaction(monkeypatch):
    """隔离全局会话、渠道能力和下载历史数据库。"""
    download_interaction_manager.clear()
    monkeypatch.setattr(
        download_module,
        "supports_interaction_buttons",
        lambda channel: channel == MessageChannel.Telegram,
    )
    monkeypatch.setattr(download_module, "supports_markdown", lambda _channel: False)
    history_oper = MagicMock()
    history_oper.get_by_hashes.return_value = {}
    monkeypatch.setattr(
        download_module,
        "DownloadHistoryOper",
        MagicMock(return_value=history_oper),
    )
    yield
    download_interaction_manager.clear()


def _make_torrent(index: int, state: str = "downloading") -> DownloaderTorrent:
    """构造带完整展示字段的下载任务。"""
    torrent_hash = f"{index:010x}{index:030x}"
    media = {}
    if index == 1:
        media = {"title": "演示剧集", "season": "S01", "episode": "E02"}
    return DownloaderTorrent(
        downloader=f"qb-{index}",
        hash=torrent_hash,
        title=f"示例种子 {index}",
        site_name=f"站点 {index}",
        size=2 * 1024**3,
        progress=68.4,
        state=state,
        dlspeed="1.5M",
        upspeed="256K",
        left_time="20分钟",
        media=media,
    )


def _build_chain(active=None, paused=None):
    """构造不连接真实下载器的下载链及调用记录。"""
    chain = DownloadChain.__new__(DownloadChain)
    chain.downloading = MagicMock(return_value=active or [])
    chain.list_torrents = MagicMock(return_value=paused or [])
    chain.set_downloading = MagicMock(return_value=True)
    chain.remove_torrents = MagicMock(return_value=True)
    chain.post_message = MagicMock()
    chain.edit_message = MagicMock(return_value=True)
    return chain


def _start(chain, channel=MessageChannel.Telegram):
    """启动下载控制台并返回最近发送的通知。"""
    chain.remote_downloading(
        channel=channel,
        userid="user-1",
        source="test-source",
        username="测试用户",
    )
    return chain.post_message.call_args.args[0]


def _callback(chain, callback_data: str):
    """以可编辑消息上下文触发按钮回调。"""
    return chain.handle_callback_interaction(
        callback_data=callback_data,
        channel=MessageChannel.Telegram,
        source="test-source",
        userid="user-1",
        username="测试用户",
        original_message_id=100,
        original_chat_id="chat-1",
    )


def _find_callback(notification, action: str, torrent_hash: str) -> str:
    """从通知按钮中定位指定任务动作。"""
    suffix = f":{action}:{torrent_hash[:10]}"
    return next(
        button["callback_data"]
        for row in notification.buttons
        for button in row
        if button["callback_data"].endswith(suffix)
    )


def test_button_list_renders_details_and_paginates_with_short_callbacks():
    """按钮面板应完整展示任务详情、每页五项并限制回调长度。"""
    torrents = [_make_torrent(index) for index in range(1, 7)]
    chain = _build_chain(active=torrents)

    notification = _start(chain)

    assert "演示剧集 S01 E02" in notification.text
    assert "示例种子 1" in notification.text
    assert "qb-1" in notification.text
    assert "站点 1" in notification.text
    assert "2.0G" in notification.text
    assert "68.4%" in notification.text
    assert "1.5M/s" in notification.text
    assert "256K/s" in notification.text
    assert "20分钟" in notification.text
    assert "下载中" in notification.text
    assert "第 1/2 页，共 6 个任务" in notification.text
    callbacks = [
        button["callback_data"]
        for row in notification.buttons
        for button in row
    ]
    assert len([item for item in callbacks if ":toggle:" in item]) == 5
    assert all(len(item.encode("utf-8")) < 64 for item in callbacks)
    assert all(torrent.hash not in item for item in callbacks for torrent in torrents)

    next_callback = next(item for item in callbacks if item.endswith(":page-next"))
    assert _callback(chain, next_callback) is True
    edited = chain.edit_message.call_args.kwargs
    assert edited["message_id"] == 100
    assert edited["chat_id"] == "chat-1"
    assert "示例种子 6" in edited["text"]
    assert "第 2/2 页" in edited["text"]
    assert download_interaction_manager.get_by_user("user-1").page == 1


def test_toggle_pauses_active_task_and_resumes_paused_task():
    """切换按钮应按当前状态暂停活动任务或继续暂停任务。"""
    active = _make_torrent(1)
    paused = _make_torrent(2, state="paused")
    chain = _build_chain(active=[active], paused=[paused])
    notification = _start(chain)

    assert "已暂停" in notification.text
    _callback(chain, _find_callback(notification, "toggle", active.hash))
    chain.set_downloading.assert_any_call(active.hash, "stop", name=active.downloader)
    assert "已暂停" in chain.edit_message.call_args.kwargs["text"]

    _callback(chain, _find_callback(notification, "toggle", paused.hash))
    chain.set_downloading.assert_any_call(paused.hash, "start", name=paused.downloader)
    assert "已继续" in chain.edit_message.call_args.kwargs["text"]


def test_delete_requires_confirmation_and_cancel_restores_task_buttons():
    """删除按钮应先进入确认态，取消后不得调用下载器删除。"""
    torrent = _make_torrent(1)
    chain = _build_chain(active=[torrent])
    notification = _start(chain)
    request = download_interaction_manager.get_by_user("user-1")

    _callback(chain, _find_callback(notification, "askdel", torrent.hash))

    assert request.awaiting_input == f"confirm_delete:{torrent.hash}"
    confirmation = chain.edit_message.call_args.kwargs
    assert torrent.title in confirmation["text"]
    assert "确认删除" in confirmation["text"]
    confirm_callbacks = [
        button["callback_data"]
        for row in confirmation["buttons"]
        for button in row
    ]
    assert any(item.endswith(f":cfmdel:{torrent.hash[:10]}") for item in confirm_callbacks)
    assert any(item.endswith(":cancel") for item in confirm_callbacks)
    assert all(len(item.encode("utf-8")) < 64 for item in confirm_callbacks)
    chain.remove_torrents.assert_not_called()

    cancel_callback = next(item for item in confirm_callbacks if item.endswith(":cancel"))
    _callback(chain, cancel_callback)

    assert request.awaiting_input is None
    restored = chain.edit_message.call_args.kwargs
    assert any(
        ":toggle:" in button["callback_data"]
        for row in restored["buttons"]
        for button in row
    )
    chain.remove_torrents.assert_not_called()


def test_confirm_delete_removes_full_hash_and_refreshes_panel():
    """确认删除应使用完整哈希和下载器名称并清理确认状态。"""
    torrent = _make_torrent(1)
    chain = _build_chain(active=[torrent])
    notification = _start(chain)
    request = download_interaction_manager.get_by_user("user-1")
    _callback(chain, _find_callback(notification, "askdel", torrent.hash))

    _callback(
        chain,
        f"downloading:{request.request_id}:cfmdel:{torrent.hash[:10]}",
    )

    chain.remove_torrents.assert_called_once_with(
        hashs=torrent.hash,
        downloader=torrent.downloader,
    )
    assert request.awaiting_input is None
    assert "已删除" in chain.edit_message.call_args.kwargs["text"]
    assert chain.downloading.call_count >= 3


def test_expired_callback_explains_how_to_restart():
    """失效会话回调应明确提示重新发起命令。"""
    chain = _build_chain(active=[_make_torrent(1)])
    notification = _start(chain)
    callback_data = notification.buttons[-1][-1]["callback_data"]
    download_interaction_manager.clear()

    assert _callback(chain, callback_data) is True

    notice = chain.post_message.call_args.args[0]
    assert "已失效" in notice.title
    assert "/downloading" in notice.title


def test_plain_text_channel_supports_numbered_commands_and_confirmation():
    """纯文本渠道应支持翻页、暂停、继续、刷新、删除确认和退出。"""
    torrents = [_make_torrent(index) for index in range(1, 7)]
    chain = _build_chain(active=torrents)
    notification = _start(chain, channel=MessageChannel.Wechat)
    request = download_interaction_manager.get_by_user("user-1")

    assert notification.buttons is None
    assert "p <序号>" in notification.text
    assert "r <序号>" in notification.text
    assert "d <序号>" in notification.text

    for text in ("n", "b", "f", "p 1", "r 1"):
        assert chain.handle_text_interaction(
            channel=MessageChannel.Wechat,
            source="test-source",
            userid="user-1",
            username="测试用户",
            text=text,
        ) is True
    chain.set_downloading.assert_any_call(torrents[0].hash, "stop", name="qb-1")
    chain.set_downloading.assert_any_call(torrents[0].hash, "start", name="qb-1")
    assert request.page == 0

    assert chain.handle_text_interaction(
        channel=MessageChannel.Wechat,
        source="test-source",
        userid="user-1",
        username="测试用户",
        text="d 1",
    ) is True
    assert request.awaiting_input == f"confirm_delete:{torrents[0].hash}"
    assert chain.handle_text_interaction(
        channel=MessageChannel.Wechat,
        source="test-source",
        userid="user-1",
        username="测试用户",
        text="取消",
    ) is True
    assert request.awaiting_input is None
    chain.remove_torrents.assert_not_called()

    chain.handle_text_interaction(
        channel=MessageChannel.Wechat,
        source="test-source",
        userid="user-1",
        username="测试用户",
        text="d 1",
    )
    chain.handle_text_interaction(
        channel=MessageChannel.Wechat,
        source="test-source",
        userid="user-1",
        username="测试用户",
        text="确认删除",
    )
    chain.remove_torrents.assert_called_once_with(
        hashs=torrents[0].hash,
        downloader=torrents[0].downloader,
    )

    assert chain.handle_text_interaction(
        channel=MessageChannel.Wechat,
        source="test-source",
        userid="user-1",
        username="测试用户",
        text="q",
    ) is True
    assert download_interaction_manager.get_by_user("user-1") is None


def test_message_chain_dispatches_download_callbacks_and_text(monkeypatch):
    """消息链应将下载会话识别为最新交互并分发回调和文本。"""
    request = download_interaction_manager.create_or_replace(
        user_id="user-1",
        command="/downloading",
        channel=MessageChannel.Wechat,
        source="test-source",
        username="测试用户",
    )
    monkeypatch.setattr(
        message_module.site_interaction_manager,
        "get_by_user",
        lambda _userid: None,
    )
    monkeypatch.setattr(
        message_module.subscribe_interaction_manager,
        "get_by_user",
        lambda _userid: None,
    )
    assert MessageChain._get_latest_slash_interaction("user-1") == "downloading"

    chain = MessageChain.__new__(MessageChain)
    monkeypatch.setattr(chain, "_handle_transfer_callback", lambda **_kwargs: False)
    callback_handler = MagicMock(return_value=True)
    monkeypatch.setattr(
        message_module.DownloadChain,
        "handle_callback_interaction",
        callback_handler,
    )
    assert chain._handle_callback(
        text=f"CALLBACK:downloading:{request.request_id}:refresh",
        channel=MessageChannel.Telegram,
        source="test-source",
        userid="user-1",
        username="测试用户",
    ) is False
    callback_handler.assert_called_once()

    monkeypatch.setattr(chain, "_handle_plugin_input_interaction", lambda **_kwargs: False)
    text_handler = MagicMock(return_value=True)
    monkeypatch.setattr(
        message_module.DownloadChain,
        "handle_text_interaction",
        text_handler,
    )
    assert chain._handle_message_core(
        channel=MessageChannel.Wechat,
        source="test-source",
        userid="user-1",
        username="测试用户",
        text="f",
    ) is False
    text_handler.assert_called_once()
