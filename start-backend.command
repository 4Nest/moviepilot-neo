#!/bin/zsh
# MoviePilot Neo 后端启动脚本
# 注意：请用自己的终端（或双击 .command）运行本脚本。
# macOS 的「本地网络」权限按启动者归属，由他人代理启动会导致无法访问局域网服务（如 Emby）。
cd "$(dirname "$0")"
exec venv/bin/python -m app.main
