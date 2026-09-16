"""2.2.16
增加多版本订阅规则与独立进度字段。

Revision ID: b9c7d1e2f3a4
Revises: a8c4e2f6b1d9
"""

from alembic import op
import sqlalchemy as sa

revision = "b9c7d1e2f3a4"
down_revision = "a8c4e2f6b1d9"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names() and any(
        column["name"] == column_name for column in inspector.get_columns(table_name)
    )


def _add_columns(table_name: str) -> None:
    for column in (
        sa.Column("version_rules", sa.JSON(), nullable=True),
        sa.Column("version_progress", sa.JSON(), nullable=True),
        sa.Column("version_mode", sa.String(), nullable=True),
    ):
        if not _has_column(table_name, column.name):
            op.add_column(table_name, column)

    bind = op.get_bind()
    table = sa.table(
        table_name,
        sa.column("version_rules", sa.JSON()),
        sa.column("version_progress", sa.JSON()),
        sa.column("version_mode", sa.String()),
    )
    bind.execute(
        table.update()
        .where(table.c.version_rules.is_(None))
        .values(version_rules=[])
    )
    bind.execute(
        table.update()
        .where(table.c.version_progress.is_(None))
        .values(version_progress={})
    )
    bind.execute(
        table.update()
        .where(table.c.version_mode.is_(None))
        .values(version_mode="any")
    )


def upgrade() -> None:
    _add_columns("subscribe")
    _add_columns("subscribehistory")


def downgrade() -> None:
    for table_name in ("subscribe", "subscribehistory"):
        for column_name in ("version_mode", "version_progress", "version_rules"):
            if _has_column(table_name, column_name):
                op.drop_column(table_name, column_name)
