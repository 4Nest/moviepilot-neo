"""多 Telegram 通知渠道的目标会话与运行状态隔离回归，全程使用内存机器人。"""

from collections.abc import Callable, Iterator
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.modules.telegram import TelegramModule
from app.modules.telegram.telegram import Telegram, apihelper
from app.schemas import Notification
from app.schemas.types import MessageChannel, NotificationType


@pytest.fixture
def create_telegram(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., Telegram]]:
    """创建完整初始化的独立渠道，隔离 SDK 全局配置并停止所有测试线程。"""
    clients = []
    for attr in ("API_URL", "FILE_URL", "proxy"):
        monkeypatch.setattr(apihelper, attr, getattr(apihelper, attr))
    # 旧实现使用类级容器，复现失败时也不能污染其他用例。
    for attr in ("_user_chat_mapping", "_typing_tasks", "_typing_stop_flags", "_callback_handlers"):
        if hasattr(Telegram, attr):
            monkeypatch.setattr(Telegram, attr, {})

    def bot_factory(*_args, **_kwargs):
        bot = Mock()
        bot.get_me.return_value = SimpleNamespace(username="test_bot")
        bot.message_handlers = {}

        def register_handler(**options):
            def register(callback):
                key = "commands" if options.get("commands") else "messages"
                bot.message_handlers[key] = callback
                return callback
            return register

        bot.message_handler.side_effect = register_handler
        bot.callback_query_handler.return_value = lambda callback: callback
        bot.send_message.side_effect = lambda **kwargs: SimpleNamespace(
            message_id=1, chat=SimpleNamespace(id=kwargs["chat_id"]),
        )
        return bot

    monkeypatch.setattr("app.modules.telegram.telegram.TeleBot", bot_factory)

    def create(name: str, chat_id: str = "-1000") -> Telegram:
        client = Telegram(name=name, TELEGRAM_TOKEN=f"fake-token-{name}", TELEGRAM_CHAT_ID=chat_id)
        clients.append(client)
        return client

    yield create
    for client in clients:
        client.stop()


def test_source_selected_channel_keeps_its_own_chat(
    create_telegram: Callable[..., Telegram], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一用户在两个渠道的群聊里发言后，定向通知必须仍发到所选渠道的群。"""
    first = create_telegram("first", "-1001")
    second = create_telegram("second", "-1002")
    first._update_user_chat_mapping(123, -1011)
    second._update_user_chat_mapping(123, -1022)
    module = TelegramModule()
    module._channel = MessageChannel.Telegram
    module._instances = {"first": first, "second": second}
    module._configs = {
        name: SimpleNamespace(name=name, switchs=[NotificationType.Download.value])
        for name in module._instances
    }
    monkeypatch.setattr(module, "get_configs", lambda: module._configs)

    module.post_message(Notification(
        source="first", channel=MessageChannel.Telegram, userid="123", title="下载完成",
    ))

    assert first.bot.send_message.call_args.kwargs["chat_id"] == "-1011"
    second.bot.send_message.assert_not_called()


def test_unseen_user_does_not_inherit_another_channels_chat(create_telegram: Callable[..., Telegram]) -> None:
    """另一个渠道见过同名用户时，当前渠道仍应按用户 ID 定向私聊。"""
    first = create_telegram("first")
    second = create_telegram("second")
    first._update_user_chat_mapping(123, -1011)

    second.send_msg(title="定向通知", userid="123")

    assert second.bot.send_message.call_args.kwargs["chat_id"] == "123"


def test_recreated_channel_does_not_keep_old_chat_mapping(create_telegram: Callable[..., Telegram]) -> None:
    """修改配置后重建渠道，应清除旧机器人的会话记录。"""
    previous = create_telegram("same")
    previous._update_user_chat_mapping(123, -1011)
    previous.stop()
    current = create_telegram("same")

    current.send_msg(title="定向通知", userid="123")

    assert current.bot.send_message.call_args.kwargs["chat_id"] == "123"


def test_channel_defaults_and_explicit_chat_remain_independent(create_telegram: Callable[..., Telegram]) -> None:
    """普通通知使用各自默认聊天；明确指定原会话时仍优先使用原会话。"""
    first = create_telegram("first", "-1001")
    second = create_telegram("second", "-1002")
    second._update_user_chat_mapping(123, -1022)

    first.send_msg(title="普通通知")
    second.send_msg(title="普通通知")
    assert first.bot.send_message.call_args.kwargs["chat_id"] == "-1001"
    assert second.bot.send_message.call_args.kwargs["chat_id"] == "-1002"

    first.send_msg(title="交互回复", userid="123", original_chat_id="-1033")
    assert first.bot.send_message.call_args.kwargs["chat_id"] == "-1033"


def test_typing_and_shutdown_do_not_stop_another_channel(create_telegram: Callable[..., Telegram]) -> None:
    """两个机器人面对相同 chat_id 时，启动和关闭其中一个不应停止另一个的 typing。"""
    first = create_telegram("first")
    second = create_telegram("second")
    first._start_typing_task("123", initial_delay_seconds=30)
    first_event = first._typing_stop_flags["123"]
    second._start_typing_task("123", initial_delay_seconds=30)
    second_event = second._typing_stop_flags["123"]

    assert not first_event.is_set()
    second.stop()
    assert second_event.is_set()
    assert not first_event.is_set()
    first.bot.stop_bot.assert_not_called()


def test_ignored_group_message_does_not_change_reply_destination(create_telegram: Callable[..., Telegram]) -> None:
    """未提及机器人的群消息不得把已有私聊目标改写成群聊。"""
    client = create_telegram("first")
    client._update_user_chat_mapping(123, 123)
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=123), chat=SimpleNamespace(id=-1011, type="supergroup"),
        text="普通群聊消息", entities=[], reply_to_message=None,
    )

    client.bot.message_handlers["messages"](message)
    client.send_msg(title="定向通知", userid="123")

    assert client.bot.send_message.call_args.kwargs["chat_id"] == "123"


@pytest.mark.parametrize("chat_type,chat_id,text", [
    ("private", 123, "搜索电影"),
    ("supergroup", -1011, "@test_bot 搜索电影"),
])
def test_accepted_message_updates_only_its_own_channel(
    create_telegram: Callable[..., Telegram], monkeypatch: pytest.MonkeyPatch,
    chat_type: str, chat_id: int, text: str,
) -> None:
    """私聊与提及机器人的群消息仍可在本渠道记录并回复原会话。"""
    first = create_telegram("first")
    second = create_telegram("second")
    second._update_user_chat_mapping(123, -1022)
    payload = {"message_id": 1, "text": text, "chat": {"id": chat_id}}
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=123), chat=SimpleNamespace(id=chat_id, type=chat_type),
        text=text, entities=[], reply_to_message=None, to_dict=lambda: payload,
    )
    request = Mock()
    request.post_res.return_value = SimpleNamespace(status_code=200)
    monkeypatch.setattr("app.modules.telegram.telegram.RequestUtils", lambda **_kwargs: request)

    first.bot.message_handlers["messages"](message)
    first.send_msg(title="交互回复", userid="123")
    second.send_msg(title="交互回复", userid="123")

    assert first.bot.send_message.call_args.kwargs["chat_id"] == str(chat_id)
    assert second.bot.send_message.call_args.kwargs["chat_id"] == "-1022"
    request.post_res.assert_called_once_with(first._ds_url, json=payload)
