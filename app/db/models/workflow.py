from datetime import datetime
from builtins import list as builtin_list
from typing import Optional

from sqlalchemy import Column, Integer, JSON, String, Index, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, load_only

from app.db import Base, db_query, get_id_column, db_update, async_db_query, async_db_update


class Workflow(Base):
    """
    工作流表
    """
    # ID
    id = get_id_column()
    # 名称
    name = Column(String, index=True, nullable=False)
    # 描述
    description = Column(String)
    # 定时器
    timer = Column(String)
    # 触发类型：timer-定时触发 event-事件触发 manual-手动触发
    trigger_type = Column(String, default='timer')
    # 事件类型（当trigger_type为event时使用）
    event_type = Column(String)
    # 事件条件（JSON格式，用于过滤事件）
    event_conditions = Column(JSON, default=dict)
    # 状态：W-等待 R-运行中 P-暂停 S-成功 F-失败
    state = Column(String, nullable=False, index=True, default='W')
    # 已执行动作（,分隔）
    current_action = Column(String)
    # 任务执行结果
    result = Column(String)
    # 已执行次数
    run_count = Column(Integer, default=0)
    # 任务列表
    actions = Column(JSON, default=builtin_list)
    # 任务流
    flows = Column(JSON, default=builtin_list)
    # 执行上下文
    context = Column(JSON, default=dict)
    # 执行配置
    execution_config = Column(JSON, default=dict)
    # 结构化执行状态
    execution_state = Column(JSON, default=dict)
    # 创建时间
    add_time = Column(String, default=lambda: datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    # 最后执行时间
    last_time = Column(String)

    __table_args__ = (
        Index('ix_workflow_trigger_type_state', 'trigger_type', 'state'),
    )

    @classmethod
    @db_query
    def list(cls, db):
        return db.query(cls).all()

    @classmethod
    @async_db_query
    async def async_list(cls, db: AsyncSession):
        result = await db.execute(select(cls))
        return result.scalars().all()

    @classmethod
    @async_db_query
    async def async_list_summaries(cls, db: AsyncSession) -> builtin_list[dict]:
        """获取卡片摘要，在数据库中排除执行上下文和节点输出，避免加载大对象。"""
        columns = (
            cls.id, cls.name, cls.description, cls.timer, cls.trigger_type,
            cls.event_type, cls.state, cls.current_action, cls.result,
            cls.run_count, cls.actions, cls.add_time, cls.last_time,
            cls.execution_state["nodes"].label("nodes"),
            cls.execution_state["runtime"]["finished_actions"].label("finished_actions"),
        )
        result = await db.execute(select(*columns))
        summaries = []
        for row in result.mappings():
            summary = dict(row)
            nodes = summary.pop("nodes")
            finished_actions = summary.pop("finished_actions")
            summary["actions"] = [
                {key: action.get(key) for key in ("id", "name", "type")}
                for action in (summary["actions"] or [])
            ]
            summary["execution_state"] = {
                "nodes": {
                    action_id: {"state": metadata.get("state")}
                    for action_id, metadata in (nodes or {}).items()
                },
                "runtime": {"finished_actions": finished_actions} if finished_actions is not None else {},
            }
            summaries.append(summary)
        return summaries

    @classmethod
    @db_query
    def get_trigger_config(cls, db: Session, wid: int) -> Optional["Workflow"]:
        """获取删除所需的触发配置，禁止隐式加载动作和运行数据。"""
        return db.query(cls).options(load_only(
            cls.id, cls.name, cls.trigger_type, cls.event_type, raiseload=True
        )).filter(cls.id == wid).first()

    @classmethod
    @async_db_query
    async def async_name_exists(cls, db: AsyncSession, name: str) -> bool:
        """检查工作流名称是否已存在，只查询标识。"""
        result = await db.execute(select(cls.id).where(cls.name == name).limit(1))
        return result.scalar() is not None

    @classmethod
    @db_query
    def get_enabled_workflows(cls, db):
        return db.query(cls).filter(cls.state != 'P').all()

    @classmethod
    @async_db_query
    async def async_get_enabled_workflows(cls, db: AsyncSession):
        result = await db.execute(select(cls).where(cls.state != 'P'))
        return result.scalars().all()

    @classmethod
    @db_query
    def get_timer_triggered_workflows(cls, db):
        """获取定时触发的工作流"""
        return db.query(cls).filter(
            and_(
                or_(
                    cls.trigger_type == 'timer',
                    cls.trigger_type.is_(None)
                ),
                cls.state != 'P'
            )
        ).all()

    @classmethod
    @async_db_query
    async def async_get_timer_triggered_workflows(cls, db: AsyncSession):
        """异步获取定时触发的工作流"""
        result = await db.execute(select(cls).where(
            and_(
                or_(
                    cls.trigger_type == 'timer',
                    cls.trigger_type.is_(None)
                ),
                cls.state != 'P'
            )
        ))
        return result.scalars().all()

    @classmethod
    @db_query
    def get_event_triggered_workflows(cls, db):
        """获取事件触发的工作流"""
        return db.query(cls).filter(
            and_(
                cls.trigger_type == 'event',
                cls.state != 'P'
            )
        ).all()

    @classmethod
    @async_db_query
    async def async_get_event_triggered_workflows(cls, db: AsyncSession):
        """异步获取事件触发的工作流"""
        result = await db.execute(select(cls).where(
            and_(
                cls.trigger_type == 'event',
                cls.state != 'P'
            )
        ))
        return result.scalars().all()

    @classmethod
    @db_query
    def get_by_name(cls, db, name: str):
        return db.query(cls).filter(cls.name == name).first()

    @classmethod
    @async_db_query
    async def async_get_by_name(cls, db: AsyncSession, name: str):
        result = await db.execute(select(cls).where(cls.name == name))
        return result.scalars().first()

    @classmethod
    @db_update
    def update_state(cls, db, wid: int, state: str):
        db.query(cls).filter(cls.id == wid).update({"state": state})
        return True

    @classmethod
    @async_db_update
    async def async_update_state(cls, db: AsyncSession, wid: int, state: str):
        from sqlalchemy import update
        await db.execute(update(cls).where(cls.id == wid).values(state=state))
        return True

    @classmethod
    @db_update
    def start(cls, db, wid: int):
        db.query(cls).filter(cls.id == wid).update({
            "state": 'R'
        })
        return True

    @classmethod
    @async_db_update
    async def async_start(cls, db: AsyncSession, wid: int):
        from sqlalchemy import update
        await db.execute(update(cls).where(cls.id == wid).values(state='R'))
        return True

    @classmethod
    @db_update
    def fail(cls, db, wid: int, result: str):
        db.query(cls).filter(and_(cls.id == wid, cls.state != "P")).update({
            "state": 'F',
            "result": result,
            "last_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        })
        return True

    @classmethod
    @async_db_update
    async def async_fail(cls, db: AsyncSession, wid: int, result: str):
        from sqlalchemy import update
        await db.execute(update(cls).where(
            and_(cls.id == wid, cls.state != "P")
        ).values(
            state='F',
            result=result,
            last_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        ))
        return True

    @classmethod
    @db_update
    def success(cls, db, wid: int, result: Optional[str] = None):
        db.query(cls).filter(and_(cls.id == wid, cls.state != "P")).update({
            "state": 'S',
            "result": result,
            "run_count": cls.run_count + 1,
            "last_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        })
        return True

    @classmethod
    @async_db_update
    async def async_success(cls, db: AsyncSession, wid: int, result: Optional[str] = None):
        from sqlalchemy import update
        await db.execute(update(cls).where(
            and_(cls.id == wid, cls.state != "P")
        ).values(
            state='S',
            result=result,
            run_count=cls.run_count + 1,
            last_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        ))
        return True

    @classmethod
    @db_update
    def reset(cls, db, wid: int, reset_count: Optional[bool] = False):
        db.query(cls).filter(cls.id == wid).update({
            "state": 'W',
            "result": None,
            "current_action": None,
            "context": {},
            "execution_state": {},
            "run_count": 0 if reset_count else cls.run_count,
        })
        return True

    @classmethod
    @async_db_update
    async def async_reset(cls, db: AsyncSession, wid: int, reset_count: Optional[bool] = False):
        from sqlalchemy import update
        await db.execute(update(cls).where(cls.id == wid).values(
            state='W',
            result=None,
            current_action=None,
            context={},
            execution_state={},
            run_count=0 if reset_count else cls.run_count,
        ))
        return True

    @classmethod
    @db_update
    def update_current_action(cls, db, wid: int, action_id: str, context: dict,
                              execution_state: Optional[dict] = None):
        workflow = db.query(cls).filter(cls.id == wid).first()
        current_actions = []
        if workflow and workflow.current_action:
            current_actions = [item for item in workflow.current_action.split(",") if item]
        if action_id and action_id not in current_actions:
            current_actions.append(action_id)
        update_values = {
            "current_action": ",".join(current_actions),
            "context": context
        }
        if execution_state is not None:
            update_values["execution_state"] = execution_state
        db.query(cls).filter(cls.id == wid).update(update_values)
        return True

    @classmethod
    @async_db_update
    async def async_update_current_action(cls, db: AsyncSession, wid: int, action_id: str, context: dict,
                                          execution_state: Optional[dict] = None):
        from sqlalchemy import update
        # 先获取当前current_action
        result = await db.execute(select(cls.current_action).where(cls.id == wid))
        current_action = result.scalar()
        current_actions = [item for item in (current_action or "").split(",") if item]
        if action_id and action_id not in current_actions:
            current_actions.append(action_id)
        new_current_action = ",".join(current_actions)

        update_values = {
            "current_action": new_current_action,
            "context": context
        }
        if execution_state is not None:
            update_values["execution_state"] = execution_state
        await db.execute(update(cls).where(cls.id == wid).values(**update_values))
        return True
