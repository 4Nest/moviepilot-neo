import unittest
from unittest.mock import patch

from app.agent import _finish_processing_status
from app.schemas.message import ChannelCapability, ChannelCapabilityManager
from app.schemas.types import MessageChannel


class TestMessageProcessingStatus(unittest.TestCase):
    def test_processing_status_capability_only_enabled_for_supported_channels(self):
        # 仅 Telegram 保留 PROCESSING_STATUS 能力
        supported = {MessageChannel.Telegram}

        for channel in MessageChannel:
            self.assertEqual(
                ChannelCapabilityManager.supports_capability(
                    channel, ChannelCapability.PROCESSING_STATUS
                ),
                channel in supported,
            )

    def test_agent_finish_processing_status_uses_module_interface(self):
        status = {
            "channel": MessageChannel.Telegram.value,
            "source": "telegram-main",
            "userid": "10001",
            "message_id": None,
            "chat_id": "-100",
            "metadata": {"kind": "typing"},
        }

        with patch("app.agent.AgentChain") as chain_cls:
            _finish_processing_status(status, user_id="fallback")

        chain_cls.return_value.finish_message_processing_status.assert_called_once_with(
            status=status,
            userid="fallback",
        )


if __name__ == "__main__":
    unittest.main()
