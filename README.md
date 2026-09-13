<p align="center">
  <img src="https://raw.githubusercontent.com/4Nest/moviepilot-neo-frontend/neo/docs/neo-icon.png" width="104" alt="MoviePilot NEO Logo" />
</p>

<h1 align="center">MoviePilot NEO</h1>

<p align="center">
  基于 MoviePilot v2 的个人定制分支，专注更清晰的媒体管理体验。
</p>

<p align="center">
  <a href="https://github.com/4Nest/moviepilot-neo/actions/workflows/build-custom.yml"><img src="https://github.com/4Nest/moviepilot-neo/actions/workflows/build-custom.yml/badge.svg?branch=neo" alt="Build" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-GPL--3.0-blue.svg" alt="GPL-3.0" /></a>
  <img src="https://img.shields.io/badge/image-linux%2Famd64-2496ED.svg" alt="linux/amd64" />
</p>

> [!IMPORTANT]
> MoviePilot NEO 是 `4Nest` 维护的个人公开 Fork，不是 MoviePilot 官方版本。项目基于上游 `v2` 分支，开发主线为 `neo`。

## 快速开始

### Docker / Unraid / NAS

当前镜像面向 **x86-64** 主机，仅提供 `linux/amd64` 架构：

```bash
docker pull ghcr.io/4nest/moviepilot-neo:latest
```

在 Unraid 或 NAS 的容器管理界面中创建容器：

| 配置 | 值 |
| --- | --- |
| Image | `ghcr.io/4nest/moviepilot-neo:latest` |
| 前端端口 | `3000`（日常访问入口） |
| 后端端口 | `3001`（通常无需映射） |
| 持久化目录 | 宿主机目录 → `/config` |

将 `/config` 映射到宿主机可读写目录。媒体目录和下载目录按自己的 NAS 路径配置；不要把数据留在容器可写层中。

## NEO 定制方向

| 方向 | 内容 |
| --- | --- |
| 品牌与界面 | NEO 霓虹 Logo、登录页、关于页及更聚焦的页面结构 |
| 识别与订阅 | 识别结果信息层级、订阅管理和批量操作体验整理 |
| 工程与运行 | 后端与前端独立维护，`neo` 分支自动构建 GHCR 镜像 |

NEO 是个人定制体验，不承诺与上游的功能范围、界面或发布节奏完全一致。通用功能请参考 [MoviePilot 官方文档](https://movie-pilot.org)。

## 从源码运行

需要 **Python 3.11+** 与 **Git**。macOS / Linux：

```bash
git clone --branch neo https://github.com/4Nest/moviepilot-neo.git
cd moviepilot-neo
python3 -m venv venv
source venv/bin/activate
python -m pip install -r requirements.txt
./scripts/start-local.sh
```

开发、测试或静态检查依赖：

```bash
python -m pip install -r requirements-dev.in
```

完整开发环境、配置目录和资源准备见 [开发环境设置](docs/development-setup.md)。

## 更新与回滚

`latest` 是滚动标签。更新前先记录当前镜像 digest：

```bash
docker image inspect \
  --format='{{index .RepoDigests 0}}' \
  ghcr.io/4nest/moviepilot-neo:latest
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
