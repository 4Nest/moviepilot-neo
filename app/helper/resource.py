import hashlib
import json
import os
import platform
import sys
from pathlib import Path
from typing import Optional

from app.core.config import settings
from app.helper.site import SiteResourceRegistry
from app.helper.sites import SitesHelper  # noqa
from app.helper.system import SystemHelper
from app.log import logger
from app.utils.http import RequestUtils
from app.utils.string import StringUtils
from app.utils.system import SystemUtils


class ResourceHelper:
    """
    检测和更新站点资源包（含二进制基础资源与 NEO 免重启站点热更层）
    """

    _DEFAULT_REPO = "4Nest/MoviePilot-Resources"
    _base_dir: Path = settings.ROOT_PATH

    def __init__(self, force: bool = False):
        """初始化并按开关检查资源包更新。"""
        self.check(force=force)

    @classmethod
    def _repo_slug(cls) -> str:
        """返回规范化的 owner/repo 资源仓库标识。"""
        raw = (getattr(settings, "RESOURCE_REPO", None) or cls._DEFAULT_REPO).strip().strip("/")
        if raw.startswith(("http://", "https://")):
            parts = [seg for seg in raw.split("/") if seg]
            if len(parts) >= 2:
                return f"{parts[-2]}/{parts[-1].removesuffix('.git')}"
        return raw or cls._DEFAULT_REPO

    @property
    def _raw_base(self) -> str:
        """返回带代理前缀的 raw.githubusercontent.com 根地址。"""
        return f"{settings.GITHUB_PROXY}https://raw.githubusercontent.com/{self._repo_slug()}/main"

    @property
    def _repo(self) -> str:
        """返回 package.v2.json 清单地址。"""
        return f"{self._raw_base}/package.v2.json"

    @property
    def _files_api(self) -> str:
        """返回二进制资源目录 GitHub Contents API 地址。"""
        return f"https://api.github.com/repos/{self._repo_slug()}/contents/resources.v2"

    @property
    def proxies(self):
        """配置了 GitHub 代理前缀时不重复走系统代理。"""
        return None if settings.GITHUB_PROXY else settings.PROXY

    @classmethod
    def check_and_update(cls) -> None:
        """定时服务与手动立即运行入口。"""
        instance = cls.__new__(cls)
        instance.check(force=True)
    @staticmethod
    def _get_python_version_tag() -> str:
        version = sys.version_info
        return f"cp{version.major}{version.minor}"

    @staticmethod
    def _get_machine_tag() -> str:
        machine = platform.machine().lower()
        if machine in {"arm64", "aarch64"}:
            return "aarch64"
        elif machine in {"x86_64", "amd64"}:
            return "x86_64"
        return machine

    @staticmethod
    def _get_needed_files() -> list[str]:
        python_version = ResourceHelper._get_python_version_tag()
        python_ver = python_version.replace("cp", "")
        system = platform.system().lower()
        machine = ResourceHelper._get_machine_tag()
        files = ["user.sites.v2.bin"]
        if system == "linux":
            files.append(f"sites.cpython-{python_ver}-{machine}-linux-gnu.so")
        elif system == "darwin":
            files.append(f"sites.cpython-{python_ver}-darwin.so")
        elif system == "windows":
            files.append(f"sites.cp{python_ver}-win_amd64.pyd")
        return files

    def _sync_neo_sites(self, neo_meta: Optional[dict]) -> bool:
        """
        同步并校验 NEO 增量站点 JSON 与动态适配器，校验通过后免重启热加载生效

        :param neo_meta: package.v2.json 中的 neo_sites 字段
        :return: 是否发生了资源更新
        """
        if not isinstance(neo_meta, dict):
            return False
        online_ver = str(neo_meta.get("version") or "").strip()
        files_spec = neo_meta.get("files")
        if not online_ver or not isinstance(files_spec, dict) or not files_spec:
            return False

        res_dir = SiteResourceRegistry.resources_dir()
        manifest_file = SiteResourceRegistry.manifest_path()
        local_ver = None
        local_files = {}
        if manifest_file.is_file():
            try:
                local_manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
                if isinstance(local_manifest, dict):
                    local_ver = str(local_manifest.get("version") or "")
                    local_files = local_manifest.get("files") or {}
            except Exception:
                local_ver = None

        expected_targets: dict[str, tuple[str, str]] = {}
        all_intact = local_ver == online_ver
        for repo_rel, spec in files_spec.items():
            if not isinstance(spec, dict):
                continue
            target_rel = str(spec.get("target") or "").strip()
            expected_sha = str(spec.get("sha256") or "").strip().lower()
            target_path = SiteResourceRegistry._safe_resolve(target_rel)
            if not target_rel or not expected_sha or target_path is None:
                logger.warning(f"跳过不合法的热更资源条目：{repo_rel}")
                return False
            expected_targets[target_rel] = (str(repo_rel).lstrip("/"), expected_sha)
            if all_intact:
                if (
                    local_files.get(target_rel) != expected_sha
                    or not target_path.is_file()
                    or hashlib.sha256(target_path.read_bytes()).hexdigest() != expected_sha
                ):
                    all_intact = False

        if not expected_targets or all_intact:
            return False

        logger.info(f"检测到 NEO 站点热更资源包 v{online_ver}，开始校验同步...")
        staged_payloads: dict[Path, bytes] = {}
        for target_rel, (repo_rel, expected_sha) in expected_targets.items():
            target_path = SiteResourceRegistry._safe_resolve(target_rel)
            if target_path is None:
                return False
            if (
                target_path.is_file()
                and hashlib.sha256(target_path.read_bytes()).hexdigest() == expected_sha
            ):
                continue
            file_url = f"{self._raw_base}/{repo_rel}"
            file_res = RequestUtils(
                proxies=self.proxies,
                headers=settings.GITHUB_HEADERS,
                timeout=30,
            ).get_res(file_url)
            if not file_res or file_res.status_code != 200:
                code = file_res.status_code if file_res is not None else "连接失败"
                logger.error(f"下载热更站点资源 {repo_rel} 失败：{code}")
                return False
            actual_sha = hashlib.sha256(file_res.content).hexdigest()
            if actual_sha != expected_sha:
                logger.error(
                    f"热更站点资源 {repo_rel} SHA-256 校验失败 "
                    f"(expected={expected_sha[:12]}, actual={actual_sha[:12]})，已放弃本次更新"
                )
                return False
            staged_payloads[target_path] = file_res.content

        res_dir.mkdir(parents=True, exist_ok=True)
        for target_path, content in staged_payloads.items():
            target_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = target_path.with_suffix(f"{target_path.suffix}.tmp")
            tmp_path.write_bytes(content)
            os.replace(tmp_path, target_path)

        new_manifest = {
            "version": online_ver,
            "files": {target_rel: sha for target_rel, (_, sha) in expected_targets.items()},
        }
        manifest_tmp = manifest_file.with_suffix(".json.tmp")
        manifest_tmp.write_text(
            json.dumps(new_manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(manifest_tmp, manifest_file)
        SiteResourceRegistry.reload()
        logger.info(f"NEO 站点热更资源包已更新至 v{online_ver} 并即时生效")
        return True

    def check(self, force: bool = False):
        """
        检测是否有更新，如有则下载安装
        """
        if not force and not settings.AUTO_UPDATE_RESOURCE:
            return None
        if SystemUtils.is_frozen():
            return None
        logger.info(f"开始检测资源包版本 ({self._repo_slug()})...")
        res = RequestUtils(
            proxies=self.proxies, headers=settings.GITHUB_HEADERS, timeout=15
        ).get_res(self._repo)
        if res and res.status_code == 200:
            try:
                resource_info = json.loads(res.text)
                neo_updated = self._sync_neo_sites(resource_info.get("neo_sites"))
                online_version = resource_info.get("version")
                if online_version:
                    logger.info(f"最新基础资源包版本：v{online_version}")
                    need_updates = {}
                    resources: dict = resource_info.get("resources") or {}
                    for rname, resource in resources.items():
                        rtype = resource.get("type")
                        platform_name = resource.get("platform")
                        target = resource.get("target")
                        version = resource.get("version")
                        if platform_name and platform_name != SystemUtils.platform():
                            continue
                        if rtype == "auth":
                            local_version = SitesHelper().auth_version
                        elif rtype == "sites":
                            local_version = SitesHelper().indexer_version
                        else:
                            continue
                        if StringUtils.compare_version(version, ">", local_version):
                            logger.info(f"{rname} 资源包有更新，最新版本：v{version}")
                        else:
                            continue
                        need_updates[rname] = target
                    if need_updates:
                        r = RequestUtils(
                            proxies=settings.PROXY,
                            headers=settings.GITHUB_HEADERS,
                            timeout=30,
                        ).get_res(self._files_api)
                        if r and not r.ok:
                            return None, f"连接仓库失败：{r.status_code} - {r.reason}"
                        elif not r:
                            return None, "连接仓库失败"
                        files_info = r.json()
                        needed_files = self._get_needed_files()
                        logger.info(f"需要下载的资源文件：{needed_files}")
                        success = True
                        for item in files_info:
                            file_name = item.get("name")
                            if file_name not in needed_files:
                                continue
                            save_path = need_updates.get(file_name)
                            if not save_path:
                                continue
                            if item.get("download_url"):
                                logger.info(f"开始更新资源文件：{file_name} ...")
                                download_url = (
                                    f"{settings.GITHUB_PROXY}{item.get('download_url')}"
                                )
                                bin_res = RequestUtils(
                                    proxies=self.proxies,
                                    headers=settings.GITHUB_HEADERS,
                                    timeout=180,
                                ).get_res(download_url)
                                if not bin_res:
                                    logger.error(f"文件 {file_name} 下载失败！")
                                    success = False
                                    break
                                elif bin_res.status_code != 200:
                                    logger.error(
                                        f"下载文件 {file_name} 失败：{bin_res.status_code} - {bin_res.reason}"
                                    )
                                    success = False
                                    break
                                file_path = self._base_dir / save_path / file_name
                                if not file_path.parent.exists():
                                    file_path.parent.mkdir(parents=True, exist_ok=True)
                                file_path.write_bytes(bin_res.content)
                        if success:
                            logger.info("二进制资源包更新完成，开始重启服务...")
                            SystemHelper.restart()
                        else:
                            logger.warn("二进制资源包更新失败，跳过升级！")
                    elif not neo_updated:
                        logger.info("所有站点资源已最新，无需更新")
            except json.JSONDecodeError:
                logger.error("资源包仓库数据解析失败！")
                return None
        else:
            logger.warn("无法连接资源包仓库！")
            return None
