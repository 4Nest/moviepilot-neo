## 开发环境设置指南

本文档旨在帮助开发者快速设置开发环境，并说明主程序、开发测试、构建工具和插件依赖的管理边界。

### 环境准备

在开始之前，请确保您的系统已安装以下软件：

- **Python 3.11 或更高版本**
- **pip** (Python 包管理器)
- **Git** (用于版本控制)
- **RAR 解压工具**：本地开发如需测试或使用 `.rar` 字幕包解压，请安装 `unar`、`unrar`、`7z` 或 `bsdtar` 之一；Docker 镜像会内置 `unar`。

Rust 加速扩展通过 `requirements.in` 中固定的 `moviepilot-rust==0.2.9+neo.4` 安装，
`--find-links` 指向 `4Nest/MoviePilot-Rust` 的对应 GitHub Release。pip 与 uv 会选择当前平台的 wheel，
主项目本地开发无需 Rust toolchain。需要修改或发布扩展时，在 `MoviePilot-Rust` 仓库构建并先发布制品。

BT/PT 识别改进配套扩展 `0.2.9+neo.4`（基于上游 `v0.2.9`，兼容 Python 3.11+）。
它通过 `ANIME_EPISODE_REVISION=1` 声明修订集号能力，`BT_PT_RECOGNITION_REVISION=4`
标识标题、文件名及副标题语料验证后的修复。通过每次解析结果的
`anime_names_resolved` 标记告知后端别名已处理，减少重复 Python 解析。
旧扩展继续保留原来的 Python 回退。仅更新后端代码不会获得新的 Rust 识别规则；
应在 Rust 仓库构建目标平台 wheel，安装到实际运行后端的环境，并重启对应进程。
此定制版本通过 GitHub Release 分发，不发布到上游 PyPI 项目；镜像构建会检查万条识别能力标记，
缺少新版扩展时直接失败。前后端版本统一为 `v2.15.15`。

站点语料及完整回归保存在 Rust 仓库 `tests/metainfo/site_samples.json`、
`expanded_samples.json`、`volume_samples.json`、`diverse_samples.json` 和
`recognition_10k.jsonl.gz`。后端集成语料位于 `tests/cases/bt_pt_recognition.json`、
`bt_pt_expanded.json`、`bt_pt_10k.jsonl.gz`，测试均离线。万条集合包含真实采集与格式变体，
生成脚本、来源统计和 SHA-256 清单均随语料保存；测试核对去重数量和校验和。
样本口径、修复行为和验证结果见 `docs/rust-bt-pt-recognition.md`。

副标题专项随 `0.2.9+neo.4` 提供，安装该 wheel 后可运行
`tests/test_subtitle_recognition.py` 的完整一万条 Rust 入口测试。
未安装 revision 4 时这部分测试会明确跳过，485 条 Python 回退用例仍可独立运行。
正式发布时先发布 Rust 制品，再更新上述固定依赖和镜像能力门禁，避免引用尚不存在的下载地址。

Release 说明统一从 `.github/release-notes/<tag>.md` 读取，格式见该目录的 `README.md`。
同版本补充发布保持 `version.py` 和前端版本号不变，通过 `build-custom.yml` 的
`publish_latest=true` 更新镜像，并合并现有版本的改动列表。

正式镜像推送及 Release 说明更新成功后，工作流通过独立的
`Notify Telegram Release Channel` 任务向 `@MoviePilot_NEO` 发布一条公告。
公告使用同一份版本说明，包含版本号、改动列表、latest 镜像和 Release 链接；
超过 Telegram 长度限制时保留完整条目，其余改动通过链接查看。
普通 neo 构建和失败的正式发布不会发送公告，同版本的 latest 补充发布也会通知。

首次启用需在 `4Nest/moviepilot-neo` 的 **Settings → Environments** 中创建
`TELEGRAM_RELEASE_BOT_TOKEN` 环境，并在其中设置同名 Secret；通知任务绑定该环境。
将对应机器人设为频道管理员、授予发布消息权限。凭证只在通知和检查步骤通过环境变量读取，
不写入代码或公告文件。配置后可手动运行 **Check Telegram Release Channel** 工作流，
默认只查询 Token 有效性与频道发文权限；需要试发或补发时，勾选 `send_announcement`，
检查通过后读取 GitHub 当前正式版 Release 说明并向频道发送一条公告，无需重新发布镜像。
若缺少凭证或发送失败，通知任务会明确报错；镜像发布结果不受影响。
修复配置后可在该次 Actions 运行中选择 **Re-run failed jobs**，无需重建镜像。
若请求超时，先检查频道是否已收到消息再重试，避免重复发送。

### 1. 创建虚拟环境

在项目根目录下创建并激活虚拟环境：

- 在 Windows 上：

  ```bash
  python -m venv venv
  .\venv\Scripts\activate
  ```

- 在 macOS/Linux 上：

  ```bash
  python3 -m venv venv
  source venv/bin/activate
  ```

虚拟环境确保项目的依赖项与系统全局环境隔离，防止冲突。

### 2. 依赖分层与安装

主程序依赖按使用场景分层，避免运行时镜像携带只在开发、测试或构建时需要的工具：

| 文件 | 用途 | 典型安装场景 |
| --- | --- | --- |
| `requirements.in` | 主程序运行时依赖。只放启动、后台任务、插件运行框架和内置功能在生产环境需要导入的包。 | Docker 镜像、CLI 本地运行、运行时依赖自愈。 |
| `requirements-dev.in` | 开发、测试、静态检查和源码构建辅助依赖。 | CI 单测、本地跑测、Pylint、显式源码构建。 |
| `requirements.txt` | 兼容入口，默认只委托到 `requirements.in`。它不是跨平台完整锁文件，不应在本地开发机上直接维护一份平台相关锁定结果。 | 旧脚本、Docker 运行时恢复、CLI 安装入口。 |

运行主程序只需要安装运行时依赖：

```bash
pip install -r requirements.txt
```

开发、测试、静态检查或执行源码编译时安装开发依赖入口：

```bash
pip install -r requirements-dev.in
```

### 2.1 本地启动脚本

不需要打开 IDE 时，可以直接使用仓库内的启动脚本。脚本会自动定位项目根目录和虚拟环境，并以模块方式启动后端，避免 `ModuleNotFoundError: No module named 'app'`。

```bash
# 默认启动后端开发服务，前台运行，按 Ctrl+C 停止
./scripts/start-local.sh
./scripts/start-local.sh backend

# 如果已经安装前端发布包，可启动完整的前后端服务
./scripts/start-local.sh service start

# 管理完整服务
./scripts/start-local.sh stop
./scripts/start-local.sh restart
./scripts/start-local.sh status
./scripts/start-local.sh logs --follow
```

默认会使用 `DEBUG=true` 和 `DEV=true`，与 IDE 开发启动保持一致；如果不需要热重载，可以这样启动以降低资源占用：

```bash
DEV=false ./scripts/start-local.sh
```

脚本会优先使用 `CONFIG_DIR`，其次使用 `MOVIEPILOT_CONFIG_DIR`，再检测 `~/Documents/moviepilot`，最后回退到仓库内的 `config` 目录。需要使用其他配置目录时，可以这样运行：

```bash
MOVIEPILOT_CONFIG_DIR=/path/to/moviepilot-config ./scripts/start-local.sh
```

首次使用前如果脚本没有执行权限，运行：

```bash
chmod +x scripts/start-local.sh
```

### 3. 修改主程序依赖

新增或升级依赖时，先确认依赖属于哪个层级：

1. **运行时依赖**：被 `app/` 生产代码直接导入，或是生产功能、后台任务、插件框架启动必需，写入 `requirements.in`。
2. **开发 / 测试 / 静态检查 / 构建依赖**：只用于单测、覆盖率、lint 辅助、源码构建等，不应进入生产运行时，写入 `requirements-dev.in`。
3. **工具依赖**：`pip-tools`、`uv`、`safety` 这类安装或审计工具不属于主程序运行依赖，按脚本或 CI 场景显式安装。
4. **插件依赖**：由插件声明并在插件安装阶段处理，不直接并入主程序 `requirements.in`。

插件安装会生成主程序依赖约束：核心包和带本地版本标记的定制包（如
`moviepilot-rust==0.2.9+neo.4`）使用 `==` 精确锁定，其他主程序依赖只允许升级。
不要为包含 `+neo.4` 等本地版本标记的版本生成 `>=` 约束，pip 和 uv 都会拒绝解析。

### 4. 准备资源与插件目录

本地源码开发时，主程序需要读取资源文件和插件源码。相关文件需要放到主程序实际加载的目录下：

- **资源文件**：将 [MoviePilot-Resources](https://github.com/jxxghp/MoviePilot-Resources) 仓库中 `resources.v2/` 下的文件同步到本仓库的 `app/helper/` 目录下。CLI 安装和 Docker 构建流程也会按这个位置准备资源。
- **插件源码**：需要开发或调试的插件放到本仓库的 `app/plugins/` 目录下，例如 `app/plugins/<插件目录>/`。主程序运行时从该目录加载插件，独立插件仓库只是源码来源。

如果资源文件没有放到 `app/helper/`，站点索引、规则和内置资源相关能力可能无法按本地开发预期工作；如果插件没有放到 `app/plugins/`，主程序也不会在本地运行时发现该插件。

### 4.1 GitHub 发版时生成插件市场默认值

源码分支中的 `ConfigModel.PLUGIN_MARKET` 只保留官方插件仓库作为离线兜底。GitHub 的正式版与 Beta 镜像构建会检出 `MoviePilot-Wiki` 的 `main` 分支，并由 `scripts/generate_plugin_market_default.py` 读取 `plugin.md` 中 `plugin-market-repos:start/end` 标记区域，将规范化、去重后的公开仓库清单写入构建工作区。

生成过程遵循以下约束：

- 标记必须唯一、顺序正确，清单不能为空且必须包含 `jxxghp/MoviePilot-Plugins`；不满足时直接终止构建。
- 生成脚本只替换 `ConfigModel` 中的 `PLUGIN_MARKET` 默认值，不写入运行时环境变量，因此用户仍可通过系统环境变量或 `/config/app.env` 覆盖。
- 正式版工作流会创建仅由 Release Tag 引用的本地快照提交，Docker 镜像和 Tag 源码归档均来自该快照；Actions 不会将生成结果回写到 `v2` 分支。
- Release Tag 快照提交信息和镜像标签会记录本次使用的 MoviePilot Wiki Commit，便于追溯清单来源。

本地验证生成结果时，先激活项目虚拟环境，再执行：

```bash
python -m scripts.generate_plugin_market_default \
  --wiki-file /path/to/MoviePilot-Wiki/plugin.md \
  --config-file app/core/config.py
```

### 5. 运行安全检查

我们使用 `safety` 工具检查依赖项中是否存在已知安全漏洞。更新运行时依赖后，应至少检查运行时入口；更新开发测试依赖时，也应覆盖开发入口。

#### 安装 safety

您可以使用以下命令安装 `safety`：

```bash
pip install safety
```

#### 执行安全检查

运行以下命令检查运行时入口：

```bash
safety check -r requirements.txt --policy-file=safety.policy.yml > safety_report.txt
```

这将生成一个名为 `safety_report.txt` 的报告文件，您可以查看其中的漏洞报告并进行相应处理。

### 6. 提交代码前的检查

在提交代码之前，请确保完成以下步骤：

1. **确认依赖分层正确**：运行时包进入 `requirements.in`；测试、覆盖率、静态检查和构建辅助进入 `requirements-dev.in`；插件依赖不并入主程序运行时依赖。

2. **运行安全检查**：确保 `safety` 检查通过，没有新的安全漏洞。

3. **运行测试**：如果项目中包含测试，请确保所有测试都通过。运行以下命令以执行测试：

   ```bash
   pytest
   ```

### 7. 参考资源

- [pip-tools 官方文档](https://github.com/jazzband/pip-tools)
- [uv 官方文档](https://docs.astral.sh/uv/)
- [safety 官方文档](https://pyup.io/safety/)
- [MoviePilot-Resources](https://github.com/jxxghp/MoviePilot-Resources)
- [MoviePilot-Plugins](https://github.com/jxxghp/MoviePilot-Plugins)
