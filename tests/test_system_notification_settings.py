import asyncio
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import schemas
from app.api.endpoints import system as system_endpoint
from app.modules.telegram import TelegramModule
from app.modules.wechat import WechatModule
from app.schemas.types import SystemConfigKey


def _telegram_conf(**overrides) -> schemas.NotificationConf:
    payload = {
        "name": "主通知",
        "type": "telegram",
        "enabled": True,
        "config": {
            "TELEGRAM_TOKEN": "token-value",
            "TELEGRAM_CHAT_ID": "chat-value",
        },
        "switchs": ["订阅"],
    }
    payload.update(overrides)
    return schemas.NotificationConf.model_validate(payload)


def test_notification_setting_normalizes_valid_channels_and_rejects_dirty_payloads():
    """通知写入必须规范化空白，并在落库前拒绝未知字段与重名渠道。"""
    value = [
        {
            "name": " 主通知 ",
            "type": "telegram",
            "enabled": True,
            "config": {
                "TELEGRAM_TOKEN": " token-value ",
                "TELEGRAM_CHAT_ID": " chat-value ",
            },
            "switchs": ["订阅"],
        }
    ]

    normalized = system_endpoint._validate_setting_value(
        SystemConfigKey.Notifications.value,
        value,
    )

    assert normalized[0]["name"] == "主通知"
    assert normalized[0]["config"] == {
        "TELEGRAM_TOKEN": "token-value",
        "TELEGRAM_CHAT_ID": "chat-value",
    }

    dirty = [{**value[0], "config": {**value[0]["config"], "TOKEN_TYPO": "secret"}}]
    with pytest.raises(HTTPException) as exc_info:
        system_endpoint._validate_setting_value(SystemConfigKey.Notifications.value, dirty)
    assert exc_info.value.status_code == 422
    assert "secret" not in str(exc_info.value.detail)

    with pytest.raises(HTTPException) as exc_info:
        system_endpoint._validate_setting_value(
            SystemConfigKey.Notifications.value,
            [value[0], value[0]],
        )
    assert exc_info.value.detail == "通知渠道名称不能重复"

def test_notification_credentials_are_masked_and_restored_by_stable_id():
    """读取不得返回凭证明文，遮罩值写回时必须按渠道 ID 恢复原凭证。"""
    saved = [
        {
            "id": "channel-1",
            "name": "主通知",
            "type": "telegram",
            "enabled": True,
            "config": {
                "TELEGRAM_TOKEN": "token-value",
                "TELEGRAM_CHAT_ID": "chat-value",
            },
        }
    ]

    visible, invalid = system_endpoint._present_notification_settings(saved)

    assert invalid == []
    assert visible[0]["id"] == "channel-1"
    assert visible[0]["config"]["TELEGRAM_TOKEN"] == system_endpoint._SECRET_MASK
    assert "token-value" not in str(visible)

    visible[0]["name"] = "重命名通知"
    restored = system_endpoint._restore_notification_secrets(visible, saved)
    assert restored[0]["name"] == "重命名通知"
    assert restored[0]["config"]["TELEGRAM_TOKEN"] == "token-value"


def test_notification_mask_cannot_be_used_without_matching_saved_channel():
    """新渠道不得把遮罩占位符当成真实凭证落库。"""
    payload = [
        {
            "id": "unknown-channel",
            "name": "伪造渠道",
            "type": "telegram",
            "enabled": True,
            "config": {
                "TELEGRAM_TOKEN": system_endpoint._SECRET_MASK,
                "TELEGRAM_CHAT_ID": "chat-value",
            },
        }
    ]

    with pytest.raises(HTTPException) as exc_info:
        system_endpoint._restore_notification_secrets(payload, [])

    assert exc_info.value.status_code == 422


def test_notification_settings_report_invalid_legacy_channels_without_leaking_secrets():
    """存量非法渠道必须返回可定位诊断，同时隐藏其中的凭证。"""
    raw = [
        {
            "name": "旧 Telegram",
            "type": "telegram",
            "enabled": True,
            "config": {"TELEGRAM_TOKEN": "legacy-token"},
        },
        "broken-row",
    ]

    visible, invalid = system_endpoint._present_notification_settings(raw)

    assert len(visible) == 1
    assert len(invalid) == 2
    assert invalid[0]["id"] == visible[0]["id"]
    assert "TELEGRAM_CHAT_ID" in invalid[0]["errors"][0]
    assert "legacy-token" not in str(visible)
    assert "legacy-token" not in str(invalid)


def test_notification_related_setting_adapters_reject_unknown_values():
    """消息范围、模板和服务配置的非法枚举或未知顶层键不得写入。"""
    invalid_values = [
        (
            SystemConfigKey.NotificationSwitchs.value,
            [{"type": "未知场景", "action": "all"}],
        ),
        (
            SystemConfigKey.NotificationTemplates.value,
            {"unknownTemplate": "{}"},
        ),
        (
            SystemConfigKey.Downloaders.value,
            [{"name": "qb", "type": "qbittorrent", "config": {}, "typo": True}],
        ),
    ]

    for key, value in invalid_values:
        with pytest.raises(HTTPException) as exc_info:
            system_endpoint._validate_setting_value(key, value)
        assert exc_info.value.status_code == 422


def test_notification_send_time_validates_ranges_and_normalizes_legacy_seconds():
    """发送时段应兼容历史秒值、保留跨午夜，并拒绝越界或脏字段。"""
    normalized = system_endpoint._validate_setting_value(
        SystemConfigKey.NotificationSendTime.value,
        [
            {"start": "23:00:59", "end": "07:30:00"},
            {"start": "08:05", "end": "18:45"},
        ],
    )

    assert normalized == [
        {"start": "23:00", "end": "07:30"},
        {"start": "08:05", "end": "18:45"},
    ]

    invalid_values = [
        {"start": "24:00", "end": "07:00"},
        {"start": "23:60", "end": "07:00"},
        {"start": "23:00", "end": "07:00", "timezone": "UTC"},
    ]
    for value in invalid_values:
        with pytest.raises(HTTPException) as exc_info:
            system_endpoint._validate_setting_value(SystemConfigKey.NotificationSendTime.value, value)
        assert exc_info.value.status_code == 422


def test_notification_templates_compile_and_restrict_output_fields():
    """模板写入前必须通过 Jinja 语法及消息输出字段校验。"""
    valid = {
        "subscribeAdded": "{'title': '{{ title_year }}', 'text': '{% if season %}{{ season }}{% endif %}'}",
    }
    assert system_endpoint._validate_setting_value(
        SystemConfigKey.NotificationTemplates.value,
        valid,
    ) == valid

    invalid_templates = [
        {"subscribeAdded": "{'title': '{% if title %}{{ title }}'}"},
        {"subscribeAdded": "['not-a-message']"},
        {"subscribeAdded": "{'title': 'ok', 'secret': '{{ token }}'}"},
    ]
    for value in invalid_templates:
        with pytest.raises(HTTPException) as exc_info:
            system_endpoint._validate_setting_value(SystemConfigKey.NotificationTemplates.value, value)
        assert exc_info.value.status_code == 422

def test_telegram_test_notification_calls_bot_api_without_module_registration():
    """Telegram 测试使用一次性 Bot API 请求，不创建正式 Telegram 客户端。"""
    response = SimpleNamespace(ok=True, content=b"{}", json=lambda: {"ok": True})

    with patch.object(system_endpoint.requests, "post", return_value=response) as post, patch(
        "app.modules.telegram.telegram.Telegram",
        side_effect=AssertionError("不得构造 Telegram 轮询客户端"),
    ):
        assert system_endpoint._send_test_notification(_telegram_conf()) == (True, None)

    post.assert_called_once()
    assert post.call_args.args[0] == "https://api.telegram.org/bottoken-value/sendMessage"
    assert post.call_args.kwargs["json"]["chat_id"] == "chat-value"


def test_wechat_bot_test_notification_always_stops_temporary_client():
    """企业微信机器人测试完成后必须关闭临时长连接。"""
    conf = schemas.NotificationConf.model_validate(
        {
            "name": "机器人",
            "type": "wechat",
            "enabled": True,
            "config": {
                "WECHAT_MODE": "bot",
                "WECHAT_BOT_ID": "bot-id",
                "WECHAT_BOT_SECRET": "bot-secret",
            },
        }
    )
    client = Mock()
    client.send_msg.return_value = True

    with patch("app.modules.wechat.wechatbot.WeChatBot", return_value=client):
        assert system_endpoint._send_test_notification(conf) == (True, None)

    client.send_msg.assert_called_once()
    client.stop.assert_called_once_with()


def test_notification_test_http_validation_does_not_echo_credentials():
    """通知测试接口返回 422 时不得回显非法配置中的凭证值。"""
    app = FastAPI()
    app.include_router(system_endpoint.router)
    app.dependency_overrides[system_endpoint.get_current_admin_async] = lambda: object()
    payload = {
        "name": "主通知",
        "type": "telegram",
        "enabled": True,
        "config": {
            "TELEGRAM_TOKEN": "token-value",
            "TELEGRAM_CHAT_ID": "chat-value",
            "UNKNOWN_SECRET": "must-not-leak",
        },
    }

    response = TestClient(app).post("/notification/test", json=payload)

    assert response.status_code == 422
    assert "must-not-leak" not in response.text


def test_notification_test_endpoint_returns_sanitized_failure_message():
    """临时发送异常不得把可能包含凭证的底层错误回显给前端。"""
    with patch.object(
        system_endpoint,
        "_send_test_notification",
        side_effect=RuntimeError("token-value"),
    ):
        response = asyncio.run(system_endpoint.test_notification(_telegram_conf().model_dump(), _=Mock()))

    assert response.success is False
    assert "token-value" not in response.message



def test_notification_test_restores_saved_masked_secret():
    """从设置页触发测试时应按渠道 ID 使用已保存凭证。"""
    saved = [
        {
            "id": "channel-1",
            "name": "主通知",
            "type": "telegram",
            "enabled": True,
            "config": {
                "TELEGRAM_TOKEN": "token-value",
                "TELEGRAM_CHAT_ID": "chat-value",
            },
        }
    ]
    payload = _telegram_conf(id="channel-1").model_dump()
    payload["config"]["TELEGRAM_TOKEN"] = system_endpoint._SECRET_MASK

    with patch.object(system_endpoint.SystemConfigOper, "get", return_value=saved), patch.object(
        system_endpoint,
        "_send_test_notification",
        return_value=(True, None),
    ) as send:
        response = asyncio.run(system_endpoint.test_notification(payload, _=Mock(id=1)))

    assert response.success is True
    assert send.call_args.args[0].config["TELEGRAM_TOKEN"] == "token-value"


def test_notification_test_returns_structured_failure_reason():
    """第三方失败应转换为稳定原因码和安全提示。"""
    with patch.object(
        system_endpoint,
        "_send_test_notification",
        return_value=(False, "AUTH_FAILED"),
    ), patch.object(system_endpoint.SystemConfigOper, "get", return_value=[]):
        response = asyncio.run(
            system_endpoint.test_notification(_telegram_conf().model_dump(), _=Mock(id=1001))
        )

    assert response.success is False
    assert response.data == {"reason": "AUTH_FAILED"}
    assert "认证" in response.message


def test_notification_test_enforces_per_admin_channel_cooldown():
    """同一管理员不得在五秒内重复测试同一渠道。"""
    system_endpoint._notification_test_last_attempts.clear()
    payload = _telegram_conf(id="channel-cooldown").model_dump()
    with patch.object(system_endpoint.SystemConfigOper, "get", return_value=[]), patch.object(
        system_endpoint,
        "_send_test_notification",
        return_value=(True, None),
    ), patch.object(system_endpoint.time, "monotonic", return_value=100.0):
        first = asyncio.run(system_endpoint.test_notification(payload, _=Mock(id=1002)))
        second = asyncio.run(system_endpoint.test_notification(payload, _=Mock(id=1002)))

    assert first.success is True
    assert second.status_code == 429
    assert b'"reason":"RATE_LIMITED"' in second.body


def test_notification_test_cooldown_cannot_be_bypassed_without_channel_id():
    """省略渠道 ID 的直接请求仍应按类型和名称进入冷却。"""
    system_endpoint._notification_test_last_attempts.clear()
    payload = _telegram_conf().model_dump(exclude={"id"})
    with patch.object(system_endpoint.SystemConfigOper, "get", return_value=[]), patch.object(
        system_endpoint,
        "_send_test_notification",
        return_value=(True, None),
    ), patch.object(system_endpoint.time, "monotonic", return_value=200.0):
        first = asyncio.run(system_endpoint.test_notification(payload, _=Mock(id=1004)))
        second = asyncio.run(system_endpoint.test_notification(payload, _=Mock(id=1004)))

    assert first.success is True
    assert second.status_code == 429


def test_notification_test_rejects_when_global_slots_are_busy():
    """全局测试发送槽位耗尽时应立即返回 429。"""
    slots = Mock()
    slots.acquire.return_value = False
    with patch.object(system_endpoint, "_notification_test_slots", slots), patch.object(
        system_endpoint.SystemConfigOper,
        "get",
        return_value=[],
    ):
        response = asyncio.run(
            system_endpoint.test_notification(_telegram_conf().model_dump(), _=Mock(id=1003))
        )

    assert response.status_code == 429
    assert b'"reason":"RATE_LIMITED"' in response.body
    slots.release.assert_not_called()


def test_notification_modules_report_missing_configuration_explicitly():
    """通知模块无可用实例时返回稳定失败语义，而不是 None。"""
    telegram = TelegramModule()
    wechat = WechatModule()

    with patch.object(telegram, "get_instances", return_value={}), patch.object(
        wechat,
        "get_instances",
        return_value={},
    ):
        assert telegram.test() == (False, "未配置通知渠道")
        assert wechat.test() == (False, "未配置通知渠道")
