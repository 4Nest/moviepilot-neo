"""上游 MoviePilot v3 配置与数据库首次迁移至 moviepilot-neo 的自动无损适配器。"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import ruamel.yaml
import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import ValidationError
from sqlalchemy.engine import Connection, Engine

from app.core.config import settings
from app.db import Base
from app.log import logger
from app.modules.themoviedb.category import HEADER_COMMENTS
from app.schemas.system import (
    AppPushNotificationConfig,
    DownloaderConf,
    MediaServerConf,
    NotificationConf,
    NotificationSwitchConf,
    StorageConf,
    TelegramNotificationConfig,
    TransferDirectoryConf,
    WechatNotificationConfig,
)

# 迁移完成后锚定的 NEO Alembic 前置版本；随后由 update_db() 继续执行 a7383c1d9e20 直达 head。
NEO_PRE_FILTER_RULE_REVISION = "c9d0e1f2a3b4"

# 上游 v3 专属数据表清单（NEO 运行时不使用，提取完插件配置后安全清理）。
V3_ONLY_TABLES: frozenset[str] = frozenset({
    "agentchat",
    "agentinvocation",
    "agenttask",
    "agenttaskrun",
    "outbox",
    "pluginidentity",
    "plugininstallation",
    "plugininstance",
    "searchsession",
    "subscribeactivity",
    "subscriptiondownloadsubmission",
    "subscriptionsearchbatch",
    "subscriptionsearchtask",
    "subscriptionsitebudget",
    "transferexecutionstep",
    "transferpending",
    "transfersettlementreceipt",
    "transfersettlementreceipt_3_0_16_archive",
    "widgetregistration",
})

# 上游 v3 内核专属废弃配置键（转换完成后清理，保留第三方插件的自定义键）。
V3_DEPRECATED_SYSTEM_CONFIG_KEYS: frozenset[str] = frozenset({
    "AIAgentConfig",
    "AIAgentMcpServers",
    "DefaultMusicSubscribeConfig",
    "MediaClassificationPolicy",
    "PluginInstances",
    "PluginInstancesImported",
    "SubscribeSearchStrategy",
})

# 6 张在 v3 3.0.2 中移除了独立媒体 ID 列的通用媒体表及待恢复字段定义。
MEDIA_TABLE_LEGACY_COLUMNS: dict[str, tuple[tuple[str, sa.types.TypeEngine], ...]] = {
    "subscribe": (
        ("tmdbid", sa.Integer()),
        ("imdbid", sa.String()),
        ("tvdbid", sa.Integer()),
        ("doubanid", sa.String()),
        ("bangumiid", sa.Integer()),
        ("anilistid", sa.Integer()),
        ("mediaid", sa.String()),
    ),
    "subscribehistory": (
        ("tmdbid", sa.Integer()),
        ("imdbid", sa.String()),
        ("tvdbid", sa.Integer()),
        ("doubanid", sa.String()),
        ("bangumiid", sa.Integer()),
        ("anilistid", sa.Integer()),
        ("mediaid", sa.String()),
    ),
    "downloadhistory": (
        ("tmdbid", sa.Integer()),
        ("imdbid", sa.String()),
        ("tvdbid", sa.Integer()),
        ("doubanid", sa.String()),
        ("bangumiid", sa.Integer()),
        ("anilistid", sa.Integer()),
    ),
    "transferhistory": (
        ("tmdbid", sa.Integer()),
        ("imdbid", sa.String()),
        ("tvdbid", sa.Integer()),
        ("doubanid", sa.String()),
        ("bangumiid", sa.Integer()),
        ("anilistid", sa.Integer()),
    ),
    "downloadfailure": (
        ("tmdbid", sa.Integer()),
        ("doubanid", sa.String()),
        ("bangumiid", sa.Integer()),
        ("anilistid", sa.Integer()),
    ),
    # 注意：NEO ORM 中 mediaserveritem.tvdbid 为 String 类型。
    "mediaserveritem": (
        ("tmdbid", sa.Integer()),
        ("imdbid", sa.String()),
        ("tvdbid", sa.String()),
    ),
}

# NEO 分支演进新增的业务字段定义（在对齐前幂等补齐）。
NEO_EXTRA_COLUMNS: dict[str, tuple[tuple[str, sa.types.TypeEngine], ...]] = {
    "subscribe": (
        ("version_rules", sa.JSON()),
        ("version_progress", sa.JSON()),
        ("version_mode", sa.String()),
        ("skip_library_check", sa.Integer()),
        ("decision_summary", sa.JSON()),
    ),
    "subscribehistory": (
        ("version_rules", sa.JSON()),
        ("version_progress", sa.JSON()),
        ("version_mode", sa.String()),
    ),
    "downloadhistory": (
        ("note", sa.JSON()),
        ("subscribe_id", sa.Integer()),
        ("version_rule_id", sa.String()),
        ("version_settings", sa.JSON()),
    ),
    "user": (
        ("notification_identity", sa.String(32)),
    ),
}

# v3 MediaClassificationPolicy 标准类型键到 TMDB Genre ID 的反向映射。
_V3_GENRE_KEY_TO_TMDB_ID: dict[str, str] = {
    "adventure": "12",
    "fantasy": "14",
    "animation": "16",
    "drama": "18",
    "horror": "27",
    "action": "28",
    "comedy": "35",
    "history": "36",
    "western": "37",
    "thriller": "53",
    "crime": "80",
    "documentary": "99",
    "science_fiction": "878",
    "mystery": "9648",
    "music": "10402",
    "romance": "10749",
    "family": "10751",
    "war": "10752",
    "kids": "10762",
    "reality": "10764",
    "talk": "10767",
    "tv_movie": "10770",
}

_ALLOWED_NOTIFICATION_SWITCHES: frozenset[str] = frozenset({
    "资源下载", "整理入库", "订阅", "站点", "媒体服务器", "手动处理", "插件", "其它",
})
_ALLOWED_NOTIFICATION_ACTIONS: frozenset[str] = frozenset({
    "all", "user", "admin", "user,admin",
})

# v3 3.0.0 默认音乐通知模板特征与 NEO 原生默认模板。
_NEO_DEFAULT_NOTIFICATION_TEMPLATES: dict[str, str] = {
    "organizeSuccess": (
        "{\n"
        "    'title': '{{ title_year }}'"
        "'{% if season_episode %} {{ season_episode }}{% endif %} 已入库',\n"
        "    'text': '{% if vote_average %}评分：{{ vote_average }}，{% endif %}'"
        "'类型：{{ type }}'"
        "'{% if category %}，类别：{{ category }}{% endif %}'"
        "'{% if resource_term %}，质量：{{ resource_term }}{% endif %}，'"
        "'共{{ file_count }}个文件，大小：{{ total_size }}'"
        "'{% if err_msg %}，以下文件处理失败：{{ err_msg }}{% endif %}'\n"
        "}"
    ),
    "downloadAdded": (
        "{\n"
        "    'title': '{{ title_year }}'"
        "'{% if download_episodes %} {{ season_fmt }} {{ download_episodes }}"
        "{% else %}{{ season_episode }}{% endif %} 开始下载',\n"
        "    'text': '{% if site_name %}站点：{{ site_name }}{% endif %}'"
        "'{% if resource_term %}\\n质量：{{ resource_term }}{% endif %}'"
        "'{% if size %}\\n大小：{{ size }}{% endif %}'"
        "'{% if torrent_title %}\\n种子：{{ torrent_title }}{% endif %}'"
        "'{% if pubdate %}\\n发布时间：{{ pubdate }}{% endif %}'"
        "'{% if freedate %}\\n免费时间：{{ freedate }}{% endif %}'"
        "'{% if seeders %}\\n做种数：{{ seeders }}{% endif %}'"
        "'{% if volume_factor %}\\n促销：{{ volume_factor }}{% endif %}'"
        "'{% if hit_and_run %}\\nHit&Run：{{ hit_and_run }}{% endif %}'"
        "'{% if labels %}\\n标签：{{ labels }}{% endif %}'"
        "'{% if description %}\\n描述：{{ description }}{% endif %}'\n"
        "}"
    ),
}

_MEDIA_SOURCE_ALIASES: dict[str, str] = {
    "tmdb": "themoviedb",
    "themoviedb": "themoviedb",
    "douban": "douban",
    "bangumi": "bangumi",
    "anilist": "anilist",
    "imdb": "imdb",
    "tvdb": "tvdb",
}


@dataclass
class V3MigrationReport:
    """承载上游 v3 迁移检测、备份与结构数据适配结果的完整报告。"""

    detected: bool = False
    dry_run: bool = False
    executed: bool = False
    reason: str = ""
    original_revision: Optional[str] = None
    target_revision: Optional[str] = None
    db_backup_path: Optional[str] = None
    category_backup_path: Optional[str] = None
    plugins_backup_archive_path: Optional[str] = None
    restored_plugin_configs: int = 0
    restored_installed_plugins: list[str] = field(default_factory=list)
    skipped_plugin_clones: list[str] = field(default_factory=list)
    projected_movie_categories: int = 0
    projected_tv_categories: int = 0
    category_yaml_written: bool = False
    sanitized_directories: int = 0
    removed_music_directories: int = 0
    removed_unsupported_notifications: list[str] = field(default_factory=list)
    removed_deprecated_config_keys: list[str] = field(default_factory=list)
    backfilled_media_rows: dict[str, int] = field(default_factory=dict)
    removed_music_subscribes: int = 0
    normalized_music_history_rows: dict[str, int] = field(default_factory=dict)
    canonical_superuser: Optional[str] = None
    superuser_env_updated: bool = False
    rebuilt_tables: list[str] = field(default_factory=list)
    dropped_v3_tables: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转换为可 JSON 序列化的字典。"""
        return asdict(self)

    def summary_lines(self) -> list[str]:
        """生成面向终端与日志的可读中文摘要行。"""
        if not self.detected:
            return ["未检测到上游 MoviePilot v3 数据库，无需执行 v3 -> NEO 迁移。"]
        mode = "预检模式 (Dry Run)" if self.dry_run else "已执行迁移"
        lines = [
            f"状态：{mode}",
            f"触发原因：{self.reason}",
            f"原 Alembic 版本：{self.original_revision or '未记录'} -> 目标前置版本：{self.target_revision or NEO_PRE_FILTER_RULE_REVISION}",
        ]
        if self.db_backup_path:
            lines.append(f"数据库备份：{self.db_backup_path}")
        if self.plugins_backup_archive_path:
            lines.append(f"v3 插件备份归档：{self.plugins_backup_archive_path}")
        if self.restored_plugin_configs or self.restored_installed_plugins:
            lines.append(
                f"插件恢复：还原配置 {self.restored_plugin_configs} 项，同步启用插件 {len(self.restored_installed_plugins)} 个"
            )
        if self.skipped_plugin_clones:
            lines.append(f"跳过 v3 插件分身：{', '.join(self.skipped_plugin_clones)}")
        if self.projected_movie_categories or self.projected_tv_categories:
            yaml_note = (
                "（将写入 category.yaml）"
                if self.dry_run and self.category_yaml_written
                else ("（已写入 category.yaml）" if self.category_yaml_written else "")
            )
            lines.append(
                f"二级分类还原：电影 {self.projected_movie_categories} 类、剧集 {self.projected_tv_categories} 类{yaml_note}"
            )
        total_backfilled = sum(self.backfilled_media_rows.values())
        if total_backfilled:
            detail = ", ".join(f"{k}={v}" for k, v in sorted(self.backfilled_media_rows.items()) if v)
            lines.append(f"媒体 ID 回填：共 {total_backfilled} 行 ({detail})")
        if self.removed_music_subscribes or self.normalized_music_history_rows:
            hist_total = sum(self.normalized_music_history_rows.values())
            lines.append(
                f"音乐条目收敛：清理活动音乐订阅 {self.removed_music_subscribes} 条，历史记录类型归一化 {hist_total} 条"
            )
        if self.canonical_superuser:
            suffix = "（已自动同步 SUPERUSER 配置）" if self.superuser_env_updated else ""
            lines.append(f"管理员账号：{self.canonical_superuser}{suffix}")
        if self.rebuilt_tables:
            lines.append(f"对齐业务表：{', '.join(self.rebuilt_tables)}")
        if self.dropped_v3_tables:
            lines.append(f"清理 v3 孤儿表：{', '.join(self.dropped_v3_tables)}")
        for warn in self.warnings:
            lines.append(f"提示：{warn}")
        return lines


def _load_neo_revisions(alembic_cfg: Config) -> set[str]:
    """读取 NEO 当前 Alembic 版本树中的全部合法 revision ID。"""
    script = ScriptDirectory.from_config(alembic_cfg)
    return {rev.revision for rev in script.walk_revisions() if rev.revision}


def _decode_json_value(raw: Any) -> Any:
    """兼容 SQLite/PostgreSQL 驱动返回原始 JSON 字符串或已反序列化对象。"""
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw
    return raw


def detect_v3_database(
    connection: Connection,
    neo_revisions: set[str],
) -> tuple[bool, Optional[str], str]:
    """
    精确判定当前数据库是否来自上游 MoviePilot v3。

    同时覆盖两类场景：
    1. ``alembic_version.version_num`` 不在 NEO 版本链中；
    2. 极少数重合 revision（如 ``a7383c1d9e20``）下物理表缺失 ``tmdbid`` 或含 v3 专属表。

    :param connection: 数据库连接
    :param neo_revisions: NEO 支持的全部 Alembic 版本号集合
    :return: ``(是否检测到 v3 数据库, 当前版本号, 判定原因)``
    """
    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names())
    current_rev: Optional[str] = None
    if "alembic_version" in tables:
        row = connection.execute(
            sa.text("SELECT version_num FROM alembic_version LIMIT 1")
        ).first()
        if row and row[0]:
            current_rev = str(row[0]).strip()

    subscribe_cols = (
        {col["name"] for col in inspector.get_columns("subscribe")}
        if "subscribe" in tables
        else set()
    )
    present_v3_tables = sorted(tables & V3_ONLY_TABLES)

    if current_rev and current_rev not in neo_revisions:
        return True, current_rev, f"Alembic 版本 {current_rev} 属于上游 v3 分支"

    if "subscribe" in tables and "media_source" in subscribe_cols and "tmdbid" not in subscribe_cols:
        return True, current_rev, "subscribe 表采用 v3 统一媒体身份结构且缺失 tmdbid 等专用列"

    if present_v3_tables and "version_rules" not in subscribe_cols:
        return True, current_rev, f"检测到上游 v3 专属数据表 ({', '.join(present_v3_tables[:4])})"

    if (
        "subscribe" in tables
        and bool(subscribe_cols & {"music_type", "identity_key", "activity_key"})
        and "version_rules" not in subscribe_cols
    ):
        return True, current_rev, "subscribe 表包含上游 v3 专属字段且尚未初始化 NEO 多版本字段"

    return False, current_rev, ""


def _backup_sqlite_database(db_path: Path, timestamp: str) -> Optional[Path]:
    """使用 SQLite 在线备份 API 创建一致性快照副本。"""
    if not db_path.exists():
        return None
    backup_path = db_path.parent / f"{db_path.name}.v3-backup-{timestamp}"
    src_conn = sqlite3.connect(str(db_path))
    dst_conn = sqlite3.connect(str(backup_path))
    try:
        with dst_conn:
            src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()
    return backup_path


def _archive_v3_plugins_backup(
    config_path: Path,
    timestamp: str,
    dry_run: bool,
) -> Optional[Path]:
    """归档 v3 遗留的 plugins_backup 目录，防止 v3 专属插件包覆盖 NEO 内置 v2 插件。"""
    backup_dir = config_path / "plugins_backup"
    if not backup_dir.exists():
        return None
    archive_path = config_path / f"plugins_backup.v3-backup-{timestamp}"
    if not dry_run:
        shutil.move(str(backup_dir), str(archive_path))
    return archive_path


def _load_system_configs(connection: Connection) -> dict[str, Any]:
    """读取并清理重复的 systemconfig 键值映射（同键保留最大 ID 记录）。"""
    inspector = sa.inspect(connection)
    if "systemconfig" not in inspector.get_table_names():
        return {}
    rows = connection.execute(
        sa.text("SELECT id, key, value FROM systemconfig ORDER BY id ASC")
    ).fetchall()
    result: dict[str, Any] = {}
    for _, key, raw_val in rows:
        if not key or not str(key).strip():
            continue
        result[str(key)] = _decode_json_value(raw_val)
    return result


def _upsert_system_config(
    connection: Connection,
    key: str,
    value: Any,
    dry_run: bool,
) -> None:
    """在迁移事务中幂等更新或插入单条 systemconfig。"""
    if dry_run:
        return
    systemconfig = sa.table(
        "systemconfig",
        sa.column("id", sa.Integer()),
        sa.column("key", sa.String()),
        sa.column("value", sa.JSON()),
    )
    existing = connection.execute(
        sa.select(systemconfig.c.id)
        .where(systemconfig.c.key == key)
        .order_by(systemconfig.c.id.desc())
    ).scalars().all()
    if existing:
        keep_id = existing[0]
        if len(existing) > 1:
            connection.execute(
                systemconfig.delete().where(systemconfig.c.id.in_(existing[1:]))
            )
        connection.execute(
            systemconfig.update()
            .where(systemconfig.c.id == keep_id)
            .values(value=value)
        )
    else:
        connection.execute(systemconfig.insert().values(key=key, value=value))


def _delete_system_config_keys(
    connection: Connection,
    keys: set[str],
    dry_run: bool,
) -> None:
    """从 systemconfig 删除指定的废弃配置键。"""
    if dry_run or not keys:
        return
    systemconfig = sa.table("systemconfig", sa.column("key", sa.String()))
    connection.execute(systemconfig.delete().where(systemconfig.c.key.in_(sorted(keys))))


def _restore_plugins_from_v3(
    connection: Connection,
    sys_configs: dict[str, Any],
    report: V3MigrationReport,
    dry_run: bool,
) -> None:
    """
    从 v3 ``plugininstance`` 表恢复插件配置到 ``systemconfig(plugin.<id>)`` 并同步启用清单。
    """
    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names())
    if "plugininstance" not in tables:
        return

    cols = {c["name"] for c in inspector.get_columns("plugininstance")}
    select_cols = [
        c for c in ("instance_id", "source_plugin_id", "config_data", "is_enabled", "is_default_target")
        if c in cols
    ]
    if "instance_id" not in select_cols:
        return

    rows = connection.execute(
        sa.text(f"SELECT {', '.join(select_cols)} FROM plugininstance ORDER BY id ASC")
    ).mappings().all()

    raw_installed = sys_configs.get("UserInstalledPlugins")
    installed_list: list[str] = (
        [str(x) for x in raw_installed if isinstance(x, str) and x.strip()]
        if isinstance(raw_installed, list)
        else []
    )
    installed_seen = {pid.lower(): pid for pid in installed_list}

    fallback_clone_configs: dict[str, dict[str, Any]] = {}
    primary_configs: dict[str, dict[str, Any]] = {}
    clone_ids: set[str] = set()

    for row in rows:
        instance_id = str(row.get("instance_id") or "").strip()
        if not instance_id:
            continue
        source_id = str(row.get("source_plugin_id") or instance_id).strip() or instance_id
        is_clone = instance_id != source_id
        is_enabled = bool(row.get("is_enabled", True)) if "is_enabled" in cols else True
        config_data = _decode_json_value(row.get("config_data")) if "config_data" in cols else None

        if is_clone:
            clone_ids.add(instance_id)
            report.skipped_plugin_clones.append(f"{instance_id} (源插件: {source_id})")
            if isinstance(config_data, dict) and config_data:
                if row.get("is_default_target") or source_id not in fallback_clone_configs:
                    fallback_clone_configs[source_id] = config_data
        else:
            if isinstance(config_data, dict) and config_data:
                primary_configs[source_id] = config_data

        if is_enabled and source_id.lower() not in installed_seen:
            installed_list.append(source_id)
            installed_seen[source_id.lower()] = source_id
            report.restored_installed_plugins.append(source_id)

    # 移除可能混入已安装列表的虚拟分身 ID
    if clone_ids:
        installed_list = [pid for pid in installed_list if pid not in clone_ids]

    all_candidate_configs = dict(fallback_clone_configs)
    all_candidate_configs.update(primary_configs)

    for pid, conf in all_candidate_configs.items():
        cfg_key = f"plugin.{pid}"
        existing_conf = sys_configs.get(cfg_key)
        if not isinstance(existing_conf, dict) or not existing_conf:
            sys_configs[cfg_key] = conf
            _upsert_system_config(connection, cfg_key, conf, dry_run=dry_run)
            report.restored_plugin_configs += 1

    if installed_list != (raw_installed if isinstance(raw_installed, list) else []):
        sys_configs["UserInstalledPlugins"] = installed_list
        _upsert_system_config(connection, "UserInstalledPlugins", installed_list, dry_run=dry_run)

    # 同步清理 PluginFolders 中引用的 v3 虚拟分身 ID
    plugin_folders = sys_configs.get("PluginFolders")
    if clone_ids and isinstance(plugin_folders, dict):
        cleaned_folders: dict[str, Any] = {}
        changed_folders = False
        for folder_name, folder_val in plugin_folders.items():
            if isinstance(folder_val, list):
                filtered = [p for p in folder_val if p not in clone_ids]
                if filtered != folder_val:
                    changed_folders = True
                cleaned_folders[folder_name] = filtered
            elif isinstance(folder_val, dict) and isinstance(folder_val.get("plugins"), list):
                filtered = [p for p in folder_val["plugins"] if p not in clone_ids]
                if filtered != folder_val["plugins"]:
                    changed_folders = True
                updated_folder = dict(folder_val)
                updated_folder["plugins"] = filtered
                cleaned_folders[folder_name] = updated_folder
            else:
                cleaned_folders[folder_name] = folder_val
        if changed_folders:
            sys_configs["PluginFolders"] = cleaned_folders
            _upsert_system_config(connection, "PluginFolders", cleaned_folders, dry_run=dry_run)

    if report.skipped_plugin_clones:
        report.warnings.append(
            f"NEO 使用单实例 v2 插件架构，已跳过 {len(report.skipped_plugin_clones)} 个 v3 插件分身并保留主插件配置"
        )


def _iter_condition_leaves(node: Any) -> list[Mapping[str, Any]]:
    """按顺序递归展开 v3 分类条件树的全部叶子节点。"""
    if not isinstance(node, Mapping):
        return []
    if "field" in node and "operator" in node:
        return [node]
    children: list[Any] = []
    if isinstance(node.get("all"), list):
        children = node["all"]
    elif isinstance(node.get("any"), list):
        children = node["any"]
    elif isinstance(node.get("not"), Mapping):
        children = [node["not"]]
    leaves: list[Mapping[str, Any]] = []
    for child in children:
        leaves.extend(_iter_condition_leaves(child))
    return leaves


def _restore_alias_value(value: str, aliases: Mapping[str, Any], standard: bool) -> str:
    """优先恢复分类字段值在迁移前的原始大小写表达。"""
    if not isinstance(aliases, Mapping):
        return value
    if standard:
        for alias, canonical in aliases.items():
            if str(canonical) == value and str(alias) == str(alias).casefold() and str(alias) != value:
                return str(alias)
        return value
    for alias, canonical in aliases.items():
        if str(canonical) == value.upper():
            return str(alias)
    return value


def _project_v3_rule_to_legacy_dict(
    rule: Mapping[str, Any],
    field_aliases: Mapping[str, Any],
    media_type: str,
) -> Optional[dict[str, Any]]:
    """将单条 v3 分类规则投影为 NEO category.yaml 规则字典。"""
    when = rule.get("when")
    if (
        not rule.get("enabled", True)
        and isinstance(when, Mapping)
        and when.get("field") == "identity.media_source"
        and when.get("value") == "themoviedb"
    ):
        return None

    tokens_by_field: dict[str, list[str]] = {}
    for cond in _iter_condition_leaves(when):
        field_id = str(cond.get("field") or "")
        operator = str(cond.get("operator") or "")
        raw_val = cond.get("value")
        values = (
            [str(v) for v in raw_val if v is not None]
            if isinstance(raw_val, list)
            else ([str(raw_val)] if raw_val is not None else [])
        )

        if field_id in {"identity.media_source", "media.type"}:
            continue

        target_field: Optional[str] = None
        projected_vals: list[str] = []
        negative = operator in {"contains_none", "not_in", "not_equals"}

        if field_id == "media.genre_keys":
            target_field = "genre_ids"
            projected_vals = [
                _V3_GENRE_KEY_TO_TMDB_ID[v]
                for v in values
                if v in _V3_GENRE_KEY_TO_TMDB_ID
            ]
        elif field_id == "media.language":
            target_field = "original_language"
            aliases = field_aliases.get(field_id, {}) if isinstance(field_aliases, Mapping) else {}
            projected_vals = [_restore_alias_value(v, aliases, standard=True) for v in values]
        elif field_id == "media.countries":
            target_field = "production_countries" if media_type == "电影" else "origin_country"
            aliases = field_aliases.get(field_id, {}) if isinstance(field_aliases, Mapping) else {}
            projected_vals = [_restore_alias_value(v, aliases, standard=True) for v in values]
        elif field_id == "media.year":
            target_field = "release_year"
            if operator == "between" and len(values) == 2:
                projected_vals = [f"{values[0]}-{values[1]}"]
            else:
                projected_vals = values
        elif field_id == "media.runtime":
            target_field = "runtime"
            projected_vals = values
        elif field_id == "media.adult":
            if operator in {"is_true", "is_false"}:
                target_field = "adult"
                projected_vals = ["TRUE" if operator == "is_true" else "FALSE"]
                negative = False
        elif field_id.startswith("ext.themoviedb."):
            ext_name = field_id.removeprefix("ext.themoviedb.")
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", ext_name):
                target_field = ext_name
                aliases = field_aliases.get(field_id, {}) if isinstance(field_aliases, Mapping) else {}
                projected_vals = [_restore_alias_value(v, aliases, standard=False) for v in values]

        if not target_field:
            continue
        if operator == "exists":
            tokens_by_field.setdefault(target_field, []).append(",")
            continue
        if not projected_vals:
            continue
        bucket = tokens_by_field.setdefault(target_field, [])
        for item in projected_vals:
            token = f"!{item}" if negative else item
            if token not in bucket:
                bucket.append(token)

    return {k: ",".join(v) for k, v in tokens_by_field.items()} if tokens_by_field else None


def _project_v3_classification_to_category_yaml(
    sys_configs: dict[str, Any],
    config_path: Path,
    timestamp: str,
    report: V3MigrationReport,
    dry_run: bool,
) -> dict[str, str]:
    """
    解析 v3 ``MediaClassificationPolicy``，生成 ``category_id -> 分类目录名`` 映射，
    并将电影与电视剧分类规则无损还原写入 ``category.yaml``。
    """
    policy_state = sys_configs.get("MediaClassificationPolicy")
    if not isinstance(policy_state, Mapping):
        return {}
    active = policy_state.get("active")
    if not isinstance(active, Mapping):
        return {}

    categories = active.get("categories")
    rules = active.get("rules")
    fallbacks = active.get("fallbacks") if isinstance(active.get("fallbacks"), Mapping) else {}
    field_aliases = active.get("field_aliases") if isinstance(active.get("field_aliases"), Mapping) else {}
    if not isinstance(categories, list):
        return {}

    rules_by_cat: dict[str, Mapping[str, Any]] = {}
    if isinstance(rules, list):
        for rule in rules:
            if not isinstance(rule, Mapping):
                continue
            target = rule.get("target")
            cat_id = target.get("category_id") if isinstance(target, Mapping) else None
            if isinstance(cat_id, str) and cat_id:
                if rule.get("enabled", True) or cat_id not in rules_by_cat:
                    rules_by_cat.setdefault(cat_id, rule)

    cat_id_to_name: dict[str, str] = {}
    projected_movie: dict[str, Any] = {}
    projected_tv: dict[str, Any] = {}

    for cat in categories:
        if not isinstance(cat, Mapping):
            continue
        cat_id = str(cat.get("id") or "").strip()
        media_type = str(cat.get("media_type") or "").strip()
        raw_path = cat.get("path")
        if isinstance(raw_path, list) and raw_path and all(isinstance(seg, str) and seg.strip() for seg in raw_path):
            display_path = "/".join(seg.strip() for seg in raw_path)
        else:
            display_path = str(cat.get("name") or "").strip()
        if not cat_id or not display_path:
            continue

        if media_type in {"电影", "电视剧"}:
            cat_id_to_name[cat_id] = display_path
        if not cat.get("enabled", True) or media_type not in {"电影", "电视剧"}:
            continue

        target_map = projected_movie if media_type == "电影" else projected_tv
        if fallbacks.get(media_type) == cat_id:
            target_map[display_path] = None
            continue

        matched_rule = rules_by_cat.get(cat_id)
        if matched_rule is None:
            target_map[display_path] = None
        else:
            target_map[display_path] = _project_v3_rule_to_legacy_dict(
                matched_rule, field_aliases, media_type
            )

    report.projected_movie_categories = len(projected_movie)
    report.projected_tv_categories = len(projected_tv)

    if not projected_movie and not projected_tv:
        return cat_id_to_name

    yaml_dumper = ruamel.yaml.YAML()
    buf = StringIO()
    buf.write(HEADER_COMMENTS)
    payload: dict[str, Any] = {
        "movie": projected_movie,
        "tv": projected_tv,
    }
    yaml_dumper.dump(payload, buf)
    rendered = buf.getvalue()

    category_path = config_path / "category.yaml"
    existing_text: Optional[str] = None
    if category_path.exists():
        try:
            existing_text = category_path.read_text(encoding="utf-8")
        except OSError:
            existing_text = None

    if existing_text != rendered:
        if not dry_run:
            config_path.mkdir(parents=True, exist_ok=True)
            if category_path.exists():
                bak_path = config_path / f"category.yaml.v3-backup-{timestamp}"
                shutil.copy2(category_path, bak_path)
                report.category_backup_path = str(bak_path)
            tmp_path = config_path / "category.yaml.tmp"
            tmp_path.write_text(rendered, encoding="utf-8")
            os.replace(tmp_path, category_path)
        report.category_yaml_written = True

    return cat_id_to_name


def _filter_model_fields(item: Mapping[str, Any], allowed_fields: set[str]) -> dict[str, Any]:
    """仅保留目标 Pydantic 模型允许的字段集合。"""
    return {k: v for k, v in item.items() if k in allowed_fields}


def _sanitize_system_configs(
    connection: Connection,
    sys_configs: dict[str, Any],
    cat_id_to_name: Mapping[str, str],
    report: V3MigrationReport,
    dry_run: bool,
) -> None:
    """清洗各类 SystemConfig JSON 载荷以严格契合 NEO `extra='forbid'` 模型校验。"""
    # 1. Directories
    raw_dirs = sys_configs.get("Directories")
    if isinstance(raw_dirs, list):
        allowed_dir_fields = set(TransferDirectoryConf.model_fields.keys())
        cleaned_dirs: list[dict[str, Any]] = []
        changed_dirs = False
        for item in raw_dirs:
            if not isinstance(item, Mapping):
                changed_dirs = True
                continue
            if str(item.get("media_type") or "").strip() == "音乐":
                report.removed_music_directories += 1
                changed_dirs = True
                continue
            entry = dict(item)
            cat_id = str(entry.get("media_category_id") or "").strip()
            if not entry.get("media_category") and cat_id and cat_id in cat_id_to_name:
                entry["media_category"] = cat_id_to_name[cat_id]
                changed_dirs = True
            if entry.get("transfer_type") == "reflink":
                entry["transfer_type"] = "copy"
                changed_dirs = True
                report.warnings.append(
                    f"目录 {entry.get('name') or entry.get('download_path')!r} 的整理方式已由 v3 reflink 调整为 copy"
                )
            filtered = _filter_model_fields(entry, allowed_dir_fields)
            if filtered != dict(item):
                changed_dirs = True
            cleaned_dirs.append(filtered)
        if changed_dirs:
            report.sanitized_directories = len(cleaned_dirs)
            sys_configs["Directories"] = cleaned_dirs
            _upsert_system_config(connection, "Directories", cleaned_dirs, dry_run=dry_run)

    # 2. Downloaders / MediaServers / Storages
    for key_name, model_cls, defaults in (
        ("Downloaders", DownloaderConf, {"bt_default": False, "bt_save_path": None}),
        ("MediaServers", MediaServerConf, {}),
        ("Storages", StorageConf, {}),
    ):
        raw_items = sys_configs.get(key_name)
        if not isinstance(raw_items, list):
            continue
        allowed = set(model_cls.model_fields.keys())
        cleaned_list: list[dict[str, Any]] = []
        changed = False
        for item in raw_items:
            if not isinstance(item, Mapping):
                changed = True
                continue
            filtered = _filter_model_fields(item, allowed)
            for def_k, def_v in defaults.items():
                if def_k not in filtered:
                    filtered[def_k] = def_v
                    changed = True
            if filtered != dict(item):
                changed = True
            cleaned_list.append(filtered)
        if changed:
            sys_configs[key_name] = cleaned_list
            _upsert_system_config(connection, key_name, cleaned_list, dry_run=dry_run)

    # 3. Notifications（仅保留 telegram / wechat / apppush 并清洗子参数）
    raw_notifs = sys_configs.get("Notifications")
    if isinstance(raw_notifs, list):
        allowed_notif_fields = set(NotificationConf.model_fields.keys())
        sub_models = {
            "telegram": set(TelegramNotificationConfig.model_fields.keys()),
            "wechat": set(WechatNotificationConfig.model_fields.keys()),
            "apppush": set(AppPushNotificationConfig.model_fields.keys()),
        }
        cleaned_notifs: list[dict[str, Any]] = []
        changed_notifs = False
        for item in raw_notifs:
            if not isinstance(item, Mapping):
                changed_notifs = True
                continue
            ntype = str(item.get("type") or "").strip().lower()
            nname = str(item.get("name") or ntype or "unnamed")
            if ntype not in sub_models:
                report.removed_unsupported_notifications.append(f"{nname} ({ntype})")
                changed_notifs = True
                continue
            filtered = _filter_model_fields(item, allowed_notif_fields)
            filtered["type"] = ntype
            raw_sub = filtered.get("config")
            filtered["config"] = (
                _filter_model_fields(raw_sub, sub_models[ntype])
                if isinstance(raw_sub, Mapping)
                else {}
            )
            raw_switchs = filtered.get("switchs")
            filtered["switchs"] = (
                [s for s in raw_switchs if isinstance(s, str) and s in _ALLOWED_NOTIFICATION_SWITCHES]
                if isinstance(raw_switchs, list)
                else []
            )
            try:
                validated = NotificationConf.model_validate(filtered).model_dump(exclude_none=True)
                if validated != dict(item):
                    changed_notifs = True
                cleaned_notifs.append(validated)
            except ValidationError as exc:
                changed_notifs = True
                report.warnings.append(f"通知渠道 {nname!r} 参数不完整已跳过：{exc.errors()[0].get('msg', '')}")
        if changed_notifs:
            sys_configs["Notifications"] = cleaned_notifs
            _upsert_system_config(connection, "Notifications", cleaned_notifs, dry_run=dry_run)
        if report.removed_unsupported_notifications:
            report.warnings.append(
                "已移除 NEO 不支持的通知渠道：" + ", ".join(report.removed_unsupported_notifications)
            )

    # 4. NotificationSwitchs
    raw_switches = sys_configs.get("NotificationSwitchs")
    if isinstance(raw_switches, list):
        allowed_sw_fields = set(NotificationSwitchConf.model_fields.keys())
        cleaned_sw: list[dict[str, Any]] = []
        changed_sw = False
        for item in raw_switches:
            if not isinstance(item, Mapping):
                changed_sw = True
                continue
            filtered = _filter_model_fields(item, allowed_sw_fields)
            if (
                filtered.get("type") not in _ALLOWED_NOTIFICATION_SWITCHES
                or filtered.get("action", "all") not in _ALLOWED_NOTIFICATION_ACTIONS
            ):
                changed_sw = True
                continue
            if filtered != dict(item):
                changed_sw = True
            cleaned_sw.append(filtered)
        if changed_sw:
            sys_configs["NotificationSwitchs"] = cleaned_sw
            _upsert_system_config(connection, "NotificationSwitchs", cleaned_sw, dry_run=dry_run)

    # 5. NotificationTemplates（将 v3 3.0.0 默认音乐模板还原为 NEO 默认模板，保留用户自定义模板）
    raw_tpls = sys_configs.get("NotificationTemplates")
    if isinstance(raw_tpls, Mapping):
        updated_tpls = dict(raw_tpls)
        changed_tpls = False
        for tpl_key, neo_default in _NEO_DEFAULT_NOTIFICATION_TEMPLATES.items():
            cur_val = str(updated_tpls.get(tpl_key) or "")
            if '{% if track_number %}' in cur_val and '{% if type == "音乐"' in cur_val:
                updated_tpls[tpl_key] = neo_default
                changed_tpls = True
        if changed_tpls:
            sys_configs["NotificationTemplates"] = updated_tpls
            _upsert_system_config(connection, "NotificationTemplates", updated_tpls, dry_run=dry_run)

    # 6. 删除 v3 专属废弃键
    deprecated_present = {k for k in sys_configs if k in V3_DEPRECATED_SYSTEM_CONFIG_KEYS}
    if deprecated_present:
        report.removed_deprecated_config_keys = sorted(deprecated_present)
        _delete_system_config_keys(connection, deprecated_present, dry_run=dry_run)


def _ensure_missing_columns(
    connection: Connection,
    table_name: str,
    specs: tuple[tuple[str, sa.types.TypeEngine], ...],
    dry_run: bool,
) -> set[str]:
    """为既有表幂等添加缺失列并返回最新列名集合。"""
    inspector = sa.inspect(connection)
    if table_name not in inspector.get_table_names():
        return set()
    existing = {col["name"] for col in inspector.get_columns(table_name)}
    dialect = connection.dialect
    for col_name, col_type in specs:
        if col_name in existing:
            continue
        if not dry_run:
            compiled_type = col_type.compile(dialect=dialect)
            quoted_table = dialect.identifier_preparer.quote(table_name)
            quoted_col = dialect.identifier_preparer.quote(col_name)
            connection.execute(
                sa.text(f"ALTER TABLE {quoted_table} ADD COLUMN {quoted_col} {compiled_type}")
            )
        existing.add(col_name)
    return existing


def _parse_positive_int(raw: Any) -> Optional[int]:
    """将正整数字符串解析为 int，非数字或 <=0 返回 None。"""
    if raw is None or isinstance(raw, bool):
        return None
    text_val = str(raw).strip()
    if not text_val or not text_val.isdigit():
        return None
    parsed = int(text_val)
    return parsed if parsed > 0 else None


def _backfill_media_and_neo_columns(
    connection: Connection,
    cat_id_to_name: Mapping[str, str],
    report: V3MigrationReport,
    dry_run: bool,
) -> None:
    """补齐 6 张媒体表的传统来源字段与 NEO 扩展字段，并从 media_source + media_id 无损回填。"""
    inspector = sa.inspect(connection)
    existing_tables = set(inspector.get_table_names())

    for table_name, specs in MEDIA_TABLE_LEGACY_COLUMNS.items():
        if table_name not in existing_tables:
            continue
        extra_specs = NEO_EXTRA_COLUMNS.get(table_name, ())
        cols = _ensure_missing_columns(connection, table_name, specs + extra_specs, dry_run=dry_run)
        if "media_source" not in cols or "media_id" not in cols:
            continue

        actual_cols = {c["name"] for c in sa.inspect(connection).get_columns(table_name)}
        target_fields = [name for name, _ in specs if name in actual_cols or dry_run]
        select_fields = ["id", "media_source", "media_id"]
        for optional_col in (*[n for n, _ in specs], "media_category", "media_category_id"):
            if optional_col in actual_cols and optional_col not in select_fields:
                select_fields.append(optional_col)

        quoted_table = connection.dialect.identifier_preparer.quote(table_name)
        quoted_select = ", ".join(connection.dialect.identifier_preparer.quote(c) for c in select_fields)
        rows = connection.execute(sa.text(f"SELECT {quoted_select} FROM {quoted_table}")).mappings().all()

        backfilled_count = 0
        for row in rows:
            raw_src = str(row.get("media_source") or "").strip().casefold()
            raw_mid = str(row.get("media_id") or "").strip()
            updates: dict[str, Any] = {}

            if raw_src and raw_mid and raw_mid != "0":
                canonical_src = _MEDIA_SOURCE_ALIASES.get(raw_src, raw_src)
                if canonical_src == "themoviedb" and "tmdbid" in target_fields and not row.get("tmdbid"):
                    int_id = _parse_positive_int(raw_mid)
                    if int_id is not None:
                        updates["tmdbid"] = int_id
                elif canonical_src == "douban" and "doubanid" in target_fields and not row.get("doubanid"):
                    updates["doubanid"] = raw_mid
                elif canonical_src == "bangumi" and "bangumiid" in target_fields and not row.get("bangumiid"):
                    int_id = _parse_positive_int(raw_mid)
                    if int_id is not None:
                        updates["bangumiid"] = int_id
                elif canonical_src == "anilist" and "anilistid" in target_fields and not row.get("anilistid"):
                    int_id = _parse_positive_int(raw_mid)
                    if int_id is not None:
                        updates["anilistid"] = int_id
                elif canonical_src == "imdb" and "imdbid" in target_fields and not row.get("imdbid"):
                    updates["imdbid"] = raw_mid
                elif canonical_src == "tvdb" and "tvdbid" in target_fields and not row.get("tvdbid"):
                    if table_name == "mediaserveritem":
                        updates["tvdbid"] = raw_mid
                    else:
                        int_id = _parse_positive_int(raw_mid)
                        if int_id is not None:
                            updates["tvdbid"] = int_id
                elif "mediaid" in target_fields and not row.get("mediaid"):
                    updates["mediaid"] = f"{canonical_src}:{raw_mid}"

            # 若原表缺失 media_category 但有 v3 media_category_id，自动补回二级分类名称
            if (
                "media_category" in actual_cols
                and "media_category_id" in actual_cols
                and not row.get("media_category")
                and row.get("media_category_id")
            ):
                mapped_cat = cat_id_to_name.get(str(row["media_category_id"]).strip())
                if mapped_cat:
                    updates["media_category"] = mapped_cat

            if updates:
                backfilled_count += 1
                if not dry_run:
                    rel = sa.table(
                        table_name,
                        sa.column("id", sa.Integer()),
                        *[sa.column(k) for k in updates],
                    )
                    connection.execute(rel.update().where(rel.c.id == row["id"]).values(**updates))

        if backfilled_count:
            report.backfilled_media_rows[table_name] = backfilled_count

    # 补齐 NEO 订阅多版本默认字段与音乐条目清理
    for sub_table in ("subscribe", "subscribehistory"):
        if sub_table not in existing_tables:
            continue
        _ensure_missing_columns(connection, sub_table, NEO_EXTRA_COLUMNS[sub_table], dry_run=dry_run)
        if not dry_run:
            rel = sa.table(
                sub_table,
                sa.column("version_rules", sa.JSON()),
                sa.column("version_progress", sa.JSON()),
                sa.column("version_mode", sa.String()),
            )
            connection.execute(rel.update().where(rel.c.version_rules.is_(None)).values(version_rules=[]))
            connection.execute(rel.update().where(rel.c.version_progress.is_(None)).values(version_progress={}))
            connection.execute(rel.update().where(rel.c.version_mode.is_(None)).values(version_mode="any"))

    if "subscribe" in existing_tables and not dry_run:
        sub_skip = sa.table("subscribe", sa.column("skip_library_check", sa.Integer()))
        connection.execute(sub_skip.update().where(sub_skip.c.skip_library_check.is_(None)).values(skip_library_check=0))

    if "downloadhistory" in existing_tables:
        _ensure_missing_columns(connection, "downloadhistory", NEO_EXTRA_COLUMNS["downloadhistory"], dry_run=dry_run)

    # 清理活动音乐订阅并归一化历史记录中的 '音乐' 类型，避免运行时 MediaType('音乐') 抛出 ValueError
    if "subscribe" in existing_tables:
        sub_type_rel = sa.table("subscribe", sa.column("id", sa.Integer()), sa.column("type", sa.String()))
        music_subs = connection.execute(
            sa.select(sa.func.count()).select_from(sub_type_rel).where(sub_type_rel.c.type == "音乐")
        ).scalar_one()
        if music_subs:
            report.removed_music_subscribes = int(music_subs)
            if not dry_run:
                connection.execute(sub_type_rel.delete().where(sub_type_rel.c.type == "音乐"))

    for hist_table in ("subscribehistory", "downloadhistory", "transferhistory", "downloadfailure"):
        if hist_table not in existing_tables:
            continue
        actual_cols = {c["name"] for c in sa.inspect(connection).get_columns(hist_table)}
        if "type" not in actual_cols:
            continue
        hist_rel = sa.table(hist_table, sa.column("type", sa.String()))
        music_cnt = connection.execute(
            sa.select(sa.func.count()).select_from(hist_rel).where(hist_rel.c.type == "音乐")
        ).scalar_one()
        if music_cnt:
            report.normalized_music_history_rows[hist_table] = int(music_cnt)
            if not dry_run:
                connection.execute(hist_rel.update().where(hist_rel.c.type == "音乐").values(type="未知"))


def _reconcile_users_and_plugindata(
    connection: Connection,
    report: V3MigrationReport,
    dry_run: bool,
) -> None:
    """补齐用户通知身份、收敛唯一超级管理员并去重 plugindata。"""
    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names())

    if "user" in tables:
        _ensure_missing_columns(connection, "user", NEO_EXTRA_COLUMNS["user"], dry_run=dry_run)
        actual_cols = {c["name"] for c in sa.inspect(connection).get_columns("user")}
        if "notification_identity" in actual_cols and not dry_run:
            user_ident = sa.table(
                "user",
                sa.column("id", sa.Integer()),
                sa.column("notification_identity", sa.String(32)),
            )
            missing_ids = connection.execute(
                sa.select(user_ident.c.id).where(
                    sa.or_(
                        user_ident.c.notification_identity.is_(None),
                        user_ident.c.notification_identity == "",
                    )
                )
            ).scalars().all()
            for uid in missing_ids:
                connection.execute(
                    user_ident.update().where(user_ident.c.id == uid).values(notification_identity=uuid4().hex)
                )

        user_rel = sa.table(
            "user",
            sa.column("id", sa.Integer()),
            sa.column("name", sa.String()),
            sa.column("is_active", sa.Boolean()),
            sa.column("is_superuser", sa.Boolean()),
        )
        rows = connection.execute(
            sa.select(user_rel.c.id, user_rel.c.name, user_rel.c.is_active, user_rel.c.is_superuser)
            .order_by(user_rel.c.id.asc())
        ).mappings().all()

        configured_super = settings.SUPERUSER
        matched = next((r for r in rows if r["name"] == configured_super), None)
        canonical_row = matched or next((r for r in rows if r["is_superuser"]), None)

        if canonical_row is not None:
            canonical_id = int(canonical_row["id"])
            canonical_name = str(canonical_row["name"])
            report.canonical_superuser = canonical_name
            if not dry_run:
                connection.execute(
                    user_rel.update()
                    .where(user_rel.c.id == canonical_id)
                    .values(is_active=True, is_superuser=True)
                )
                connection.execute(
                    user_rel.update()
                    .where(user_rel.c.id != canonical_id)
                    .values(is_superuser=False)
                )
            if canonical_name != configured_super:
                if "SUPERUSER" not in os.environ:
                    if not dry_run:
                        ok, _ = settings.update_setting("SUPERUSER", canonical_name)
                        report.superuser_env_updated = bool(ok)
                    else:
                        report.superuser_env_updated = True
                else:
                    report.warnings.append(
                        f"数据库原超级管理员为 {canonical_name!r}，但环境变量固定了 SUPERUSER={configured_super!r}，"
                        f"请确认环境变量与登录用户名一致"
                    )

    if "plugindata" in tables and not dry_run:
        pdata = sa.table(
            "plugindata",
            sa.column("id", sa.Integer()),
            sa.column("plugin_id", sa.String()),
            sa.column("key", sa.String()),
        )
        keep_ids = sa.select(sa.func.min(pdata.c.id)).group_by(pdata.c.plugin_id, pdata.c.key)
        connection.execute(pdata.delete().where(pdata.c.id.not_in(keep_ids)))


def _sqlite_table_needs_rebuild(
    connection: Connection,
    table_name: str,
    neo_table: sa.Table,
) -> bool:
    """检查 SQLite 物理表是否存在 v3 遗留列、缺失列、非空差异或残留 CHECK/FOREIGN KEY/UNIQUE 约束。"""
    inspector = sa.inspect(connection)
    db_cols = {c["name"]: c for c in inspector.get_columns(table_name)}
    neo_cols = {c.name: c for c in neo_table.columns}

    if set(db_cols.keys()) != set(neo_cols.keys()):
        return True

    for col_name, neo_col in neo_cols.items():
        if neo_col.primary_key:
            continue
        if bool(db_cols[col_name].get("nullable", True)) != bool(neo_col.nullable):
            return True

    if inspector.get_foreign_keys(table_name) or inspector.get_check_constraints(table_name):
        return True

    if inspector.get_unique_constraints(table_name):
        return True

    neo_unique_indexes = {
        tuple(col.name for col in idx.columns)
        for idx in neo_table.indexes
        if idx.unique
    }
    for idx in inspector.get_indexes(table_name):
        if idx.get("unique") and tuple(idx.get("column_names") or ()) not in neo_unique_indexes:
            return True

    ddl_row = connection.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:name"),
        {"name": table_name},
    ).first()
    ddl = str(ddl_row[0] or "").upper() if ddl_row else ""
    if "CHECK" in ddl or "REFERENCES" in ddl:
        return True
    return False


def _rebuild_sqlite_table_to_neo_schema(
    connection: Connection,
    table_name: str,
    neo_table: sa.Table,
) -> None:
    """按 NEO ORM 原生定义无损重建单张 SQLite 业务表及索引。"""
    preparer = connection.dialect.identifier_preparer
    temp_name = f"__neo_mig_{table_name}"
    quoted_orig = preparer.quote(table_name)
    quoted_temp = preparer.quote(temp_name)

    connection.execute(sa.text(f"DROP TABLE IF EXISTS {quoted_temp}"))

    temp_meta = sa.MetaData()
    neo_table.to_metadata(temp_meta, name=temp_name)
    # 先只建表本体，索引在 RENAME 回原表名后按真实名称创建，避免索引名冲突
    temp_table = temp_meta.tables[temp_name]
    temp_table.indexes.clear()
    temp_table.create(bind=connection)

    existing_cols = {c["name"] for c in sa.inspect(connection).get_columns(table_name)}
    copy_cols = [c.name for c in neo_table.columns if c.name in existing_cols]
    quoted_cols = ", ".join(preparer.quote(c) for c in copy_cols)
    if copy_cols:
        connection.execute(
            sa.text(
                f"INSERT INTO {quoted_temp} ({quoted_cols}) "
                f"SELECT {quoted_cols} FROM {quoted_orig}"
            )
        )

    connection.execute(sa.text(f"DROP TABLE {quoted_orig}"))
    connection.execute(sa.text(f"ALTER TABLE {quoted_temp} RENAME TO {quoted_orig}"))

    for idx in neo_table.indexes:
        idx.create(bind=connection, checkfirst=True)


def _align_postgresql_table_to_neo_schema(
    connection: Connection,
    table_name: str,
    neo_table: sa.Table,
) -> bool:
    """将单张 PostgreSQL 业务表对齐为 NEO 原生列与约束定义。"""
    inspector = sa.inspect(connection)
    preparer = connection.dialect.identifier_preparer
    quoted_table = preparer.quote(table_name)
    changed = False

    # 1. 移除外键、检查约束与多余唯一约束
    for fk in inspector.get_foreign_keys(table_name):
        if fk.get("name"):
            connection.execute(
                sa.text(f"ALTER TABLE {quoted_table} DROP CONSTRAINT IF EXISTS {preparer.quote(fk['name'])}")
            )
            changed = True
    for ck in inspector.get_check_constraints(table_name):
        if ck.get("name"):
            connection.execute(
                sa.text(f"ALTER TABLE {quoted_table} DROP CONSTRAINT IF EXISTS {preparer.quote(ck['name'])}")
            )
            changed = True
    for uq in inspector.get_unique_constraints(table_name):
        if uq.get("name"):
            connection.execute(
                sa.text(f"ALTER TABLE {quoted_table} DROP CONSTRAINT IF EXISTS {preparer.quote(uq['name'])}")
            )
            changed = True

    neo_index_names = {idx.name for idx in neo_table.indexes if idx.name}
    for idx in inspector.get_indexes(table_name):
        idx_name = idx.get("name")
        if idx_name and idx_name not in neo_index_names:
            connection.execute(sa.text(f"DROP INDEX IF EXISTS {preparer.quote(idx_name)}"))
            changed = True

    # 2. 补齐缺失列、修正可空性并移除 v3 多余列
    db_cols = {c["name"]: c for c in sa.inspect(connection).get_columns(table_name)}
    neo_cols = {c.name: c for c in neo_table.columns}

    for col_name, neo_col in neo_cols.items():
        quoted_col = preparer.quote(col_name)
        if col_name not in db_cols:
            compiled_type = neo_col.type.compile(dialect=connection.dialect)
            connection.execute(sa.text(f"ALTER TABLE {quoted_table} ADD COLUMN {quoted_col} {compiled_type}"))
            changed = True
        elif not neo_col.primary_key:
            db_nullable = bool(db_cols[col_name].get("nullable", True))
            if db_nullable != bool(neo_col.nullable):
                action = "DROP NOT NULL" if neo_col.nullable else "SET NOT NULL"
                connection.execute(sa.text(f"ALTER TABLE {quoted_table} ALTER COLUMN {quoted_col} {action}"))
                changed = True

    for extra_col in sorted(set(db_cols.keys()) - set(neo_cols.keys())):
        connection.execute(sa.text(f"ALTER TABLE {quoted_table} DROP COLUMN IF EXISTS {preparer.quote(extra_col)}"))
        changed = True

    for idx in neo_table.indexes:
        idx.create(bind=connection, checkfirst=True)

    return changed


def _align_schema_and_cleanup_v3_tables(
    connection: Connection,
    report: V3MigrationReport,
    dry_run: bool,
) -> None:
    """删除 v3 专属孤儿表，并将全部 NEO 业务表精确收敛为原生 Schema。"""
    import app.db.models  # noqa: F401  # pylint: disable=import-outside-toplevel,unused-import

    inspector = sa.inspect(connection)
    existing_tables = set(inspector.get_table_names())
    dialect_name = connection.dialect.name
    preparer = connection.dialect.identifier_preparer

    # 1. 按子表优先顺序删除 v3 专属孤儿表（先删除含外键引用的从表）
    ordered_v3_drops = [
        "transferexecutionstep",
        "transfersettlementreceipt",
        "transfersettlementreceipt_3_0_16_archive",
        "transferpending",
        "widgetregistration",
        *sorted(V3_ONLY_TABLES - {
            "transferexecutionstep",
            "transfersettlementreceipt",
            "transfersettlementreceipt_3_0_16_archive",
            "transferpending",
            "widgetregistration",
        }),
    ]
    for v3_table in ordered_v3_drops:
        if v3_table not in existing_tables:
            continue
        report.dropped_v3_tables.append(v3_table)
        if not dry_run:
            cascade = " CASCADE" if dialect_name == "postgresql" else ""
            connection.execute(sa.text(f"DROP TABLE IF EXISTS {preparer.quote(v3_table)}{cascade}"))

    # 2. 创建 NEO 新增缺失表（如 schedulerhistory）并对齐全部共用表
    for table_name, neo_table in sorted(Base.metadata.tables.items()):
        if table_name not in existing_tables:
            report.rebuilt_tables.append(table_name)
            if not dry_run:
                neo_table.create(bind=connection, checkfirst=True)
            continue

        if dialect_name == "sqlite":
            if _sqlite_table_needs_rebuild(connection, table_name, neo_table):
                report.rebuilt_tables.append(table_name)
                if not dry_run:
                    _rebuild_sqlite_table_to_neo_schema(connection, table_name, neo_table)
        else:
            if dry_run:
                db_cols = {c["name"] for c in inspector.get_columns(table_name)}
                neo_cols = {c.name for c in neo_table.columns}
                if (
                    db_cols != neo_cols
                    or inspector.get_foreign_keys(table_name)
                    or inspector.get_check_constraints(table_name)
                    or inspector.get_unique_constraints(table_name)
                ):
                    report.rebuilt_tables.append(table_name)
            else:
                if _align_postgresql_table_to_neo_schema(connection, table_name, neo_table):
                    report.rebuilt_tables.append(table_name)


def _stamp_neo_revision(connection: Connection, target_revision: str, dry_run: bool) -> None:
    """将 alembic_version 单值重置为 NEO 前置锚点版本。"""
    if dry_run:
        return
    inspector = sa.inspect(connection)
    if "alembic_version" not in inspector.get_table_names():
        connection.execute(
            sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
        )
    else:
        connection.execute(sa.text("DELETE FROM alembic_version"))
    connection.execute(
        sa.text("INSERT INTO alembic_version (version_num) VALUES (:rev)"),
        {"rev": target_revision},
    )


def migrate_v3_database_if_needed(
    engine: Engine,
    alembic_cfg: Config,
    *,
    config_path: Optional[Path] = None,
    dry_run: bool = False,
) -> V3MigrationReport:
    """
    检测并在需要时将上游 MoviePilot v3 配置与数据库原子无损迁移为 NEO 结构。

    :param engine: SQLAlchemy 同步引擎
    :param alembic_cfg: 已绑定 script_location 与数据库 URL 的 Alembic 配置
    :param config_path: MoviePilot 配置根目录，默认读取 ``settings.CONFIG_PATH``
    :param dry_run: 为 True 时仅预检统计即将执行的转换，不修改任何数据库或磁盘文件
    :return: 结构化迁移报告
    """
    resolved_config_path = Path(config_path or settings.CONFIG_PATH)
    neo_revisions = _load_neo_revisions(alembic_cfg)
    report = V3MigrationReport(dry_run=dry_run)

    with engine.connect() as check_conn:
        detected, current_rev, reason = detect_v3_database(check_conn, neo_revisions)

    report.detected = detected
    report.original_revision = current_rev
    report.reason = reason
    if not detected:
        return report

    report.target_revision = NEO_PRE_FILTER_RULE_REVISION
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    logger.info(
        f"检测到上游 MoviePilot v3 数据环境（{reason}），"
        f"{'开始执行预检分析...' if dry_run else '开始执行首次自动迁移适配...'}"
    )

    if not dry_run and engine.dialect.name == "sqlite":
        db_file = Path(engine.url.database) if engine.url.database else (resolved_config_path / "user.db")
        backup_file = _backup_sqlite_database(db_file, timestamp)
        if backup_file:
            report.db_backup_path = str(backup_file)
            logger.info(f"已创建 v3 数据库安全备份：{backup_file}")

    archived_plugins = _archive_v3_plugins_backup(resolved_config_path, timestamp, dry_run=dry_run)
    if archived_plugins:
        report.plugins_backup_archive_path = str(archived_plugins)
        if not dry_run:
            logger.info(f"已归档 v3 插件备份目录：{archived_plugins}")

    with engine.connect() as conn:
        is_sqlite = conn.dialect.name == "sqlite"
        raw_dbapi_conn = conn.connection.dbapi_connection if is_sqlite else None
        if is_sqlite and not dry_run and raw_dbapi_conn is not None:
            raw_dbapi_conn.execute("PRAGMA foreign_keys=OFF")
        trans = conn.begin()
        try:
            sys_configs = _load_system_configs(conn)
            _restore_plugins_from_v3(conn, sys_configs, report, dry_run=dry_run)
            cat_id_to_name = _project_v3_classification_to_category_yaml(
                sys_configs,
                resolved_config_path,
                timestamp,
                report,
                dry_run=dry_run,
            )
            _sanitize_system_configs(conn, sys_configs, cat_id_to_name, report, dry_run=dry_run)
            _backfill_media_and_neo_columns(conn, cat_id_to_name, report, dry_run=dry_run)
            _reconcile_users_and_plugindata(conn, report, dry_run=dry_run)
            _align_schema_and_cleanup_v3_tables(conn, report, dry_run=dry_run)
            _stamp_neo_revision(conn, NEO_PRE_FILTER_RULE_REVISION, dry_run=dry_run)

            if dry_run:
                trans.rollback()
            else:
                trans.commit()
                report.executed = True
        except Exception:
            trans.rollback()
            raise
        finally:
            if is_sqlite and not dry_run and raw_dbapi_conn is not None:
                raw_dbapi_conn.execute("PRAGMA foreign_keys=ON")

    for line in report.summary_lines():
        logger.info(f"[v3->NEO] {line}")

    return report
