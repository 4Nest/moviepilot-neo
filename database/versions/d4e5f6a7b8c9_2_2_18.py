"""2.2.18 收敛为唯一管理员账号，清理普通用户数据及权限列。

仅保留环境变量 SUPERUSER（缺省 admin）指定的 canonical admin：
- 预检恰好命中一行 name=SUPERUSER 且 is_superuser=true 的管理员，
  零行时跳过删除（由应用启动的初始管理员逻辑创建），
  多行同名时抛出异常并输出 ID 清单，整笔回滚；
- 强制 canonical admin is_active=true、is_superuser=true，
  不重置其密码、OTP、头像、settings 与 ID；
- 事务内删除普通用户的 PassKey、UserConfig、Subscribe、SubscribeHistory，
  删除前记录每表将删除的行数；
- 保留 downloadhistory 审计记录及 message/siteuserdata 等外部身份列；
- 删除 user 表 permissions 列（ORM 已同步移除该字段）。

downgrade 为空实现：普通用户数据删除不可逆，无法恢复。
"""

import logging
import os

from alembic import op
import sqlalchemy as sa

revision = "d4e5f6a7b8c9"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")


def _has_table(table_name: str) -> bool:
    """检查数据表是否存在。"""
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    """检查数据表是否已存在指定字段。"""
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names() and any(
        column["name"] == column_name for column in inspector.get_columns(table_name)
    )


def _user_table() -> sa.Table:
    """构造迁移所需的 user 表最小列集合。"""
    return sa.table(
        "user",
        sa.column("id", sa.Integer()),
        sa.column("name", sa.String()),
        sa.column("is_active", sa.Boolean()),
        sa.column("is_superuser", sa.Boolean()),
    )


def _locate_canonical_admin(bind, superuser_name: str) -> int | None:
    """预检并返回 canonical admin 的用户 ID。

    零行返回 None（跳过删除，由应用启动逻辑创建初始管理员）；
    多行同名抛出异常并输出 ID 清单，由事务整笔回滚。
    """
    user = _user_table()
    admin_ids = bind.execute(
        sa.select(user.c.id).where(
            user.c.name == superuser_name,
            user.c.is_superuser.is_(True),
        )
    ).scalars().all()
    if len(admin_ids) > 1:
        raise RuntimeError(
            f"发现多个名为 {superuser_name} 的管理员账号，ID 清单: {admin_ids}，"
            f"无法确定唯一管理员，迁移中止"
        )
    if not admin_ids:
        logger.warning(
            f"未找到名为 {superuser_name} 的管理员账号，跳过普通用户清理，"
            f"初始管理员将由应用启动逻辑创建"
        )
        return None
    return admin_ids[0]


def _delete_normal_user_rows(bind, admin_id: int, admin_name: str) -> None:
    """事务内删除普通用户在各业务表中的数据，删除前记录行数。"""
    user = _user_table()
    normal_users = bind.execute(
        sa.select(user.c.id, user.c.name).where(user.c.id != admin_id)
    ).all()
    normal_ids = [row.id for row in normal_users]
    normal_names = [row.name for row in normal_users]

    # PassKey 按 user_id 外键归属，绝不转挂 admin
    if _has_table("passkey"):
        passkey = sa.table("passkey", sa.column("user_id", sa.Integer()))
        condition = passkey.c.user_id != admin_id
        count = bind.execute(
            sa.select(sa.func.count()).select_from(passkey).where(condition)
        ).scalar_one()
        logger.info(f"将删除普通用户 PassKey {count} 行")
        if count:
            bind.execute(passkey.delete().where(condition))

    # UserConfig/Subscribe/SubscribeHistory 按 username 匹配普通用户名
    for table_name in ("userconfig", "subscribe", "subscribehistory"):
        if not _has_table(table_name):
            continue
        table = sa.table(table_name, sa.column("username", sa.String()))
        condition = table.c.username.in_(normal_names) if normal_names else sa.false()
        count = bind.execute(
            sa.select(sa.func.count()).select_from(table).where(condition)
        ).scalar_one()
        logger.info(f"将删除普通用户 {table_name} {count} 行")
        if count:
            bind.execute(table.delete().where(condition))

    # 最后删除普通用户本体
    condition = user.c.id != admin_id
    count = bind.execute(
        sa.select(sa.func.count()).select_from(user).where(condition)
    ).scalar_one()
    logger.info(f"将删除普通用户 user {count} 行")
    if count:
        bind.execute(user.delete().where(condition))
    logger.info(f"唯一管理员 {admin_name} (id={admin_id}) 保留完成")


def upgrade() -> None:
    """收敛为唯一管理员账号并清理普通用户数据。"""
    bind = op.get_bind()
    if not _has_table("user"):
        return

    superuser_name = os.environ.get("SUPERUSER", "admin")
    admin_id = _locate_canonical_admin(bind, superuser_name)
    if admin_id is not None:
        user = _user_table()
        # 强制 canonical admin 可用且为管理员，不触碰密码/OTP/头像/settings/ID
        bind.execute(
            user.update()
            .where(user.c.id == admin_id)
            .values(is_active=True, is_superuser=True)
        )
        _delete_normal_user_rows(bind, admin_id, superuser_name)

    # 权限体系随唯一 admin 一并移除
    if _has_column("user", "permissions"):
        op.drop_column("user", "permissions")


def downgrade() -> None:
    """普通用户数据删除不可逆，仅恢复 permissions 列结构，不恢复任何数据。"""
    if _has_table("user") and not _has_column("user", "permissions"):
        op.add_column("user", sa.Column("permissions", sa.JSON(), nullable=True))
