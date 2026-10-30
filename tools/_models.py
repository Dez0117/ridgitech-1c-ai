from datetime import date
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Period(Arguments):
    start: date
    end_exclusive: date

    @model_validator(mode="after")
    def ordered(self):
        if self.start >= self.end_exclusive:
            raise ValueError("start must precede end_exclusive")
        return self


class MetricQuery(Arguments):
    metric_id: str = Field(min_length=1, max_length=200)
    organization_id: str = Field(min_length=1, max_length=200)


class PeriodQuery(MetricQuery):
    period: Period


FilterValue = StrictStr | StrictBool | StrictInt
BusinessTool = Literal[
    "get_sales_turnovers", "get_top_counterparties",
    "get_cash_receipts_by_direction", "get_customer_balances",
]


class PlanStep(Arguments):
    step_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    tool: BusinessTool
    arguments: dict[str, Any]
    depends_on: list[str] = Field(default_factory=list)


class QueryPlan(Arguments):
    metric_id: str = Field(min_length=1, max_length=200)
    semantic_version: str = Field(min_length=1, max_length=200)
    catalog_version: str = Field(min_length=1, max_length=200)
    organization_id: str = Field(min_length=1, max_length=200)
    steps: list[PlanStep] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def dependencies(self):
        pending = {step.step_id: set(step.depends_on) for step in self.steps}
        if len(pending) != len(self.steps):
            raise ValueError("Step IDs must be unique")
        for step in self.steps:
            if len(set(step.depends_on)) != len(step.depends_on):
                raise ValueError("Dependencies must be unique")
            if any(dep not in pending for dep in step.depends_on):
                raise ValueError("Unknown dependency")
        done = set()
        while pending:
            ready = {name for name, dependencies in pending.items() if dependencies <= done}
            if not ready:
                raise ValueError("Cyclic dependencies")
            done.update(ready)
            pending = {name: deps for name, deps in pending.items() if name not in ready}
        return self


class BackendResult(Arguments):
    data: dict[str, Any] | list[Any]
    sources: list[str | dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    stats: dict[str, int | float] = Field(default_factory=dict)
    complete: bool = False


class MetricPolicy(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)

    metric_id: str = Field(min_length=1)
    approval: Literal["approved", "draft", "demo"]
    semantic_version: str = Field(min_length=1)
    catalog_version: str = Field(min_length=1)
    supported_tools: list[BusinessTool]
    time_kind: Literal["period", "as_of"]
    allowed_group_by: list[str] = Field(default_factory=list)
    allowed_filters: list[str] = Field(default_factory=list)
    required_filters: dict[str, FilterValue] = Field(default_factory=dict)


__all__ = ["Arguments", "AwareDatetime", "BackendResult", "FilterValue", "MetricQuery", "PeriodQuery", "QueryPlan"]
