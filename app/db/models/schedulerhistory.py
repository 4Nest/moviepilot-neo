from typing import List, Optional

from sqlalchemy import Column, Index, Integer, String, Text
from sqlalchemy.orm import Session

from app.db import Base, db_query, db_update, get_id_column


class SchedulerHistory(Base):
    """后台任务执行历史。"""

    id = get_id_column()
    job_id = Column(String, nullable=False)
    name = Column(String)
    provider = Column(String)
    status = Column(String, nullable=False)
    success = Column(Integer, nullable=False, default=0)
    started_at = Column(String)
    finished_at = Column(String, nullable=False)
    error = Column(Text)

    __table_args__ = (
        Index("ix_schedulerhistory_job_finished", "job_id", "finished_at"),
        Index("ix_schedulerhistory_finished", "finished_at"),
    )

    @classmethod
    @db_update
    def record(
        cls,
        db: Session,
        *,
        job_id: str,
        name: Optional[str],
        provider: Optional[str],
        success: bool,
        started_at: Optional[str],
        finished_at: str,
        error: Optional[str],
        keep_per_job: int = 20,
    ) -> "SchedulerHistory":
        """写入一次执行结果，并限制单任务历史数量。"""
        history = cls(
            job_id=job_id,
            name=name,
            provider=provider,
            status="success" if success else "failed",
            success=int(success),
            started_at=started_at,
            finished_at=finished_at,
            error=error,
        )
        db.add(history)
        db.flush()

        stale_ids = [
            row[0]
            for row in (
                db.query(cls.id)
                .filter(cls.job_id == job_id)
                .order_by(cls.finished_at.desc(), cls.id.desc())
                .offset(max(keep_per_job, 1))
                .all()
            )
        ]
        if stale_ids:
            db.query(cls).filter(cls.id.in_(stale_ids)).delete(synchronize_session=False)
        return history

    @classmethod
    @db_query
    def list_recent(
        cls,
        db: Session,
        limit: int = 100,
        failed_only: bool = False,
    ) -> List["SchedulerHistory"]:
        """按完成时间倒序返回最近执行历史。"""
        query = db.query(cls)
        if failed_only:
            query = query.filter(cls.success == 0)
        return (
            query.order_by(cls.finished_at.desc(), cls.id.desc())
            .limit(min(max(limit, 1), 500))
            .all()
        )
