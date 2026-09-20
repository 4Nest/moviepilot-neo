import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.orm import Session

from app.db.models.schedulerhistory import SchedulerHistory


def test_scheduler_history_migration_and_retention(monkeypatch) -> None:
    """迁移可重复执行，且每个任务只保留最近 20 次结果。"""
    migration = importlib.import_module("database.versions.a7b8c9d0e1f2_2_2_21")
    engine = sa.create_engine("sqlite://")

    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        monkeypatch.setattr(migration, "op", Operations(context))
        migration.upgrade()
        migration.upgrade()

    with Session(engine) as session:
        for index in range(25):
            SchedulerHistory.record(
                db=session,
                job_id="demo",
                name="演示任务",
                provider="[系统]",
                success=index % 2 == 0,
                started_at=f"2026-09-20 00:00:{index:02d}",
                finished_at=f"2026-09-20 00:01:{index:02d}",
                error=None if index % 2 == 0 else f"error-{index}",
            )
        session.commit()
        rows = (
            session.query(SchedulerHistory)
            .filter(SchedulerHistory.job_id == "demo")
            .order_by(SchedulerHistory.finished_at.desc())
            .all()
        )

    assert len(rows) == 20
    assert rows[0].finished_at == "2026-09-20 00:01:24"
    assert rows[-1].finished_at == "2026-09-20 00:01:05"

    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        monkeypatch.setattr(migration, "op", Operations(context))
        migration.downgrade()
        assert "schedulerhistory" not in sa.inspect(connection).get_table_names()
