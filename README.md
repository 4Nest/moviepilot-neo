<p align="center">
  <img src="https://raw.githubusercontent.com/4Nest/moviepilot-neo-frontend/neo/docs/neo-icon.png" width="104" alt="MoviePilot NEO Logo" />
</p>

<h1 align="center">MoviePilot NEO</h1>

<p align="center">
  面向 NAS 的精简 MoviePilot 使用体验。
</p>

<p align="center">
  <a href="https://github.com/4Nest/moviepilot-neo/actions/workflows/build-custom.yml"><img src="https://github.com/4Nest/moviepilot-neo/actions/workflows/build-custom.yml/badge.svg?branch=neo" alt="Build" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-GPL--3.0-blue.svg" alt="GPL-3.0" /></a>
  <img src="https://img.shields.io/badge/image-linux%2Famd64-2496ED.svg" alt="linux/amd64" />
</p>

> [!NOTE]
> 上游 MoviePilot v2 已停止维护；NEO 是 `4Nest` 基于 v2 的个人定制版本，不代表官方项目。

### Docker / Unraid / NAS

当前镜像面向 **x86-64** 主机，仅提供 `linux/amd64` 架构。

开发测试版（跟随 `neo` 分支更新）：

```bash
docker pull ghcr.io/4nest/moviepilot-neo:neo
```

正式稳定版（GitHub Release 版本标签）：

```bash
docker pull ghcr.io/4nest/moviepilot-neo:latest
```

也可以固定到不可变版本：

```bash
docker pull ghcr.io/4nest/moviepilot-neo:v1.0.0
```

在 Unraid 或 NAS 的容器管理界面中创建容器时，正式环境建议使用 `:latest` 或具体版本标签，测试新提交时使用 `:neo`：

| 配置 | 值 |
| --- | --- |
| Image | `ghcr.io/4nest/moviepilot-neo:latest` |
| 前端端口 | `3000`（日常访问入口） |
| 后端端口 | `3001`（通常无需映射） |
| 持久化目录 | 宿主机目录 → `/config` |

将 `/config` 映射到宿主机可读写目录。媒体目录和下载目录按自己的 NAS 路径配置；不要把数据留在容器可写层中。

## 项目定位

NEO 只做一件事：在保留 MoviePilot 核心能力的基础上，提供更精简、更聚焦的日常使用体验。

## 从源码运行

需要 **Python 3.11+** 与 **Git**。macOS / Linux：

```bash
git clone --branch neo https://github.com/4Nest/moviepilot-neo.git
cd moviepilot-neo
python3 -m venv venv
source venv/bin/activate
## 更新与回滚

`latest` 仅在推送 `v*` 版本标签时更新，代表最近一次正式稳定版；`neo` 跟随开发分支更新，仅用于测试。

正式版更新：

```bash
docker pull ghcr.io/4nest/moviepilot-neo:latest
```

需要固定版本时使用具体版本标签：

```bash
docker pull ghcr.io/4nest/moviepilot-neo:v1.0.0
```

回滚时使用此前记录的不可变 digest，而不是重新拉取 `latest`：

```text
ghcr.io/4nest/moviepilot-neo@sha256:<digest>
```

更新：

```bash
docker pull ghcr.io/4nest/moviepilot-neo:latest
```

回滚时使用此前记录的不可变 digest，而不是重新拉取 `latest`：

```text
ghcr.io/4nest/moviepilot-neo@sha256:<digest>
```

## 项目入口

| 内容 | 链接 |
| --- | --- |
| 前端仓库 | [4Nest/moviepilot-neo-frontend](https://github.com/4Nest/moviepilot-neo-frontend) |
| 上游项目 | [jxxghp/MoviePilot](https://github.com/jxxghp/MoviePilot) |
| CLI 使用 | [docs/cli.md](docs/cli.md) |
| 环境诊断 | [docs/doctor.md](docs/doctor.md) |
| 订阅生命周期 | [docs/subscribe-lifecycle.md](docs/subscribe-lifecycle.md) |
| 问题反馈 | [Issues](https://github.com/4Nest/moviepilot-neo/issues) |

## License

[GNU General Public License v3.0](LICENSE)，与上游许可证保持一致。
