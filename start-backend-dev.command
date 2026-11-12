#!/bin/zsh
# MoviePilot Neo 后端开发模式启动脚本：app/ 下 .py 文件改动后自动重启后端
# 注意：请用自己的终端（或双击 .command）运行本脚本。
# macOS 的「本地网络」权限按启动者归属，由他人代理启动会导致无法访问局域网服务（如 Emby）。
#
# 不使用 DEV=true：它会停用全部定时服务（订阅搜索、RSS 刷新、下载整理），且当前启动方式下 uvicorn reload 不生效。
# 忽略 app/plugins：插件安装/更新会写入 .py 文件，不忽略会触发重启循环。
# 重启时先发 SIGINT 走后端优雅停止，30 秒未退出再强制结束。
cd "$(dirname "$0")"
exec venv/bin/watchfiles \
    --filter python \
    --ignore-paths app/plugins \
    --sigint-timeout 30 \
    "venv/bin/python -m app.main" \
    app
