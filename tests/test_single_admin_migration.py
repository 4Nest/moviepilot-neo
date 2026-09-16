import importlib

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def _create_legacy_tables(connection) -> dict[str, sa.Table]:
    """创建执行唯一 admin 迁移前的最小历史表结构。"""
    metadata = sa.MetaData()
    tables = {
        # 用户表含待删除的 permissions 列
        "user": sa.Table(
            "user", metadata,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("email", sa.String()),
            sa.Column("hashed_password", sa.String()),
            sa.Column("is_active", sa.Boolean()),
            sa.Column("is_superuser", sa.Boolean()),
            sa.Column("avatar", sa.String()),
            sa.Column("is_otp", sa.Boolean()),
            sa.Column("otp_secret", sa.String()),
            sa.Column("permissions", sa.JSON()),
            sa.Column("settings", sa.JSON()),
        ),
        "passkey": sa.Table(
            "passkey", metadata,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("credential_id", sa.String(), nullable=False),
            sa.Column("public_key", sa.String(), nullable=False),
        ),
        "userconfig": sa.Table(
            "userconfig", metadata,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("username", sa.String()),
            sa.Column("key", sa.String()),
            sa.Column("value", sa.JSON()),
        ),
        "subscribe": sa.Table(
            "subscribe", metadata,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("username", sa.String()),
            sa.Column("name", sa.String()),
        ),
        "subscribehistory": sa.Table(
            "subscribehistory", metadata,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("username", sa.String()),
            sa.Column("name", sa.String()),
        ),
        # 下载历史是审计记录，迁移必须保留
        "downloadhistory": sa.Table(
            "downloadhistory", metadata,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("userid", sa.String()),
            sa.Column("username", sa.String()),
        ),
    }
    metadata.create_all(connection)
    return tables


def _insert_user(tables, connection, **overrides) -> dict:
    """插入一行用户数据并返回完整行内容。"""
    row = {
        "id": 1,
        "name": "admin",
        "email": None,
        "hashed_password": "hashed",
        "is_active": True,
        "is_superuser": True,
        "avatar": "avatar.png",
        "is_otp": True,
        "otp_secret": "secret",
        "permissions": {"dashboard": True},
        "settings": {"theme": "dark"},
    }
    row.update(overrides)
    connection.execute(tables["user"].insert(), row)
    return row


def _run_upgrade(migration, monkeypatch, connection) -> None:
    """在指定连接上以 alembic Operations 执行迁移。"""
    context = MigrationContext.configure(connection)
    monkeypatch.setattr(migration, "op", Operations(context))
    migration.upgrade()


@pytest.fixture()
def migration():
    return importlib.import_module("database.versions.d4e5f6a7b8c9_2_2_18")


@pytest.fixture(autouse=True)
def _superuser_env(monkeypatch) -> None:
    """固定 canonical admin 名称，避免外部环境变量干扰。"""
    monkeypatch.setenv("SUPERUSER", "admin")


def test_only_admin_keeps_all_data(migration, monkeypatch) -> None:
    """仅存在 admin 时不删除任何数据，并强制 admin 可用及移除权限列。"""
    engine = sa.create_engine("sqlite://")

    with engine.begin() as connection:
        tables = _create_legacy_tables(connection)
        admin = _insert_user(tables, connection, is_active=False)
        connection.execute(tables["passkey"].insert(), {
            "id": 1, "user_id": admin["id"],
            "credential_id": "cred-admin", "public_key": "pk",
        })
        connection.execute(tables["userconfig"].insert(), {
            "id": 1, "username": "admin", "key": "k", "value": "v",
        })
        connection.execute(tables["subscribe"].insert(), {
            "id": 1, "username": "admin", "name": "电影订阅",
        })
        connection.execute(tables["subscribehistory"].insert(), {
            "id": 1, "username": "admin", "name": "电影订阅",
        })
        connection.execute(tables["downloadhistory"].insert(), {
            "id": 1, "userid": "1", "username": "admin",
        })

        _run_upgrade(migration, monkeypatch, connection)
        # 幂等：重复执行不报错
        _run_upgrade(migration, monkeypatch, connection)

        counts = {
            name: connection.execute(
                sa.select(sa.func.count()).select_from(table)
            ).scalar_one()
            for name, table in tables.items()
        }
        migrated_user = sa.Table("user", sa.MetaData(), autoload_with=connection)
        admin_row = connection.execute(
            sa.select(migrated_user).where(migrated_user.c.id == admin["id"])
        ).mappings().one()

    assert counts == {
        "user": 1, "passkey": 1, "userconfig": 1,
        "subscribe": 1, "subscribehistory": 1, "downloadhistory": 1,
    }
    assert "permissions" not in migrated_user.c
    # 强制启用，但密码/OTP/头像/settings/ID 不被重置
    assert admin_row["is_active"] == True  # noqa: E712
    assert admin_row["is_superuser"] == True  # noqa: E712
    assert admin_row["hashed_password"] == admin["hashed_password"]
    assert admin_row["otp_secret"] == admin["otp_secret"]
    assert admin_row["avatar"] == admin["avatar"]
    assert admin_row["settings"] == admin["settings"]


def test_removes_normal_user_data(migration, monkeypatch) -> None:
    """普通用户及其 PassKey/UserConfig/Subscribe/SubscribeHistory 被删除，审计与 admin 数据保留。"""
    engine = sa.create_engine("sqlite://")

    with engine.begin() as connection:
        tables = _create_legacy_tables(connection)
        admin = _insert_user(tables, connection)
        normal = _insert_user(
            tables, connection,
            id=2, name="alice", is_superuser=False,
            hashed_password="alice-hashed", otp_secret=None, is_otp=False,
        )
        for table_name, rows in {
            "passkey": [
                {"id": 1, "user_id": admin["id"], "credential_id": "cred-admin", "public_key": "pk1"},
                {"id": 2, "user_id": normal["id"], "credential_id": "cred-alice", "public_key": "pk2"},
            ],
            "userconfig": [
                {"id": 1, "username": "admin", "key": "k1", "value": "v1"},
                {"id": 2, "username": "alice", "key": "k2", "value": "v2"},
            ],
            "subscribe": [
                {"id": 1, "username": "admin", "name": "电影订阅"},
                {"id": 2, "username": "alice", "name": "剧集订阅"},
            ],
            "subscribehistory": [
                {"id": 1, "username": "admin", "name": "电影订阅"},
                {"id": 2, "username": "alice", "name": "剧集订阅"},
            ],
            "downloadhistory": [
                {"id": 1, "userid": str(admin["id"]), "username": "admin"},
                {"id": 2, "userid": str(normal["id"]), "username": "alice"},
            ],
        }.items():
            connection.execute(tables[table_name].insert(), rows)

        _run_upgrade(migration, monkeypatch, connection)
        # 幂等：二次执行不再删除也不报错
        _run_upgrade(migration, monkeypatch, connection)

        migrated_user = sa.Table("user", sa.MetaData(), autoload_with=connection)
        remaining = {
            name: connection.execute(
                sa.select(table)
            ).mappings().all()
            for name, table in {**tables, "user": migrated_user}.items()
        }

    assert [row["name"] for row in remaining["user"]] == ["admin"]
    # 普通用户业务数据按策略删除，绝不转挂 admin
    assert [row["credential_id"] for row in remaining["passkey"]] == ["cred-admin"]
    assert all(row["user_id"] == admin["id"] for row in remaining["passkey"])
    assert [row["username"] for row in remaining["userconfig"]] == ["admin"]
    assert [row["username"] for row in remaining["subscribe"]] == ["admin"]
    assert [row["username"] for row in remaining["subscribehistory"]] == ["admin"]
    # 下载历史作为审计记录完整保留
    assert sorted(row["username"] for row in remaining["downloadhistory"]) == ["admin", "alice"]
    # admin 身份字段不变
    admin_row = next(row for row in remaining["user"] if row["name"] == "admin")
    assert admin_row["id"] == admin["id"]
    assert admin_row["hashed_password"] == admin["hashed_password"]


def test_missing_admin_skips_cleanup(migration, monkeypatch, caplog) -> None:
    """数据库缺少 canonical admin 时跳过删除，由应用启动逻辑创建初始管理员。"""
    engine = sa.create_engine("sqlite://")

    with engine.begin() as connection:
        tables = _create_legacy_tables(connection)
        _insert_user(tables, connection, id=2, name="alice", is_superuser=False)
        connection.execute(tables["userconfig"].insert(), {
            "id": 1, "username": "alice", "key": "k", "value": "v",
        })

        with caplog.at_level("WARNING", logger="alembic.runtime.migration"):
            _run_upgrade(migration, monkeypatch, connection)

        migrated_user = sa.Table("user", sa.MetaData(), autoload_with=connection)
        users = connection.execute(sa.select(migrated_user)).mappings().all()
        userconfigs = connection.execute(sa.select(tables["userconfig"])).mappings().all()

    assert "跳过普通用户清理" in caplog.text
    assert [row["name"] for row in users] == ["alice"]
    assert len(userconfigs) == 1
    # 权限列移除是结构变更，与管理员是否存在无关
    assert "permissions" not in migrated_user.c


def test_duplicate_admin_names_raise_and_rollback(migration, monkeypatch) -> None:
    """同名管理员多于一行时迁移失败并整笔回滚，数据与结构保持不变。"""
    engine = sa.create_engine("sqlite://")

    with engine.begin() as connection:
        tables = _create_legacy_tables(connection)
        _insert_user(tables, connection, id=1, name="admin", is_superuser=True)
        _insert_user(tables, connection, id=2, name="admin", is_superuser=True)
        _insert_user(tables, connection, id=3, name="alice", is_superuser=False)
        connection.execute(tables["passkey"].insert(), {
            "id": 1, "user_id": 3, "credential_id": "cred-alice", "public_key": "pk",
        })

    with pytest.raises(RuntimeError, match="ID 清单"):
        with engine.begin() as connection:
            _run_upgrade(migration, monkeypatch, connection)

    # 回滚后所有用户、PassKey 及 permissions 列均保持不变
    with engine.connect() as connection:
        users = connection.execute(
            sa.select(tables["user"]).order_by(tables["user"].c.id)
        ).mappings().all()
        passkeys = connection.execute(sa.select(tables["passkey"])).mappings().all()
        migrated_user = sa.Table("user", sa.MetaData(), autoload_with=connection)

    assert [row["name"] for row in users] == ["admin", "admin", "alice"]
    assert len(passkeys) == 1
    assert "permissions" in migrated_user.c
