"""
修正存量订阅与订阅历史中 version_rules 非空但 version_mode 非 'all' 的不一致数据。

早期版本 POST 新增订阅未回填 version_mode，导致带结构化多版本规则的订阅以模型默认值
'any' 落库并复制到订阅历史表。本次迁移统一将两张表中 version_rules 非空且 version_mode
不为 'all' 的记录收敛为 'all'，兼容 SQLite 与 PostgreSQL。
"""

import json
from typing import Any

import sqlalchemy as sa
from alembic import op

from app.log import logger

revision = "d1e2f3a4b5c6"
down_revision = "a7383c1d9e20"
branch_labels = None
depends_on = None


def _has_non_empty_version_rules(raw_value: Any) -> bool:
    """判断 version_rules 列值是否为非空规则列表。"""
    if raw_value is None:
        return False
    parsed = raw_value
    if isinstance(raw_value, (str, bytes, bytearray)):
        text = raw_value.decode("utf-8", errors="ignore") if isinstance(raw_value, (bytes, bytearray)) else raw_value
        text = text.strip()
        if not text:
            return False
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            return False
    return isinstance(parsed, list) and len(parsed) > 0


def _repair_table(connection: sa.Connection, table_name: str) -> int:
    """修复单张表中 version_rules 非空但 version_mode != 'all' 的存量记录。"""
    inspector = sa.inspect(connection)
    if table_name not in inspector.get_table_names():
        return 0
    columns = {col["name"] for col in inspector.get_columns(table_name)}
    if not {"id", "version_rules", "version_mode"}.issubset(columns):
        return 0

    table = sa.table(
        table_name,
        sa.column("id", sa.Integer),
        sa.column("version_rules"),
        sa.column("version_mode", sa.String),
    )
    rows = connection.execute(
        sa.select(table.c.id, table.c.version_rules, table.c.version_mode).where(
            table.c.version_rules.is_not(None)
        )
    ).all()

    dirty_ids = [
        row.id
        for row in rows
        if _has_non_empty_version_rules(row.version_rules) and row.version_mode != "all"
    ]
    if not dirty_ids:
        return 0

    connection.execute(
        table.update().where(table.c.id.in_(dirty_ids)).values(version_mode="all")
    )
    logger.info(f"迁移 {table_name}：已修正 {len(dirty_ids)} 条 version_mode 不一致的存量记录")
    return len(dirty_ids)


def upgrade() -> None:
    connection = op.get_bind()
    _repair_table(connection, "subscribe")
    _repair_table(connection, "subscribehistory")


def downgrade() -> None:
    pass
