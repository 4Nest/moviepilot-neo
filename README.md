<p align="center">
  <img src="https://raw.githubusercontent.com/4Nest/moviepilot-neo-frontend/beta/docs/neo-icon.png" width="104" alt="MoviePilot NEO Logo" />
</p>

<h1 align="center">MoviePilot NEO</h1>

<p align="center">
  面向 NAS 的精简 MoviePilot 使用体验。
</p>

<p align="center">
  <a href="https://github.com/4Nest/moviepilot-neo/actions/workflows/build-custom.yml"><img src="https://github.com/4Nest/moviepilot-neo/actions/workflows/build-custom.yml/badge.svg?branch=beta" alt="Build" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-GPL--3.0-blue.svg" alt="GPL-3.0" /></a>
  <img src="https://img.shields.io/badge/image-linux%2Famd64-2496ED.svg" alt="linux/amd64" />
</p>

> [!NOTE]
> 上游 MoviePilot v2 已停止维护；NEO 是 `4Nest` 基于 v2 的个人定制版本，不代表官方项目。

### Docker

`latest`（正式稳定版）与 `beta`（开发测试版）同步发布到 **Docker Hub** 与 **GHCR**（`linux/amd64`，关闭 `provenance` 证明索引包装以适配飞牛 fnOS / 群晖等 NAS 原生镜像更新检测）：

```bash
# 推荐飞牛 OS (fnOS) / 国内含 Docker Hub 镜像加速环境使用：
docker pull 4nest/moviepilot-neo:latest
docker pull 4nest/moviepilot-neo:beta

# 海外或直连 GHCR 环境：
docker pull ghcr.io/4nest/moviepilot-neo:latest
docker pull ghcr.io/4nest/moviepilot-neo:beta
```

### 从上游 MoviePilot v3 首次迁移至 NEO

直接将原有 v3 容器的 `/config` 目录（或 PostgreSQL 数据库）挂载至 `moviepilot-neo` 启动即可：

- **全自动备份与适配**：首次启动检测到上游 `v3` 数据库时，会自动将 SQLite 备份为 `/config/user.db.v3-backup-<时间戳>`（并归档 `/config/plugins_backup`），无损回填订阅/下载/整理历史的媒体 ID（`TMDB` / `豆瓣` / `Bangumi` / `AniList` 等），从 `plugininstance` 恢复全部插件配置，将 `MediaClassificationPolicy` 二级分类策略还原为 `category.yaml`，并对齐表结构与管理员账号。
- **手动预检 / 执行 CLI**：也可在容器内运行 `moviepilot migrate-v3 --dry-run` 预览迁移报告，或 `moviepilot migrate-v3` 手动触发。


## 项目入口

| 内容 | 链接 |
| --- | --- |
| 前端仓库 | [4Nest/moviepilot-neo-frontend](https://github.com/4Nest/moviepilot-neo-frontend) |
| 上游项目 | [jxxghp/MoviePilot](https://github.com/jxxghp/MoviePilot) |
| Telegram 频道 | [MoviePilot_NEO](https://t.me/MoviePilot_NEO) |
| CLI 使用 | [docs/cli.md](docs/cli.md) |
| 环境诊断 | [docs/doctor.md](docs/doctor.md) |
| 订阅生命周期 | [docs/subscribe-lifecycle.md](docs/subscribe-lifecycle.md) |
| MCP 接入 | [docs/mcp-api.md](docs/mcp-api.md) |
| 问题反馈 | [Issues](https://github.com/4Nest/moviepilot-neo/issues) |

## License

[GNU General Public License v3.0](LICENSE)，与上游许可证保持一致。
