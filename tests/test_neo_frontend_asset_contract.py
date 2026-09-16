from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/build-custom.yml"
DOCKERFILE = ROOT / "docker/Dockerfile"


def test_neo_image_waits_for_commit_pinned_frontend_asset() -> None:
    """NEO 镜像必须等待并绑定前端 neo 当前提交的不可变资产。"""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert 'gh api "repos/${FRONTEND_REPO}/commits/neo"' in workflow
    assert 'frontend_asset="dist-${frontend_sha}.zip"' in workflow
    # 必须跟随 302 校验最终 CDN 地址,否则资产已建立但 CDN 未同步时会误判就绪
    assert 'curl -fsSIL "$frontend_asset_url"' in workflow
    assert "FRONTEND_DIST_ASSET=${{ steps.frontend.outputs.asset }}" in workflow
    assert "FRONTEND_DIST_TAG=${{ steps.frontend.outputs.sha }}" in workflow


def test_docker_frontend_download_fails_closed() -> None:
    """前端资产缺失或下载失败时镜像构建必须失败，不能嵌入旧页面。"""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "ARG FRONTEND_DIST_ASSET=dist.zip" in dockerfile
    assert "releases/download/${FRONTEND_VERSION}/${FRONTEND_DIST_ASSET}" in dockerfile
    assert "curl -fsSL" in dockerfile
    assert "-o /tmp/frontend-dist.zip" in dockerfile
