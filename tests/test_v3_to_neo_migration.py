"""上游 MoviePilot v3 首次迁移至 moviepilot-neo 的全链路回归测试。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import ruamel.yaml
import sqlalchemy as sa
from alembic.command import upgrade
from click.testing import CliRunner
from sqlalchemy.orm import Session

from app.cli import cli
from app.core.config import settings
from app.core.security import get_password_hash, verify_password
from app.db.init import build_alembic_config
from app.db.models.downloadhistory import DownloadHistory
from app.db.models.mediaserver import MediaServerItem
from app.db.models.subscribe import Subscribe
from app.db.models.systemconfig import SystemConfig
from app.db.models.transferhistory import TransferHistory
from app.db.models.user import User
from app.db.v3_migration import migrate_v3_database_if_needed
from app.schemas.system import NotificationConf, TransferDirectoryConf


def _build_realistic_v3_sqlite_db(db_path: Path, *, revision: str = "4f62a73e81c9", admin_password_hash: str) -> None:
    """在指定路径构造包含 v3 典型表结构、约束、插件表与分类策略的 SQLite 数据库。"""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            f"""
            PRAGMA foreign_keys=ON;

            CREATE TABLE alembic_version (
                version_num VARCHAR(32) NOT NULL PRIMARY KEY
            );
            INSERT INTO alembic_version (version_num) VALUES ('{revision}');

            CREATE TABLE user (
                id INTEGER NOT NULL PRIMARY KEY,
                name VARCHAR NOT NULL,
                email VARCHAR,
                hashed_password VARCHAR,
                is_active BOOLEAN,
                is_superuser BOOLEAN,
                avatar VARCHAR,
                is_otp BOOLEAN,
                otp_secret VARCHAR,
                permissions JSON,
                settings JSON,
                notification_identity VARCHAR(32)
            );
            CREATE UNIQUE INDEX ux_user_name ON user (name);

            CREATE TABLE userconfig (
                id INTEGER NOT NULL PRIMARY KEY,
                username VARCHAR NOT NULL REFERENCES user(name) ON DELETE CASCADE ON UPDATE CASCADE,
                key VARCHAR NOT NULL,
                value JSON,
                CONSTRAINT uq_userconfig_username_key UNIQUE (username, key)
            );

            CREATE TABLE widgetregistration (
                id INTEGER NOT NULL PRIMARY KEY,
                registration_id VARCHAR(36) NOT NULL,
                installation_id VARCHAR(36) NOT NULL,
                user_id INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
                owner VARCHAR(32) NOT NULL,
                secret_digest VARCHAR(64) NOT NULL,
                expires_at FLOAT NOT NULL
            );

            CREATE TABLE systemconfig (
                id INTEGER NOT NULL PRIMARY KEY,
                key VARCHAR NOT NULL,
                value JSON
            );
            CREATE UNIQUE INDEX ux_systemconfig_key ON systemconfig (key);

            CREATE TABLE plugininstance (
                id INTEGER NOT NULL PRIMARY KEY,
                instance_id VARCHAR(128) NOT NULL UNIQUE,
                source_plugin_id VARCHAR(128) NOT NULL,
                plugin_name VARCHAR(255),
                plugin_desc VARCHAR(255),
                plugin_icon VARCHAR(255),
                config_data JSON,
                log_level VARCHAR(16),
                log_expires_at VARCHAR(40),
                is_default_target BOOLEAN NOT NULL DEFAULT 0,
                is_enabled BOOLEAN NOT NULL DEFAULT 1,
                created_at VARCHAR(40) NOT NULL,
                updated_at VARCHAR(40) NOT NULL
            );

            CREATE TABLE plugindata (
                id INTEGER NOT NULL PRIMARY KEY,
                plugin_id VARCHAR NOT NULL,
                key VARCHAR NOT NULL,
                value JSON
            );

            CREATE TABLE subscribe (
                id INTEGER NOT NULL PRIMARY KEY,
                name VARCHAR NOT NULL,
                year VARCHAR,
                type VARCHAR,
                keyword VARCHAR,
                media_source VARCHAR,
                media_id VARCHAR,
                season INTEGER,
                poster VARCHAR,
                backdrop VARCHAR,
                vote FLOAT,
                description VARCHAR,
                filter VARCHAR,
                include VARCHAR,
                exclude VARCHAR,
                quality VARCHAR,
                resolution VARCHAR,
                effect VARCHAR,
                total_episode INTEGER,
                start_episode INTEGER,
                lack_episode INTEGER,
                note JSON,
                state VARCHAR NOT NULL,
                last_update VARCHAR,
                date VARCHAR,
                username VARCHAR,
                sites JSON,
                downloader VARCHAR,
                best_version INTEGER,
                best_version_full INTEGER,
                current_priority INTEGER,
                episode_priority JSON,
                save_path VARCHAR,
                search_imdbid INTEGER,
                manual_total_episode INTEGER,
                custom_words VARCHAR,
                media_category VARCHAR,
                media_category_id VARCHAR,
                filter_groups JSON,
                episode_group VARCHAR,
                music_type VARCHAR,
                total_tracks INTEGER,
                downloaded_tracks JSON,
                identity_key VARCHAR(64),
                activity_key VARCHAR(32),
                CONSTRAINT ck_subscribe_media_identity CHECK (
                    (media_source IS NULL AND media_id IS NULL) OR
                    (media_source IS NOT NULL AND trim(media_source) <> '' AND media_id IS NOT NULL AND trim(media_id) <> '' AND trim(media_id) <> '0')
                )
            );
            CREATE UNIQUE INDEX uq_subscribe_identity_key ON subscribe (identity_key);

            CREATE TABLE subscribehistory (
                id INTEGER NOT NULL PRIMARY KEY,
                name VARCHAR NOT NULL,
                year VARCHAR,
                type VARCHAR,
                media_source VARCHAR,
                media_id VARCHAR,
                season INTEGER,
                username VARCHAR,
                media_category VARCHAR,
                media_category_id VARCHAR,
                music_type VARCHAR
            );

            CREATE TABLE downloadhistory (
                id INTEGER NOT NULL PRIMARY KEY,
                path VARCHAR NOT NULL,
                type VARCHAR NOT NULL,
                title VARCHAR NOT NULL,
                year VARCHAR,
                media_source VARCHAR,
                media_id VARCHAR,
                seasons VARCHAR,
                episodes VARCHAR,
                downloader VARCHAR,
                download_hash VARCHAR,
                media_category VARCHAR,
                media_category_id VARCHAR,
                music_type VARCHAR,
                note JSON
            );

            CREATE TABLE transferhistory (
                id INTEGER NOT NULL PRIMARY KEY,
                src VARCHAR,
                src_storage VARCHAR NOT NULL DEFAULT 'local',
                dest VARCHAR,
                dest_storage VARCHAR,
                mode VARCHAR,
                type VARCHAR,
                category VARCHAR,
                title VARCHAR,
                year VARCHAR,
                media_source VARCHAR,
                media_id VARCHAR,
                seasons VARCHAR,
                episodes VARCHAR,
                status BOOLEAN,
                errmsg VARCHAR,
                date VARCHAR,
                files JSON,
                episode_group VARCHAR,
                auto_paused BOOLEAN NOT NULL DEFAULT 0,
                media_category_id VARCHAR,
                transfer_task_id VARCHAR(64),
                CONSTRAINT ck_transferhistory_media_identity CHECK (
                    (media_source IS NULL AND media_id IS NULL) OR
                    (media_source IS NOT NULL AND media_id IS NOT NULL AND trim(media_id) <> '0')
                )
            );
            CREATE UNIQUE INDEX ux_transferhistory_src_storage ON transferhistory (src, src_storage);

            CREATE TABLE downloadfailure (
                id INTEGER NOT NULL PRIMARY KEY,
                fingerprint VARCHAR NOT NULL,
                type VARCHAR,
                title VARCHAR,
                media_source VARCHAR,
                media_id VARCHAR
            );

            CREATE TABLE mediaserveritem (
                id INTEGER NOT NULL PRIMARY KEY,
                server VARCHAR,
                library VARCHAR,
                item_id VARCHAR,
                item_type VARCHAR,
                title VARCHAR,
                media_source VARCHAR,
                media_id VARCHAR
            );

            CREATE TABLE transferpending (
                id INTEGER NOT NULL PRIMARY KEY,
                storage VARCHAR NOT NULL,
                src_path VARCHAR NOT NULL,
                task_id VARCHAR(64) NOT NULL
            );

            CREATE TABLE transferexecutionstep (
                id INTEGER NOT NULL PRIMARY KEY,
                task_id VARCHAR(64) NOT NULL REFERENCES transferpending(task_id),
                operation_id VARCHAR(64) NOT NULL
            );

            CREATE TABLE outbox (
                id INTEGER NOT NULL PRIMARY KEY,
                topic VARCHAR NOT NULL
            );
            """
        )

        # 插入多用户与外键记录（自定义超级管理员名 nestadmin + 普通用户 alice）
        conn.execute(
            "INSERT INTO user (id, name, hashed_password, is_active, is_superuser, permissions, settings) "
            "VALUES (1, 'nestadmin', ?, 1, 1, '{\"manage\": true}', '{}'), "
            "(2, 'alice', ?, 1, 1, '{}', '{\"auth_source\": \"emby\", \"emby_user_id\": \"u1\"}')",
            (admin_password_hash, admin_password_hash),
        )
        conn.execute(
            "INSERT INTO userconfig (username, key, value) VALUES ('nestadmin', 'Layout', '\"dark\"')"
        )
        conn.execute(
            "INSERT INTO widgetregistration (registration_id, installation_id, user_id, owner, secret_digest, expires_at) "
            "VALUES ('r1', 'i1', 1, 'nestadmin', 'digest', 9999999999.0)"
        )

        # 插入插件配置（在 v3 3.0.35 中已从 systemconfig 移入 plugininstance）
        conn.execute(
            "INSERT INTO plugininstance (instance_id, source_plugin_id, config_data, is_default_target, is_enabled, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, 1, '2026-09-15T00:00:00Z', '2026-09-15T00:00:00Z'), "
            "(?, ?, ?, 0, 1, '2026-09-15T00:00:00Z', '2026-09-15T00:00:00Z')",
            (
                "SeedCleaner",
                "SeedCleaner",
                json.dumps({"enabled": True, "cron": "0 4 * * *"}),
                "SeedCleaner__clone1",
                "SeedCleaner",
                json.dumps({"enabled": True, "cron": "0 5 * * *"}),
            ),
        )
        conn.execute(
            "INSERT INTO plugindata (plugin_id, key, value) VALUES "
            "('SeedCleaner', 'history', '[1]'), ('SeedCleaner', 'history', '[1, 2]')"
        )

        # 插入 v3 MediaClassificationPolicy、Directories（含 media_category_id 与 reflink 和音乐目录）、Notifications、过滤规则
        policy_payload = {
            "active": {
                "revision": 2,
                "categories": [
                    {
                        "id": "legacy.movie.动画电影",
                        "media_type": "电影",
                        "name": "动画电影",
                        "path": ["动画电影"],
                        "enabled": True,
                    },
                    {
                        "id": "legacy.movie.外语电影",
                        "media_type": "电影",
                        "name": "外语电影",
                        "path": ["外语电影"],
                        "enabled": True,
                    },
                    {
                        "id": "custom.tv.anime",
                        "media_type": "电视剧",
                        "name": "日番",
                        "path": ["动漫", "日番"],
                        "enabled": True,
                    },
                ],
                "fallbacks": {"电影": "legacy.movie.外语电影"},
                "field_aliases": {"media.countries": {"jp": "JP"}},
                "rules": [
                    {
                        "id": "rule.movie.anim",
                        "enabled": True,
                        "media_types": ["电影"],
                        "target": {"category_id": "legacy.movie.动画电影"},
                        "when": {
                            "all": [
                                {"field": "media.genre_keys", "operator": "contains_any", "value": ["animation"]},
                            ]
                        },
                    },
                    {
                        "id": "rule.tv.jp_anime",
                        "enabled": True,
                        "media_types": ["电视剧"],
                        "target": {"category_id": "custom.tv.anime"},
                        "when": {
                            "all": [
                                {"field": "media.genre_keys", "operator": "contains_any", "value": ["animation"]},
                                {"field": "media.countries", "operator": "contains_any", "value": ["JP"]},
                            ]
                        },
                    },
                ],
            },
            "history": [],
        }

        directories_payload = [
            {
                "name": "主电影库",
                "priority": 1,
                "storage": "local",
                "download_path": "/downloads/movies",
                "library_path": "/media/movies",
                "library_storage": "local",
                "media_type": "电影",
                "media_category": None,
                "media_category_id": "legacy.movie.动画电影",
                "transfer_type": "reflink",
                "monitor_type": "downloader",
            },
            {
                "name": "音乐目录",
                "priority": 2,
                "storage": "local",
                "download_path": "/downloads/music",
                "media_type": "音乐",
                "transfer_type": "link",
            },
        ]

        notifications_payload = [
            {
                "name": "TG主通知",
                "type": "telegram",
                "enabled": True,
                "config": {
                    "TELEGRAM_TOKEN": "123456:ABCDEF",
                    "TELEGRAM_CHAT_ID": "998877",
                    "V3_EXTRA_PARAM": "drop_me",
                },
                "switchs": ["资源下载", "订阅", "V3未知开关"],
                "v3_extra_root": True,
            },
            {
                "name": "VoceChat通知",
                "type": "vocechat",
                "enabled": True,
                "config": {"host": "http://localhost"},
                "switchs": ["订阅"],
            },
        ]

        sys_rows = [
            ("UserInstalledPlugins", ["EmbyUserManager", "SeedCleaner__clone1"]),
            ("PluginFolders", {"常用": ["EmbyUserManager", "SeedCleaner__clone1"]}),
            ("MediaClassificationPolicy", policy_payload),
            ("Directories", directories_payload),
            ("Notifications", notifications_payload),
            (
                "NotificationTemplates",
                {
                    "organizeSuccess": "{% if track_number %}{% endif %}{% if type == \"音乐\" %}{% endif %}",
                    "subscribeAdded": "用户自定义模板保留",
                },
            ),
            ("AIAgentConfig", {"model": "gpt"}),
            ("CustomFilterRules", [{"id": "中字优先", "name": "中字优先", "include": "中字"}]),
            ("UserFilterRuleGroups", [{"name": "默认组", "rule_string": "中字优先 & 4K"}]),
        ]
        for k, v in sys_rows:
            conn.execute("INSERT INTO systemconfig (key, value) VALUES (?, ?)", (k, json.dumps(v, ensure_ascii=False)))

        # 插入各类媒体订阅与历史（验证 TMDB / 豆瓣 / Bangumi / 插件前缀 / 音乐类型处理）
        conn.execute(
            """
            INSERT INTO subscribe (id, name, year, type, media_source, media_id, season, state, username, media_category_id, identity_key)
            VALUES
                (1, '机智的医生生活', '2020', '电视剧', 'themoviedb', '96102', 1, 'R', 'nestadmin', 'custom.tv.anime', 'k1'),
                (2, '流浪地球2', '2023', '电影', 'douban', '35267208', NULL, 'R', 'nestadmin', 'legacy.movie.动画电影', 'k2'),
                (3, '葬送的芙莉莲', '2023', '电视剧', 'bangumi', '400602', 1, 'R', 'alice', NULL, 'k3'),
                (4, '凡人修仙传', '2020', '电视剧', 'bilibili', '28228367', 1, 'R', 'nestadmin', NULL, 'k4'),
                (5, 'Hotel California', '1976', '音乐', 'musicbrainz', 'mb-123', NULL, 'R', 'nestadmin', NULL, 'k5')
            """
        )
        conn.execute(
            """
            INSERT INTO downloadhistory (id, path, type, title, media_source, media_id, media_category_id)
            VALUES
                (1, '/downloads/tv/hp', '电视剧', '机智的医生生活', 'themoviedb', '96102', 'custom.tv.anime'),
                (2, '/downloads/music/hc', '音乐', 'Hotel California', 'musicbrainz', 'mb-123', NULL)
            """
        )
        conn.execute(
            """
            INSERT INTO transferhistory (id, src, src_storage, type, title, media_source, media_id, status)
            VALUES
                (1, '/downloads/tv/hp/S01E01.mkv', 'local', '电视剧', '机智的医生生活', 'themoviedb', '96102', 1),
                (2, '/downloads/music/hc/01.flac', 'local', '音乐', 'Hotel California', 'musicbrainz', 'mb-123', 1)
            """
        )
        conn.execute(
            """
            INSERT INTO mediaserveritem (id, server, item_id, item_type, title, media_source, media_id)
            VALUES (1, 'Emby', 'emby-1', 'Series', 'The Office', 'tvdb', '73244')
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_v3_to_neo_full_migration_and_orm_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """完整模拟从上游 v3.2+ 切换到 NEO：验证备份、回填、插件与分类恢复、Alembic 升级及后续 ORM 读写。"""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    plugins_backup = config_dir / "plugins_backup" / "v3onlyplugin"
    plugins_backup.mkdir(parents=True, exist_ok=True)
    (plugins_backup / "__init__.py").write_text("from app.runtime.config import settings\n", encoding="utf-8")

    db_path = config_dir / "user.db"
    pwd_hash = get_password_hash("MyV3SecretPass!")
    _build_realistic_v3_sqlite_db(db_path, revision="4f62a73e81c9", admin_password_hash=pwd_hash)

    monkeypatch.delenv("SUPERUSER", raising=False)
    monkeypatch.setattr(type(settings), "CONFIG_PATH", property(lambda _self: config_dir))
    monkeypatch.setattr(settings, "SUPERUSER", "admin")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    alembic_cfg = build_alembic_config()
    alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")

    # 1. Dry-run 预检不改动数据库与文件
    dry_report = migrate_v3_database_if_needed(
        engine, alembic_cfg, config_path=config_dir, dry_run=True
    )
    assert dry_report.detected is True
    assert dry_report.executed is False
    assert (config_dir / "plugins_backup").exists()
    assert not list(config_dir.glob("user.db.v3-backup-*"))

    # 2. 正式执行迁移 + Alembic upgrade head（涵盖 a7383c1d9e20 过滤规则 ID 迁移）
    report = migrate_v3_database_if_needed(
        engine, alembic_cfg, config_path=config_dir, dry_run=False
    )
    assert report.detected is True
    assert report.executed is True
    upgrade(alembic_cfg, "head")

    # 3. 校验数据库备份与 plugins_backup 归档
    backups = list(config_dir.glob("user.db.v3-backup-*"))
    assert len(backups) == 1 and backups[0].stat().st_size > 0
    assert not (config_dir / "plugins_backup").exists()
    assert len(list(config_dir.glob("plugins_backup.v3-backup-*"))) == 1

    # 4. 校验 category.yaml 无损还原
    cat_file = config_dir / "category.yaml"
    assert cat_file.exists()
    yaml_data = ruamel.yaml.YAML().load(cat_file.read_text(encoding="utf-8"))
    assert yaml_data["movie"]["动画电影"]["genre_ids"] == "16"
    assert "外语电影" in yaml_data["movie"] and yaml_data["movie"]["外语电影"] is None
    assert yaml_data["tv"]["动漫/日番"]["genre_ids"] == "16"
    assert yaml_data["tv"]["动漫/日番"]["origin_country"] == "jp"

    # 5. 校验管理员账号自动对齐与原 bcrypt 密码直通认证
    assert settings.SUPERUSER == "nestadmin"
    with Session(engine) as session:
        admin = session.query(User).filter(User.name == "nestadmin").one()
        alice = session.query(User).filter(User.name == "alice").one()
        assert admin.is_superuser is True and admin.is_active is True
        assert verify_password("MyV3SecretPass!", admin.hashed_password) is True
        assert len(admin.notification_identity) == 32
        assert alice.is_superuser is False

        # 6. 校验媒体 ID 无损回填、音乐订阅移除与二级分类名补全
        subs = {s.id: s for s in session.query(Subscribe).all()}
        assert set(subs.keys()) == {1, 2, 3, 4}  # id=5 音乐订阅已安全移除
        assert subs[1].tmdbid == 96102 and subs[1].media_category == "动漫/日番"
        assert subs[1].version_rules == [] and subs[1].version_mode == "any"
        assert subs[2].doubanid == "35267208" and subs[2].media_category == "动画电影"
        assert subs[3].bangumiid == 400602 and subs[3].username == "alice"
        assert subs[4].mediaid == "bilibili:28228367"

        dh_rows = {d.id: d for d in session.query(DownloadHistory).all()}
        assert dh_rows[1].tmdbid == 96102 and dh_rows[1].media_category == "动漫/日番"
        assert dh_rows[2].type == "未知"  # 音乐历史类型已安全归一化为 '未知'

        ms_item = session.query(MediaServerItem).filter(MediaServerItem.id == 1).one()
        assert ms_item.tvdbid == "73244"

        # 7. 校验插件配置恢复、Directories/Notifications 严格通过 NEO extra='forbid' 校验、规则 ID 已升级
        cfgs = {c.key: c.value for c in session.query(SystemConfig).all()}
        assert cfgs["plugin.SeedCleaner"] == {"enabled": True, "cron": "0 4 * * *"}
        assert "SeedCleaner" in cfgs["UserInstalledPlugins"]
        assert "SeedCleaner__clone1" not in cfgs["UserInstalledPlugins"]
        assert cfgs["PluginFolders"]["常用"] == ["EmbyUserManager"]
        assert "MediaClassificationPolicy" not in cfgs
        assert "AIAgentConfig" not in cfgs

        parsed_dirs = [TransferDirectoryConf(**d) for d in cfgs["Directories"]]
        assert len(parsed_dirs) == 1
        assert parsed_dirs[0].media_category == "动画电影"
        assert parsed_dirs[0].transfer_type == "copy"

        parsed_notifs = [NotificationConf(**n) for n in cfgs["Notifications"]]
        assert len(parsed_notifs) == 1
        assert parsed_notifs[0].type == "telegram"
        assert parsed_notifs[0].switchs == ["资源下载", "订阅"]

        # 验证 a7383c1d9e20 已将中文过滤规则 ID 自动规范化为合法 ASCII ID
        assert cfgs["CustomFilterRules"][0]["id"] == "Rule"
        assert cfgs["UserFilterRuleGroups"][0]["rule_string"] == "Rule & 4K"

        # 8. 校验重建后的表已彻底清除 v3 的 NOT NULL / CHECK / UNIQUE 冲突约束，NEO ORM 写入畅通无阻
        session.add(
            TransferHistory(
                src="/downloads/tv/hp/S01E01.mkv",  # 与既有行相同 src，验证已移除 v3 唯一索引拦截
                src_storage=None,  # 验证 src_storage 允许 NULL
                type="电视剧",
                title="机智的医生生活",
                tmdbid=96102,
                media_source="",  # 验证已移除 ck_transferhistory_media_identity 拦截
                media_id="0",
                status=True,
            )
        )
        session.commit()

    # 9. 校验 v3 孤儿表已清理且再次运行完全幂等（detected=False）
    inspector = sa.inspect(engine)
    remaining_tables = set(inspector.get_table_names())
    assert "plugininstance" not in remaining_tables
    assert "transferpending" not in remaining_tables
    assert "schedulerhistory" in remaining_tables

    second_report = migrate_v3_database_if_needed(
        engine, alembic_cfg, config_path=config_dir, dry_run=False
    )
    assert second_report.detected is False
    engine.dispose()


def test_colliding_revision_id_still_detected_and_cli_works(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """即使 v3 库停在与 NEO 同名的 revision (a7383c1d9e20)，仍能通过表结构特征准确识别并经 CLI 迁移。"""
    config_dir = tmp_path / "config_cli"
    config_dir.mkdir(parents=True, exist_ok=True)
    db_path = config_dir / "user.db"
    _build_realistic_v3_sqlite_db(
        db_path,
        revision="a7383c1d9e20",
        admin_password_hash=get_password_hash("Admin123!"),
    )

    test_engine = sa.create_engine(f"sqlite:///{db_path}")
    monkeypatch.delenv("SUPERUSER", raising=False)
    monkeypatch.setattr(type(settings), "CONFIG_PATH", property(lambda _self: config_dir))
    monkeypatch.setattr(settings, "SUPERUSER", "nestadmin")
    monkeypatch.setattr("app.db.Engine", test_engine)

    runner = CliRunner()
    dry_res = runner.invoke(cli, ["migrate-v3", "--dry-run", "--json"])
    assert dry_res.exit_code == 0, dry_res.output
    dry_payload = json.loads(dry_res.output)
    assert dry_payload["detected"] is True
    assert dry_payload["executed"] is False

    exec_res = runner.invoke(cli, ["migrate-v3", "--json"])
    assert exec_res.exit_code == 0, exec_res.output
    exec_payload = json.loads(exec_res.output)
    assert exec_payload["detected"] is True
    assert exec_payload["executed"] is True

    with test_engine.connect() as conn:
        rev = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
        assert rev == "d1e2f3a4b5c6"
        cols = {c["name"] for c in sa.inspect(conn).get_columns("subscribe")}
        assert {"tmdbid", "doubanid", "bangumiid", "version_rules"} <= cols
    test_engine.dispose()


def test_postgresql_schema_alignment_drops_cross_table_fks_before_indexes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PostgreSQL 对齐必须先移除跨表外键并使用 CASCADE，避免删除 ux_user_name 时报 DependentObjectsStillExist。"""
    from types import SimpleNamespace
    from sqlalchemy.dialects import postgresql
    from app.db.v3_migration import V3MigrationReport, _align_schema_and_cleanup_v3_tables

    executed_sql: list[str] = []
    fks_by_table = {
        "user": [],
        "userconfig": [{"name": "fk_userconfig_username_user"}],
    }
    indexes_by_table = {
        "user": [{"name": "user_pkey"}, {"name": "ux_user_name"}],
        "userconfig": [{"name": "userconfig_pkey"}],
    }
    columns_by_table = {
        "user": [
            {"name": "id", "nullable": False},
            {"name": "name", "nullable": True},
            {"name": "legacy_v3_col", "nullable": True},
        ],
        "userconfig": [
            {"name": "id", "nullable": False},
            {"name": "username", "nullable": False},
            {"name": "key", "nullable": False},
            {"name": "value", "nullable": True},
        ],
    }

    class _FakeInspector:
        def get_table_names(self):
            return ["user", "userconfig"]

        def get_foreign_keys(self, table_name: str):
            return list(fks_by_table.get(table_name, []))

        def get_check_constraints(self, table_name: str):
            return []

        def get_unique_constraints(self, table_name: str):
            return []

        def get_pk_constraint(self, table_name: str):
            return {"name": f"{table_name}_pkey"}

        def get_indexes(self, table_name: str):
            return list(indexes_by_table.get(table_name, []))

        def get_columns(self, table_name: str):
            return list(columns_by_table.get(table_name, []))

    class _FakePgConnection:
        def __init__(self):
            self.dialect = postgresql.dialect()

        def execute(self, statement, *args, **kwargs):
            sql = str(statement)
            executed_sql.append(sql)
            # 模拟外键删除后 inspector 不再返回该外键
            if "fk_userconfig_username_user" in sql:
                fks_by_table["userconfig"] = []
            # 模拟 PostgreSQL 依赖检查：若 userconfig 外键未先清理即删 ux_user_name 则失败
            if "ux_user_name" in sql and fks_by_table["userconfig"]:
                raise RuntimeError("DependentObjectsStillExist: fk_userconfig_username_user")
            if "SELECT 1 FROM \"user\" WHERE name IS NULL" in sql or "SELECT 1 FROM user WHERE name IS NULL" in sql:
                return SimpleNamespace(first=lambda: (1,))
            return SimpleNamespace(first=lambda: None)

    fake_meta = sa.MetaData()
    sa.Table(
        "user",
        fake_meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String, nullable=False),
    )
    sa.Table(
        "userconfig",
        fake_meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("username", sa.String, nullable=True),
        sa.Column("key", sa.String, nullable=True),
        sa.Column("value", sa.JSON, nullable=True),
    )

    monkeypatch.setattr("app.db.v3_migration.Base", SimpleNamespace(metadata=fake_meta))
    monkeypatch.setattr("app.db.v3_migration.sa.inspect", lambda _conn: _FakeInspector())

    report = V3MigrationReport(dry_run=False)
    _align_schema_and_cleanup_v3_tables(_FakePgConnection(), report, dry_run=False)

    fk_drop_idx = next(
        i for i, sql in enumerate(executed_sql)
        if "DROP CONSTRAINT IF EXISTS fk_userconfig_username_user CASCADE" in sql
    )
    ux_drop_idx = next(
        i for i, sql in enumerate(executed_sql)
        if "DROP INDEX IF EXISTS ux_user_name CASCADE" in sql
    )
    assert fk_drop_idx < ux_drop_idx
    assert not any("DROP INDEX IF EXISTS user_pkey" in sql for sql in executed_sql)
    assert any("DROP COLUMN IF EXISTS legacy_v3_col CASCADE" in sql for sql in executed_sql)
    assert not any("ALTER COLUMN name SET NOT NULL" in sql for sql in executed_sql)
    assert any("ALTER COLUMN username DROP NOT NULL" in sql for sql in executed_sql)
    assert set(report.rebuilt_tables) == {"user", "userconfig"}


def test_native_neo_or_v2_database_is_not_treated_as_v3(tmp_path: Path) -> None:
    """原生 NEO 数据库或标准 v2.2.15 数据库绝不误触发 v3 迁移逻辑。"""
    import app.db.models  # noqa: F401
    from app.db import Base

    db_path = tmp_path / "native_neo.db"
    engine = sa.create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
        conn.execute(sa.text("INSERT INTO alembic_version (version_num) VALUES ('a8c4e2f6b1d9')"))

    alembic_cfg = build_alembic_config()
    alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    report = migrate_v3_database_if_needed(engine, alembic_cfg, config_path=tmp_path)
    assert report.detected is False
    assert report.executed is False
    assert not list(tmp_path.glob("*.v3-backup-*"))
    engine.dispose()
