"""2.2.17 增加多版本下载历史关联字段。"""

from alembic import op
import sqlalchemy as sa

revision = "c1d2e3f4a5b6"
down_revision = "b9c7d1e2f3a4"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names() and any(
        column["name"] == column_name for column in inspector.get_columns(table_name)
    )


def upgrade() -> None:
    for column in (
        sa.Column("subscribe_id", sa.Integer(), nullable=True),
        sa.Column("version_rule_id", sa.String(), nullable=True),
        sa.Column("version_settings", sa.JSON(), nullable=True),
    ):
        if not _has_column("downloadhistory", column.name):
            op.add_column("downloadhistory", column)


def downgrade() -> None:
    for name in ("version_settings", "version_rule_id", "subscribe_id"):
        if _has_column("downloadhistory", name):
            op.drop_column("downloadhistory", name)
