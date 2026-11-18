"""工作流零副作用流程模拟器（支持真实数据只读演练与离线静态沙盒）。"""
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from app.chain.workflow import WorkflowExecutor
from app.core.config import settings
from app.core.context import Context, TorrentInfo
from app.core.metainfo import MetaInfo
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.torrent import TorrentHelper
from app.log import logger
from app.schemas import (
    Action,
    ActionContext,
    ActionFlow,
    MediaType,
    WorkflowSimulateDiagnostic,
    WorkflowSimulateRequest,
    WorkflowSimulateResponse,
)
from app.utils.string import StringUtils
from app.workflow import WorkFlowManager
from app.workflow.actions import ActionChain
from app.workflow.incremental import (
    BASELINE_CLOCK_SKEW_MINUTES,
    build_scope_signature,
    parse_torrent_pubdate,
    torrent_fingerprints,
)

VALID_SCENARIOS = {
    "live_dry_run": "真实数据演练（不执行下载/入库/通知）",
    "normal": "离线沙盒：常规有数据",
    "empty_source": "离线沙盒：源数据为空",
    "empty_filter": "离线沙盒：过滤结果为空",
}

MAX_PREVIEW_ITEMS = 10
MAX_LIVE_RECOGNIZE = 12
DYNAMIC_CONTEXT_ACTIONS = {"InvokePluginAction", "SendEventAction"}


class _ExecutorSemanticsHelper(WorkflowExecutor):
    """复用 WorkflowExecutor 条件求值、路径解析与策略规则的无副作用辅助器。"""

    def __init__(self, context: ActionContext, workflowmanager: WorkFlowManager):
        """仅绑定纯内存上下文与管理器引用，不创建线程池或修改全局状态。"""
        # pylint: disable=super-init-not-called
        self.context = context
        self.workflowmanager = workflowmanager


class WorkflowSimulator:
    """
    零副作用工作流流程模拟器。

    仅在内存中校验静态图结构、输入输出契约、动作参数与流转条件，
    绝不发起真实网络请求、插件调用、消息通知、存储扫描或数据库读写。
    """

    def __init__(self, workflowmanager: Optional[WorkFlowManager] = None):
        """
        初始化工作流模拟器。
        :param workflowmanager: 可选注入的工作流管理器实例
        """
        self.workflowmanager = workflowmanager or WorkFlowManager()

    def simulate(self, request: WorkflowSimulateRequest) -> WorkflowSimulateResponse:
        """
        执行工作流零副作用模拟试跑并返回完整诊断报告。
        :param request: 模拟请求载荷
        :return: 模拟执行结果与静态/动态诊断信息
        """
        diagnostics: List[WorkflowSimulateDiagnostic] = []
        raw_scenario = (request.scenario or "normal").strip()
        if raw_scenario not in VALID_SCENARIOS:
            diagnostics.append(
                WorkflowSimulateDiagnostic(
                    level="warning",
                    node_id=None,
                    message=f"未知模拟场景「{raw_scenario}」，已自动回退为 normal（常规有数据）",
                )
            )
            scenario = "normal"
        else:
            scenario = raw_scenario

        raw_actions = request.actions or []
        raw_flows = request.flows or []

        if not raw_actions:
            diagnostics.append(
                WorkflowSimulateDiagnostic(
                    level="warning",
                    node_id=None,
                    message="工作流尚未添加任何动作节点",
                )
            )
            return WorkflowSimulateResponse(
                success=True,
                scenario=scenario,
                summary="工作流为空，无可执行动作节点",
                execution_order=[],
                nodes={},
                edges={},
                diagnostics=diagnostics,
            )

        actions, action_order = self._parse_actions(raw_actions, diagnostics)
        if not actions:
            return WorkflowSimulateResponse(
                success=False,
                scenario=scenario,
                summary="所有动作节点均缺少有效 ID，无法模拟",
                execution_order=[],
                nodes={},
                edges={},
                diagnostics=diagnostics,
            )

        flows, edge_keys, outgoing_flows, incoming_flows = self._parse_flows(
            raw_flows, actions, diagnostics
        )

        # 1. 静态图结构诊断（环路、无起点、孤立节点、输入契约断层）
        has_cycle_error = self._diagnose_cycles_and_roots(
            actions, action_order, incoming_flows, outgoing_flows, diagnostics
        )
        self._diagnose_isolated_nodes(actions, action_order, incoming_flows, outgoing_flows, diagnostics)
        self._diagnose_input_contracts(actions, action_order, incoming_flows, diagnostics)

        # 2. 按拓扑与策略调度纯内存模拟执行
        sim_state = self._run_topological_simulation(
            actions=actions,
            action_order=action_order,
            flows=flows,
            edge_keys=edge_keys,
            outgoing_flows=outgoing_flows,
            incoming_flows=incoming_flows,
            scenario=scenario,
            workflow_id=request.workflow_id,
            diagnostics=diagnostics,
        )

        overall_success = bool(sim_state["success"] and not has_cycle_error)
        summary = self._build_summary(
            scenario=scenario,
            overall_success=overall_success,
            has_cycle_error=has_cycle_error,
            total_nodes=len(actions),
            nodes=sim_state["nodes"],
            execution_order=sim_state["execution_order"],
        )

        return WorkflowSimulateResponse(
            success=overall_success,
            scenario=scenario,
            summary=summary,
            execution_order=sim_state["execution_order"],
            nodes=sim_state["nodes"],
            edges=sim_state["edges"],
            diagnostics=diagnostics,
        )

    def _get_action_display_name(self, action: Action) -> str:
        """获取节点中文显示名称，绝不实例化动作类以避免副作用。"""
        if action.name:
            return action.name
        actions_map = getattr(self.workflowmanager, "_actions", None) or {}
        action_cls = actions_map.get(action.type or "")
        if action_cls is not None:
            cls_name = getattr(action_cls, "name", None)
            if isinstance(cls_name, str) and cls_name:
                return cls_name
        return action.type or action.id or "未命名节点"

    def _parse_actions(
        self,
        raw_actions: List[dict],
        diagnostics: List[WorkflowSimulateDiagnostic],
    ) -> Tuple[Dict[str, Action], List[str]]:
        """解析动作字典列表并记录重复或缺失 ID 诊断。"""
        actions: Dict[str, Action] = {}
        action_order: List[str] = []
        for idx, item in enumerate(raw_actions):
            if not isinstance(item, dict):
                continue
            action = Action(**item)
            if not action.id:
                diagnostics.append(
                    WorkflowSimulateDiagnostic(
                        level="error",
                        node_id=None,
                        message=f"第 {idx + 1} 个动作节点缺少节点 ID",
                    )
                )
                continue
            if action.id in actions:
                diagnostics.append(
                    WorkflowSimulateDiagnostic(
                        level="warning",
                        node_id=action.id,
                        message=f"存在重复的节点 ID「{action.id}」，后续定义已覆盖前序节点",
                    )
                )
            else:
                action_order.append(action.id)
            actions[action.id] = action
        return actions, action_order

    @staticmethod
    def _parse_flows(
        raw_flows: List[dict],
        actions: Dict[str, Action],
        diagnostics: List[WorkflowSimulateDiagnostic],
    ) -> Tuple[List[ActionFlow], Dict[int, str], Dict[str, List[ActionFlow]], Dict[str, List[ActionFlow]]]:
        """解析流程连线并构建有效出入边邻接表。"""
        flows: List[ActionFlow] = []
        edge_keys: Dict[int, str] = {}
        used_edge_ids: Set[str] = set()
        outgoing_flows: Dict[str, List[ActionFlow]] = defaultdict(list)
        incoming_flows: Dict[str, List[ActionFlow]] = defaultdict(list)

        for idx, item in enumerate(raw_flows):
            if not isinstance(item, dict):
                continue
            flow = ActionFlow(**item)
            if not flow.source or not flow.target:
                diagnostics.append(
                    WorkflowSimulateDiagnostic(
                        level="warning",
                        node_id=flow.source or flow.target,
                        message="存在缺少源节点或目标节点的无效连线，模拟时已忽略",
                    )
                )
                continue
            if flow.source not in actions or flow.target not in actions:
                missing_side = flow.source if flow.source not in actions else flow.target
                diagnostics.append(
                    WorkflowSimulateDiagnostic(
                        level="warning",
                        node_id=flow.source if flow.source in actions else None,
                        message=f"连线引用了不存在的节点「{missing_side}」，模拟时已忽略",
                    )
                )
                continue

            base_id = flow.id or f"{flow.source}->{flow.target}"
            edge_id = base_id if base_id not in used_edge_ids else f"{base_id}:{idx}"
            used_edge_ids.add(edge_id)
            flows.append(flow)
            edge_keys[id(flow)] = edge_id
            outgoing_flows[flow.source].append(flow)
            incoming_flows[flow.target].append(flow)

        return flows, edge_keys, outgoing_flows, incoming_flows

    def _diagnose_cycles_and_roots(
        self,
        actions: Dict[str, Action],
        action_order: List[str],
        incoming_flows: Dict[str, List[ActionFlow]],
        outgoing_flows: Dict[str, List[ActionFlow]],
        diagnostics: List[WorkflowSimulateDiagnostic],
    ) -> bool:
        """使用 Kahn 算法检测缺少无入边起始节点与有向环路，精确指出环路节点名称。"""
        in_degree = {node_id: len(incoming_flows.get(node_id, [])) for node_id in action_order}
        roots = [node_id for node_id in action_order if in_degree[node_id] == 0]

        queue = deque(roots)
        visited_count = 0
        while queue:
            current = queue.popleft()
            visited_count += 1
            for flow in outgoing_flows.get(current, []):
                in_degree[flow.target] -= 1
                if in_degree[flow.target] == 0:
                    queue.append(flow.target)

        if visited_count == len(actions):
            return False

        # 在未剥离的残余子图上继续反向剥离出度为 0 的纯汇点，精确锁定环内节点
        remaining = {node_id for node_id in action_order if in_degree[node_id] > 0}
        out_degree = {
            node_id: sum(1 for flow in outgoing_flows.get(node_id, []) if flow.target in remaining)
            for node_id in remaining
        }
        sink_queue = deque([node_id for node_id in action_order if node_id in remaining and out_degree[node_id] == 0])
        while sink_queue:
            sink = sink_queue.popleft()
            remaining.discard(sink)
            for prev_flow in incoming_flows.get(sink, []):
                src = prev_flow.source
                if src in remaining:
                    out_degree[src] -= 1
                    if out_degree[src] == 0:
                        sink_queue.append(src)

        cycle_ids = [node_id for node_id in action_order if node_id in remaining] or [
            node_id for node_id in action_order if in_degree[node_id] > 0
        ]
        cycle_names = "、".join(f"「{self._get_action_display_name(actions[nid])}」" for nid in cycle_ids)
        first_cycle_node = cycle_ids[0] if cycle_ids else None

        if not roots:
            diagnostics.append(
                WorkflowSimulateDiagnostic(
                    level="error",
                    node_id=first_cycle_node,
                    message=f"工作流缺少无入边的起始节点且存在有向环路，环路涉及节点：{cycle_names}",
                )
            )
        else:
            diagnostics.append(
                WorkflowSimulateDiagnostic(
                    level="error",
                    node_id=first_cycle_node,
                    message=f"工作流存在有向环路，无法完成拓扑调度，环路涉及节点：{cycle_names}",
                )
            )
        return True

    def _diagnose_isolated_nodes(
        self,
        actions: Dict[str, Action],
        action_order: List[str],
        incoming_flows: Dict[str, List[ActionFlow]],
        outgoing_flows: Dict[str, List[ActionFlow]],
        diagnostics: List[WorkflowSimulateDiagnostic],
    ) -> None:
        """当动作总数大于 1 时，检查既无入边也无出边的非 NoteAction 孤立节点。"""
        if len(actions) <= 1:
            return
        for node_id in action_order:
            action = actions[node_id]
            if action.type == "NoteAction":
                continue
            if not incoming_flows.get(node_id) and not outgoing_flows.get(node_id):
                node_name = self._get_action_display_name(action)
                diagnostics.append(
                    WorkflowSimulateDiagnostic(
                        level="info",
                        node_id=node_id,
                        message=f"节点「{node_name}」未连接任何上下游连线，将作为独立节点运行",
                    )
                )

    def _diagnose_input_contracts(
        self,
        actions: Dict[str, Action],
        action_order: List[str],
        incoming_flows: Dict[str, List[ActionFlow]],
        diagnostics: List[WorkflowSimulateDiagnostic],
    ) -> None:
        """检查每个节点的上游前驱链路是否产出了该节点契约所需的输入字段。"""
        for node_id in action_order:
            action = actions[node_id]
            required_inputs = self._get_effective_required_inputs(action)
            if not required_inputs:
                continue

            ancestors = self._collect_ancestors(node_id, incoming_flows)
            if any(actions[anc_id].type in DYNAMIC_CONTEXT_ACTIONS for anc_id in ancestors if anc_id in actions):
                continue

            produced_fields: Set[str] = set()
            for anc_id in ancestors:
                anc_action = actions.get(anc_id)
                if not anc_action:
                    continue
                produced_fields.update(self._get_declared_output_names(anc_action))

            node_name = self._get_action_display_name(action)
            for field in required_inputs:
                field_name = field["name"]
                if field_name not in produced_fields:
                    label = field.get("label") or field_name
                    field_desc = f"{label}({field_name})" if label != field_name else field_name
                    diagnostics.append(
                        WorkflowSimulateDiagnostic(
                            level="warning",
                            node_id=node_id,
                            message=f"节点「{node_name}」需要上游提供「{field_desc}」，但上游链路未产出该数据",
                        )
                    )

    def _get_effective_required_inputs(self, action: Action) -> List[dict]:
        """根据动作契约与动作模式参数计算实际必需的上游输入字段列表。"""
        contract = self.workflowmanager.get_action_contract(action.type or "") or {}
        inputs = list(contract.get("inputs") or [])
        if not inputs:
            return []
        data = action.data if isinstance(action.data, dict) else {}

        # FetchTorrentsAction 在默认 keyword 搜索模式下无需上游 medias
        if action.type == "FetchTorrentsAction":
            search_type = data.get("search_type") or "keyword"
            if search_type == "keyword":
                return []

        # TransferFileAction 根据 source 参数决定实际消费 downloads 还是 fileitems
        if action.type == "TransferFileAction":
            source = data.get("source") or "downloads"
            expected_field = "downloads" if source == "downloads" else "fileitems"
            return [item for item in inputs if item.get("name") == expected_field]

        # FetchDownloadsAction 无入边时可作为轮询起始节点，仅在有入边或契约检查时按需提示
        if action.type == "FetchDownloadsAction":
            return inputs

        return inputs

    def _get_declared_output_names(self, action: Action) -> Set[str]:
        """提取动作契约与显式配置声明的所有输出字段名称。"""
        names: Set[str] = set()
        contract = self.workflowmanager.get_action_contract(action.type or "") or {}
        for item in contract.get("outputs") or []:
            if isinstance(item, dict) and item.get("name"):
                names.add(item["name"])
            elif isinstance(item, str):
                names.add(item)

        custom_outputs = action.outputs or (action.data or {}).get("outputs")
        if isinstance(custom_outputs, dict):
            for key, cfg in custom_outputs.items():
                names.add(key)
                if isinstance(cfg, dict) and cfg.get("target"):
                    names.add(cfg["target"])
        elif isinstance(custom_outputs, list):
            for item in custom_outputs:
                if isinstance(item, dict):
                    if item.get("name"):
                        names.add(item["name"])
                    if item.get("target"):
                        names.add(item["target"])
                elif isinstance(item, str):
                    names.add(item)
        return names

    @staticmethod
    def _collect_ancestors(node_id: str, incoming_flows: Dict[str, List[ActionFlow]]) -> Set[str]:
        """反向遍历收集指定节点的所有上游前驱节点 ID（不含自身）。"""
        ancestors: Set[str] = set()
        stack = [flow.source for flow in incoming_flows.get(node_id, []) if flow.source != node_id]
        while stack:
            current = stack.pop()
            if current in ancestors:
                continue
            ancestors.add(current)
            for flow in incoming_flows.get(current, []):
                if flow.source != node_id and flow.source not in ancestors:
                    stack.append(flow.source)
        return ancestors

    def _run_topological_simulation(
        self,
        actions: Dict[str, Action],
        action_order: List[str],
        flows: List[ActionFlow],
        edge_keys: Dict[int, str],
        outgoing_flows: Dict[str, List[ActionFlow]],
        incoming_flows: Dict[str, List[ActionFlow]],
        scenario: str,
        workflow_id: Optional[int],
        diagnostics: List[WorkflowSimulateDiagnostic],
    ) -> dict:
        """按拓扑顺序与 WorkflowExecutor 完整语义执行纯内存模拟或只读演练。"""
        context = ActionContext()
        context.workflow_context = {}
        context.node_outputs = {}
        context.runtime_state = {}
        context.artifacts = {}
        semantics = _ExecutorSemanticsHelper(context=context, workflowmanager=self.workflowmanager)

        node_states: Dict[str, str] = {nid: "pending" for nid in action_order}
        node_messages: Dict[str, str] = {nid: "[模拟] 未到达该节点" for nid in action_order}
        node_outputs_summary: Dict[str, Dict[str, int]] = {nid: {} for nid in action_order}
        node_steps: Dict[str, Optional[int]] = {nid: None for nid in action_order}
        node_previews: Dict[str, List[dict]] = {nid: [] for nid in action_order}
        node_intercepted: Dict[str, bool] = {nid: False for nid in action_order}

        edge_states: Dict[str, str] = {}
        for flow in flows:
            edge_states[edge_keys[id(flow)]] = "skipped"

        flow_finished: Set[str] = set()
        flow_satisfied: Set[str] = set()
        flow_failed: Set[str] = set()

        queue: deque[str] = deque()
        queued_or_executed: Set[str] = set()
        execution_order: List[str] = []
        has_failure = False
        halted = False

        def enqueue_node(nid: str) -> None:
            if halted or nid not in actions:
                return
            if node_states.get(nid) != "pending" or nid in queued_or_executed:
                return
            queued_or_executed.add(nid)
            node_states[nid] = "queued"
            queue.append(nid)

        def skip_node(nid: str, reason: str) -> None:
            if nid not in actions or node_states.get(nid) not in ("pending", "queued"):
                return
            node_states[nid] = "skipped"
            node_messages[nid] = reason
            release_successors(nid, source_success=False)

        def evaluate_target_state(target_id: str) -> None:
            nonlocal has_failure, halted
            if halted or not target_id or target_id not in actions:
                return
            if node_states.get(target_id) != "pending":
                return
            in_flows = incoming_flows.get(target_id, [])
            if not in_flows:
                enqueue_node(target_id)
                return

            total_count = len(in_flows)
            finished_count = sum(1 for f in in_flows if edge_keys[id(f)] in flow_finished)
            satisfied_count = sum(1 for f in in_flows if edge_keys[id(f)] in flow_satisfied)
            failed_count = sum(1 for f in in_flows if edge_keys[id(f)] in flow_failed)
            join_policy = semantics.get_action_join_policy(actions[target_id], in_flows)

            if join_policy == "fail_fast":
                if failed_count > 0:
                    skip_node(target_id, "[模拟] 上游失败触发 fail_fast，已取消后续节点")
                elif finished_count == total_count and satisfied_count == total_count:
                    enqueue_node(target_id)
                elif finished_count == total_count:
                    skip_node(target_id, "[模拟] 上游条件未全部满足，已跳过")
                return

            if join_policy == "any_success":
                if satisfied_count > 0:
                    enqueue_node(target_id)
                elif finished_count == total_count:
                    skip_node(target_id, "[模拟] 所有上游条件均未满足，已跳过")
                return

            if join_policy == "all_done":
                if finished_count == total_count:
                    enqueue_node(target_id)
                return

            if join_policy != "all_success":
                has_failure = True
                halted = True
                diagnostics.append(
                    WorkflowSimulateDiagnostic(
                        level="error",
                        node_id=target_id,
                        message=f"节点「{self._get_action_display_name(actions[target_id])}」汇合策略无效：{join_policy}",
                    )
                )
                return

            if finished_count != total_count:
                return
            if satisfied_count == total_count:
                enqueue_node(target_id)
            else:
                skip_node(target_id, "[模拟] 上游条件未全部满足，已跳过")

        def release_successors(source_id: str, source_success: bool) -> None:
            nonlocal has_failure, halted
            out_flows = outgoing_flows.get(source_id, [])
            branch_policy = semantics.get_action_branch_policy(actions.get(source_id), out_flows)
            matched_exclusive_edge: Optional[str] = None
            source_state = node_states.get(source_id)

            for flow in out_flows:
                ekey = edge_keys[id(flow)]
                if ekey in flow_finished:
                    continue
                condition_matched = False
                if source_success:
                    cond_expr = semantics.get_flow_condition(flow)
                    try:
                        condition_matched = semantics.evaluate_condition(cond_expr)
                    except ValueError as err:
                        has_failure = True
                        halted = True
                        edge_states[ekey] = "failed"
                        flow_finished.add(ekey)
                        flow_failed.add(ekey)
                        diagnostics.append(
                            WorkflowSimulateDiagnostic(
                                level="error",
                                node_id=source_id,
                                message=f"连线条件表达式求值失败：{err}",
                            )
                        )
                        return

                    if branch_policy == "exclusive" and condition_matched and matched_exclusive_edge:
                        condition_matched = False
                    elif branch_policy == "exclusive" and condition_matched:
                        matched_exclusive_edge = ekey

                    edge_states[ekey] = "matched" if condition_matched else "unmatched"
                else:
                    if source_state == "failed":
                        edge_states[ekey] = "failed"
                        flow_failed.add(ekey)
                    else:
                        edge_states[ekey] = "skipped"

                flow_finished.add(ekey)
                if source_success and condition_matched:
                    flow_satisfied.add(ekey)
                evaluate_target_state(flow.target)

        # 初始化无入边的起始节点入队
        for nid in action_order:
            if not incoming_flows.get(nid):
                enqueue_node(nid)

        step_counter = 0
        while queue and not halted:
            current_id = queue.popleft()
            if node_states.get(current_id) != "queued":
                continue

            action = actions[current_id]
            step_counter += 1
            node_steps[current_id] = step_counter
            execution_order.append(current_id)

            ok, msg, outputs, preview_items, intercepted = self._simulate_single_action_ext(
                action=action,
                has_incoming=bool(incoming_flows.get(current_id)),
                semantics=semantics,
                scenario=scenario,
                workflow_id=workflow_id,
                diagnostics=diagnostics,
            )
            node_previews[current_id] = preview_items
            node_intercepted[current_id] = intercepted

            if not ok:
                node_states[current_id] = "failed"
                node_messages[current_id] = msg
                node_outputs_summary[current_id] = {}
                fail_policy = semantics.get_action_fail_policy(action)
                if fail_policy != "ignore":
                    has_failure = True

                if fail_policy == "stop" or fail_policy not in ("continue", "ignore"):
                    if fail_policy not in ("stop", "continue", "ignore"):
                        diagnostics.append(
                            WorkflowSimulateDiagnostic(
                                level="error",
                                node_id=current_id,
                                message=f"节点「{self._get_action_display_name(action)}」配置了无效失败策略：{fail_policy}",
                            )
                        )
                    # 源节点失败且终止时，将其直接出边标为 failed，后续节点保持 unreached
                    for out_flow in outgoing_flows.get(current_id, []):
                        edge_states[edge_keys[id(out_flow)]] = "failed"
                    halted = True
                    queue.clear()
                    break

                # continue / ignore：记录失败出边并继续释放下游（供 all_done 等汇合节点消费）
                release_successors(current_id, source_success=False)
                continue

            # 节点模拟成功：写入上下文与 node_outputs 供下游条件与动作精确读取
            node_states[current_id] = "success"
            node_messages[current_id] = msg
            node_outputs_summary[current_id] = {
                key: len(val) if isinstance(val, (list, tuple, set, dict)) else (0 if val is None else 1)
                for key, val in outputs.items()
            }
            context.node_outputs[current_id] = outputs
            context.runtime_state["last_outputs"] = outputs
            self._merge_simulated_outputs(action, outputs, semantics)

            release_successors(current_id, source_success=True)

        # 汇总最终节点与连线结构（仍处于 pending/queued 的节点统一归为 unreached）
        nodes_payload: Dict[str, dict] = {}
        for nid in action_order:
            action = actions[nid]
            raw_state = node_states[nid]
            final_state = "unreached" if raw_state in ("pending", "queued") else raw_state
            nodes_payload[nid] = {
                "id": nid,
                "name": self._get_action_display_name(action),
                "type": action.type or "",
                "state": final_state,
                "message": node_messages[nid],
                "outputs_summary": node_outputs_summary[nid],
                "step": node_steps[nid],
                "preview_items": node_previews[nid],
                "dry_run_intercepted": node_intercepted[nid],
            }

        edges_payload: Dict[str, dict] = {}
        for flow in flows:
            ekey = edge_keys[id(flow)]
            edges_payload[ekey] = {
                "id": ekey,
                "source": flow.source,
                "target": flow.target,
                "condition": semantics.get_flow_condition(flow),
                "state": edge_states[ekey],
            }

        return {
            "success": not has_failure,
            "execution_order": execution_order,
            "nodes": nodes_payload,
            "edges": edges_payload,
        }

    @staticmethod
    def _merge_simulated_outputs(
        action: Action,
        outputs: Dict[str, Any],
        semantics: _ExecutorSemanticsHelper,
    ) -> None:
        """将模拟节点输出合并至内存上下文与 artifacts 分区。"""
        if action.type in ("TransferFileAction", "ScrapeFileAction") and "fileitems" in outputs:
            # 整理与刮削动作将当前待处理文件集合更新为本节点处理后的样本列表
            semantics.merge_output_value("fileitems", outputs["fileitems"], "replace")
            return
        semantics.merge_action_outputs(action, outputs)

    @staticmethod
    def _read_upstream_list(field_name: str, action: Action, semantics: _ExecutorSemanticsHelper) -> list:
        """读取上游传入的列表字段，支持显式 inputs 映射与默认上下文字段。"""
        explicit_inputs = action.inputs or (action.data or {}).get("inputs")
        if explicit_inputs:
            paths = (
                [p.strip() for p in explicit_inputs.splitlines() if p.strip()]
                if isinstance(explicit_inputs, str)
                else list(explicit_inputs)
            )
            for path in paths:
                if isinstance(path, str) and (path == field_name or path.endswith(f".{field_name}")):
                    resolved = semantics.resolve_action_input(path)
                    if isinstance(resolved, list):
                        return resolved
        val = semantics.resolve_action_input(field_name)
        return list(val) if isinstance(val, list) else []

    def _simulate_single_action_ext(
        self,
        action: Action,
        has_incoming: bool,
        semantics: _ExecutorSemanticsHelper,
        scenario: str,
        workflow_id: Optional[int],
        diagnostics: List[WorkflowSimulateDiagnostic],
    ) -> Tuple[bool, str, Dict[str, Any], List[dict], bool]:
        """执行单节点模拟（支持离线沙盒与真实数据只读演练），返回状态、说明、输出、预览条目与拦截标志。"""
        write_actions = {
            "AddDownloadAction",
            "AddSubscribeAction",
            "TransferFileAction",
            "ScrapeFileAction",
            "SendMessageAction",
            "SendEventAction",
            "InvokePluginAction",
        }
        action_type = action.type or ""
        if scenario == "live_dry_run":
            return self._dry_run_single_action(
                action=action,
                has_incoming=has_incoming,
                semantics=semantics,
                workflow_id=workflow_id,
                diagnostics=diagnostics,
            )

        ok, msg, outputs = self._simulate_single_action(
            action=action,
            has_incoming=has_incoming,
            semantics=semantics,
            scenario=scenario,
        )
        preview = self._extract_preview_from_outputs(outputs)
        has_side_effect = False
        if ok and action_type in write_actions:
            if action_type in ("SendMessageAction", "SendEventAction", "InvokePluginAction"):
                has_side_effect = True
            else:
                has_side_effect = any(bool(v) for v in outputs.values() if isinstance(v, list))
        return ok, msg, outputs, preview, has_side_effect

    @staticmethod
    def _extract_preview_from_outputs(outputs: Dict[str, Any]) -> List[dict]:
        """从节点输出对象列表中提取最多 MAX_PREVIEW_ITEMS 条可读预览摘要。"""
        items: List[dict] = []
        for key in ("torrents", "medias", "downloads", "fileitems", "subscribes"):
            raw_list = outputs.get(key)
            if not isinstance(raw_list, list) or not raw_list:
                continue
            for entry in raw_list[:MAX_PREVIEW_ITEMS]:
                if isinstance(entry, Context) and entry.torrent_info:
                    t = entry.torrent_info
                    sub_parts = [
                        p
                        for p in (
                            t.site_name or (f"站点#{t.site}" if t.site else None),
                            StringUtils.str_filesize(t.size) if t.size else None,
                            t.volume_factor if t.uploadvolumefactor is not None else None,
                            t.pubdate,
                        )
                        if p
                    ]
                    items.append({
                        "title": t.title or "未命名资源",
                        "subtitle": " · ".join(sub_parts),
                    })
                elif isinstance(entry, dict):
                    title = (
                        entry.get("title")
                        or entry.get("name")
                        or entry.get("download_id")
                        or entry.get("path")
                        or "样本条目"
                    )
                    sub_parts = [
                        str(p)
                        for p in (
                            entry.get("site"),
                            StringUtils.str_filesize(entry["size"]) if entry.get("size") else None,
                            entry.get("year"),
                            entry.get("path") if entry.get("path") != title else None,
                        )
                        if p
                    ]
                    items.append({"title": str(title), "subtitle": " · ".join(sub_parts)})
                else:
                    title = (
                        getattr(entry, "title", None)
                        or getattr(entry, "name", None)
                        or getattr(entry, "download_id", None)
                        or str(entry)
                    )
                    year = getattr(entry, "year", None)
                    vote = getattr(entry, "vote_average", None)
                    path_val = getattr(entry, "path", None)
                    sub_parts = [
                        p
                        for p in (
                            f"{year}" if year else None,
                            f"★ {vote}" if vote else None,
                            str(path_val) if path_val and str(path_val) != str(title) else None,
                        )
                        if p
                    ]
                    items.append({"title": str(title), "subtitle": " · ".join(sub_parts)})
            break
        return items

    @staticmethod
    def _readonly_check_incremental(
        workflow_id: Optional[int],
        action_id: str,
        scope_signature: str,
        candidates: List[Context],
    ) -> Tuple[List[Context], str]:
        """只读比对增量追新基线，绝不修改或写入 SystemConfig 缓存。"""
        if not workflow_id:
            return candidates, "（尚未保存工作流ID，本次以当前全部存量继续演练下游规则；首次正式运行将自动建档跳过存量）"
        workflow_cache = SystemConfigOper().get(f"WorkflowCache-{workflow_id}")
        state = workflow_cache.get(f"{action_id}__incremental") if isinstance(workflow_cache, dict) else None
        if not isinstance(state, dict) or state.get("scope_signature") != scope_signature:
            return (
                candidates,
                f"（当前尚未建立追新基线：首次正式运行将记录这 {len(candidates)} 条为存量不下载；本次演练继续放行以测试下游过滤规则）",
            )

        seen_set = set(state.get("seen") or [])
        baseline_dt = parse_torrent_pubdate(state.get("baseline_time"))
        cutoff_dt = (
            baseline_dt - timedelta(minutes=BASELINE_CLOCK_SKEW_MINUTES)
            if baseline_dt is not None
            else None
        )
        fresh: List[Context] = []
        skipped = 0
        for ctx in candidates:
            t_info = getattr(ctx, "torrent_info", None)
            fps = torrent_fingerprints(t_info)
            if fps and any(fp in seen_set for fp in fps):
                skipped += 1
                continue
            pub_dt = parse_torrent_pubdate(getattr(t_info, "pubdate", None))
            if cutoff_dt is not None and pub_dt is not None and pub_dt < cutoff_dt:
                skipped += 1
                continue
            fresh.append(ctx)
        return fresh, f"（已比对追新基线：新资源 {len(fresh)} 条，跳过存量 {skipped} 条）"

    def _dry_run_single_action(
        self,
        action: Action,
        has_incoming: bool,
        semantics: _ExecutorSemanticsHelper,
        workflow_id: Optional[int],
        diagnostics: List[WorkflowSimulateDiagnostic],
    ) -> Tuple[bool, str, Dict[str, Any], List[dict], bool]:
        """执行真实数据只读演练（检索与过滤使用真实只读数据，一切写操作与外部触发 100% 拦截）。"""
        # pylint: disable=import-outside-toplevel,too-many-locals,too-many-return-statements,too-many-branches,too-many-statements
        action_type = action.type or ""
        data = action.data if isinstance(action.data, dict) else {}

        try:
            # 1. 只读源动作节点
            if action_type == "FetchRssAction":
                url = (data.get("url") or "").strip() if isinstance(data.get("url"), str) else ""
                if not url:
                    return False, "[演练] 缺少必填参数：RSS 地址 (url)", {}, [], False
                from app.chain.media import MediaChain
                from app.helper.rss import RssHelper

                headers = {}
                if data.get("content_type"):
                    headers["Content-Type"] = str(data["content_type"])
                if data.get("referer"):
                    headers["Referer"] = str(data["referer"])
                if data.get("ua"):
                    headers["User-Agent"] = str(data["ua"])
                timeout_val = min(max(int(data.get("timeout") or 15), 3), 15)
                rss_items = RssHelper().parse(
                    url=url,
                    proxy=settings.PROXY if data.get("proxy") else None,
                    timeout=timeout_val,
                    headers=headers,
                )
                if rss_items is None or rss_items is False:
                    return False, f"[演练] RSS 地址请求失败：{url}", {}, [], False
                candidates: List[Context] = []
                for item in rss_items or []:
                    if not item.get("title"):
                        continue
                    t_info = TorrentInfo(
                        title=item.get("title"),
                        description=item.get("description"),
                        enclosure=item.get("enclosure"),
                        page_url=item.get("link"),
                        size=item.get("size") or 0,
                        pubdate=item["pubdate"].strftime("%Y-%m-%d %H:%M:%S") if item.get("pubdate") else None,
                    )
                    candidates.append(
                        Context(
                            meta_info=MetaInfo(title=t_info.title, subtitle=t_info.description),
                            media_info=None,
                            torrent_info=t_info,
                        )
                    )
                inc_note = ""
                if data.get("only_new"):
                    scope_sig = build_scope_signature("rss", url)
                    candidates, inc_note = self._readonly_check_incremental(
                        workflow_id, action.id or "", scope_sig, candidates
                    )
                matched: List[Context] = []
                if data.get("match_media"):
                    for idx, cand in enumerate(candidates):
                        if idx < MAX_LIVE_RECOGNIZE:
                            cand.media_info = MediaChain().recognize_by_meta(cand.meta_info, obtain_images=False)
                            if not cand.media_info:
                                continue
                        matched.append(cand)
                else:
                    matched = candidates
                outputs = {"torrents": matched}
                return (
                    True,
                    f"[演练] RSS 获取到 {len(matched)} 条候选资源{inc_note}",
                    outputs,
                    self._extract_preview_from_outputs(outputs),
                    False,
                )

            if action_type == "FetchTorrentsAction":
                from app.chain.media import MediaChain
                from app.chain.search import SearchChain

                search_type = data.get("search_type") or "keyword"
                sites = data.get("sites") if isinstance(data.get("sites"), list) else []
                season_raw = data.get("season")
                season_val = (
                    int(season_raw)
                    if season_raw is not None and str(season_raw).strip() != ""
                    else None
                )
                if search_type == "keyword":
                    name = (data.get("name") or "").strip() if isinstance(data.get("name"), str) else ""
                    if not name:
                        return False, "[演练] 按名称搜索时缺少资源名称", {}, [], False
                    raw_torrents = SearchChain().search_by_title(title=name, sites=sites) or []
                    candidates = []
                    for torrent in raw_torrents:
                        if data.get("year") and torrent.meta_info.year != str(data.get("year")).strip():
                            continue
                        if season_val is not None and torrent.meta_info.begin_season != season_val:
                            continue
                        candidates.append(torrent)
                    inc_note = ""
                    if data.get("only_new"):
                        scope_sig = build_scope_signature(
                            "keyword", name, data.get("year"), data.get("type"), season_val, sorted(sites)
                        )
                        candidates, inc_note = self._readonly_check_incremental(
                            workflow_id, action.id or "", scope_sig, candidates
                        )
                    matched = []
                    target_mtype = MediaType(data["type"]) if data.get("type") else None
                    for idx, torrent in enumerate(candidates):
                        if data.get("match_media") and idx < MAX_LIVE_RECOGNIZE and not torrent.media_info:
                            torrent.media_info = MediaChain().recognize_by_meta(
                                torrent.meta_info, obtain_images=False
                            )
                            if not torrent.media_info:
                                continue
                        if target_mtype and torrent.media_info and torrent.media_info.type != target_mtype:
                            continue
                        matched.append(torrent)
                    outputs = {"torrents": matched}
                    return (
                        True,
                        f"[演练] 按关键字「{name}」搜索命中 {len(matched)} 条资源{inc_note}",
                        outputs,
                        self._extract_preview_from_outputs(outputs),
                        False,
                    )

                upstream_medias = self._read_upstream_list("medias", action, semantics)
                if not upstream_medias:
                    return True, "[演练] 上游无媒体列表数据，本次搜索 0 条资源", {"torrents": []}, [], False
                found_torrents: List[Context] = []
                for media in upstream_medias[:2]:
                    m_type_raw = getattr(media, "type", None) or (
                        media.get("type") if isinstance(media, dict) else None
                    )
                    found = SearchChain().search_by_id(
                        tmdbid=getattr(media, "tmdb_id", None),
                        doubanid=getattr(media, "douban_id", None),
                        bangumiid=getattr(media, "bangumi_id", None),
                        anilistid=getattr(media, "anilist_id", None),
                        mtype=MediaType(m_type_raw) if m_type_raw else None,
                        sites=sites,
                    )
                    if found:
                        found_torrents.extend(found)
                outputs = {"torrents": found_torrents}
                trunc_hint = f"（演练仅抽样前 2/{len(upstream_medias)} 部媒体）" if len(upstream_medias) > 2 else ""
                return (
                    True,
                    f"[演练] 基于上游媒体搜索到 {len(found_torrents)} 条资源{trunc_hint}",
                    outputs,
                    self._extract_preview_from_outputs(outputs),
                    False,
                )

            if action_type == "FetchMediasAction":
                from app.workflow.actions.fetch_medias import FetchMediasAction

                act = FetchMediasAction(action.id or "sim_fetch_medias")
                sub_ctx = act.execute(workflow_id or 0, data, ActionContext())
                medias_out = list(sub_ctx.medias or [])
                outputs = {"medias": medias_out}
                return (
                    act.success,
                    f"[演练] 获取到 {len(medias_out)} 条媒体数据",
                    outputs,
                    self._extract_preview_from_outputs(outputs),
                    False,
                )

            if action_type == "ScanFileAction":
                directory = (data.get("directory") or "").strip() if isinstance(data.get("directory"), str) else ""
                storage = data.get("storage") or "local"
                if not directory:
                    return False, "[演练] 缺少扫描目录路径 (directory)", {}, [], False
                from app.chain.storage import StorageChain

                storagechain = StorageChain()
                fileitem = storagechain.get_file_item(storage, Path(directory))
                if not fileitem:
                    return False, f"[演练] 目录不存在：【{storage}】{directory}", {}, [], False
                files = storagechain.list_files(fileitem, recursion=True) or []
                media_exts = settings.RMT_MEDIAEXT + settings.RMT_SUBEXT + settings.RMT_AUDIOEXT
                matched_files = [
                    f for f in files if f.extension and f".{f.extension.lower()}" in media_exts
                ]
                outputs = {"fileitems": matched_files}
                return (
                    True,
                    f"[演练] 只读扫描目录发现 {len(matched_files)} 个媒体文件",
                    outputs,
                    self._extract_preview_from_outputs(outputs),
                    False,
                )

            if action_type == "FetchDownloadsAction":
                upstream_downloads = self._read_upstream_list("downloads", action, semantics)
                if upstream_downloads:
                    outputs = {"downloads": upstream_downloads}
                    return (
                        True,
                        f"[演练] 已同步上游 {len(upstream_downloads)} 个预览下载任务状态",
                        outputs,
                        self._extract_preview_from_outputs(outputs),
                        False,
                    )
                if not has_incoming:
                    torrents = ActionChain().list_torrents() or []
                    dl_previews = [
                        {
                            "download_id": getattr(t, "hash", ""),
                            "title": getattr(t, "title", None) or getattr(t, "name", "下载任务"),
                            "completed": getattr(t, "progress", 0) >= 100,
                            "path": getattr(t, "path", ""),
                        }
                        for t in torrents
                    ]
                    outputs = {"downloads": dl_previews}
                    return (
                        True,
                        f"[演练] 只读读取下载器队列共 {len(dl_previews)} 个任务",
                        outputs,
                        self._extract_preview_from_outputs(outputs),
                        False,
                    )
                return True, "[演练] 上游无待处理下载任务", {"downloads": []}, [], False

            # 2. 真实规则过滤节点
            if action_type == "FilterTorrentsAction":
                for field_key, field_label in (("include", "包含规则"), ("exclude", "排除规则")):
                    pattern = data.get(field_key)
                    if isinstance(pattern, str) and pattern.strip():
                        try:
                            re.compile(pattern)
                        except re.error as err:
                            return False, f"[演练] {field_label}正则表达式非法：{err}", {}, [], False
                raw_age = data.get("max_age_hours")
                min_pub_dt = None
                if raw_age is not None and str(raw_age).strip() != "":
                    try:
                        age_val = float(raw_age)
                    except (TypeError, ValueError):
                        return False, f"[演练] 发布时效参数非法：{raw_age}", {}, [], False
                    if age_val > 0:
                        min_pub_dt = datetime.now() - timedelta(hours=age_val)

                upstream_torrents = self._read_upstream_list("torrents", action, semantics)
                in_count = len(upstream_torrents)
                if in_count == 0:
                    return True, "[演练] 上游传入 0 条资源，过滤结果 0 条", {"torrents": []}, [], False

                kept: List[Any] = []
                rule_groups = data.get("rule_groups") if isinstance(data.get("rule_groups"), list) else []
                for item in upstream_torrents:
                    t_info = item.torrent_info if isinstance(item, Context) else None
                    if t_info is None:
                        kept.append(item)
                        continue
                    if min_pub_dt is not None:
                        pub_dt = parse_torrent_pubdate(t_info.pubdate)
                        if pub_dt is None or pub_dt < min_pub_dt:
                            continue
                    if not TorrentHelper().filter_torrent(
                        torrent_info=t_info,
                        filter_params={
                            "quality": data.get("quality"),
                            "resolution": data.get("resolution"),
                            "effect": data.get("effect"),
                            "include": data.get("include"),
                            "exclude": data.get("exclude"),
                            "size": data.get("size"),
                        },
                    ):
                        continue
                    if rule_groups and not ActionChain().filter_torrents(
                        rule_groups=rule_groups,
                        torrent_list=[t_info],
                        mediainfo=item.media_info,
                    ):
                        continue
                    kept.append(item)

                outputs = {"torrents": kept}
                return (
                    True,
                    f"[演练] 过滤 {in_count} 条候选资源 → 命中保留 {len(kept)} 条（筛除 {in_count - len(kept)} 条）",
                    outputs,
                    self._extract_preview_from_outputs(outputs),
                    False,
                )

            if action_type == "FilterMediasAction":
                upstream_medias = self._read_upstream_list("medias", action, semantics)
                in_count = len(upstream_medias)
                if in_count == 0:
                    return True, "[演练] 上游传入 0 条媒体，过滤结果 0 条", {"medias": []}, [], False
                vote_min = float(data["vote"]) if data.get("vote") not in (None, "") else None
                kept_medias = []
                for m in upstream_medias:
                    m_type = getattr(m, "type", None) or (m.get("type") if isinstance(m, dict) else None)
                    m_vote = getattr(m, "vote_average", None)
                    if m_vote is None and isinstance(m, dict):
                        m_vote = m.get("vote_average")
                    m_year = getattr(m, "year", None) or (m.get("year") if isinstance(m, dict) else None)
                    if data.get("type") and str(m_type) != str(data.get("type")):
                        continue
                    if vote_min is not None and float(m_vote or 0) < vote_min:
                        continue
                    if data.get("year") and str(m_year or "") != str(data.get("year")).strip():
                        continue
                    kept_medias.append(m)
                outputs = {"medias": kept_medias}
                return (
                    True,
                    f"[演练] 过滤 {in_count} 条媒体 → 命中保留 {len(kept_medias)} 条",
                    outputs,
                    self._extract_preview_from_outputs(outputs),
                    False,
                )

            # 3. 写操作/外部触发动作节点 —— 100% 演练拦截，零副作用
            if action_type == "AddDownloadAction":
                cap_val: Optional[int] = None
                raw_cap = data.get("max_downloads")
                if raw_cap is not None and str(raw_cap).strip() != "":
                    try:
                        parsed_cap = int(raw_cap)
                        if parsed_cap > 0:
                            cap_val = parsed_cap
                    except (TypeError, ValueError):
                        return False, f"[演练] 单次最大下载数参数非法：{raw_cap}", {}, [], False

                upstream_torrents = self._read_upstream_list("torrents", action, semantics)
                if not upstream_torrents:
                    return True, "[演练] 上游无命中资源，本次不会添加任何下载任务", {"downloads": []}, [], False

                # 只读检查历史已下载缓存（不写入缓存）
                workflow_cache = (
                    SystemConfigOper().get(f"WorkflowCache-{workflow_id}") if workflow_id else None
                )
                seen_downloads = set(
                    (workflow_cache.get(action.id or "") or [])
                    if isinstance(workflow_cache, dict)
                    else []
                )
                planned_torrents = []
                skipped_cached = 0
                for item in upstream_torrents:
                    if isinstance(item, Context) and item.torrent_info:
                        ckey = f"{item.torrent_info.site}-{item.torrent_info.title}"
                        if ckey in seen_downloads:
                            skipped_cached += 1
                            continue
                    planned_torrents.append(item)

                capped_note = ""
                if cap_val and len(planned_torrents) > cap_val:
                    capped_note = f"，受单次上限 {cap_val} 截断（其余 {len(planned_torrents) - cap_val} 条跳过）"
                    planned_torrents = planned_torrents[:cap_val]

                cache_note = f"，跳过已下载缓存 {skipped_cached} 条" if skipped_cached else ""
                dl_target = data.get("downloader") or "默认下载器"
                preview_items = self._extract_preview_from_outputs({"torrents": planned_torrents})
                sim_downloads = [
                    {
                        "download_id": f"dryrun-{idx + 1}",
                        "title": p["title"],
                        "downloader": dl_target,
                        "completed": True,
                    }
                    for idx, p in enumerate(preview_items)
                ]
                if len(planned_torrents) > len(sim_downloads):
                    sim_downloads.extend(
                        self._make_samples("downloads", len(planned_torrents) - len(sim_downloads))
                    )
                return (
                    True,
                    f"[演练拦截] 预计向「{dl_target}」推送 {len(planned_torrents)} 个下载任务{capped_note}{cache_note}（已安全拦截，未实际提交下载器）",
                    {"downloads": sim_downloads},
                    preview_items,
                    True,
                )

            if action_type == "AddSubscribeAction":
                upstream_medias = self._read_upstream_list("medias", action, semantics)
                if not upstream_medias:
                    return True, "[演练] 上游无命中媒体，本次不会添加任何订阅", {"subscribes": []}, [], False
                preview_items = self._extract_preview_from_outputs({"medias": upstream_medias})
                return (
                    True,
                    f"[演练拦截] 预计新增 {len(upstream_medias)} 个媒体订阅（已安全拦截，未写入订阅库）",
                    {"subscribes": self._make_samples("subscribes", len(upstream_medias))},
                    preview_items,
                    True,
                )

            if action_type == "TransferFileAction":
                source = data.get("source") or "downloads"
                input_field = "downloads" if source == "downloads" else "fileitems"
                items_in = self._read_upstream_list(input_field, action, semantics)
                if not items_in:
                    return True, "[演练] 上游无待整理条目，本次不执行文件整理", {"fileitems": []}, [], False
                preview_items = self._extract_preview_from_outputs({input_field: items_in})
                return (
                    True,
                    f"[演练拦截] 预计整理归档 {len(items_in)} 个项目（已安全拦截，未移动或硬链接磁盘文件）",
                    {"fileitems": self._make_samples("fileitems", len(items_in))},
                    preview_items,
                    True,
                )

            if action_type == "ScrapeFileAction":
                items_in = self._read_upstream_list("fileitems", action, semantics)
                if not items_in:
                    return True, "[演练] 上游无待刮削文件，本次不执行刮削", {"fileitems": []}, [], False
                preview_items = self._extract_preview_from_outputs({"fileitems": items_in})
                return (
                    True,
                    f"[演练拦截] 预计刮削 {len(items_in)} 个媒体文件元数据（已安全拦截，未写入磁盘）",
                    {"fileitems": self._make_samples("fileitems", len(items_in))},
                    preview_items,
                    True,
                )

            if action_type == "SendMessageAction":
                return True, "[演练拦截] 预计发送工作流通知消息（已安全拦截，未实际推送）", {}, [], True

            if action_type == "SendEventAction":
                return True, "[演练拦截] 预计广播系统事件（已安全拦截，未实际触发）", {}, [], True

            if action_type == "InvokePluginAction":
                plugin_id = (data.get("plugin_id") or "").strip() if isinstance(data.get("plugin_id"), str) else ""
                act_id = (data.get("action_id") or "").strip() if isinstance(data.get("action_id"), str) else ""
                if not plugin_id or not act_id:
                    return False, "[演练] 未选择要调用的插件或动作", {}, [], False
                return (
                    True,
                    f"[演练拦截] 预计调用插件「{plugin_id}」动作「{act_id}」（已安全拦截，未实际执行插件）",
                    {},
                    [],
                    True,
                )

        except Exception as err:
            logger.warning(f"工作流演练节点 {action.id} 异常回退: {err}")
            diagnostics.append(
                WorkflowSimulateDiagnostic(
                    level="warning",
                    node_id=action.id,
                    message=f"实时只读查询出现异常（{err}），该节点已回退为静态校验",
                )
            )

        ok, msg, outputs = self._simulate_single_action(
            action=action,
            has_incoming=has_incoming,
            semantics=semantics,
            scenario="normal",
        )
        return ok, msg, outputs, self._extract_preview_from_outputs(outputs), False

    def _simulate_single_action(
        self,
        action: Action,
        has_incoming: bool,
        semantics: _ExecutorSemanticsHelper,
        scenario: str,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """校验单个动作参数并根据场景生成纯内存模拟输出（零外部调用）。"""
        action_type = action.type or ""
        data = action.data if isinstance(action.data, dict) else {}

        # 1. 源动作节点
        if action_type == "FetchRssAction":
            url = data.get("url")
            if not (isinstance(url, str) and url.strip()):
                return False, "[模拟] 缺少必填参数：RSS 地址 (url)", {}
            only_new_hint = "（增量追新：首次运行仅建档，后续仅放行新资源）" if data.get("only_new") else ""
            if scenario == "empty_source":
                return True, f"[模拟] RSS 源返回 0 条资源（空源数据场景）{only_new_hint}", {"torrents": []}
            samples = self._make_samples("torrents", 3)
            return True, f"[模拟] 成功获取 3 条 RSS 资源样本{only_new_hint}", {"torrents": samples}

        if action_type == "FetchTorrentsAction":
            search_type = data.get("search_type") or "keyword"
            only_new_hint = "（增量追新：首次运行仅建档，后续仅放行新资源）" if data.get("only_new") else ""
            if search_type == "keyword":
                name = data.get("name")
                if not (isinstance(name, str) and name.strip()):
                    return False, "[模拟] 按名称搜索时缺少资源名称", {}
                if scenario == "empty_source":
                    return True, f"[模拟] 按名称「{name.strip()}」搜索返回 0 条资源（空源数据场景）{only_new_hint}", {"torrents": []}
                samples = self._make_samples("torrents", 3)
                return True, f"[模拟] 按名称「{name.strip()}」模拟搜索到 3 条资源{only_new_hint}", {"torrents": samples}

            upstream_medias = self._read_upstream_list("medias", action, semantics)
            if scenario == "empty_source":
                return True, f"[模拟] 按媒体列表搜索返回 0 条资源（空源数据场景）{only_new_hint}", {"torrents": []}
            if not upstream_medias:
                return True, "[模拟] 上游无待处理数据，本次不产生实际操作", {"torrents": []}
            samples = self._make_samples("torrents", 3)
            return True, f"[模拟] 基于上游 {len(upstream_medias)} 条媒体搜索到 3 条资源{only_new_hint}", {"torrents": samples}

        if action_type == "FetchMediasAction":
            if scenario == "empty_source":
                return True, "[模拟] 获取到 0 条媒体数据（空源数据场景）", {"medias": []}
            samples = self._make_samples("medias", 3)
            return True, "[模拟] 成功获取 3 条媒体推荐样本", {"medias": samples}

        if action_type == "ScanFileAction":
            directory = data.get("directory")
            if not (isinstance(directory, str) and directory.strip()):
                return False, "[模拟] 缺少扫描目录路径 (directory)", {}
            if scenario == "empty_source":
                return True, f"[模拟] 扫描目录「{directory.strip()}」发现 0 个媒体文件（空源数据场景）", {"fileitems": []}
            samples = self._make_samples("fileitems", 3)
            return True, f"[模拟] 扫描目录「{directory.strip()}」发现 3 个媒体文件", {"fileitems": samples}

        if action_type == "FetchDownloadsAction":
            if scenario == "empty_source":
                return True, "[模拟] 当前队列无下载任务（空源数据场景）", {"downloads": []}
            upstream_downloads = self._read_upstream_list("downloads", action, semantics)
            if upstream_downloads:
                count = len(upstream_downloads)
                return True, f"[模拟] 已同步 {count} 个下载任务状态（全部完成）", {
                    "downloads": self._make_samples("downloads", count)
                }
            if not has_incoming:
                return True, "[模拟] 获取到 3 个下载任务状态样本", {
                    "downloads": self._make_samples("downloads", 3)
                }
            return True, "[模拟] 上游无待处理数据，本次不产生实际操作", {"downloads": []}

        # 2. 过滤动作节点
        if action_type == "FilterTorrentsAction":
            for field_key, field_label in (("include", "包含规则"), ("exclude", "排除规则")):
                pattern = data.get(field_key)
                if isinstance(pattern, str) and pattern.strip():
                    try:
                        re.compile(pattern)
                    except re.error as err:
                        return False, f"[模拟] {field_label}正则表达式非法：{err}", {}
            age_hint = ""
            raw_age = data.get("max_age_hours")
            if raw_age is not None and str(raw_age).strip() != "":
                try:
                    age_val = float(raw_age)
                except (TypeError, ValueError):
                    return False, f"[模拟] 发布时间范围（小时）参数非法：{raw_age}", {}
                if age_val > 0:
                    age_hint = f"（仅保留最近 {age_val:g} 小时内发布）"
            upstream_torrents = self._read_upstream_list("torrents", action, semantics)
            in_count = len(upstream_torrents)
            if in_count == 0:
                return True, f"[模拟] 上游无待过滤资源，过滤结果 0 条{age_hint}", {"torrents": []}
            if scenario == "empty_filter":
                return True, f"[模拟] 过滤 {in_count} 条资源后匹配 0 条（过滤为空场景）{age_hint}", {"torrents": []}
            out_count = min(2, in_count)
            return True, f"[模拟] 过滤 {in_count} 条资源后保留 {out_count} 条{age_hint}", {
                "torrents": upstream_torrents[:out_count]
            }

        if action_type == "FilterMediasAction":
            upstream_medias = self._read_upstream_list("medias", action, semantics)
            in_count = len(upstream_medias)
            if in_count == 0:
                return True, "[模拟] 上游无待过滤媒体，过滤结果 0 条", {"medias": []}
            if scenario == "empty_filter":
                return True, f"[模拟] 过滤 {in_count} 条媒体后匹配 0 条（过滤为空场景）", {"medias": []}
            out_count = min(2, in_count)
            return True, f"[模拟] 过滤 {in_count} 条媒体后保留 {out_count} 条", {
                "medias": upstream_medias[:out_count]
            }

        # 3. 消费动作节点
        if action_type == "AddDownloadAction":
            cap_val: Optional[int] = None
            raw_cap = data.get("max_downloads")
            if raw_cap is not None and str(raw_cap).strip() != "":
                try:
                    parsed_cap = int(raw_cap)
                except (TypeError, ValueError):
                    return False, f"[模拟] 单次最大下载数参数非法：{raw_cap}", {}
                if parsed_cap > 0:
                    cap_val = parsed_cap
            in_count = len(self._read_upstream_list("torrents", action, semantics))
            if in_count == 0:
                return True, "[模拟] 上游无待处理数据，本次不产生实际操作", {"downloads": []}
            actual_count = min(in_count, cap_val) if cap_val else in_count
            cap_hint = f"（单次上限 {cap_val}）" if cap_val else ""
            return True, f"[模拟] 预计添加 {actual_count} 个下载任务{cap_hint}（未实际提交下载器）", {
                "downloads": self._make_samples("downloads", actual_count)
            }

        if action_type == "AddSubscribeAction":
            in_count = len(self._read_upstream_list("medias", action, semantics))
            if in_count == 0:
                return True, "[模拟] 上游无待处理数据，本次不产生实际操作", {"subscribes": []}
            return True, f"[模拟] 预计添加 {in_count} 个媒体订阅（未实际写入数据库）", {
                "subscribes": self._make_samples("subscribes", in_count)
            }

        if action_type == "TransferFileAction":
            source = data.get("source") or "downloads"
            input_field = "downloads" if source == "downloads" else "fileitems"
            in_count = len(self._read_upstream_list(input_field, action, semantics))
            if in_count == 0:
                return True, "[模拟] 上游无待处理数据，本次不产生实际操作", {"fileitems": []}
            return True, f"[模拟] 预计整理 {in_count} 个媒体文件（未实际移动或硬链接文件）", {
                "fileitems": self._make_samples("fileitems", in_count)
            }

        if action_type == "ScrapeFileAction":
            in_count = len(self._read_upstream_list("fileitems", action, semantics))
            if in_count == 0:
                return True, "[模拟] 上游无待处理数据，本次不产生实际操作", {"fileitems": []}
            return True, f"[模拟] 预计刮削 {in_count} 个媒体文件元数据（未实际写入磁盘）", {
                "fileitems": self._make_samples("fileitems", in_count)
            }

        # 4. 通知、事件、插件与备注节点
        if action_type == "SendMessageAction":
            return True, "[模拟] 预计发送工作流执行通知消息（未实际发送）", {}

        if action_type == "SendEventAction":
            return True, "[模拟] 预计广播工作流执行事件（未实际发送）", {}

        if action_type == "InvokePluginAction":
            plugin_id = data.get("plugin_id")
            action_id = data.get("action_id")
            if not (isinstance(plugin_id, str) and plugin_id.strip()) or not (
                isinstance(action_id, str) and action_id.strip()
            ):
                return False, "[模拟] 未选择要调用的插件或动作", {}
            return (
                True,
                f"[模拟] 预计调用插件「{plugin_id.strip()}」动作「{action_id.strip()}」（未实际执行插件代码）",
                {},
            )

        if action_type == "NoteAction":
            return True, "[模拟] 备注节点，不参与实际业务处理", {}

        # 未知或扩展动作类型校验
        actions_map = getattr(self.workflowmanager, "_actions", None) or {}
        if action_type in actions_map:
            return True, f"[模拟] 动作「{self._get_action_display_name(action)}」已模拟通过", {}
        return False, f"[模拟] 未知动作类型：{action_type or '未指定'}", {}

    @staticmethod
    def _make_samples(kind: str, count: int) -> List[dict]:
        """构造纯内存轻量样本字典列表。"""
        if count <= 0:
            return []
        if kind == "torrents":
            return [
                {
                    "title": f"示例番剧 S01E{idx:02d} 1080p WEB-DL AAC",
                    "site": "示例站点",
                    "size": int((1.1 + idx * 0.25) * 1024 * 1024 * 1024),
                }
                for idx in range(1, count + 1)
            ]
        if kind == "medias":
            return [
                {"title": f"示例影视作品 {idx}", "year": "2026", "type": "电视剧", "vote_average": 8.5}
                for idx in range(1, count + 1)
            ]
        if kind == "fileitems":
            return [
                {
                    "name": f"示例媒体文件_E{idx:02d}.mkv",
                    "path": f"/media/Anime/示例媒体文件_E{idx:02d}.mkv",
                    "storage": "local",
                }
                for idx in range(1, count + 1)
            ]
        if kind == "downloads":
            return [
                {
                    "download_id": f"sim-task-{idx}",
                    "title": f"示例下载任务 #{idx}",
                    "downloader": "默认下载器",
                    "completed": True,
                    "path": f"/downloads/示例媒体文件_E{idx:02d}.mkv",
                }
                for idx in range(1, count + 1)
            ]
        if kind == "subscribes":
            return [
                {"id": idx, "name": f"示例订阅剧集 {idx}", "year": "2026"}
                for idx in range(1, count + 1)
            ]
        return [{"title": f"示例条目 {idx}"} for idx in range(1, count + 1)]

    @staticmethod
    def _build_summary(
        scenario: str,
        overall_success: bool,
        has_cycle_error: bool,
        total_nodes: int,
        nodes: Dict[str, dict],
        execution_order: List[str],
    ) -> str:
        """构建模拟试跑中文结果摘要。"""
        scenario_label = VALID_SCENARIOS.get(scenario, scenario)
        counts = defaultdict(int)
        for item in nodes.values():
            counts[item["state"]] += 1

        if has_cycle_error:
            return (
                f"模拟未通过（场景：{scenario_label}）：工作流图存在环路或缺少起始节点，"
                f"已执行 {len(execution_order)}/{total_nodes} 个节点"
            )
        if not overall_success:
            return (
                f"模拟未通过（场景：{scenario_label}）：共 {total_nodes} 个节点，"
                f"成功 {counts['success']} 个，失败 {counts['failed']} 个，"
                f"跳过 {counts['skipped']} 个，未到达 {counts['unreached']} 个"
            )
        return (
            f"模拟通过（场景：{scenario_label}）：共 {total_nodes} 个节点，"
            f"成功执行 {counts['success']} 个，条件跳过 {counts['skipped']} 个"
        )
