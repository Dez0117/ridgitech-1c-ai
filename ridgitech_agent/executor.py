import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP, localcontext
from pathlib import Path
from typing import Any, Dict, List, Protocol

from .models import AgentError, ExecutionContext, ExecutionPlan


@dataclass
class Page:
    rows: List[Dict[str, Any]]
    has_more: bool


class DataAdapter(Protocol):
    base_id: str

    def read_page(self, read, *, top: int, skip: int, timeout: float) -> Page:
        """Apply all filters and stable ordering before paging; respect timeout.

        OData adapters must decode decimals without float and prove end-of-data.
        """
        ...


def matches(row, filters):
    for condition in filters:
        actual = row.get(condition.field)
        expected = condition.value
        if type(actual) is not type(expected):
            return False
        if condition.operator == "eq" and actual != expected:
            return False
        if condition.operator == "ge" and actual < expected:
            return False
        if condition.operator == "lt" and actual >= expected:
            return False
    return True


class DemoAdapter:
    """Synthetic collection only; does not connect to 1C."""

    base_id = "demo"

    def __init__(self, path: Path):
        self.rows = json.loads(path.read_text(encoding="utf-8"))

    def read_page(self, read, *, top, skip, timeout):
        if read.entity_name != "DemoSales":
            raise AgentError("SOURCE_NOT_AVAILABLE", "Тестовый источник отсутствует.")
        filtered = sorted((r for r in self.rows if matches(r, read.filters)), key=lambda r: r[read.order_by])
        rows = [{f: row[f] for f in read.select} for row in filtered[skip:skip + top]]
        return Page(rows, skip + len(rows) < len(filtered))


def odata_parameters(read, schema, *, top, skip):
    """OData v3 query parameters, without connection URL or credentials."""
    clauses = []
    for condition in read.filters:
        kind = schema.fields[condition.field]
        if kind == "Edm.Boolean" and type(condition.value) is bool:
            literal = str(condition.value).lower()
        elif kind == "Edm.DateTime" and isinstance(condition.value, str):
            datetime.fromisoformat(condition.value)
            literal = "datetime'" + condition.value + "'"
        elif kind == "Edm.String" and isinstance(condition.value, str):
            literal = "'" + condition.value.replace("'", "''") + "'"
        else:
            raise AgentError("INVALID_PLAN", "Тип фильтра не поддерживается адаптером.")
        clauses.append(f"({condition.field} {condition.operator} {literal})")
    return {"$select": ",".join(read.select), "$filter": " and ".join(clauses),
            "$orderby": read.order_by + " asc", "$top": top, "$skip": skip, "$format": "json"}


def remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise AgentError("TIMEOUT", "Превышено время обработки; сузьте период или повторите запрос.")
    return seconds


def execute(plan: ExecutionPlan, context: ExecutionContext, adapter: DataAdapter, schema, deadline):
    if (plan.base_id != context.base_id or adapter.base_id != context.base_id
            or plan.request_id != context.request_id):
        raise AgentError("ACCESS_DENIED", "Контекст исполнителя не совпадает с планом.")
    totals = {}
    count = pages = 0
    previous_key = None
    limits = context.limits
    with localcontext() as arithmetic:
        arithmetic.prec = 50
        while True:
            remaining(deadline)
            if pages >= limits.max_pages:
                raise AgentError("PAGE_LIMIT", "Достигнут лимит страниц; сузьте период или фильтры.")
            top = min(limits.page_size, limits.max_rows - count)
            page = adapter.read_page(plan.read, top=top, skip=count, timeout=remaining(deadline))
            remaining(deadline)
            pages += 1
            if (type(page.has_more) is not bool or len(page.rows) > top
                    or (not page.rows and page.has_more)):
                raise AgentError("INCOMPLETE_DATA", "Источник не подтвердил корректность пагинации.")
            for row in page.rows:
                remaining(deadline)
                if not isinstance(row, dict) or any(field not in row for field in plan.read.select):
                    raise AgentError("INVALID_DATA", "В источнике отсутствуют обязательные поля.")
                for field in plan.read.select:
                    if schema.fields[field] == "Edm.DateTime":
                        value = row[field]
                        try:
                            parsed = datetime.fromisoformat(value)
                            if parsed.tzinfo is not None or parsed.isoformat() != value:
                                raise ValueError("Date is not normalized")
                        except (TypeError, ValueError):
                            raise AgentError("INVALID_DATA", "Источник вернул некорректную или ненормализованную дату.")
                if not matches(row, plan.read.filters):
                    raise AgentError("INVALID_DATA", "Источник вернул строки вне фильтров плана.")
                key = row[plan.read.order_by]
                if type(key) is not str or (previous_key is not None and key <= previous_key):
                    raise AgentError("INCOMPLETE_DATA", "Нарушены уникальность или порядок строк при пагинации.")
                previous_key = key
                value = row[plan.amount_field]
                if type(value) is not str or not re.fullmatch(r"-?\d{1,28}(\.\d{1,6})?", value, flags=re.ASCII):
                    raise AgentError("INVALID_DATA", "Сумма должна быть конечным decimal без преобразования через float.")
                group = tuple(row[f] for f in plan.group_fields.values())
                if any(type(v) is not str for v in group):
                    raise AgentError("INVALID_DATA", "Пустое или некорректное значение разреза.")
                totals[group] = totals.get(group, Decimal(0)) + Decimal(value)
                count += 1
            if not page.has_more:
                break
            if count >= limits.max_rows:
                raise AgentError("ROW_LIMIT", "Достигнут лимит строк; итог неполный и не выдан. Сузьте период или фильтры.")
        quantum = Decimal(1).scaleb(-plan.decimal_places)
        data = [{**dict(zip(plan.group_fields, group)),
                 "value": format(value.quantize(quantum, rounding=ROUND_HALF_UP), f".{plan.decimal_places}f"),
                 "unit": plan.unit} for group, value in sorted(totals.items())]
        total = sum(totals.values(), Decimal(0)).quantize(quantum, rounding=ROUND_HALF_UP)
    warnings = []
    if not count:
        warnings.append("Выборка пуста; отсутствие данных не подтверждает нулевое значение метрики.")
    elif total == 0:
        warnings.append("Расчёт дал нулевой итог; проверьте взаимное погашение сумм и фильтры.")
    return {"data": data, "total": format(total, f".{plan.decimal_places}f") if count else None,
            "warnings": warnings, "row_count": count, "pages": pages,
            "request_preview": odata_parameters(plan.read, schema, top=limits.page_size, skip=0)}
