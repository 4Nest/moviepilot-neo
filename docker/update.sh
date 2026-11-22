#!/bin/bash
# shellcheck shell=bash

Green="\033[32m"
Red="\033[31m"
Yellow='\033[33m'
Font="\033[0m"
INFO="[${Green}INFO${Font}]"
ERROR="[${Red}ERROR${Font}]"
WARN="[${Yellow}WARN${Font}]"
function INFO() {
    echo -e "${INFO} ${1}"
}
function ERROR() {
    echo -e "${ERROR} ${1}"
}
function WARN() {
    echo -e "${WARN} ${1}"
}

# 设置虚拟环境路径（兼容群晖等系统必须这样配置）
VENV_PATH="${VENV_PATH:-/opt/venv}"
export PATH="${VENV_PATH}/bin:$PATH"

CONFIG_DIR="${CONFIG_DIR:-/config}"
APP_ROOT="${MP_UPDATE_APP_ROOT:-/app}"
PUBLIC_ROOT="${MP_UPDATE_PUBLIC_ROOT:-/public}"
SYSTEM_SYNC_ENABLED="${MP_UPDATE_SYNC_SYSTEM_FILES:-true}"

function apply_package_cache_env() {
    PACKAGE_CACHE_ROOT="${PACKAGE_CACHE_ROOT:-${CONFIG_DIR}/.cache}"
    export PACKAGE_CACHE_ROOT
    export PIP_CACHE_DIR="${PIP_CACHE_DIR:-${PACKAGE_CACHE_ROOT}/pip}"
    export UV_CACHE_DIR="${UV_CACHE_DIR:-${PACKAGE_CACHE_ROOT}/uv}"
    mkdir -p "${PIP_CACHE_DIR}" "${UV_CACHE_DIR}"
}

# 规范化 GITHUB_PROXY，确保非空时末尾带 /
function normalize_github_proxy() {
    local proxy="${1:-}"
    if [[ -n "${proxy}" ]]; then
        printf '%s/' "${proxy%/}"
    else
        printf ''
    fi
}

# 优先级转换
function mp_get_version_priority() {
    local version="$1"
    if [[ $version =~ ^[0-9]+$ ]]; then
        echo "$version"
    else
        case "$version" in
            "stable") echo -1 ;;
            "rc") echo -2 ;;
            "beta") echo -3 ;;
            "alpha") echo -4 ;;
            *) echo -5 ;;
        esac
    fi
}

function mp_run_auto_update() {
    apply_package_cache_env

    local auto_update_mode
    auto_update_mode="$(printf '%s' "${MOVIEPILOT_AUTO_UPDATE:-}" | tr '[:upper:]' '[:lower:]')"
    if [[ "${auto_update_mode}" != "true" ]] && [[ "${auto_update_mode}" != "release" ]] && [[ "${auto_update_mode}" != "dev" ]]; then
        if [[ "${auto_update_mode}" = "false" ]]; then
            INFO "程序自动升级已关闭，如需自动升级请在创建容器时设置环境变量：MOVIEPILOT_AUTO_UPDATE=release"
        else
            INFO "MOVIEPILOT_AUTO_UPDATE 变量设置错误"
        fi
        return 0
    fi

    local tmp_path
    tmp_path="$(mktemp -d 2>/dev/null || true)"
    if [[ -z "${tmp_path}" ]] || [[ ! -d "${tmp_path}" ]]; then
        tmp_path="/tmp/mp_update_path"
        rm -rf "${tmp_path}"
        mkdir -p "${tmp_path}"
    fi

    local -a pip_env=()
    local -a pip_index_args=()
    local pip_log="不使用代理"
    local -a curl_base_args=("-fsSL" "--connect-timeout" "15" "--max-time" "300")
    local -a curl_header_args=("-H" "User-Agent: moviepilot-neo-updater")
    local github_proxy=""
    local github_log="不使用代理"

    local normalized_gh_proxy
    normalized_gh_proxy="$(normalize_github_proxy "${GITHUB_PROXY:-}")"

    if [[ -n "${GITHUB_TOKEN:-}" ]]; then
        curl_header_args+=("--oauth2-bearer" "${GITHUB_TOKEN}")
    fi

    # 1. 探测 PyPI 连通性（镜像站 > 全局代理 > 直连，不安装任何测试包）
    local mode_idx
    for mode_idx in 0 1 2; do
        case "${mode_idx}" in
            0)
                if [[ -n "${PIP_PROXY:-}" ]]; then
                    local -a probe_args=("-fsSL" "--connect-timeout" "8" "--max-time" "15")
                    if [[ -n "${PROXY_HOST:-}" ]]; then
                        probe_args+=("-x" "${PROXY_HOST}")
                    fi
                    if curl "${probe_args[@]}" "${PIP_PROXY}" >/dev/null 2>&1; then
                        pip_index_args=("-i" "${PIP_PROXY}")
                        pip_log="镜像代理模式"
                        if [[ -n "${PROXY_HOST:-}" ]]; then
                            pip_env=(
                                "HTTP_PROXY=${PROXY_HOST}"
                                "HTTPS_PROXY=${PROXY_HOST}"
                                "http_proxy=${PROXY_HOST}"
                                "https_proxy=${PROXY_HOST}"
                            )
                        fi
                        break
                    fi
                fi
                ;;
            1)
                if [[ -n "${PROXY_HOST:-}" ]]; then
                    if curl -fsSL --connect-timeout 8 --max-time 15 -x "${PROXY_HOST}" "https://pypi.org/simple/" >/dev/null 2>&1; then
                        pip_index_args=()
                        pip_env=(
                            "HTTP_PROXY=${PROXY_HOST}"
                            "HTTPS_PROXY=${PROXY_HOST}"
                            "http_proxy=${PROXY_HOST}"
                            "https_proxy=${PROXY_HOST}"
                        )
                        pip_log="全局代理模式"
                        break
                    fi
                fi
                ;;
            2)
                pip_index_args=()
                pip_env=()
                pip_log="不使用代理"
                break
                ;;
        esac
    done

    # 2. 探测 GitHub 连通性（镜像站 > 全局代理 > 直连）
    for mode_idx in 0 1 2; do
        case "${mode_idx}" in
            0)
                if [[ -n "${normalized_gh_proxy}" ]]; then
                    if curl -fsSL --connect-timeout 8 --max-time 15 "${normalized_gh_proxy}https://raw.githubusercontent.com/4Nest/moviepilot-neo/beta/README.md" >/dev/null 2>&1; then
                        github_proxy="${normalized_gh_proxy}"
                        github_log="镜像代理模式"
                        break
                    fi
                fi
                ;;
            1)
                if [[ -n "${PROXY_HOST:-}" ]]; then
                    if curl -fsSL --connect-timeout 8 --max-time 15 -x "${PROXY_HOST}" "https://raw.githubusercontent.com/4Nest/moviepilot-neo/beta/README.md" >/dev/null 2>&1; then
                        curl_base_args=("-fsSL" "--connect-timeout" "15" "--max-time" "300" "-x" "${PROXY_HOST}")
                        github_proxy=""
                        github_log="全局代理模式"
                        break
                    fi
                fi
                ;;
            2)
                github_proxy=""
                github_log="不使用代理"
                break
                ;;
        esac
    done

    INFO "PIP：${pip_log}，Github：${github_log}"

    extract_zip_archive() {
        local zip_path="$1"
        local out_dir="$2"
        if command -v busybox >/dev/null 2>&1; then
            busybox unzip -q "${zip_path}" -d "${out_dir}"
        else
            unzip -q "${zip_path}" -d "${out_dir}"
        fi
    }

    # 下载 zip 归档并解压到 ${tmp_path}/${target_dir}，兼容任意单顶层目录名（如 moviepilot-neo-* / MoviePilot-* / dist）
    download_and_unzip_to() {
        local url="$1"
        local target_dir="$2"
        local retries=0
        local max_retries=3
        local archive_file="${tmp_path}/${target_dir}.zip"
        local extract_dir="${tmp_path}/_extract_${target_dir}"
        local dest_dir="${tmp_path}/${target_dir}"

        INFO "→ 正在下载 ${url}..."
        while (( retries < max_retries )); do
            rm -rf "${archive_file}" "${extract_dir}" "${dest_dir}"
            mkdir -p "${extract_dir}"
            if curl "${curl_base_args[@]}" "${curl_header_args[@]}" "${url}" -o "${archive_file}" \
                && [[ -s "${archive_file}" ]] \
                && extract_zip_archive "${archive_file}" "${extract_dir}"; then
                rm -f "${archive_file}"
                local -a top_entries=()
                while IFS= read -r -d '' entry; do
                    top_entries+=("${entry}")
                done < <(find "${extract_dir}" -mindepth 1 -maxdepth 1 -print0)

                if (( ${#top_entries[@]} == 1 )) && [[ -d "${top_entries[0]}" ]]; then
                    mv "${top_entries[0]}" "${dest_dir}"
                    rm -rf "${extract_dir}"
                elif (( ${#top_entries[@]} > 0 )); then
                    mv "${extract_dir}" "${dest_dir}"
                else
                    rm -rf "${extract_dir}"
                    WARN "解压 ${url} 后内容为空，正在进行第 $((retries + 1)) 次重试..."
                    retries=$((retries + 1))
                    continue
                fi
                return 0
            fi
            WARN "下载或解压 ${url} 失败，正在进行第 $((retries + 1)) 次重试..."
            retries=$((retries + 1))
        done

        rm -rf "${archive_file}" "${extract_dir}"
        ERROR "下载 ${url} 失败，已达到最大重试次数！"
        return 1
    }

    fetch_latest_v2_release_tag() {
        local repo="$1"
        local api_url="https://api.github.com/repos/${repo}/releases"
        local body=""
        body="$(curl "${curl_base_args[@]}" "${curl_header_args[@]}" -H "Accept: application/vnd.github+json" "${api_url}" 2>/dev/null || true)"
        if [[ -z "${body}" ]]; then
            return 1
        fi
        local tags
        tags="$(printf '%s' "${body}" | jq -r '.[] | select((.draft // false) == false and (.prerelease // false) == false) | .tag_name' 2>/dev/null | grep '^v2\.' || true)"
        if [[ -z "${tags}" ]]; then
            return 1
        fi
        printf '%s\n' "${tags}" | sort -V | tail -n 1
    }

    install_backend_and_download_resources() {
        local backend_ref_zip="$1"
        if ! download_and_unzip_to "${github_proxy}https://github.com/4Nest/moviepilot-neo/archive/refs/${backend_ref_zip}" "App"; then
            WARN "后端程序下载失败，继续使用旧的程序来启动..."
            return 1
        fi
        if [[ ! -f "${tmp_path}/App/app/main.py" ]] || [[ ! -f "${tmp_path}/App/version.py" ]]; then
            ERROR "下载的后端归档结构不完整（缺少 app/main.py 或 version.py），放弃本次更新..."
            return 1
        fi
        INFO "后端程序下载成功"

        # 获取对应前端版本号并下载前端产物
        local frontend_version=""
        if [[ "${backend_ref_zip}" == "heads/beta.zip" ]]; then
            INFO "→ 正在获取前端最新版本号..."
            frontend_version="$(fetch_latest_v2_release_tag "4Nest/moviepilot-neo-frontend" || true)"
            if [[ -z "${frontend_version}" ]]; then
                # API 受限时回退读取新后端 version.py 中声明的 FRONTEND_VERSION
                frontend_version="$(sed -n "s/^[[:space:]]*FRONTEND_VERSION[[:space:]]*=[[:space:]]*['\"]\([^'\"]*\)['\"].*/\1/p" "${tmp_path}/App/version.py" | head -n 1)"
            fi
            if [[ "${frontend_version}" != v* ]]; then
                WARN "未找到有效的前端版本号，继续使用旧程序启动..."
                return 1
            fi
            INFO "前端最新版本号：${frontend_version}"
        else
            INFO "→ 正在获取前端版本号..."
            frontend_version="$(sed -n "s/^[[:space:]]*FRONTEND_VERSION[[:space:]]*=[[:space:]]*['\"]\([^'\"]*\)['\"].*/\1/p" "${tmp_path}/App/version.py" | head -n 1)"
            if [[ "${frontend_version}" != v* ]]; then
                WARN "前端版本号获取失败，继续使用旧程序启动..."
                return 1
            fi
            INFO "前端版本号：${frontend_version}"
        fi

        if ! download_and_unzip_to "${github_proxy}https://github.com/4Nest/moviepilot-neo-frontend/releases/download/${frontend_version}/dist.zip" "dist"; then
            WARN "前端程序下载失败，继续使用旧的程序来启动..."
            return 1
        fi
        if [[ ! -f "${tmp_path}/dist/index.html" ]]; then
            ERROR "下载的前端产物结构不完整（缺少 index.html），放弃本次更新..."
            return 1
        fi
        INFO "前端程序下载成功"

        # 检查依赖是否有变化（在替换 /app 前先完成编译与安装，失败则不动原 /app）
        INFO "→ 检查依赖变化..."
        local compiled_req_file=""
        if [[ -f "${tmp_path}/App/requirements.in" ]]; then
            if [[ ! -f "${APP_ROOT}/requirements.in" ]] || ! cmp -s "${APP_ROOT}/requirements.in" "${tmp_path}/App/requirements.in"; then
                INFO "检测到依赖变化，正在更新虚拟环境..."
                local compile_input="${tmp_path}/App/requirements.in"
                if [[ -n "${github_proxy}" ]]; then
                    compile_input="${tmp_path}/requirements-proxy.in"
                    sed "s|^--find-links https://github.com/|--find-links ${github_proxy}https://github.com/|" \
                        "${tmp_path}/App/requirements.in" > "${compile_input}"
                fi
                compiled_req_file="${tmp_path}/requirements.compiled.txt"
                if ! env "${pip_env[@]}" "${VENV_PATH}/bin/pip-compile" --emit-find-links "${compile_input}" -o "${compiled_req_file}"; then
                    ERROR "依赖编译失败，放弃本次更新并保留原环境"
                    return 1
                fi
                if ! env "${pip_env[@]}" "${VENV_PATH}/bin/pip" install "${pip_index_args[@]}" -r "${compiled_req_file}"; then
                    ERROR "依赖安装失败，放弃本次更新并保留原环境"
                    return 1
                fi
                INFO "正在更新 CloakBrowser 浏览器内核"
                if ! env "${pip_env[@]}" "${VENV_PATH}/bin/python" -m cloakbrowser install; then
                    WARN "CloakBrowser 浏览器内核更新失败，后续首次使用时可能重新下载"
                fi
                INFO "依赖更新成功"
            else
                INFO "依赖无变化，跳过依赖更新"
                if [[ -f "${APP_ROOT}/requirements.txt" ]]; then
                    compiled_req_file="${tmp_path}/requirements.preserved.txt"
                    cp -a "${APP_ROOT}/requirements.txt" "${compiled_req_file}"
                fi
            fi
        else
            WARN "未找到 requirements.in 文件，跳过依赖检查"
        fi

        # 备份外部已安装插件与站点认证资源（放在临时目录内，更新结束后随 tmp_path 自动清理）
        local plugins_backup_dir="${tmp_path}/_plugins_backup"
        local resources_backup_dir="${tmp_path}/_resources_backup"
        mkdir -p "${plugins_backup_dir}" "${resources_backup_dir}"

        INFO "→ 正在备份已安装插件与站点资源..."
        if [[ -d "${APP_ROOT}/app/plugins" ]]; then
            local plugin_entry
            for plugin_entry in "${APP_ROOT}/app/plugins"/*; do
                [[ -e "${plugin_entry}" ]] || continue
                local base_name
                base_name="$(basename "${plugin_entry}")"
                [[ "${base_name}" == "__init__.py" || "${base_name}" == "__pycache__" ]] && continue
                cp -a "${plugin_entry}" "${plugins_backup_dir}/"
            done
        fi
        if [[ -f "${APP_ROOT}/app/helper/user.sites.v2.bin" ]]; then
            cp -a "${APP_ROOT}/app/helper/user.sites.v2.bin" "${resources_backup_dir}/"
        fi
        local so_entry
        for so_entry in "${APP_ROOT}/app/helper"/sites.cp*; do
            [[ -e "${so_entry}" ]] || continue
            cp -a "${so_entry}" "${resources_backup_dir}/"
        done

        # 替换后端与前端目录
        rm -rf "${APP_ROOT}"
        mkdir -p "${APP_ROOT}"
        cp -a "${tmp_path}/App/." "${APP_ROOT}/"
        if [[ -n "${compiled_req_file}" ]] && [[ -f "${compiled_req_file}" ]]; then
            cp -f "${compiled_req_file}" "${APP_ROOT}/requirements.txt"
        fi

        rm -rf "${PUBLIC_ROOT}"
        mkdir -p "${PUBLIC_ROOT}"
        cp -a "${tmp_path}/dist/." "${PUBLIC_ROOT}/"

        # 恢复外部插件（仅补齐新版仓库中不存在的插件目录，避免旧文件覆盖新版内置插件）
        mkdir -p "${APP_ROOT}/app/plugins"
        local backed_plugin
        for backed_plugin in "${plugins_backup_dir}"/*; do
            [[ -e "${backed_plugin}" ]] || continue
            local plugin_name
            plugin_name="$(basename "${backed_plugin}")"
            if [[ ! -e "${APP_ROOT}/app/plugins/${plugin_name}" ]]; then
                cp -a "${backed_plugin}" "${APP_ROOT}/app/plugins/"
            fi
        done

        # 同步容器入口脚本与 Nginx 模板，使下次重启直接生效最新更新逻辑
        if [[ "${SYSTEM_SYNC_ENABLED}" == "true" ]]; then
            [[ -f "${APP_ROOT}/docker/nginx.common.conf" ]] && cp -f "${APP_ROOT}/docker/nginx.common.conf" /etc/nginx/common.conf
            [[ -f "${APP_ROOT}/docker/nginx.template.conf" ]] && cp -f "${APP_ROOT}/docker/nginx.template.conf" /etc/nginx/nginx.template.conf
            [[ -f "${APP_ROOT}/docker/docker_http_proxy.conf" ]] && cp -f "${APP_ROOT}/docker/docker_http_proxy.conf" /etc/nginx/docker_http_proxy.conf
            if [[ -f "${APP_ROOT}/docker/update.sh" ]]; then
                cp -f "${APP_ROOT}/docker/update.sh" /usr/local/bin/mp_update.sh
                chmod +x /usr/local/bin/mp_update.sh
            fi
            if [[ -f "${APP_ROOT}/docker/entrypoint.sh" ]]; then
                cp -f "${APP_ROOT}/docker/entrypoint.sh" /entrypoint.sh
                chmod +x /entrypoint.sh
            fi
        fi

        INFO "程序部分更新成功，前端版本：${frontend_version}，后端版本：${backend_ref_zip}"

        # 更新站点资源（下载到临时文件校验非空后再覆盖，失败则从备份完整回退）
        INFO "→ 开始更新站点资源..."
        mkdir -p "${APP_ROOT}/app/helper"
        local python_version arch arch_suffix
        python_version="$(python3 -c 'import sys; print(f"cpython-{sys.version_info.major}{sys.version_info.minor}")')"
        arch="$(uname -m)"
        if [[ "${arch}" == "aarch64" || "${arch}" == "arm64" ]]; then
            arch_suffix="aarch64-linux-gnu"
        else
            arch_suffix="x86_64-linux-gnu"
        fi
        INFO "当前 Python 版本：${python_version}，架构：${arch}"

        # 先恢复已有资源兜底，再尝试拉取最新资源覆盖
        if [[ -f "${resources_backup_dir}/user.sites.v2.bin" ]]; then
            cp -a "${resources_backup_dir}/user.sites.v2.bin" "${APP_ROOT}/app/helper/"
        fi
        for so_entry in "${resources_backup_dir}"/sites.cp*; do
            [[ -e "${so_entry}" ]] || continue
            cp -a "${so_entry}" "${APP_ROOT}/app/helper/"
        done

        local sites_bin_tmp="${tmp_path}/user.sites.v2.bin"
        if curl "${curl_base_args[@]}" "${github_proxy}https://raw.githubusercontent.com/jxxghp/MoviePilot-Resources/main/resources.v2/user.sites.v2.bin" -o "${sites_bin_tmp}" \
            && [[ -s "${sites_bin_tmp}" ]]; then
            mv -f "${sites_bin_tmp}" "${APP_ROOT}/app/helper/user.sites.v2.bin"
        else
            WARN "user.sites.v2.bin 下载失败，继续使用旧的资源来启动..."
        fi

        local sites_file="sites.${python_version}-${arch_suffix}.so"
        local sites_so_tmp="${tmp_path}/${sites_file}"
        if curl "${curl_base_args[@]}" "${github_proxy}https://raw.githubusercontent.com/jxxghp/MoviePilot-Resources/main/resources.v2/${sites_file}" -o "${sites_so_tmp}" \
            && [[ -s "${sites_so_tmp}" ]]; then
            mv -f "${sites_so_tmp}" "${APP_ROOT}/app/helper/${sites_file}"
        else
            WARN "${sites_file} 下载失败，继续使用旧的资源来启动..."
        fi
        INFO "站点资源更新完成"
        return 0
    }

    compare_and_upgrade_release() {
        local v1="${1#[vV]}"
        local v2="${2#[vV]}"
        local release_tag="${3:-$2}"
        local -a current_ver_parts=()
        local -a release_ver_parts=()
        IFS='.-' read -ra current_ver_parts <<< "${v1}"
        IFS='.-' read -ra release_ver_parts <<< "${v2}"
        local i current_ver release_ver

        for ((i = 0; i < ${#current_ver_parts[@]} || i < ${#release_ver_parts[@]}; i++)); do
            local current_ver_part="${current_ver_parts[i]:-0}"
            local release_ver_part="${release_ver_parts[i]:-0}"
            current_ver="$(mp_get_version_priority "${current_ver_part}")"
            release_ver="$(mp_get_version_priority "${release_ver_part}")"

            if (( current_ver == -5 || release_ver == -5 )); then
                ERROR "存在不合法版本号，无法判断，跳过更新步骤..."
                return 1
            elif (( current_ver > release_ver )); then
                WARN "当前版本高于远程版本，跳过更新步骤..."
                return 1
            elif (( current_ver < release_ver )); then
                INFO "发现新版本 (${release_tag})，开始自动升级..."
                install_backend_and_download_resources "tags/${release_tag}.zip"
                return $?
            fi
        done
        WARN "当前版本已是最新版本，跳过更新步骤..."
        return 0
    }

    if [[ "${auto_update_mode}" == "dev" ]]; then
        INFO "Dev 更新模式"
        install_backend_and_download_resources "heads/beta.zip" || true
    else
        INFO "Release 更新模式"
        local old_version=""
        if [[ -f "${APP_ROOT}/version.py" ]]; then
            old_version="$(grep -m 1 "^[[:space:]]*APP_VERSION[[:space:]]*=" "${APP_ROOT}/version.py" | tr -d '\r\n' | awk -F'#' '{print $1}' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
        fi
        if [[ "${old_version}" == *APP_VERSION* ]]; then
            local current_version
            current_version="$(printf '%s' "${old_version}" | sed -rn "s/APP_VERSION[[:space:]]*=[[:space:]]*['\"]([^'\"]+)['\"].*/\1/p")"
            INFO "当前版本号：${current_version}"
            local latest_release
            latest_release="$(fetch_latest_v2_release_tag "4Nest/moviepilot-neo" || true)"
            if [[ -z "${latest_release}" ]]; then
                WARN "未找到任何 Neo v2 后端版本，继续启动..."
            else
                compare_and_upgrade_release "${current_version}" "${latest_release}" "${latest_release}" || true
            fi
        else
            WARN "当前版本号获取失败，继续启动..."
        fi
    fi

    rm -rf "${tmp_path}"
    return 0
}

mp_run_auto_update
