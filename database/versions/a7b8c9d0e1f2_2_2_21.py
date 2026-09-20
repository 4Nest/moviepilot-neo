"""2.2.21 新增后台任务执行历史表。

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-09-20
"""

from alembic import op
import sqlalchemy as sa


revision = "a7b8c9d0e1f2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _has_index(table_name: str, index_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(
        index["name"] == index_name
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    )


def upgrade() -> None:
    """创建后台任务执行历史表及查询索引。"""
    if not _has_table("schedulerhistory"):
        op.create_table(
            "schedulerhistory",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("job_id", sa.String(), nullable=False),
            sa.Column("name", sa.String(), nullable=True),
            sa.Column("provider", sa.String(), nullable=True),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("success", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("started_at", sa.String(), nullable=True),
            sa.Column("finished_at", sa.String(), nullable=False),
            sa.Column("error", sa.Text(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _has_index("schedulerhistory", "ix_schedulerhistory_job_finished"):
        op.create_index(
            "ix_schedulerhistory_job_finished",
            "schedulerhistory",
            ["job_id", "finished_at"],
        )
    if not _has_index("schedulerhistory", "ix_schedulerhistory_finished"):
        op.create_index(
            "ix_schedulerhistory_finished",
            "schedulerhistory",
            ["finished_at"],
        )


def downgrade() -> None:
    """删除后台任务执行历史表。"""
    if _has_table("schedulerhistory"):
        op.drop_table("schedulerhistory")
