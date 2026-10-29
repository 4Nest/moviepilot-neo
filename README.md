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
  <img src="https://img.shields.io/badge/image-linux%2Famd64%20%7C%20linux%2Farm64-2496ED.svg" alt="linux/amd64 | linux/arm64" />
</p>

> [!NOTE]
> 上游 MoviePilot v2 已停止维护；NEO 是 `4Nest` 基于 v2 的个人定制版本，不代表官方项目。

### Docker

`latest` 为正式稳定版，发布到 Docker Hub 与 GHCR，支持 `linux/amd64`、`linux/arm64`：

```bash
docker pull 4nest/moviepilot-neo:latest
# 或
docker pull ghcr.io/4nest/moviepilot-neo:latest
```

`beta` 为开发测试版（跟随 `beta` 分支更新），仅发布于 GHCR，仅提供 `linux/amd64`：

```bash
docker pull ghcr.io/4nest/moviepilot-neo:beta
```


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
