from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from tools._models import BackendResult


METRIC_ID = "cash_receipts_by_direction"
MOVEMENTS = "AccumulationRegister_ДвиженияДенежныхСредств_RecordType"
CATALOGS = {
    "organization": "Catalog_Организации",
    "direction": "Catalog_НаправленияДеятельности",
    "counterparty": "Catalog_Контрагенты",
}
ENTITIES = frozenset({MOVEMENTS, *CATALOGS.values()})
Operation = Literal["search_metrics", "get_metric_definition", "get_entity_schema", "resolve_dimension", "get_cash_receipts_by_direction"]


class RequestScope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: str = Field(min_length=1, max_length=200)
    base_id: str = Field(min_length=1, max_length=100)
    allowed_metric_ids: list[str] = Field(max_length=100)
    allowed_organization_ids: list[str] = Field(max_length=1000)
    allowed_entity_names: list[str] = Field(max_length=100)
    allow_demo_metrics: bool = False


class DataCall(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    operation: Operation
    arguments: dict[str, Any]
    context: RequestScope
    timeout: float = Field(gt=0, le=60)
    max_rows: int | None = Field(default=None, ge=1, le=100000)


__all__ = ["BackendResult", "DataCall", "RequestScope"]
