"""2.2.23 为通知设备绑定增加不可变用户身份。

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-11-05
"""

from uuid import uuid4

from alembic import op
import sqlalchemy as sa


revision = "c9d0e1f2a3b4"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为既有账号逐个分配独立身份；新库由 ORM 建表时已有字段，仍可幂等执行。"""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "user" not in inspector.get_table_names():
        return
    columns = {item["name"] for item in inspector.get_columns("user")}
    if "notification_identity" not in columns:
        op.add_column("user", sa.Column("notification_identity", sa.String(32), nullable=True))
    table = sa.table("user", sa.column("id", sa.Integer()), sa.column("notification_identity", sa.String(32)))
    for user_id in bind.execute(
        sa.select(table.c.id).where(
            sa.or_(table.c.notification_identity.is_(None), table.c.notification_identity == "")
        )
    ).scalars():
        bind.execute(table.update().where(table.c.id == user_id).values(notification_identity=uuid4().hex))
    columns_map = {item["name"]: item for item in sa.inspect(bind).get_columns("user")}
    if columns_map["notification_identity"]["nullable"]:
        with op.batch_alter_table("user") as batch:
            batch.alter_column("notification_identity", existing_type=sa.String(32), nullable=False)


def downgrade() -> None:
    """只移除新增身份字段，不修改账号及其余认证数据。"""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "user" not in inspector.get_table_names():
        return
    if any(item["name"] == "notification_identity" for item in inspector.get_columns("user")):
        op.drop_column("user", "notification_identity")
