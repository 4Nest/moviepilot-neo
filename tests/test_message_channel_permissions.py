import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.modules.telegram import TelegramModule


class TestMessageChannelPermissions(unittest.TestCase):
    """消息渠道管理员权限测试。"""

    def test_telegram_command_callback_blocks_non_admin(self):
        """Telegram 命令型按钮回调应拦截非管理员。"""
        module = TelegramModule()
        client = SimpleNamespace(answer_callback_query=SimpleNamespace())

        import unittest.mock

        client = SimpleNamespace(answer_callback_query=unittest.mock.Mock(), bot_username=None)

        with patch.object(
            module,
            "get_config",
            return_value=SimpleNamespace(
                name="telegram-test", config={"TELEGRAM_ADMINS": "10001"}
            ),
        ), patch.object(module, "get_instance", return_value=client):
            message = module.message_parser(
                source="telegram-test",
                body=json.dumps(
                    {
                        "callback_query": {
                            "id": "callback-1",
                            "from": {"id": 10002, "username": "tester"},
                            "data": "/sites",
                            "message": {"message_id": 12, "chat": {"id": "-100"}},
                        }
                    }
                ),
                form={},
                args={},
            )

        self.assertIsNone(message)
        client.answer_callback_query.assert_called_once_with(
            callback_query_id="callback-1",
            text="只有管理员才有权限执行此命令",
            show_alert=True,
        )

    def test_telegram_non_command_callbacks_allow_non_admin(self):
        """非命令型按钮回调不应套用管理员限制。"""
        telegram_module = TelegramModule()
        import unittest.mock

        telegram_client = SimpleNamespace(
            answer_callback_query=unittest.mock.Mock(), bot_username=None
        )
        with patch.object(
            telegram_module,
            "get_config",
            return_value=SimpleNamespace(
                name="telegram-test", config={"TELEGRAM_ADMINS": "10001"}
            ),
        ), patch.object(telegram_module, "get_instance", return_value=telegram_client):
            telegram_message = telegram_module.message_parser(
                source="telegram-test",
                body=json.dumps(
                    {
                        "callback_query": {
                            "id": "callback-1",
                            "from": {"id": 10002, "username": "tester"},
                            "data": "sites:req:refresh",
                        }
                    }
                ),
                form={},
                args={},
            )
        self.assertIsNotNone(telegram_message)
        telegram_client.answer_callback_query.assert_not_called()


if __name__ == "__main__":
    unittest.main()
