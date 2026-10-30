from datetime import date
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Period(Model):
    start: date
    end_exclusive: date

    @model_validator(mode="after")
    def ordered(self):
        if self.start >= self.end_exclusive:
            raise ValueError("start must precede end_exclusive")
        return self


class Filter(Model):
    field: str
    operator: Literal["eq", "ge", "lt"]
    value: Union[StrictStr, StrictBool]


class EntitySchema(Model):
    entity_name: str
    fields: Dict[str, Literal["Edm.String", "Edm.Decimal", "Edm.DateTime", "Edm.Boolean"]]
    key_field: str
    expand_paths: List[str] = Field(default_factory=list)


class MetricDefinition(Model):
    metric_id: str
    name: str
    description: str
    synonyms: List[str]
    approval: Literal["demo", "approved", "draft"]
    entity_name: str
    amount_field: str
    date_field: str
    organization_field: str
    mandatory_filters: List[Filter]
    dimensions: Dict[str, str]
    unit: str
    decimal_places: int = Field(ge=0, le=6)
    aggregation: Literal["sum"] = "sum"


class CatalogData(Model):
    base_id: str
    semantic_version: str
    catalog_version: str
    entities: List[EntitySchema]
    metrics: List[MetricDefinition]


class QueryPlan(Model):
    metric_id: str
    semantic_version: str
    catalog_version: str
    organization_id: str = Field(min_length=1, max_length=200)
    period: Period
    group_by: List[str] = Field(default_factory=list, max_length=3)


class Limits(Model):
    max_rows: int = Field(default=1000, ge=1, le=100000)
    page_size: int = Field(default=100, ge=1, le=1000)
    max_pages: int = Field(default=100, ge=1, le=1000)
    timeout_seconds: float = Field(default=60.0, gt=0, le=60)
    max_model_turns: int = Field(default=8, ge=1, le=20)
    max_tool_calls: int = Field(default=16, ge=1, le=40)


class ExecutionContext(Model):
    request_id: str
    base_id: str
    allowed_metric_ids: List[str]
    allowed_organization_ids: List[str]
    current_date: date
    timezone: str = "Europe/Moscow"
    demo_mode: bool = False
    limits: Limits = Field(default_factory=Limits)


class ReadStep(Model):
    entity_name: str
    select: List[str]
    filters: List[Filter]
    order_by: str


class ExecutionPlan(Model):
    plan_id: str
    request_id: str
    base_id: str
    query: QueryPlan
    read: ReadStep
    amount_field: str
    group_fields: Dict[str, str]
    aggregation: Literal["sum"]
    unit: str
    decimal_places: int


class ErrorInfo(Model):
    code: str
    message: str


class AgentResult(Model):
    status: Literal["completed", "needs_clarification", "failed"]
    request_id: str
    message: str
    query_plan: Optional[ExecutionPlan] = None
    data: List[Dict[str, str]] = Field(default_factory=list)
    sources: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    stats: Dict[str, Union[int, float]] = Field(default_factory=dict)
    trace: List[Dict[str, Any]] = Field(default_factory=list)
    error: Optional[ErrorInfo] = None


class AgentError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
