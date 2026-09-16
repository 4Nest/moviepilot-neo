"""2.2.19 补回下载历史 note 附加信息列。

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-09-15
"""

from alembic import op
import sqlalchemy as sa


revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
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
    """为下载历史补充 note 附加信息列（JSON,记录下载来源等）。"""
    if not _has_column("downloadhistory", "note"):
        op.add_column("downloadhistory", sa.Column("note", sa.JSON(), nullable=True))


def downgrade() -> None:
    """回滚下载历史 note 列。"""
    if _has_column("downloadhistory", "note"):
        op.drop_column("downloadhistory", "note")
