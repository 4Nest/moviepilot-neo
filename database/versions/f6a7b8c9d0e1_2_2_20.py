"""2.2.20 订阅新增跳过媒体库存在检测开关。

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-09-15
"""

from alembic import op
import sqlalchemy as sa


revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    """检查数据表是否已存在指定字段。"""
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(
        column["name"] == column_name
        for column in inspector.get_columns(table_name)
    )


def upgrade() -> None:
    """为订阅表补充跳过媒体库存在检测开关列。"""
    if not _has_column("subscribe", "skip_library_check"):
        op.add_column(
            "subscribe",
            sa.Column("skip_library_check", sa.Integer(), nullable=True, server_default="0"),
        )


def downgrade() -> None:
    """回滚订阅跳过媒体库存在检测开关列。"""
    if _has_column("subscribe", "skip_library_check"):
        op.drop_column("subscribe", "skip_library_check")
