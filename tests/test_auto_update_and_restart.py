"""重启与 Docker 自动更新链路（update.sh + SystemHelper）回归测试。"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.helper.system import SystemHelper


REPO_ROOT = Path(__file__).resolve().parents[1]
UPDATE_SCRIPT = REPO_ROOT / "docker" / "update.sh"


def _make_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _create_backend_zip(zip_path: Path, top_dir: str, app_version: str, frontend_version: str, req_in: str) -> None:
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(f"{top_dir}/app/main.py", "print('neo-main')\n")
        zf.writestr(f"{top_dir}/app/plugins/__init__.py", "# init\n")
        zf.writestr(f"{top_dir}/app/plugins/seedcleaner/plugin.py", "VERSION = 'builtin-new'\n")
        zf.writestr(
            f"{top_dir}/version.py",
            f"APP_VERSION = '{app_version}'\nFRONTEND_VERSION = '{frontend_version}'\n",
        )
        zf.writestr(f"{top_dir}/requirements.in", req_in)
        zf.writestr(f"{top_dir}/requirements.txt", "-r requirements.in\n")


def _create_frontend_zip(zip_path: Path, version_text: str) -> None:
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("dist/index.html", f"<html><body>{version_text}</body></html>\n")
        zf.writestr("dist/version.txt", f"{version_text}\n")


def _prepare_mock_env(tmp_path: Path) -> dict[str, Path]:
    app_dir = tmp_path / "app"
    public_dir = tmp_path / "public"
    config_dir = tmp_path / "config"
    venv_bin = tmp_path / "venv" / "bin"
    fixtures_dir = tmp_path / "fixtures"

    (app_dir / "app" / "plugins" / "seedcleaner").mkdir(parents=True)
    (app_dir / "app" / "plugins" / "thirdpartyfoo").mkdir(parents=True)
    (app_dir / "app" / "helper").mkdir(parents=True)
    public_dir.mkdir(parents=True)
    config_dir.mkdir(parents=True)
    venv_bin.mkdir(parents=True)
    fixtures_dir.mkdir(parents=True)

    (app_dir / "app" / "main.py").write_text("print('old-main')\n", encoding="utf-8")
    (app_dir / "version.py").write_text(
        "APP_VERSION = 'v2.16.14'\nFRONTEND_VERSION = 'v2.16.14'\n",
        encoding="utf-8",
    )
    (app_dir / "requirements.in").write_text("fastapi~=0.136.3\n", encoding="utf-8")
    (app_dir / "requirements.txt").write_text("fastapi==0.136.3\n", encoding="utf-8")
    (app_dir / "app" / "plugins" / "seedcleaner" / "plugin.py").write_text(
        "VERSION = 'builtin-old'\n",
        encoding="utf-8",
    )
    (app_dir / "app" / "plugins" / "thirdpartyfoo" / "plugin.py").write_text(
        "VERSION = 'external-1.0'\n",
        encoding="utf-8",
    )
    (app_dir / "app" / "helper" / "user.sites.v2.bin").write_bytes(b"old-bin")
    (app_dir / "app" / "helper" / "sites.cpython-312-x86_64-linux-gnu.so").write_bytes(b"old-so")
    (public_dir / "index.html").write_text("old-frontend\n", encoding="utf-8")

    # 软链真实 python3 用于计算 cpython 版本
    real_python = REPO_ROOT / "venv" / "bin" / "python"
    (venv_bin / "python").symlink_to(real_python)
    (venv_bin / "python3").symlink_to(real_python)

    return {
        "app_dir": app_dir,
        "public_dir": public_dir,
        "config_dir": config_dir,
        "venv_bin": venv_bin,
        "fixtures_dir": fixtures_dir,
    }


def test_update_sh_release_mode_handles_lowercase_neo_archive_plugins_and_so_fallback(tmp_path: Path):
    """release 模式应正确解压 moviepilot-neo-* 归档、更新内置插件并保留外部插件、在 .so 下载失败时恢复备份。"""
    dirs = _prepare_mock_env(tmp_path)
    fixtures = dirs["fixtures_dir"]

    _create_backend_zip(
        fixtures / "backend-release.zip",
        top_dir="moviepilot-neo-2.16.15",
        app_version="v2.16.15",
        frontend_version="v2.16.15",
        req_in="fastapi~=0.136.3\n",
    )
    _create_frontend_zip(fixtures / "dist.zip", version_text="v2.16.15")
    (fixtures / "releases.json").write_text(
        json.dumps([
            {"tag_name": "v2.16.15", "draft": False, "prerelease": False},
            {"tag_name": "v2.99.0-rc1", "draft": False, "prerelease": True},
        ]),
        encoding="utf-8",
    )

    # 模拟 curl：站点 .so 故意返回失败，验证旧 .so 被自动保留
    _make_executable(
        dirs["venv_bin"] / "curl",
        f"""#!/bin/bash
out=""
url=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -x|-H|--connect-timeout|--max-time|--oauth2-bearer) shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
if [[ "$url" == *"api.github.com/repos/4Nest/moviepilot-neo/releases"* ]]; then
  cat "{fixtures}/releases.json"
  exit 0
elif [[ "$url" == *"archive/refs/tags/v2.16.15.zip"* ]]; then
  cp "{fixtures}/backend-release.zip" "$out"
  exit 0
elif [[ "$url" == *"releases/download/v2.16.15/dist.zip"* ]]; then
  cp "{fixtures}/dist.zip" "$out"
  exit 0
elif [[ "$url" == *"user.sites.v2.bin"* ]]; then
  printf 'new-bin' > "$out"
  exit 0
elif [[ "$url" == *"sites.cpython-"* ]]; then
  exit 22
fi
exit 0
""",
    )

    env = os.environ.copy()
    env.update({
        "MOVIEPILOT_AUTO_UPDATE": "release",
        "VENV_PATH": str(tmp_path / "venv"),
        "CONFIG_DIR": str(dirs["config_dir"]),
        "MP_UPDATE_APP_ROOT": str(dirs["app_dir"]),
        "MP_UPDATE_PUBLIC_ROOT": str(dirs["public_dir"]),
        "MP_UPDATE_SYNC_SYSTEM_FILES": "false",
    })
    res = subprocess.run(["/bin/bash", str(UPDATE_SCRIPT)], env=env, capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stdout + res.stderr

    # 1. 后端与前端已更新至 v2.16.15（跳过了 prerelease v2.99.0-rc1）
    assert "v2.16.15" in (dirs["app_dir"] / "version.py").read_text(encoding="utf-8")
    assert "v2.16.15" in (dirs["public_dir"] / "index.html").read_text(encoding="utf-8")
    # 2. 内置插件升级为新版，外部已安装插件无损保留
    assert "builtin-new" in (dirs["app_dir"] / "app" / "plugins" / "seedcleaner" / "plugin.py").read_text(encoding="utf-8")
    assert "external-1.0" in (dirs["app_dir"] / "app" / "plugins" / "thirdpartyfoo" / "plugin.py").read_text(encoding="utf-8")
    # 3. 编译锁定的 requirements.txt 未被源码中的 -r requirements.in 覆盖
    assert (dirs["app_dir"] / "requirements.txt").read_text(encoding="utf-8") == "fastapi==0.136.3\n"
    # 4. bin 更新为新版，下载失败的 .so 从备份无损恢复
    assert (dirs["app_dir"] / "app" / "helper" / "user.sites.v2.bin").read_bytes() == b"new-bin"
    assert (dirs["app_dir"] / "app" / "helper" / "sites.cpython-312-x86_64-linux-gnu.so").read_bytes() == b"old-so"


def test_update_sh_dev_mode_compiles_with_emit_find_links_and_aborts_safely_on_broken_zip(tmp_path: Path):
    """dev 模式在依赖变更时必须携带 --emit-find-links + GITHUB_PROXY，且坏包绝不清空原 /app。"""
    dirs = _prepare_mock_env(tmp_path)
    fixtures = dirs["fixtures_dir"]
    compile_args_log = tmp_path / "pip_compile_args.txt"
    compiled_in_snapshot = tmp_path / "compiled_in_snapshot.txt"

    _create_backend_zip(
        fixtures / "backend-beta.zip",
        top_dir="moviepilot-neo-beta",
        app_version="v2.16.15",
        frontend_version="v2.16.15",
        req_in=(
            "--find-links https://github.com/4Nest/MoviePilot-Rust/releases/expanded_assets/v0.2.9%2Bneo.6\n"
            "moviepilot-rust==0.2.9+neo.6\n"
        ),
    )
    _create_frontend_zip(fixtures / "dist.zip", version_text="v2.16.15-beta")

    _make_executable(
        dirs["venv_bin"] / "pip-compile",
        f"""#!/bin/bash
printf '%s\\n' "$@" > "{compile_args_log}"
out=""
in_file=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    --emit-find-links) shift ;;
    *) in_file="$1"; shift ;;
  esac
done
cp "$in_file" "{compiled_in_snapshot}"
printf '# compiled\\nmoviepilot-rust==0.2.9+neo.6\\n' > "$out"
""",
    )
    _make_executable(dirs["venv_bin"] / "pip", "#!/bin/bash\nexit 0\n")

    _make_executable(
        dirs["venv_bin"] / "curl",
        f"""#!/bin/bash
out=""
url=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -x|-H|--connect-timeout|--max-time|--oauth2-bearer) shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
if [[ "$url" == *"api.github.com/repos/4Nest/moviepilot-neo-frontend/releases"* ]]; then
  printf '[{{"tag_name":"v2.16.15","draft":false,"prerelease":false}}]'
  exit 0
elif [[ "$url" == *"https://ghproxy.example/https://github.com/4Nest/moviepilot-neo/archive/refs/heads/beta.zip"* ]]; then
  cp "{fixtures}/backend-beta.zip" "$out"
  exit 0
elif [[ "$url" == *"https://ghproxy.example/https://github.com/4Nest/moviepilot-neo-frontend/releases/download/v2.16.15/dist.zip"* ]]; then
  cp "{fixtures}/dist.zip" "$out"
  exit 0
elif [[ -n "$out" ]]; then
  printf 'ok' > "$out"
fi
exit 0
""",
    )

    env = os.environ.copy()
    env.update({
        "MOVIEPILOT_AUTO_UPDATE": "dev",
        "GITHUB_PROXY": "https://ghproxy.example",  # 故意不带末尾斜杠，验证自动补齐
        "VENV_PATH": str(tmp_path / "venv"),
        "CONFIG_DIR": str(dirs["config_dir"]),
        "MP_UPDATE_APP_ROOT": str(dirs["app_dir"]),
        "MP_UPDATE_PUBLIC_ROOT": str(dirs["public_dir"]),
        "MP_UPDATE_SYNC_SYSTEM_FILES": "false",
    })
    res = subprocess.run(["/bin/bash", str(UPDATE_SCRIPT)], env=env, capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "--emit-find-links" in compile_args_log.read_text(encoding="utf-8")
    assert "--find-links https://ghproxy.example/https://github.com/4Nest/MoviePilot-Rust" in compiled_in_snapshot.read_text(encoding="utf-8")
    assert "v2.16.15-beta" in (dirs["public_dir"] / "index.html").read_text(encoding="utf-8")
    assert "# compiled" in (dirs["app_dir"] / "requirements.txt").read_text(encoding="utf-8")

    # 再验证：当归档缺失 app/main.py 时，原 /app 绝不被清空
    with zipfile.ZipFile(fixtures / "backend-beta.zip", "w") as broken_zf:
        broken_zf.writestr("moviepilot-neo-beta/README.md", "broken")
    res2 = subprocess.run(["/bin/bash", str(UPDATE_SCRIPT)], env=env, capture_output=True, text=True, check=False)
    assert res2.returncode == 0
    assert (dirs["app_dir"] / "app" / "main.py").exists()


def test_system_helper_restart_works_without_docker_sock_and_delays_sigterm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """未挂载 docker.sock 时，Docker 容器内 restart/upgrade 应走优雅退出标记并延迟发送 SIGTERM。"""
    intent_file = tmp_path / "moviepilot.intentional_restart"
    pending_file = tmp_path / "moviepilot.pending_update"
    scheduled: list[float] = []

    monkeypatch.setattr("app.helper.system.SystemUtils.is_docker", staticmethod(lambda: True))
    monkeypatch.setattr(SystemHelper, "_SystemHelper__docker_restart_intent_file", intent_file)
    monkeypatch.setattr(SystemHelper, "_SystemHelper__one_shot_update_flag_file", pending_file)
    monkeypatch.setattr(SystemHelper, "_check_restart_policy", staticmethod(lambda: None))
    monkeypatch.setattr(
        SystemHelper,
        "_docker_api_restart",
        staticmethod(lambda: pytest.fail("未挂载 docker.sock 时不应降级调用 _docker_api_restart")),
    )
    monkeypatch.setattr(SystemHelper, "_schedule_self_sigterm", staticmethod(lambda delay=0.3: scheduled.append(delay)))
    monkeypatch.setattr("app.helper.system.settings.MOVIEPILOT_AUTO_UPDATE", "false")

    ok, msg = SystemHelper.upgrade("dev")
    assert ok is True
    assert "dev" in msg
    assert pending_file.read_text(encoding="utf-8") == "dev"
    assert intent_file.exists()
    assert len(scheduled) == 1


def test_system_helper_restart_uses_docker_api_when_policy_is_explicitly_no(monkeypatch: pytest.MonkeyPatch):
    """当 Docker API 可用且明确检测到容器重启策略为 no 时，应调用 _docker_api_restart。"""
    monkeypatch.setattr("app.helper.system.SystemUtils.is_docker", staticmethod(lambda: True))
    monkeypatch.setattr(SystemHelper, "_check_restart_policy", staticmethod(lambda: False))
    monkeypatch.setattr(SystemHelper, "_docker_api_restart", staticmethod(lambda: (True, "via-docker-api")))

    ok, msg = SystemHelper.restart()
    assert ok is True
    assert msg == "via-docker-api"


def test_get_container_id_supports_cgroup_v2_and_hostname_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """_get_container_id 应支持从 cgroup v2 路径和 HOSTNAME 提取容器 ID。"""
    cid_64 = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90"
    fake_mountinfo = tmp_path / "mountinfo"
    fake_cgroup = tmp_path / "cgroup"
    fake_mountinfo.write_text("36 28 0:31 / /proc rw,nosuid\n", encoding="utf-8")
    fake_cgroup.write_text(f"0::/system.slice/docker-{cid_64}.scope\n", encoding="utf-8")

    orig_read_text = Path.read_text

    def _fake_read_text(self: Path, *args, **kwargs):
        if str(self) == "/proc/self/mountinfo":
            return orig_read_text(fake_mountinfo, *args, **kwargs)
        if str(self) == "/proc/self/cgroup":
            return orig_read_text(fake_cgroup, *args, **kwargs)
        return orig_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _fake_read_text)
    assert SystemHelper._get_container_id() == cid_64

    # cgroup 也无 64 位 ID 时，回退读取 12 位 hex HOSTNAME
    fake_cgroup.write_text("0::/\n", encoding="utf-8")
    monkeypatch.setenv("HOSTNAME", "1a2b3c4d5e6f")
    assert SystemHelper._get_container_id() == "1a2b3c4d5e6f"
