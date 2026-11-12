"""
搜索诊断模型与站点搜索观察上下文。
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator, List, Literal, Optional

from pydantic import BaseModel, Field


class SearchDiagnosticSite(BaseModel):
    """本次请求实际调度的来源事实，不包含请求地址和原始错误。"""

    site_id: Optional[int] = Field(default=None, description="站点主键；插件来源为 None")
    site_name: str = Field(description="站点名称；插件使用插件资源源，缺失名称使用未知站点")
    attempted: bool = Field(description="是否实际发起过该来源请求，冷却跳过时为 False")
    outcome: Literal["success", "empty", "error", "cooldown", "circuit_open", "unknown"] = Field(description="来源结果")
    raw_count: int = Field(default=0, ge=0, description="来源报告的原始资源数，同站点本轮请求累加")


class SearchDiagnosticCounts(BaseModel):
    """当前搜索处理的候选与最终保留数量。"""

    candidates: int = Field(default=0, ge=0, description="来源返回并交给本次过滤匹配的候选数")
    retained: int = Field(default=0, ge=0, description="完成过滤、匹配与去重后的结果数")


class SearchDiagnosticRejectionDetail(BaseModel):
    """规则或搜索条件排除资源的具体原因，同一资源可命中多条明细。"""

    code: str = Field(description="稳定明细原因代码")
    count: int = Field(ge=0, description="命中该具体条件的次数")
    rule_group: Optional[str] = Field(default=None, description="相关规则组名称")
    rule: Optional[str] = Field(default=None, description="相关规则名称")
    expected: Optional[str] = Field(default=None, description="规则要求的条件或范围")
    actual: Optional[str] = Field(default=None, description="资源的实际值；无安全且有意义的值时为 None")


class SearchDiagnosticRejection(BaseModel):
    """有实际过滤证据的资源拒绝计数。"""

    code: str = Field(description="稳定原因代码")
    count: int = Field(ge=0, description="该阶段排除的候选数，不含规则不适用警告")
    details: List[SearchDiagnosticRejectionDetail] = Field(
        default_factory=list,
        description="具体过滤条件明细；同一资源可命中多条，明细数量不可相加为资源数",
    )


class SearchDiagnosticRuleGroup(BaseModel):
    """所选规则组在本次媒体上下文中的适用性。"""

    name: str = Field(description="所选规则组名称")
    status: Literal["effective", "skipped"] = Field(description="effective 表示适用；skipped 不代表资源被拒绝")
    reason: Optional[Literal["rule_group_not_applicable", "rule_group_missing"]] = Field(
        default=None,
        description="跳过原因",
    )
    media_type: Optional[str] = Field(default=None, description="规则限定的媒体类型")
    categories: List[str] = Field(default_factory=list, description="规则限定的分类名称列表")


class SearchDiagnosticWarning(BaseModel):
    """配置适用性或来源请求警告，不计入资源拒绝数。"""

    code: str = Field(description="稳定警告代码")
    rule_group: Optional[str] = Field(default=None, description="相关规则组名称；来源警告为 None")
    rule_ids: List[str] = Field(
        default_factory=list,
        exclude_if=lambda value: not value,
        description="规则配置错误涉及的完整 ID；保留旧 ID 空白，没有相关 ID 时省略",
    )


class SearchDiagnostics(BaseModel):
    """与一次搜索结果绑定的诊断快照。"""

    version: Literal[1] = Field(default=1, description="诊断契约版本")
    search_id: str = Field(description="本轮搜索的不透明标识")
    scope: Literal["page", "search"] = Field(default="search", description="page 仅描述单页；search 表示完整搜索请求")
    searched_sites: List[SearchDiagnosticSite] = Field(default_factory=list, description="本次实际调度的来源")
    stage_counts: SearchDiagnosticCounts = Field(
        default_factory=SearchDiagnosticCounts,
        description="候选数与保留数",
    )
    rejection_reasons: List[SearchDiagnosticRejection] = Field(
        default_factory=list,
        description="资源拒绝原因与数量",
    )
    rule_groups: List[SearchDiagnosticRuleGroup] = Field(default_factory=list, description="所选规则组的适用性")
    warnings: List[SearchDiagnosticWarning] = Field(default_factory=list, description="规则适用性和来源状态警告")


@dataclass
class SiteSearchObservation:
    """记录单次站点搜索是否发起请求及调用结果。"""

    reported: bool = False
    attempted: bool = False
    outcome: Literal["success", "empty", "error", "cooldown", "circuit_open", "unknown"] = "unknown"
    raw_count: int = 0


_site_search_observation: ContextVar[Optional[SiteSearchObservation]] = ContextVar(
    "site_search_observation",
    default=None,
)


@contextmanager
def capture_site_search_observation() -> Iterator[SiteSearchObservation]:
    """捕获当前异步任务内一次站点搜索的状态报告。"""

    observation = SiteSearchObservation()
    token = _site_search_observation.set(observation)
    try:
        yield observation
    finally:
        _site_search_observation.reset(token)


def report_site_search_outcome(
        *,
        attempted: bool,
        outcome: Literal["success", "empty", "error", "cooldown", "circuit_open", "unknown"],
        raw_count: int = 0,
) -> None:
    """向当前搜索观察上下文报告站点请求结果，不影响原有搜索返回值。"""

    observation = _site_search_observation.get()
    if observation is None:
        return
    observation.reported = True
    observation.attempted = attempted
    observation.outcome = outcome
    observation.raw_count = raw_count
