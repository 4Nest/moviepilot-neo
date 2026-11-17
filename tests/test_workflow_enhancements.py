"""工作流模拟器、副本克隆、执行诊断详情与卡片摘要增强的单元测试。"""
import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.endpoints import workflow as workflow_endpoint
from app.chain.download import DownloadChain
from app.chain.search import SearchChain
from app.chain.storage import StorageChain
from app.chain.transfer import TransferChain
from app.core.event import eventmanager
from app.core.plugin import PluginManager
from app.db import Engine
from app.db.models.workflow import Workflow
from app.db.systemconfig_oper import SystemConfigOper
from app.db.user_oper import get_current_admin, get_current_admin_async
from app.db.workflow_oper import WorkflowOper
from app.helper.rss import RssHelper
from app.workflow import WorkFlowManager
from app.workflow.actions import ActionChain


@pytest.fixture
def client():
    """构建挂载工作流路由并注入管理员鉴权的测试客户端。"""
    # 先确保动作契约已初始化，避免把单例首次加载计入请求期调用
    WorkFlowManager()
    app = FastAPI()
    app.include_router(workflow_endpoint.router, prefix="/api/v1/workflow")
    admin_stub = type("AdminStub", (), {"id": 1, "name": "admin", "is_superuser": True, "is_active": True})()
    app.dependency_overrides[get_current_admin] = lambda: admin_stub
    app.dependency_overrides[get_current_admin_async] = lambda: admin_stub
    return TestClient(app)


@pytest.fixture
def created_workflow_ids():
    """记录测试期间创建的工作流 ID 并在结束后统一清理。"""
    ids = []
    yield ids
    for wid in ids:
        Workflow.delete(None, wid)


def _branching_rss_workflow_payload(scenario: str) -> dict:
    """构造含条件分支的 RSS 过滤下载/通知工作流模拟请求载荷。"""
    return {
        "scenario": scenario,
        "execution_config": {"max_workers": 2},
        "actions": [
            {
                "id": "rss_1",
                "type": "FetchRssAction",
                "name": "获取RSS资源",
                "data": {"url": "https://example.com/rss.xml"},
            },
            {
                "id": "filter_1",
                "type": "FilterTorrentsAction",
                "name": "过滤资源",
                "data": {"include": "1080p|2160p", "exclude": "CAM"},
            },
            {
                "id": "dl_1",
                "type": "AddDownloadAction",
                "name": "添加下载",
                "data": {"downloader": "qb"},
            },
            {
                "id": "msg_empty",
                "type": "SendMessageAction",
                "name": "无匹配通知",
                "data": {},
            },
        ],
        "flows": [
            {
                "id": "e_rss_filter",
                "source": "rss_1",
                "target": "filter_1",
                "condition": "outputs.rss_1.torrents.count > 0",
            },
            {
                "id": "e_filter_dl",
                "source": "filter_1",
                "target": "dl_1",
                "condition": "outputs.filter_1.torrents.count > 0",
            },
            {
                "id": "e_filter_msg",
                "source": "filter_1",
                "target": "msg_empty",
                "condition": "outputs.filter_1.torrents.count == 0",
            },
        ],
    }


def test_simulate_scenarios_normal_empty_source_and_empty_filter(client: TestClient):
    """验证 normal、empty_source、empty_filter 三种场景下的条件命中与跳过行为。"""
    # 1. normal：源产出 3 条 -> 过滤收敛为 2 条 -> 命中下载分支，跳过无匹配通知分支
    resp_normal = client.post(
        "/api/v1/workflow/simulate",
        json=_branching_rss_workflow_payload("normal"),
    )
    assert resp_normal.status_code == 200
    body_normal = resp_normal.json()
    assert body_normal["success"] is True
    sim_normal = body_normal["data"]
    assert sim_normal["success"] is True
    assert sim_normal["scenario"] == "normal"
    assert sim_normal["execution_order"] == ["rss_1", "filter_1", "dl_1"]
    assert sim_normal["nodes"]["rss_1"]["state"] == "success"
    assert sim_normal["nodes"]["rss_1"]["outputs_summary"] == {"torrents": 3}
    assert sim_normal["nodes"]["rss_1"]["step"] == 1
    assert sim_normal["nodes"]["filter_1"]["state"] == "success"
    assert sim_normal["nodes"]["filter_1"]["outputs_summary"] == {"torrents": 2}
    assert sim_normal["nodes"]["dl_1"]["state"] == "success"
    assert sim_normal["nodes"]["dl_1"]["outputs_summary"] == {"downloads": 2}
    assert sim_normal["nodes"]["msg_empty"]["state"] == "skipped"
    assert sim_normal["edges"]["e_rss_filter"]["state"] == "matched"
    assert sim_normal["edges"]["e_filter_dl"]["state"] == "matched"
    assert sim_normal["edges"]["e_filter_msg"]["state"] == "unmatched"

    # 2. empty_source：源产出 0 条 -> 过滤及下游全部条件跳过
    resp_empty_src = client.post(
        "/api/v1/workflow/simulate",
        json=_branching_rss_workflow_payload("empty_source"),
    )
    assert resp_empty_src.status_code == 200
    sim_empty_src = resp_empty_src.json()["data"]
    assert sim_empty_src["success"] is True
    assert sim_empty_src["scenario"] == "empty_source"
    assert sim_empty_src["execution_order"] == ["rss_1"]
    assert sim_empty_src["nodes"]["rss_1"]["state"] == "success"
    assert sim_empty_src["nodes"]["rss_1"]["outputs_summary"] == {"torrents": 0}
    assert sim_empty_src["nodes"]["filter_1"]["state"] == "skipped"
    assert sim_empty_src["nodes"]["dl_1"]["state"] == "skipped"
    assert sim_empty_src["nodes"]["msg_empty"]["state"] == "skipped"
    assert sim_empty_src["edges"]["e_rss_filter"]["state"] == "unmatched"
    assert sim_empty_src["edges"]["e_filter_dl"]["state"] == "skipped"
    assert sim_empty_src["edges"]["e_filter_msg"]["state"] == "skipped"

    # 3. empty_filter：源产出 3 条 -> 过滤产出 0 条 -> 跳过下载分支，命中无匹配通知分支
    resp_empty_flt = client.post(
        "/api/v1/workflow/simulate",
        json=_branching_rss_workflow_payload("empty_filter"),
    )
    assert resp_empty_flt.status_code == 200
    sim_empty_flt = resp_empty_flt.json()["data"]
    assert sim_empty_flt["success"] is True
    assert sim_empty_flt["scenario"] == "empty_filter"
    assert sim_empty_flt["execution_order"] == ["rss_1", "filter_1", "msg_empty"]
    assert sim_empty_flt["nodes"]["rss_1"]["outputs_summary"] == {"torrents": 3}
    assert sim_empty_flt["nodes"]["filter_1"]["outputs_summary"] == {"torrents": 0}
    assert sim_empty_flt["nodes"]["dl_1"]["state"] == "skipped"
    assert sim_empty_flt["nodes"]["msg_empty"]["state"] == "success"
    assert sim_empty_flt["edges"]["e_filter_dl"]["state"] == "unmatched"
    assert sim_empty_flt["edges"]["e_filter_msg"]["state"] == "matched"


def test_simulate_exclusive_branch_and_consumer_zero_items(client: TestClient):
    """验证 exclusive 互斥分支策略以及无条件连线时消费节点收到 0 条数据的行为。"""
    payload = {
        "scenario": "empty_source",
        "actions": [
            {
                "id": "scan_1",
                "type": "ScanFileAction",
                "name": "扫描目录",
                "branch_policy": "exclusive",
                "data": {"directory": "/media/incoming"},
            },
            {
                "id": "transfer_1",
                "type": "TransferFileAction",
                "name": "整理文件",
                "data": {"source": "fileitems"},
            },
            {
                "id": "msg_fallback",
                "type": "SendMessageAction",
                "name": "兜底通知",
                "data": {},
            },
        ],
        "flows": [
            {"id": "e1", "source": "scan_1", "target": "transfer_1", "condition": "fileitems.count >= 0"},
            {"id": "e2", "source": "scan_1", "target": "msg_fallback", "condition": "fileitems.count == 0"},
        ],
    }
    resp = client.post("/api/v1/workflow/simulate", json=payload)
    sim = resp.json()["data"]
    assert sim["success"] is True
    assert sim["edges"]["e1"]["state"] == "matched"
    assert sim["edges"]["e2"]["state"] == "unmatched"
    assert sim["nodes"]["transfer_1"]["state"] == "success"
    assert "上游无待处理数据" in sim["nodes"]["transfer_1"]["message"]
    assert sim["nodes"]["transfer_1"]["outputs_summary"] == {"fileitems": 0}
    assert sim["nodes"]["msg_fallback"]["state"] == "skipped"


def test_simulate_cycle_detection_and_static_graph_diagnostics(client: TestClient):
    """验证空工作流、Kahn 环路检测以及孤立节点静态诊断。"""
    # 空动作列表 -> warning
    empty_resp = client.post("/api/v1/workflow/simulate", json={"actions": [], "flows": []})
    empty_sim = empty_resp.json()["data"]
    assert any(d["level"] == "warning" for d in empty_sim["diagnostics"])

    # 纯环路无起始节点 -> error 并指出环路节点名称
    cycle_payload = {
        "actions": [
            {"id": "n1", "type": "FetchMediasAction", "name": "榜单节点A", "data": {}},
            {"id": "n2", "type": "FilterMediasAction", "name": "过滤节点B", "data": {}},
        ],
        "flows": [
            {"id": "f1", "source": "n1", "target": "n2"},
            {"id": "f2", "source": "n2", "target": "n1"},
        ],
    }
    cycle_sim = client.post("/api/v1/workflow/simulate", json=cycle_payload).json()["data"]
    assert cycle_sim["success"] is False
    error_diags = [d for d in cycle_sim["diagnostics"] if d["level"] == "error"]
    assert error_diags
    assert "榜单节点A" in error_diags[0]["message"]
    assert "过滤节点B" in error_diags[0]["message"]
    assert cycle_sim["nodes"]["n1"]["state"] == "unreached"
    assert cycle_sim["nodes"]["n2"]["state"] == "unreached"

    # 含孤立非 Note 节点 -> info/warning，而孤立 NoteAction 不误报
    isolated_payload = {
        "actions": [
            {"id": "rss_1", "type": "FetchRssAction", "name": "RSS源", "data": {"url": "https://a.com/rss"}},
            {"id": "note_1", "type": "NoteAction", "name": "画布备注", "data": {}},
            {"id": "msg_iso", "type": "SendMessageAction", "name": "孤立通知", "data": {}},
        ],
        "flows": [],
    }
    iso_sim = client.post("/api/v1/workflow/simulate", json=isolated_payload).json()["data"]
    iso_node_ids = {d["node_id"] for d in iso_sim["diagnostics"] if d["level"] in ("info", "warning")}
    assert "msg_iso" in iso_node_ids
    assert "note_1" not in iso_node_ids


def test_simulate_input_contract_gap_and_dynamic_upstream_exemption(client: TestClient):
    """验证输入契约断层告警，且 InvokePluginAction/SendEventAction 作为前驱时放行不误报。"""
    # 孤立放置 AddDownloadAction -> 产生缺少 torrents 的 warning
    orphan_dl = client.post(
        "/api/v1/workflow/simulate",
        json={
            "actions": [{"id": "dl_only", "type": "AddDownloadAction", "name": "添加下载", "data": {}}],
            "flows": [],
        },
    ).json()["data"]
    warnings = [d for d in orphan_dl["diagnostics"] if d["level"] == "warning" and d["node_id"] == "dl_only"]
    assert warnings
    assert "添加下载" in warnings[0]["message"]
    assert "torrents" in warnings[0]["message"]

    # 前驱包含 InvokePluginAction 时放行 AddDownloadAction，不误报断层
    plugin_upstream = client.post(
        "/api/v1/workflow/simulate",
        json={
            "actions": [
                {
                    "id": "plg_1",
                    "type": "InvokePluginAction",
                    "name": "调用插件",
                    "data": {"plugin_id": "DemoPlugin", "action_id": "gen_torrents"},
                },
                {"id": "dl_1", "type": "AddDownloadAction", "name": "添加下载", "data": {}},
            ],
            "flows": [{"id": "e1", "source": "plg_1", "target": "dl_1"}],
        },
    ).json()["data"]
    dl_warnings = [
        d for d in plugin_upstream["diagnostics"]
        if d["node_id"] == "dl_1" and "torrents" in d["message"]
    ]
    assert not dl_warnings

    # 前驱包含 SendEventAction 时同样放行契约检查
    event_upstream = client.post(
        "/api/v1/workflow/simulate",
        json={
            "actions": [
                {"id": "evt_1", "type": "SendEventAction", "name": "发送事件", "data": {}},
                {"id": "sub_1", "type": "AddSubscribeAction", "name": "添加订阅", "data": {}},
            ],
            "flows": [{"id": "e1", "source": "evt_1", "target": "sub_1"}],
        },
    ).json()["data"]
    sub_warnings = [
        d for d in event_upstream["diagnostics"]
        if d["node_id"] == "sub_1" and "medias" in d["message"]
    ]
    assert not sub_warnings

    # FetchTorrentsAction 在 search_type="media" 且无上游 medias 时产生契约断层 warning
    media_search_gap = client.post(
        "/api/v1/workflow/simulate",
        json={
            "actions": [
                {
                    "id": "ft_media",
                    "type": "FetchTorrentsAction",
                    "name": "按媒体搜种",
                    "data": {"search_type": "media"},
                }
            ],
            "flows": [],
        },
    ).json()["data"]
    ft_warnings = [
        d for d in media_search_gap["diagnostics"]
        if d["level"] == "warning" and d["node_id"] == "ft_media"
    ]
    assert ft_warnings and "medias" in ft_warnings[0]["message"]


def test_simulate_media_subscribe_pipeline_and_partial_cycle_isolation(client: TestClient):
    """验证媒体过滤订阅链路、fail_policy=ignore 以及局部环路精确节点定位。"""
    media_pipeline = {
        "scenario": "normal",
        "actions": [
            {"id": "m1", "type": "FetchMediasAction", "name": "获取榜单", "data": {}},
            {"id": "m2", "type": "FilterMediasAction", "name": "过滤高分", "data": {"vote": 8.0}},
            {"id": "m3", "type": "AddSubscribeAction", "name": "添加订阅", "data": {}},
            {
                "id": "opt_plugin",
                "type": "InvokePluginAction",
                "name": "可选插件",
                "fail_policy": "ignore",
                "data": {"plugin_id": "", "action_id": ""},
            },
        ],
        "flows": [
            {"id": "e1", "source": "m1", "target": "m2"},
            {"id": "e2", "source": "m2", "target": "m3"},
            {"id": "e3", "source": "m3", "target": "opt_plugin"},
        ],
    }
    sim = client.post("/api/v1/workflow/simulate", json=media_pipeline).json()["data"]
    assert sim["success"] is True
    assert sim["nodes"]["m1"]["outputs_summary"] == {"medias": 3}
    assert sim["nodes"]["m2"]["outputs_summary"] == {"medias": 2}
    assert sim["nodes"]["m3"]["outputs_summary"] == {"subscribes": 2}
    assert sim["nodes"]["opt_plugin"]["state"] == "failed"

    # Start -> LoopA <-> LoopB -> Sink：环路诊断只应点名 LoopA 与 LoopB，不应误伤 Start 与 Sink
    partial_cycle = client.post(
        "/api/v1/workflow/simulate",
        json={
            "actions": [
                {"id": "start", "type": "FetchMediasAction", "name": "起始节点", "data": {}},
                {"id": "loop_a", "type": "FilterMediasAction", "name": "环节点甲", "data": {}},
                {"id": "loop_b", "type": "FilterMediasAction", "name": "环节点乙", "data": {}},
                {"id": "sink", "type": "SendMessageAction", "name": "汇点通知", "data": {}},
            ],
            "flows": [
                {"id": "f1", "source": "start", "target": "loop_a"},
                {"id": "f2", "source": "loop_a", "target": "loop_b"},
                {"id": "f3", "source": "loop_b", "target": "loop_a"},
                {"id": "f4", "source": "loop_b", "target": "sink"},
            ],
        },
    ).json()["data"]
    assert partial_cycle["success"] is False
    err_msg = next(d["message"] for d in partial_cycle["diagnostics"] if d["level"] == "error")
    assert "环节点甲" in err_msg and "环节点乙" in err_msg
    assert "起始节点" not in err_msg and "汇点通知" not in err_msg


def test_simulate_param_validation_fail_policies_and_zero_side_effects(monkeypatch, client: TestClient):
    """验证必填参数校验、非法正则拦截、fail_policy(stop/continue) 以及全程零外部副作用。"""
    def _forbidden(*_args, **_kwargs):
        pytest.fail("模拟试跑严禁触发任何外部调用或副作用！")

    monkeypatch.setattr(RssHelper, "parse", _forbidden)
    monkeypatch.setattr(SearchChain, "search_by_title", _forbidden)
    monkeypatch.setattr(SearchChain, "search_by_id", _forbidden)
    monkeypatch.setattr(DownloadChain, "download_single", _forbidden)
    monkeypatch.setattr(StorageChain, "get_file_item", _forbidden)
    monkeypatch.setattr(TransferChain, "do_transfer", _forbidden)
    monkeypatch.setattr(PluginManager, "get_plugin_actions", _forbidden)
    monkeypatch.setattr(eventmanager, "send_event", _forbidden)
    monkeypatch.setattr(ActionChain, "post_message", _forbidden)
    monkeypatch.setattr(SystemConfigOper, "set", _forbidden)

    # 1. fail_policy="stop"（默认）：FetchRssAction 缺少 url -> 失败并终止，下游 unreached，出边 failed
    stop_payload = {
        "actions": [
            {"id": "rss_bad", "type": "FetchRssAction", "name": "RSS缺参数", "data": {"url": ""}},
            {"id": "dl_next", "type": "AddDownloadAction", "name": "后续下载", "data": {}},
        ],
        "flows": [{"id": "e1", "source": "rss_bad", "target": "dl_next"}],
    }
    stop_sim = client.post("/api/v1/workflow/simulate", json=stop_payload).json()["data"]
    assert stop_sim["success"] is False
    assert stop_sim["nodes"]["rss_bad"]["state"] == "failed"
    assert stop_sim["nodes"]["rss_bad"]["message"] == "[模拟] 缺少必填参数：RSS 地址 (url)"
    assert stop_sim["edges"]["e1"]["state"] == "failed"
    assert stop_sim["nodes"]["dl_next"]["state"] == "unreached"

    # 2. 非法正则 + fail_policy="continue" + 下游 join_policy="all_done"：失败后继续释放后继边
    continue_payload = {
        "actions": [
            {
                "id": "search_ok",
                "type": "FetchTorrentsAction",
                "name": "按名搜索",
                "data": {"search_type": "keyword", "name": "Dune"},
            },
            {
                "id": "filter_bad_re",
                "type": "FilterTorrentsAction",
                "name": "坏正则过滤",
                "fail_policy": "continue",
                "data": {"include": "[unclosed_regex"},
            },
            {
                "id": "msg_after",
                "type": "SendMessageAction",
                "name": "错误通知",
                "join_policy": "all_done",
                "data": {},
            },
        ],
        "flows": [
            {"id": "e1", "source": "search_ok", "target": "filter_bad_re"},
            {"id": "e2", "source": "filter_bad_re", "target": "msg_after"},
        ],
    }
    cont_sim = client.post("/api/v1/workflow/simulate", json=continue_payload).json()["data"]
    assert cont_sim["success"] is False
    assert cont_sim["execution_order"] == ["search_ok", "filter_bad_re", "msg_after"]
    assert cont_sim["nodes"]["filter_bad_re"]["state"] == "failed"
    assert "正则" in cont_sim["nodes"]["filter_bad_re"]["message"]
    assert cont_sim["edges"]["e2"]["state"] == "failed"
    assert cont_sim["nodes"]["msg_after"]["state"] == "success"

    # 3. 覆盖其余必填参数校验（FetchTorrents keyword 空 name、ScanFile 空 directory、InvokePlugin 缺参数）
    other_invalid = client.post(
        "/api/v1/workflow/simulate",
        json={
            "actions": [
                {
                    "id": "ft_bad",
                    "type": "FetchTorrentsAction",
                    "name": "搜索缺词",
                    "fail_policy": "continue",
                    "data": {"search_type": "keyword", "name": ""},
                },
                {
                    "id": "scan_bad",
                    "type": "ScanFileAction",
                    "name": "扫描缺目录",
                    "fail_policy": "continue",
                    "data": {"directory": ""},
                },
                {
                    "id": "plg_bad",
                    "type": "InvokePluginAction",
                    "name": "插件缺ID",
                    "fail_policy": "continue",
                    "data": {"plugin_id": "OnlyPlugin", "action_id": ""},
                },
            ],
            "flows": [],
        },
    ).json()["data"]
    assert other_invalid["nodes"]["ft_bad"]["message"] == "[模拟] 按名称搜索时缺少资源名称"
    assert other_invalid["nodes"]["scan_bad"]["message"] == "[模拟] 缺少扫描目录路径 (directory)"
    assert other_invalid["nodes"]["plg_bad"]["message"] == "[模拟] 未选择要调用的插件或动作"


def test_clone_workflow_auto_naming_and_state_reset(client: TestClient, created_workflow_ids: list):
    """验证复制工作流自动递增命名、保留配置且重置运行期状态为暂停态 P。"""
    with Session(Engine) as db:
        source = Workflow(
            name="自动追番工作流",
            description="测试复制描述",
            timer="0 */6 * * *",
            trigger_type="event",
            event_type="transfer.complete",
            event_conditions={"mediatype": "电视剧"},
            state="S",
            current_action="a1,a2",
            result="ok",
            run_count=12,
            actions=[{"id": "a1", "name": "扫描", "type": "ScanFileAction", "data": {"directory": "/tv"}}],
            flows=[{"id": "f1", "source": "a1", "target": "a2"}],
            execution_config={"max_workers": 2},
            context={"fileitems": [{"path": "/tv/1.mkv"}]},
            execution_state={"nodes": {"a1": {"state": "success"}}},
            last_time="2026-11-17 10:00:00",
        )
        db.add(source)
        db.commit()
        src_id = source.id
        created_workflow_ids.append(src_id)

    # 第一次克隆 -> "自动追番工作流 - 副本"
    r1 = client.post(f"/api/v1/workflow/{src_id}/clone").json()
    assert r1["success"] is True
    assert r1["message"] == "复制工作流成功"
    assert r1["data"]["name"] == "自动追番工作流 - 副本"
    created_workflow_ids.append(r1["data"]["id"])

    # 第二次克隆 -> "自动追番工作流 - 副本 2"
    r2 = client.post(f"/api/v1/workflow/{src_id}/clone").json()
    assert r2["success"] is True
    assert r2["data"]["name"] == "自动追番工作流 - 副本 2"
    created_workflow_ids.append(r2["data"]["id"])

    # 第三次克隆 -> "自动追番工作流 - 副本 3"
    r3 = client.post(f"/api/v1/workflow/{src_id}/clone").json()
    assert r3["success"] is True
    assert r3["data"]["name"] == "自动追番工作流 - 副本 3"
    created_workflow_ids.append(r3["data"]["id"])

    # 校验副本字段细节
    with Session(Engine) as db:
        cloned = WorkflowOper(db).get(r1["data"]["id"])
        assert cloned.description == "测试复制描述"
        assert cloned.timer == "0 */6 * * *"
        assert cloned.trigger_type == "event"
        assert cloned.event_type == "transfer.complete"
        assert cloned.event_conditions == {"mediatype": "电视剧"}
        assert cloned.actions == [{"id": "a1", "name": "扫描", "type": "ScanFileAction", "data": {"directory": "/tv"}}]
        assert cloned.flows == [{"id": "f1", "source": "a1", "target": "a2"}]
        assert cloned.execution_config == {"max_workers": 2}
        assert cloned.state == "P"
        assert cloned.current_action is None
        assert cloned.result is None
        assert cloned.run_count == 0
        assert cloned.context == {}
        assert cloned.execution_state == {}
        assert cloned.last_time is None

    # 不存在的工作流返回失败
    r_missing = client.post("/api/v1/workflow/99999999/clone").json()
    assert r_missing["success"] is False


def test_get_workflow_execution_lightweight_diagnostics(client: TestClient, created_workflow_ids: list):
    """验证执行诊断接口返回轻量条数摘要、耗时与历史，绝不下发原始大型输出载荷。"""
    huge_torrents = [{"title": f"BigTorrent-{i}", "payload": "X" * 50000} for i in range(5)]
    with Session(Engine) as db:
        wf = Workflow(
            name="诊断详情工作流",
            state="F",
            result="添加下载 失败",
            run_count=3,
            last_time="2026-11-17 12:00:05",
            actions=[
                {"id": "rss_1", "name": "拉取RSS", "type": "FetchRssAction", "data": {}},
                {"id": "dl_1", "name": "添加下载", "type": "AddDownloadAction", "data": {}},
                {"id": "msg_1", "name": "完成通知", "type": "SendMessageAction", "data": {}},
            ],
            flows=[
                {"id": "e1", "source": "rss_1", "target": "dl_1"},
                {"id": "e2", "source": "dl_1", "target": "msg_1"},
            ],
            context={
                "progress": 67,
                "torrents": huge_torrents,
                "execute_history": [
                    {"action": "拉取RSS", "result": True, "message": "获取到 5 个资源"},
                    {"action": "添加下载", "result": False, "message": "下载器连接失败"},
                ],
            },
            execution_state={
                "nodes": {
                    "rss_1": {
                        "state": "success",
                        "started_at": "2026-11-17 12:00:00",
                        "finished_at": "2026-11-17 12:00:02",
                        "attempt": 1,
                        "message": "获取到 5 个资源",
                    },
                    "dl_1": {
                        "state": "failed",
                        "started_at": "2026-11-17 12:00:02",
                        "finished_at": "2026-11-17 12:00:05",
                        "attempt": 2,
                        "message": "下载器连接失败",
                    },
                },
                "outputs": {
                    "rss_1": {"torrents": huge_torrents},
                },
                "errors": {
                    "dl_1": "下载器连接失败",
                },
                "runtime": {
                    "progress": 67,
                    "finished_actions": 2,
                },
            },
        )
        db.add(wf)
        db.commit()
        wid = wf.id
        created_workflow_ids.append(wid)

    resp = client.get(f"/api/v1/workflow/{wid}/execution")
    assert resp.status_code == 200
    raw_text = resp.text
    assert len(raw_text) < 4000  # 远小于 250KB 原始种子数据
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    assert data["id"] == wid
    assert data["name"] == "诊断详情工作流"
    assert data["state"] == "F"
    assert data["result"] == "添加下载 失败"
    assert data["run_count"] == 3
    assert data["progress"] == 67
    assert len(data["nodes"]) == 3

    n_rss, n_dl, n_msg = data["nodes"]
    assert n_rss["id"] == "rss_1"
    assert n_rss["state"] == "success"
    assert n_rss["duration_ms"] == 2000
    assert n_rss["attempt"] == 1
    assert n_rss["outputs_summary"] == {"torrents": 5}
    assert n_rss["error"] is None

    assert n_dl["id"] == "dl_1"
    assert n_dl["state"] == "failed"
    assert n_dl["duration_ms"] == 3000
    assert n_dl["attempt"] == 2
    assert n_dl["error"] == "下载器连接失败"
    assert n_dl["outputs_summary"] == {}

    assert n_msg["id"] == "msg_1"
    assert n_msg["state"] == "pending"

    assert data["execute_history"] == [
        {"action": "拉取RSS", "result": True, "message": "获取到 5 个资源"},
        {"action": "添加下载", "result": False, "message": "下载器连接失败"},
    ]


def test_async_list_summaries_includes_event_conditions_and_node_metadata(created_workflow_ids: list):
    """验证 async_list_summaries 包含 event_conditions 与轻量节点元数据，且不读取大型上下文。"""
    with Session(Engine) as db:
        wf = Workflow(
            name="卡片摘要增强测试",
            trigger_type="event",
            event_type="download.added",
            event_conditions={"site": "MTeam", "category": "Movie"},
            state="F",
            actions=[{"id": "step_1", "name": "动作1", "type": "FetchRssAction", "data": {"url": "https://x"}}],
            context={"torrents": ["HUGE" * 50000]},
            execution_state={
                "nodes": {
                    "step_1": {
                        "state": "failed",
                        "started_at": "2026-11-17 09:00:00",
                        "finished_at": "2026-11-17 09:00:03",
                        "attempt": 2,
                        "message": "RSS 超时",
                    }
                },
                "outputs": {"step_1": {"torrents": ["HUGE" * 50000]}},
                "runtime": {"finished_actions": 1},
            },
        )
        db.add(wf)
        db.commit()
        wid = wf.id
        created_workflow_ids.append(wid)

    async def _verify():
        rows = await WorkflowOper().async_list_summaries()
        row = next(item for item in rows if item["id"] == wid)
        assert row["event_conditions"] == {"site": "MTeam", "category": "Movie"}
        node_meta = row["execution_state"]["nodes"]["step_1"]
        assert node_meta == {
            "state": "failed",
            "started_at": "2026-11-17 09:00:00",
            "finished_at": "2026-11-17 09:00:03",
            "attempt": 2,
            "message": "RSS 超时",
        }
        assert len(json.dumps(row, ensure_ascii=False)) < 1500

    asyncio.run(_verify())


# ==================== 增量追新基线 / 发布时间窗口 / 单次下载上限测试 ====================


class _MemorySystemConfig:
    """纯内存 SystemConfig 替身，供增量基线缓存单元测试使用。"""

    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value):
        self.store[key] = value


def test_fetch_torrents_only_new_baseline_and_incremental_release(monkeypatch):
    """开启 only_new 时：首次运行静默建档不输出且不刮削，后续仅放行并识别新发布资源；改关键字自动重建基线。"""
    from datetime import datetime, timedelta
    from app.core.context import Context, MediaInfo, TorrentInfo
    from app.core.metainfo import MetaInfo
    from app.schemas import ActionContext, MediaType
    from app.workflow.actions.fetch_torrents import FetchTorrentsAction

    mem_cfg = _MemorySystemConfig()
    now = datetime.now()
    old_t1 = Context(
        meta_info=MetaInfo(title="[ADE] Anime A S01E01 x265"),
        torrent_info=TorrentInfo(
            site=1,
            title="[ADE] Anime A S01E01 x265",
            page_url="https://pt.example/details.php?id=101",
            pubdate=(now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    old_t2 = Context(
        meta_info=MetaInfo(title="[ADE] Anime B S01E02 x265"),
        torrent_info=TorrentInfo(
            site=1,
            title="[ADE] Anime B S01E02 x265",
            page_url="https://pt.example/details.php?id=102",
            pubdate=(now - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )

    search_results = [[old_t1, old_t2]]
    recognize_calls = []

    monkeypatch.setattr(
        "app.workflow.actions.fetch_torrents.SearchChain.search_by_title",
        lambda self, title, sites=None: list(search_results[0]),
    )

    def _fake_recognize(self, meta, obtain_images=False):
        recognize_calls.append(meta.org_string)
        media = MediaInfo()
        media.type = MediaType.TV
        media.title = "测试番剧"
        return media

    monkeypatch.setattr(
        "app.workflow.actions.fetch_torrents.MediaChain.recognize_by_meta",
        _fake_recognize,
    )

    # 第 1 次运行：建立存量基线，0 输出，0 次 TMDB 识别
    act1 = FetchTorrentsAction("fetch_1")
    act1.systemconfigoper = mem_cfg
    ctx1 = act1.execute(
        workflow_id=88,
        params={
            "search_type": "keyword",
            "name": "x265 -ADE",
            "type": "电视剧",
            "sites": [1],
            "match_media": True,
            "only_new": True,
        },
        context=ActionContext(),
    )
    assert ctx1.torrents == []
    assert recognize_calls == []
    assert "已建立追新基线" in act1.message

    # 第 2 次运行：包含旧种 old_t1、翻页露出的陈年老种 ancient_t、以及刚发布的 fresh_t
    ancient_t = Context(
        meta_info=MetaInfo(title="[ADE] Ancient S01 x265"),
        torrent_info=TorrentInfo(
            site=1,
            title="[ADE] Ancient S01 x265",
            page_url="https://pt.example/details.php?id=50",
            pubdate=(now - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    fresh_t = Context(
        meta_info=MetaInfo(title="[ADE] New Anime S01E03 x265"),
        torrent_info=TorrentInfo(
            site=1,
            title="[ADE] New Anime S01E03 x265",
            page_url="https://pt.example/details.php?id=203",
            pubdate=(now + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    search_results[0] = [fresh_t, old_t1, ancient_t]

    act2 = FetchTorrentsAction("fetch_1")
    act2.systemconfigoper = mem_cfg
    ctx2 = act2.execute(
        workflow_id=88,
        params={
            "search_type": "keyword",
            "name": "x265 -ADE",
            "type": "电视剧",
            "sites": [1],
            "match_media": True,
            "only_new": True,
        },
        context=ActionContext(),
    )
    assert len(ctx2.torrents) == 1
    assert ctx2.torrents[0].torrent_info.title == "[ADE] New Anime S01E03 x265"
    # 仅对唯一的新种执行了媒体识别
    assert len(recognize_calls) == 1

    # 第 3 次运行：修改搜索词后自动重建新范围基线，不会误把新官组存量当增量下载
    act3 = FetchTorrentsAction("fetch_1")
    act3.systemconfigoper = mem_cfg
    ctx3 = act3.execute(
        workflow_id=88,
        params={
            "search_type": "keyword",
            "name": "x265 -VCB-Studio",
            "type": "电视剧",
            "sites": [1],
            "match_media": True,
            "only_new": True,
        },
        context=ActionContext(),
    )
    assert ctx3.torrents == []
    assert "已建立追新基线" in act3.message


def test_filter_torrents_max_age_hours_and_add_download_cap(monkeypatch):
    """过滤资源发布时间窗口与添加下载单次上限应协同生效。"""
    from datetime import datetime, timedelta
    from app.core.context import Context, MediaInfo, TorrentInfo
    from app.core.metainfo import MetaInfo
    from app.schemas import ActionContext, MediaType
    from app.workflow.actions.add_download import AddDownloadAction
    from app.workflow.actions.filter_torrents import FilterTorrentsAction

    now = datetime.now()

    def _make_ctx(idx: int, hours_ago: float | None):
        media = MediaInfo()
        media.type = MediaType.TV
        media.title = f"番剧{idx}"
        pub = (
            (now - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
            if hours_ago is not None
            else None
        )
        return Context(
            meta_info=MetaInfo(title=f"[ADE] Show {idx} BluRay x265"),
            media_info=media,
            torrent_info=TorrentInfo(
                site=1,
                title=f"[ADE] Show {idx} BluRay x265",
                pubdate=pub,
            ),
        )

    c_recent_1 = _make_ctx(1, 2)
    c_recent_2 = _make_ctx(2, 6)
    c_recent_3 = _make_ctx(3, 12)
    c_expired = _make_ctx(4, 72)
    c_no_date = _make_ctx(5, None)

    monkeypatch.setattr(
        "app.workflow.actions.filter_torrents.ActionChain.filter_torrents",
        lambda self, rule_groups, torrent_list, mediainfo=None: torrent_list,
    )

    flt = FilterTorrentsAction("flt_1")
    ctx = ActionContext()
    ctx.torrents = [c_recent_1, c_recent_2, c_recent_3, c_expired, c_no_date]
    ctx = flt.execute(workflow_id=99, params={"max_age_hours": "24"}, context=ctx)
    assert [t.torrent_info.title for t in ctx.torrents] == [
        "[ADE] Show 1 BluRay x265",
        "[ADE] Show 2 BluRay x265",
        "[ADE] Show 3 BluRay x265",
    ]

    # 空字符串 max_age_hours 不应报错或过滤
    flt_empty = FilterTorrentsAction("flt_2")
    ctx_all_input = ActionContext()
    ctx_all_input.torrents = [c_expired, c_no_date]
    ctx_all = flt_empty.execute(
        workflow_id=99,
        params={"max_age_hours": ""},
        context=ctx_all_input,
    )
    assert len(ctx_all.torrents) == 2

    # 添加下载设置 max_downloads=2：3 个候选仅下载前 2 个
    mem_cfg = _MemorySystemConfig()
    monkeypatch.setattr(
        "app.workflow.actions.add_download.DownloadChain.download_single",
        lambda self, context, downloader=None, save_path=None, label=None: f"dl-{context.torrent_info.title}",
    )
    dl_act = AddDownloadAction("dl_1")
    dl_act.systemconfigoper = mem_cfg
    out_ctx = dl_act.execute(
        workflow_id=99,
        params={"max_downloads": "2"},
        context=ctx,
    )
    assert len(out_ctx.downloads) == 2
    assert "触发单次上限 2" in dl_act.message
    assert "跳过剩余 1 个" in dl_act.message
