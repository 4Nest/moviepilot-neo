"""生成正式版 Telegram 公告并校验发送结果；网络发送由发布工作流负责。"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional


RELEASE_CHANNEL = "@MoviePilot_NEO"
MAX_MESSAGE_LENGTH = 4096


def _text_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def build_release_payload(version: str, notes: str, repository: str) -> dict[str, Any]:
    """把维护好的改动列表生成单条纯文本公告，超长时保留完整条目和发布链接。"""
    lines = [line.strip() for line in notes.splitlines() if line.strip()]
    if not lines or any(not line.startswith("- ") for line in lines):
        raise ValueError("发布说明必须是非空的中文无序列表")
    header = f"MoviePilot NEO {version} 正式版更新\n\n"
    footer = (
        "\n\n镜像：ghcr.io/4nest/moviepilot-neo:latest\n"
        f"发布说明：https://github.com/{repository}/releases/tag/{version}"
    )
    summary = "\n…更多改动请查看发布说明。"
    text = header + "\n".join(lines) + footer
    if _text_length(text) > MAX_MESSAGE_LENGTH:
        selected = []
        for line in lines:
            candidate = header + "\n".join([*selected, line]) + summary + footer
            if _text_length(candidate) > MAX_MESSAGE_LENGTH:
                break
            selected.append(line)
        text = header + "\n".join(selected) + summary + footer
    if _text_length(text) > MAX_MESSAGE_LENGTH:
        raise ValueError("版本号或发布链接过长")
    return {
        "chat_id": RELEASE_CHANNEL,
        "text": text,
        "link_preview_options": {"is_disabled": True},
    }


def validate_telegram_response(response: Any) -> int:
    """确认 Telegram 已向目标频道发送消息，拒绝把 HTTP 成功误当作发送成功。"""
    if not isinstance(response, dict) or response.get("ok") is not True:
        raise ValueError("Telegram 发送失败，请检查机器人 Token、频道发文权限和 API 限制")
    result = response.get("result")
    if not isinstance(result, dict) or type(result.get("message_id")) is not int or result["message_id"] <= 0:
        raise ValueError("Telegram 未返回有效的消息编号")
    chat = result.get("chat")
    if not isinstance(chat, dict) or chat.get("type") != "channel" \
            or str(chat.get("username", "")).casefold() != RELEASE_CHANNEL[1:].casefold():
        raise ValueError("Telegram 返回的目标频道不匹配")
    return result["message_id"]


def main(argv: Optional[list[str]] = None) -> int:
    """生成发送载荷或验证 API 响应，执行过程不读取机器人凭证。"""
    parser = argparse.ArgumentParser(description="正式版 Telegram 发布公告")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--version", required=True)
    prepare.add_argument("--repository", required=True)
    prepare.add_argument("--notes-file", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    check = commands.add_parser("check")
    check.add_argument("--response-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            payload = build_release_payload(
                args.version, args.notes_file.read_text(encoding="utf-8"), args.repository,
            )
            args.output.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        else:
            response = json.loads(args.response_file.read_text(encoding="utf-8"))
            message_id = validate_telegram_response(response)
            print(f"正式版公告已发送：https://t.me/{RELEASE_CHANNEL[1:]}/{message_id}")
    except (OSError, ValueError) as error:
        print(f"Telegram 公告处理失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
