"""工作流零副作用流程模拟器。"""
import re
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional, Set, Tuple

from app.chain.workflow import WorkflowExecutor
from app.schemas import (
    Action,
    ActionContext,
    ActionFlow,
    WorkflowSimulateDiagnostic,
    WorkflowSimulateRequest,
    WorkflowSimulateResponse,
)
from app.workflow import WorkFlowManager

VALID_SCENARIOS = {
    "normal": "常规有数据",
    "empty_source": "源数据为空",
    "empty_filter": "过滤结果为空",
}

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
        diagnostics: List[WorkflowSimulateDiagnostic],
    ) -> dict:
        """按拓扑顺序与 WorkflowExecutor 完整语义执行纯内存模拟。"""
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

            ok, msg, outputs = self._simulate_single_action(
                action=action,
                has_incoming=bool(incoming_flows.get(current_id)),
                semantics=semantics,
                scenario=scenario,
            )

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
            if scenario == "empty_source":
                return True, "[模拟] RSS 源返回 0 条资源（空源数据场景）", {"torrents": []}
            samples = self._make_samples("torrents", 3)
            return True, "[模拟] 成功获取 3 条 RSS 资源样本", {"torrents": samples}

        if action_type == "FetchTorrentsAction":
            search_type = data.get("search_type") or "keyword"
            if search_type == "keyword":
                name = data.get("name")
                if not (isinstance(name, str) and name.strip()):
                    return False, "[模拟] 按名称搜索时缺少资源名称", {}
                if scenario == "empty_source":
                    return True, f"[模拟] 按名称「{name.strip()}」搜索返回 0 条资源（空源数据场景）", {"torrents": []}
                samples = self._make_samples("torrents", 3)
                return True, f"[模拟] 按名称「{name.strip()}」模拟搜索到 3 条资源", {"torrents": samples}

            upstream_medias = self._read_upstream_list("medias", action, semantics)
            if scenario == "empty_source":
                return True, "[模拟] 按媒体列表搜索返回 0 条资源（空源数据场景）", {"torrents": []}
            if not upstream_medias:
                return True, "[模拟] 上游无待处理数据，本次不产生实际操作", {"torrents": []}
            samples = self._make_samples("torrents", 3)
            return True, f"[模拟] 基于上游 {len(upstream_medias)} 条媒体搜索到 3 条资源", {"torrents": samples}

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
            upstream_torrents = self._read_upstream_list("torrents", action, semantics)
            in_count = len(upstream_torrents)
            if in_count == 0:
                return True, "[模拟] 上游无待过滤资源，过滤结果 0 条", {"torrents": []}
            if scenario == "empty_filter":
                return True, f"[模拟] 过滤 {in_count} 条资源后匹配 0 条（过滤为空场景）", {"torrents": []}
            out_count = min(2, in_count)
            return True, f"[模拟] 过滤 {in_count} 条资源后保留 {out_count} 条", {
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
            in_count = len(self._read_upstream_list("torrents", action, semantics))
            if in_count == 0:
                return True, "[模拟] 上游无待处理数据，本次不产生实际操作", {"downloads": []}
            return True, f"[模拟] 预计添加 {in_count} 个下载任务（未实际提交下载器）", {
                "downloads": self._make_samples("downloads", in_count)
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
                {"title": f"Sample Torrent {idx}", "site": "SampleSite", "size": 1024 * idx}
                for idx in range(1, count + 1)
            ]
        if kind == "medias":
            return [
                {"title": f"Sample Media {idx}", "year": "2025", "type": "电影", "vote_average": 8.5}
                for idx in range(1, count + 1)
            ]
        if kind == "fileitems":
            return [
                {"name": f"Sample_File_{idx}.mkv", "path": f"/mock/Sample_File_{idx}.mkv", "storage": "local"}
                for idx in range(1, count + 1)
            ]
        if kind == "downloads":
            return [
                {
                    "download_id": f"mock-download-{idx}",
                    "downloader": "default",
                    "completed": True,
                    "path": f"/mock/Sample_File_{idx}.mkv",
                }
                for idx in range(1, count + 1)
            ]
        if kind == "subscribes":
            return [
                {"id": idx, "name": f"Sample Subscribe {idx}", "year": "2025"}
                for idx in range(1, count + 1)
            ]
        return [{"title": f"Sample {idx}"} for idx in range(1, count + 1)]

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
