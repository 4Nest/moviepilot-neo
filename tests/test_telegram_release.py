"""正式版 Telegram 公告的内容、长度与发送确认测试；全程不连接 Telegram。"""

import json
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

from scripts.telegram_release import build_release_payload, main, validate_telegram_response


def _display_text(markup: str) -> str:
    parser = HTMLParser(convert_charrefs=True)
    parts = []
    parser.handle_data = parts.append
    parser.feed(markup)
    return "".join(parts)


def test_announcement_preserves_notes_with_readable_layout() -> None:
    """公告具有标题、条目间距、等宽镜像和链接按钮，资源标签仍按原文显示。"""
    notes = '- 修复 <CHS>、A&B 和 [资源] 识别。\n- 支持 "引号" 与 `latest`。\n'
    payload = build_release_payload("v2.15.15", notes, "4Nest/moviepilot-neo")
    assert payload["chat_id"] == "@MoviePilot_NEO"
    assert payload["text"].startswith("🚀 <b>MoviePilot NEO</b>\n<code>v2.15.15</code> · 正式版更新\n\n")
    assert '• 修复 <CHS>、A&B 和 [资源] 识别。\n\n• 支持 "引号" 与 `latest`。' in _display_text(payload["text"])
    assert "<CHS>" not in payload["text"]
    assert "<code>ghcr.io/4nest/moviepilot-neo:latest</code>" in payload["text"]
    assert payload["parse_mode"] == "HTML"
    assert payload["link_preview_options"] == {"is_disabled": True}
    assert payload["reply_markup"]["inline_keyboard"] == [[
        {"text": "完整更新日志", "url": "https://github.com/4Nest/moviepilot-neo/releases/tag/v2.15.15"},
        {"text": "项目主页", "url": "https://github.com/4Nest/moviepilot-neo"},
    ]]


@pytest.mark.parametrize("count,collapsed", [(1, False), (6, False), (7, True), (15, True)])
def test_long_lists_can_expand_without_losing_entries(count: int, collapsed: bool) -> None:
    """短公告直接展示，长公告支持折叠，展开后仍有全部条目且保持原有顺序。"""
    notes = "\n".join(f"- 修复第 {index} 项问题。" for index in range(count))
    markup = build_release_payload("v2.15.15", notes, "4Nest/moviepilot-neo")["text"]
    assert ("<blockquote expandable>" in markup) is collapsed
    entries = [line for line in _display_text(markup).splitlines() if line.startswith("• ")]
    assert entries == [f"• 修复第 {index} 项问题。" for index in range(count)]


def test_html_in_notes_and_version_cannot_inject_formatting_or_links() -> None:
    """版本与说明中的 HTML 必须只显示原文，不能插入标签、链接或打断折叠块。"""
    version = 'v2.15.15<beta>&"'
    note = '</blockquote><a href="https://example.com">内容</a>'
    payload = build_release_payload(version, f"- {note}", "4Nest/moviepilot-neo")
    assert version in _display_text(payload["text"])
    assert note in _display_text(payload["text"])
    assert '<a href=' not in payload["text"]
    assert '</blockquote>' not in payload["text"]
    assert payload["reply_markup"]["inline_keyboard"][0][0]["url"].endswith('v2.15.15%3Cbeta%3E%26%22')


def test_escaped_characters_do_not_prematurely_truncate_notes() -> None:
    """转义后 HTML 超过 4096 字符但显示文本未超限时，不能丢弃完整条目。"""
    note = "&<>" * 400
    payload = build_release_payload("v2.15.15", f"- {note}", "4Nest/moviepilot-neo")
    assert len(payload["text"]) > 4096
    assert f"• {note}" in _display_text(payload["text"])
    assert "更多改动" not in payload["text"]
    assert "<blockquote expandable>" in payload["text"]


def test_oversized_version_is_rejected() -> None:
    """仅版本标题就超限时必须失败，不能提交必然被 Telegram 拒绝的消息。"""
    with pytest.raises(ValueError, match="版本号过长"):
        build_release_payload("v" * 4096, "- 修复问题。", "4Nest/moviepilot-neo")


@pytest.mark.parametrize("notes", ["", "\n", "# 自动生成记录", "- 一项修复\n不符合说明格式"])
def test_announcement_rejects_missing_or_invalid_notes(notes: str) -> None:
    """缺少维护好的改动说明时，不应向频道发送空公告或任意文本。"""
    with pytest.raises(ValueError, match="无序列表"):
        build_release_payload("v2.15.15", notes, "4Nest/moviepilot-neo")


@pytest.mark.parametrize("line", ["- " + "修复" * 40, "- " + "🚀" * 40, "- " + "长" * 5000],
                         ids=["many_bullets", "emoji", "oversized_bullet"])
def test_long_announcement_keeps_complete_bullets_and_release_link(line: str) -> None:
    """超长说明压缩为单条合法消息，不能截断条目、Unicode 字符或发布链接。"""
    payload = build_release_payload("v2.15.15", "\n".join([line] * 100), "4Nest/moviepilot-neo")
    text = _display_text(payload["text"])
    assert len(text.encode("utf-16-le")) // 2 <= 4096
    assert "更多改动请查看完整更新日志" in text
    assert payload["reply_markup"]["inline_keyboard"][0][0]["url"].endswith("/releases/tag/v2.15.15")
    assert all(item == f"• {line[2:]}" for item in text.splitlines() if item.startswith("• "))


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
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert _display_text(payload["text"]).startswith("🚀 MoviePilot NEO")
    assert payload["parse_mode"] == "HTML"


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
