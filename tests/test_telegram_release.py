"""正式版 Telegram 公告的内容、长度与发送确认测试；全程不连接 Telegram。"""

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.telegram_release import build_release_payload, main, validate_telegram_response


def test_announcement_preserves_notes_as_plain_text() -> None:
    """资源标签和 Markdown 特殊字符原样展示，不影响公告链接或频道目标。"""
    notes = '- 修复 <CHS>、A&B 和 [资源] 识别。\n- 支持 "引号" 与 `latest`。\n'
    payload = build_release_payload("v2.15.15", notes, "4Nest/moviepilot-neo")
    assert payload["chat_id"] == "@MoviePilot_NEO"
    assert payload["text"].startswith("MoviePilot NEO v2.15.15 正式版更新\n\n")
    assert notes.strip() in payload["text"]
    assert payload["text"].endswith("https://github.com/4Nest/moviepilot-neo/releases/tag/v2.15.15")
    assert "ghcr.io/4nest/moviepilot-neo:latest" in payload["text"]
    assert "parse_mode" not in payload
    assert payload["link_preview_options"] == {"is_disabled": True}


@pytest.mark.parametrize("notes", ["", "\n", "# 自动生成记录", "- 一项修复\n不符合说明格式"])
def test_announcement_rejects_missing_or_invalid_notes(notes: str) -> None:
    """缺少维护好的改动说明时，不应向频道发送空公告或任意文本。"""
    with pytest.raises(ValueError, match="无序列表"):
        build_release_payload("v2.15.15", notes, "4Nest/moviepilot-neo")


@pytest.mark.parametrize("line", ["- " + "修复" * 40, "- " + "🚀" * 40, "- " + "长" * 5000])
def test_long_announcement_keeps_complete_bullets_and_release_link(line: str) -> None:
    """超长说明压缩为单条合法消息，不能截断条目、Unicode 字符或发布链接。"""
    payload = build_release_payload("v2.15.15", "\n".join([line] * 100), "4Nest/moviepilot-neo")
    text = payload["text"]
    assert len(text.encode("utf-16-le")) // 2 <= 4096
    assert "更多改动请查看发布说明" in text
    assert text.endswith("https://github.com/4Nest/moviepilot-neo/releases/tag/v2.15.15")
    assert all(item == line for item in text.splitlines() if item.startswith("- "))


def test_success_requires_a_message_in_the_target_channel() -> None:
    """只接受目标频道返回的有效消息编号，频道用户名比较不区分大小写。"""
    assert validate_telegram_response({
        "ok": True,
        "result": {"message_id": 123, "chat": {"type": "channel", "username": "moviepilot_neo"}},
    }) == 123


@pytest.mark.parametrize("response", [
    None, [], {}, {"ok": False, "description": "Forbidden"},
    {"ok": "true"}, {"ok": True, "result": True},
    {"ok": True, "result": {"message_id": 0}},
    {"ok": True, "result": {"message_id": True}},
    {"ok": True, "result": {"message_id": 123, "chat": {"type": "private", "username": "MoviePilot_NEO"}}},
    {"ok": True, "result": {"message_id": 123, "chat": {"type": "channel", "username": "another"}}},
])
def test_invalid_or_failed_response_is_not_reported_as_success(response: Any) -> None:
    """网络层成功、业务失败或错误频道不能让发布任务误报通知成功。"""
    with pytest.raises(ValueError):
        validate_telegram_response(response)


def test_prepare_cli_writes_utf8_json(tmp_path: Path) -> None:
    """工作流准备命令生成可直接提交给 Telegram 的 JSON 文件。"""
    notes_file = tmp_path / "notes.md"
    notes_file.write_text("- 修复订阅时间。\n", encoding="utf-8")
    output = tmp_path / "payload.json"
    assert main([
        "prepare", "--version", "v2.15.15", "--repository", "4Nest/moviepilot-neo",
        "--notes-file", str(notes_file), "--output", str(output),
    ]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["text"].startswith("MoviePilot NEO")


def test_check_cli_reports_success_link_and_rejects_invalid_json(tmp_path: Path, capsys) -> None:
    """工作流只在确认发送后输出消息链接，非 JSON 响应必须返回失败。"""
    response_file = tmp_path / "response.json"
    response_file.write_text(json.dumps({
        "ok": True,
        "result": {"message_id": 123, "chat": {"type": "channel", "username": "MoviePilot_NEO"}},
    }), encoding="utf-8")
    assert main(["check", "--response-file", str(response_file)]) == 0
    assert "https://t.me/MoviePilot_NEO/123" in capsys.readouterr().out
    response_file.write_text("<html>Bad Gateway</html>", encoding="utf-8")
    assert main(["check", "--response-file", str(response_file)]) == 1
    assert "已发送" not in capsys.readouterr().out
