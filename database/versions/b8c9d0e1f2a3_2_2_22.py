"""2.2.22 新增订阅最近判定摘要。

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-09-20
"""

from alembic import op
import sqlalchemy as sa


revision = "b8c9d0e1f2a3"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def upgrade() -> None:
    """为当前订阅增加最近搜索判定摘要。"""
    if not _has_column("subscribe", "decision_summary"):
        op.add_column("subscribe", sa.Column("decision_summary", sa.JSON(), nullable=True))


def downgrade() -> None:
    """移除订阅最近搜索判定摘要。"""
    if _has_column("subscribe", "decision_summary"):
        op.drop_column("subscribe", "decision_summary")
