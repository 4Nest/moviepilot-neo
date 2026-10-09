"""工作流摘要、创建和删除接口的隔离回归测试。"""
import asyncio
import json
from unittest.mock import Mock

import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app import schemas
from app.api.endpoints import workflow as endpoint
from app.db import AsyncSessionFactory, Engine
from app.db.models.workflow import Workflow
from app.db.workflow_oper import WorkflowOper


@pytest.fixture
def workflow_id():
    """准备带完整配置和大型输出的工作流，并在用例结束时清理。"""
    with Session(Engine) as db:
        workflow = Workflow(
            name="摘要测试", trigger_type="event", event_type="download.completed",
            state="S", current_action="search", run_count=8,
            actions=[{"id": "search", "name": "搜索", "type": "FetchTorrents",
                      "data": {"keyword": "CHD"}}],
            flows=[{"source": "search", "target": "download"}],
            event_conditions={"site": "CHD"}, execution_config={"max_workers": 3},
            context={"torrents": ["x" * 100000]},
            execution_state={"nodes": {"search": {"state": "success", "attempt": 1}},
                             "outputs": {"search": ["x" * 100000]},
                             "runtime": {"finished_actions": 1}},
        )
        db.add(workflow)
        db.commit()
        wid = workflow.id
    yield wid
    Workflow.delete(None, wid)


def test_summary_preserves_card_status_and_detail_preserves_configuration(workflow_id):
    """摘要保留进度且不传输出，默认列表和详情仍可用于完整编辑。"""
    async def check():
        """在异步会话中核对摘要和完整配置。"""
        async with AsyncSessionFactory() as db:
            summaries = await endpoint.list_workflows(db=db, summary=True)
            summary = schemas.Workflow.model_validate(next(row for row in summaries if row["id"] == workflow_id))
            assert summary.actions == [{"id": "search", "name": "搜索", "type": "FetchTorrents"}]
            assert summary.execution_state == {"nodes": {"search": {"state": "success"}},
                                               "runtime": {"finished_actions": 1}}
            assert summary.current_action == "search"
            assert summary.run_count == 8
            assert len(summary.model_dump_json()) < 1000
            full_list = await endpoint.list_workflows(db=db)
            full = schemas.Workflow.model_validate(next(row for row in full_list if row.id == workflow_id))
            detail = schemas.Workflow.model_validate(await endpoint.get_workflow(workflow_id, db=db))
            assert full == detail
            assert detail.actions[0]["data"] == {"keyword": "CHD"}
            assert detail.flows == [{"source": "search", "target": "download"}]
            assert detail.execution_config == {"max_workers": 3}
            assert detail.event_conditions == {"site": "CHD"}
            assert len(json.dumps(detail.execution_state["outputs"])) > 100000
    asyncio.run(check())


def test_summary_without_runtime_retains_legacy_progress_fallback(workflow_id):
    """旧工作流无结构化进度时，摘要不能把未提供的完成数变成零。"""
    with Session(Engine) as db:
        db.query(Workflow).filter(Workflow.id == workflow_id).update({"execution_state": {}})
        db.commit()
    async def check():
        """核对旧数据摘要仍允许前端推断完成进度。"""
        rows = await WorkflowOper().async_list_summaries()
        row = next(item for item in rows if item["id"] == workflow_id)
        assert row["execution_state"] == {"nodes": {}, "runtime": {}}
        assert row["current_action"] == "search"
    asyncio.run(check())


def test_create_returns_id_and_rejects_duplicate_name(workflow_id):
    """创建返回可供前端补入单张卡片的 ID，重复名称继续拒绝。"""
    async def check():
        """核对创建返回标识和重名检查。"""
        async with AsyncSessionFactory() as db:
            duplicate = await endpoint.create_workflow(schemas.Workflow(name="摘要测试"), db=db)
            assert duplicate.success is False
            created = await endpoint.create_workflow(schemas.Workflow(name="新工作流"), db=db)
            assert created.success is True
            wid = created.data["id"]
            try:
                detail = await WorkflowOper(db).async_get(wid)
                assert detail.name == "新工作流"
                assert detail.state == "P"
            finally:
                await Workflow.async_delete(db, wid)
    asyncio.run(check())


@pytest.mark.parametrize("trigger_type", ["event", "timer", "manual", None])
def test_delete_cleans_trigger_registration_without_loading_runtime(monkeypatch, workflow_id, trigger_type):
    """删除仅加载必要触发配置，并保留各类任务的清理行为。"""
    scheduler = Mock()
    manager = Mock()
    config = Mock()
    monkeypatch.setattr(endpoint, "Scheduler", lambda: scheduler)
    monkeypatch.setattr(endpoint, "WorkFlowManager", lambda: manager)
    monkeypatch.setattr(endpoint, "SystemConfigOper", lambda: config)
    with Session(Engine) as db:
        db.query(Workflow).filter(Workflow.id == workflow_id).update({"trigger_type": trigger_type})
        db.commit()
        workflow = WorkflowOper(db).get_trigger_config(workflow_id)
        assert {"context", "execution_state", "actions"} <= inspect(workflow).unloaded
        result = endpoint.delete_workflow(workflow_id, db=db)
        assert result.success is True
        assert WorkflowOper(db).get(workflow_id) is None
    assert scheduler.remove_workflow_job.call_count == int(trigger_type in ("timer", None))
    assert manager.remove_workflow_event.call_count == int(trigger_type == "event")
    config.delete.assert_called_once_with(f"WorkflowCache-{workflow_id}")
