# 12 — Collaboration, Versioning, Build, and Release

## Commit Conventions

This project uses **Conventional Commits** for commit history. User-facing Release notes are maintained separately and are not generated from commit messages.

### Format

```
<type>(<optional scope>): <description>

[optional body]

[optional footer]
```

### Commit Types

| Type | When to use |
|---|---|
| `feat` | A new feature visible to users |
| `fix` | A bug fix |
| `docs` | Documentation only changes |
| `chore` | Maintenance, dependency updates, tooling changes |
| `refactor` | Code restructuring without behavior change |
| `test` | Adding or modifying tests |
| `ci` | CI/CD pipeline changes |
| `perf` | Performance improvements |

### Examples

```
feat: support MiniMax audio provider
fix: sign media server image proxy URLs
docs: add API client configuration examples
chore: upgrade pydantic to 2.9.0
refactor: extract transfer path resolution into helper
test: add subscribe endpoint validation tests
ci: improve docker build cache
```

### Rules

- **Only create a commit when the user explicitly asks for one.**
- Keep the subject line under 72 characters.
- Use the imperative mood in the subject line ("add", "fix", "remove", not "added", "fixed", "removed").
- If a commit introduces a breaking change, append `!` after the type and include `BREAKING CHANGE:` in the footer.

---

## Branch Policy

- Do not casually create, rename, or delete branches without user instruction.
- The main development branch is the project default — check `git branch` rather than assuming it is `main` or `master`.
- Feature work lives on dedicated branches and is merged via pull request.
- Do not force-push to shared branches.

---

## Version Numbers

- Do not casually change version numbers in `version.py` or related files.
- Version changes are part of the release workflow and are only made when the task explicitly involves a release.
- The `FRONTEND_VERSION` field in `version.py` controls which frontend release the CLI and Docker build will download. Only update it as part of a coordinated frontend release.
- 版本号只按修订号逐次加一：每次发布在上一个正式版基础上递增最后一位（如 `v2.17.0` → `v2.17.1`），不做 minor 跳变；前后端版本号必须保持一致，发布时同步修改后端 `version.py` 的 `APP_VERSION` / `FRONTEND_VERSION` 与前端 `package.json` 的 `version`。

---

## Docker Build and Release

- Release 说明遵循 `.github/release-notes/README.md`：标题仅为版本号，正文使用简洁准确的中文改动列表，读取 `.github/release-notes/<tag>.md`。
- 同版本补充改动合并进已有列表，不追加章节，不写测试日志、安装命令或构建信息；缺少说明文件时停止发布。
- 按用户要求仅更新 `latest` 时，保留前后端版本号及现有版本标签，通过 `build-custom.yml` 的 `publish_latest=true` 发布并更新对应 Release 说明。
- 正式镜像与 Release 更新成功后，由工作流通知 `@MoviePilot_NEO`；通知任务绑定 `TELEGRAM_RELEASE_BOT_TOKEN` Environment，使用其中的同名 Secret 和同一份中文改动说明。`beta` 开发镜像构建不通知，通知失败单独重试。`check-telegram.yml` 默认只读检查；用户要求试发或补发时可启用 `send_announcement`，发送已发布的当前正式版说明。
- `latest` 为正式稳定版，同时发布到 Docker Hub 与 GHCR，支持 `linux/amd64`、`linux/arm64`；`beta` 跟随 `beta` 分支构建，仅发布到 GHCR，仅 `linux/amd64`。

- The primary Docker image bundles the backend (Python app), frontend static files (from `public/`), and resource data.
- Docker build and release are managed by CI. Do not manually trigger or alter the Docker release flow unless the task explicitly requires it.
- If a Dockerfile change is needed, update `Dockerfile` and verify the build locally before submitting.

---

## CI/CD

- CI runs on every push and pull request. The pipeline typically includes:
  - Dependency installation
  - pytest test suite
  - pylint static analysis
  - Docker image build (on main branch or tags)
- Do not merge code that fails CI unless there is an explicit, documented reason and user approval.

---

## Pull Request Guidelines

- Keep PRs focused on a single concern. Separate refactors, features, and bug fixes into distinct PRs when practical.
- Include in the PR description:
  - What changed and why
  - How the change was validated
  - Any known risks or compatibility impact
  - Migration steps if config or database schema changed
- Tag the PR with the appropriate label (`bug`, `feature`, `docs`, `chore`).

---

## Dependency Release Process

When updating a dependency:

1. Decide the dependency layer: runtime packages go to `requirements.in`; test, coverage, lint, and explicit build tooling go to `requirements-dev.in`.
2. Keep `requirements.txt` as the compatibility entry that delegates to `requirements.in`; do not commit a locally generated cross-platform lock file.
3. Run `safety check -r requirements.txt --policy-file=safety.policy.yml`; include the dev dependency entry when `requirements-dev.in` changed.
4. Run the full test suite: `pytest`.

---

## Local CLI Release

The `moviepilot` CLI is the local-mode entrypoint. Its update path is:

```bash
moviepilot update all     # updates backend + frontend + resources
moviepilot update backend # git pull + reinstall deps
moviepilot update frontend
```

Bootstrap installer changes live in `scripts/bootstrap-local.sh`. Only modify this script if the task explicitly involves the bootstrap flow.

*Last Updated: 2026-05-25*
