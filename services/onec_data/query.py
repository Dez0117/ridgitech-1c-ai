"""Типизированный план чтения и его проверка по опубликованной схеме OData v3."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from tools import BackendError
from .models import ENTITIES


Identifier = Annotated[str, Field(pattern=r"^[^\W\d]\w{0,199}$")]
Value = str | bool | int | float


class QueryPart(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Condition(QueryPart):
    field: Identifier
    operator: Literal["eq", "ne", "ge", "gt", "le", "lt", "contains"] = "eq"
    value: Value


class Ordering(QueryPart):
    field: Identifier
    direction: Literal["asc", "desc"] = "asc"


class EntityQuery(QueryPart):
    entity_name: Identifier
    select: list[Identifier] = Field(min_length=1, max_length=64)
    filters: list[Condition] = Field(default_factory=list, max_length=32)
    any_of: list[Condition] = Field(default_factory=list, max_length=16)
    order_by: list[Ordering] = Field(default_factory=list, max_length=16)
    limit: int | None = Field(default=None, ge=1, le=100000)
    sum_field: Identifier | None = None
    group_by: list[Identifier] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def grouping(self):
        if self.group_by and not self.sum_field:
            raise ValueError("Для группировки нужно указать поле суммы")
        return self


@dataclass(frozen=True)
class CompiledQuery:
    entity_name: str
    select: str
    order_by: str
    predicate: str | None
    max_rows: int | None

    def reader_arguments(self):
        return {"select": self.select, "order_by": self.order_by,
                "predicate": self.predicate, "max_rows": self.max_rows}


def string_literal(value):
    return "'" + value.replace("'", "''") + "'"


def literal(value, kind):
    try:
        if kind == "Edm.String" and isinstance(value, str) and len(value) <= 2000 and all(ord(char) >= 32 for char in value):
            return string_literal(value)
        if kind == "Edm.Guid" and isinstance(value, str):
            return "guid'" + str(UUID(value)) + "'"
        if kind == "Edm.Boolean" and type(value) is bool:
            return "true" if value else "false"
        ranges = {"Edm.Int16": (-2 ** 15, 2 ** 15 - 1), "Edm.Int32": (-2 ** 31, 2 ** 31 - 1),
                  "Edm.Int64": (-2 ** 63, 2 ** 63 - 1), "Edm.Byte": (0, 255), "Edm.SByte": (-128, 127)}
        if kind in ranges and type(value) is int:
            if ranges[kind][0] <= value <= ranges[kind][1]:
                return str(value) + ("L" if kind == "Edm.Int64" else "")
        if kind in {"Edm.Decimal", "Edm.Double", "Edm.Single"} and type(value) in {str, int, float}:
            if len(str(value)) > 100:
                raise ValueError
            amount = Decimal(str(value))
            if amount.is_finite() and abs(amount) < Decimal("1e28") and -28 <= amount.as_tuple().exponent <= 28:
                text = format(amount, "f")
                if kind in {"Edm.Single", "Edm.Double"} and "." not in text:
                    text += ".0"
                return text + {"Edm.Decimal": "M", "Edm.Single": "F", "Edm.Double": "D"}[kind]
        if kind in {"Edm.DateTime", "Edm.DateTimeOffset"} and isinstance(value, str):
            point = datetime.fromisoformat(value)
            if (point.tzinfo is None) != (kind == "Edm.DateTime"):
                raise ValueError
            return ("datetime" if kind == "Edm.DateTime" else "datetimeoffset") + "'" + point.isoformat() + "'"
    except (ValueError, InvalidOperation):
        pass
    raise BackendError("INVALID_ARGUMENTS", "Значение фильтра не соответствует типу поля 1С.")


def compile_query(query, schema, *, allowed_entities=ENTITIES):
    """Проверить источник, поля и литералы; сортировка всегда включает ключи."""
    try:
        plan = query if isinstance(query, EntityQuery) else EntityQuery.model_validate(query)
    except ValidationError:
        raise BackendError("INVALID_ARGUMENTS", "Некорректный структурированный план чтения.") from None
    if plan.entity_name not in allowed_entities or plan.entity_name not in schema:
        raise BackendError("ACCESS_DENIED", "Источник плана недоступен.")
    entity = schema[plan.entity_name]
    fields, keys = entity["fields"], entity["keys"]
    if not keys or any(key not in fields for key in keys):
        raise BackendError("INVALID_SCHEMA", "Не определены ключи источника 1С.")
    selected = list(dict.fromkeys([*plan.select, *plan.group_by, *([plan.sum_field] if plan.sum_field else []), *keys]))
    referenced = {*selected, *(item.field for item in plan.filters), *(item.field for item in plan.any_of), *(item.field for item in plan.order_by)}
    if not referenced <= fields.keys():
        raise BackendError("INVALID_ARGUMENTS", "План содержит неизвестное поле 1С.")
    if plan.sum_field and fields[plan.sum_field] not in {"Edm.Decimal", "Edm.Double", "Edm.Single", "Edm.Int16", "Edm.Int32", "Edm.Int64"}:
        raise BackendError("INVALID_ARGUMENTS", "Суммировать можно только числовое поле.")

    def condition(item):
        kind = fields[item.field]
        if item.operator == "contains":
            if kind != "Edm.String" or not isinstance(item.value, str) or len(item.value) > 200 or any(ord(char) < 32 for char in item.value):
                raise BackendError("INVALID_ARGUMENTS", "Поиск подстроки допустим только в строковом поле.")
            # Эта публикация 1С не принимает вложенный tolower как аргумент substringof.
            return f"substringof({string_literal(item.value.lower())},{item.field})"
        if kind == "Edm.Boolean" and item.operator not in {"eq", "ne"}:
            raise BackendError("INVALID_ARGUMENTS", "Для логического поля допустимы только eq и ne.")
        return f"{item.field} {item.operator} {literal(item.value, kind)}"

    predicates = [condition(item) for item in plan.filters]
    if plan.any_of:
        alternatives = " or ".join(condition(item) for item in plan.any_of)
        predicates.append("(" + alternatives + ")" if len(plan.any_of) > 1 else alternatives)
    ordering = list(plan.order_by)
    known = {item.field for item in ordering}
    if len(known) != len(ordering):
        raise BackendError("INVALID_ARGUMENTS", "Поле сортировки не должно повторяться.")
    try:
        ordering.extend(Ordering(field=key) for key in keys if key not in known)
    except ValidationError:
        raise BackendError("INVALID_SCHEMA", "Источник содержит некорректное имя ключа.") from None
    return CompiledQuery(plan.entity_name, ",".join(selected),
                         ",".join(item.field + (" desc" if item.direction == "desc" else "") for item in ordering),
                         " and ".join(predicates) or None, plan.limit)
