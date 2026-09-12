# MoviePilot Neo

[MoviePilot](https://github.com/jxxghp/MoviePilot) v2 的个人分支（Fork），基于上游 `v2` 分支。

<p>
  <img src="https://raw.githubusercontent.com/4Nest/moviepilot-neo-frontend/neo/docs/neo-icon.png" width="96" alt="NEO" />
</p>

## 与官方版本的差异

**界面精简**
- 登录页去Logo，只保留表单
- 移除：日历页、热门订阅、分享统计、订阅分享筛选器、AI 助手悬浮入口、智能助手配置（含初始化向导步骤）
- 通知渠道只保留 Telegram / 企业微信
- 多语言精简为仅简体中文

**功能增强**
- 识别测试页重构：结果区重排（名称(年份)、突出季集、分类上移、识别标题区分、媒体 ID 徽章、查看详情直达官方页）
- 订阅分享：批量管理模式（选择/批量删除）、详情页重排
- 重命名格式：简易/进阶双模式编辑器（字段流拼接、jinja 表达式支持、实时预览、重置默认）

**后端**
- 识别接口失败时返回完整元信息（前端可展示识别词处理过程）
- 自动更新源指向本仓库（`4Nest/moviepilot-neo`）
- 通知渠道后端同步精简（删除 discord/feishu/qqbot/slack/synologychat/vocechat/webpush/wechatclawbot 模块）

## Docker 镜像

```
ghcr.io/4nest/moviepilot-neo:latest
```

- 仅 `linux/amd64`（面向常见 x86 Unraid / NAS 主机）
- 每次合并到 `neo` 分支自动构建并覆盖 `latest`
- 前端产物来自 [moviepilot-neo-frontend](https://github.com/4Nest/moviepilot-neo-frontend) 的 Release

**更新与回滚**：`latest` 会被持续覆盖，生产更新前请记录当前镜像 digest，回滚用 digest 部署：

```
ghcr.io/4nest/moviepilot-neo@sha256:<digest>
```

## 开发与同步流程

- `neo` 分支：开发主线，CI 构建与发布都走它
- 功能分支 `feature/<name>` → PR 到 `neo`（全量测试门禁）→ squash 合并触发镜像构建
- 上游同步：`git fetch upstream && git merge upstream/v2`，冲突在本地 `neo` 解决，不强制推送

## 文档与社区

- 官方文档（功能通用）：[movie-pilot.org](https://movie-pilot.org)
- 上游项目：[jxxghp/MoviePilot](https://github.com/jxxghp/MoviePilot)
- 前端仓库：[4Nest/moviepilot-neo-frontend](https://github.com/4Nest/moviepilot-neo-frontend)

## License

GPL-3.0（与上游一致）
