import asyncio
import json
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from uuid import UUID

from pydantic import ValidationError

from tools import BackendError, BackendResult
from tools import get_cash_receipts_by_direction, get_entity_schema, get_metric_definition, resolve_dimension, search_metrics
from .client import ODataReader
from .models import CATALOGS, METRIC_ID, MOVEMENTS


ARGUMENTS = {
    "search_metrics": search_metrics.Arguments,
    "get_metric_definition": get_metric_definition.Arguments,
    "get_entity_schema": get_entity_schema.Arguments,
    "resolve_dimension": resolve_dimension.Arguments,
    "get_cash_receipts_by_direction": get_cash_receipts_by_direction.Arguments,
}
NOTICE = "Демонстрационная методика: состав поступлений, валюта и правила учёта требуют подтверждения заказчиком."


def normalized(value):
    return value.casefold().replace("ё", "е").strip()


class OneCDataService:
    """Каталог и один сценарий поступлений по направлению."""

    def __init__(self, reader: ODataReader):
        self.reader = reader
        self.settings = reader.settings
        self.catalog_lock = asyncio.Lock()
        self.cached = None
        self.cached_at = 0.0

    async def snapshot(self):
        async with self.catalog_lock:
            if self.cached is not None and time.monotonic() - self.cached_at < self.settings.onec_catalog_ttl_seconds:
                return self.cached
            schema, version = await self.reader.metadata()
            dimensions = {}
            for kind in ("organization", "direction"):
                dimensions[kind] = await self.load_dimension(kind)
            allowed = {str(UUID(key.strip())) for key in self.settings.onec_organization_ids.split(",") if key.strip()}
            if allowed:
                dimensions["organization"] = [row for row in dimensions["organization"] if row["id"] in allowed]
            self.cached = {"schema": schema, "version": version, "dimensions": dimensions}
            self.cached_at = time.monotonic()
            return self.cached

    async def load_dimension(self, kind):
        rows, _ = await self.reader.rows(CATALOGS[kind], select="Ref_Key,Description", order_by="Ref_Key", predicate="DeletionMark eq false")
        return [{"id": str(UUID(row["Ref_Key"])), "name": row["Description"]} for row in rows]

    def definition(self, snapshot):
        return {
            "metric_id": METRIC_ID,
            "name": "Поступления денежных средств по направлению",
            "description": "Сумма активных движений регистра ДвиженияДенежныхСредств с видом Поступление. Валюта не подтверждена.",
            "synonyms": ["поступления", "поступлений", "поступило", "деньги", "денег", "денежных", "направление", "направлению"],
            "entity_name": MOVEMENTS,
            "approval": "demo",
            "semantic_version": "cash-receipts-v1",
            "catalog_version": snapshot["version"],
            "supported_tools": ["get_cash_receipts_by_direction"],
            "time_kind": "period",
            "allowed_group_by": [],
            "allowed_filters": ["active", "movement_kind"],
            "required_filters": {"active": True, "movement_kind": "Поступление"},
            "unit": "ед. учёта",
            "decimal_places": 2,
            "timezone": "Europe/Moscow",
            "period_rule": "start_inclusive_end_exclusive",
            "max_rows_per_plan": self.settings.odata_max_rows,
        }

    async def bootstrap(self):
        snapshot = await self.snapshot()
        return {
            "base_id": self.settings.onec_base_id,
            "allowed_metric_ids": [METRIC_ID],
            "allowed_entity_names": sorted(snapshot["schema"]),
            "organizations": snapshot["dimensions"]["organization"],
            "directions": snapshot["dimensions"]["direction"],
            "catalog_version": snapshot["version"],
            "metric": self.definition(snapshot),
        }

    async def call(self, request):
        if request.context.base_id != self.settings.onec_base_id:
            raise BackendError("ACCESS_DENIED", "База недоступна в текущем контексте.")
        try:
            args = ARGUMENTS[request.operation].model_validate_json(json.dumps(request.arguments)).model_dump(mode="json")
        except ValidationError:
            raise BackendError("INVALID_ARGUMENTS", "Проверьте аргументы операции.") from None
        scope = request.context
        for key, allowed in (("metric_id", scope.allowed_metric_ids), ("organization_id", scope.allowed_organization_ids), ("entity_name", scope.allowed_entity_names)):
            if key in args and args[key] not in allowed:
                raise BackendError("ACCESS_DENIED", "Параметр недоступен в текущем контексте.")
        snapshot = await self.snapshot()
        definition = self.definition(snapshot)
        operation = request.operation
        if "metric_id" in args and args["metric_id"] != METRIC_ID:
            raise BackendError("METRIC_NOT_FOUND", "Метрика не поддерживается сервисом 1С.")
        if operation == "search_metrics":
            query = set(re.findall(r"\w+", normalized(args["query"])))
            labels = set(re.findall(r"\w+", normalized(" ".join([definition["name"], *definition["synonyms"]]))))
            found = query & labels and METRIC_ID in scope.allowed_metric_ids
            return BackendResult(data=[definition] if found else [], complete=True)
        if operation == "get_metric_definition":
            return BackendResult(data=definition, complete=True)
        if operation == "get_entity_schema":
            entity = args["entity_name"]
            if entity not in snapshot["schema"]:
                raise BackendError("ACCESS_DENIED", "Источник не опубликован для этого сценария.")
            return BackendResult(data={"entity_name": entity, **snapshot["schema"][entity], "expand_paths": [], "catalog_version": snapshot["version"]}, complete=True)
        if operation == "resolve_dimension":
            if CATALOGS[args["dimension"]] not in scope.allowed_entity_names:
                raise BackendError("ACCESS_DENIED", "Справочник недоступен.")
            if args["dimension"] == "counterparty":
                candidates = await self.load_dimension("counterparty")
            else:
                candidates = snapshot["dimensions"][args["dimension"]]
            if args["dimension"] == "organization":
                candidates = [row for row in candidates if row["id"] in scope.allowed_organization_ids]
            query = normalized(args["query"])
            return BackendResult(data=[row for row in candidates if query in normalized(row["name"]) or query == row["id"]], complete=True)
        if MOVEMENTS not in scope.allowed_entity_names:
            raise BackendError("ACCESS_DENIED", "Регистр недоступен.")
        if definition["approval"] != "approved" and not (definition["approval"] == "demo" and scope.allow_demo_metrics):
            raise BackendError("UNAPPROVED_METRIC", "Для этой методики требуется явный демонстрационный режим.")
        return await self.receipts(args, snapshot, max_rows=request.max_rows)

    async def receipts(self, args, snapshot, *, max_rows=None):
        try:
            organization = str(UUID(args["organization_id"]))
            direction = str(UUID(args["direction_id"]))
        except ValueError:
            raise BackendError("INVALID_ARGUMENTS", "Ожидались идентификаторы 1С в формате UUID.") from None
        for kind, key in (("organization", organization), ("direction", direction)):
            if key not in {row["id"] for row in snapshot["dimensions"][kind]}:
                raise BackendError("ACCESS_DENIED", "Организация или направление недоступны.")
        start = datetime.fromisoformat(args["period"]["start"])
        end = datetime.fromisoformat(args["period"]["end_exclusive"])
        if (end - start).days > 366:
            raise BackendError("INVALID_ARGUMENTS", "Период не должен превышать 366 дней.")
        for key, value in args.get("filters", {}).items():
            expected = self.definition(snapshot)["required_filters"]
            if key not in expected or type(value) is not type(expected[key]) or value != expected[key]:
                raise BackendError("INVALID_ARGUMENTS", "Фильтры метрики нельзя переопределять.")
        predicate = (
            f"Period ge datetime'{start.isoformat()}' and Period lt datetime'{end.isoformat()}' "
            f"and Организация_Key eq guid'{organization}' and НаправлениеДеятельности_Key eq guid'{direction}' "
            "and Active eq true and ВидДвиженияДС eq 'Поступление'"
        )
        keys = snapshot["schema"][MOVEMENTS]["keys"]
        fields = list(dict.fromkeys(["Period", "Active", "Организация_Key", "НаправлениеДеятельности_Key", "ВидДвиженияДС", "Сумма", *keys]))
        rows, pages = await self.reader.rows(MOVEMENTS, select=",".join(fields), order_by=",".join(keys), predicate=predicate, max_rows=max_rows)
        total = Decimal(0)
        identities = set()
        with localcontext() as decimal_context:
            decimal_context.prec = 50
            for row in rows:
                try:
                    identity = tuple(row[key] for key in keys)
                    point = datetime.fromisoformat(row["Period"])
                    if identity in identities or not start <= point < end or row["Active"] is not True or row["Организация_Key"] != organization or row["НаправлениеДеятельности_Key"] != direction or row["ВидДвиженияДС"] != "Поступление":
                        raise ValueError
                    value = row["Сумма"]
                    if isinstance(value, bool) or value is None:
                        raise ValueError
                    amount = Decimal(str(value))
                    if not amount.is_finite() or abs(amount) >= Decimal("1e28"):
                        raise ValueError
                    total += amount
                    identities.add(identity)
                except (KeyError, TypeError, ValueError, InvalidOperation):
                    raise BackendError("INVALID_API_RESPONSE", "Данные 1С не прошли проверку периода, фильтров или сумм.") from None
            value = format(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f") if rows else None
        sources = [{
            "kind": "odata", "base_id": self.settings.onec_base_id, "entity": MOVEMENTS,
            "organization_id": organization, "direction_id": direction,
            "period": args["period"], "mandatory_filters": self.definition(snapshot)["required_filters"],
            "semantic_version": "cash-receipts-v1", "catalog_version": snapshot["version"],
            "aggregation": "Сумма поля Сумма по всем полученным активным движениям поступлений.",
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
        }]
        warnings = [NOTICE, "Даты трактуются как даты учёта 1С в Europe/Moscow; согласованность страниц при изменении базы не гарантируется."]
        if not rows:
            warnings.append("Выборка пуста; отсутствие данных не подтверждает нулевой итог.")
        return BackendResult(
            data={"rows": [{"organization_id": organization, "direction_id": direction, "value": value}] if rows else [], "total": value, "unit": "ед. учёта"},
            sources=sources, warnings=warnings, stats={"rows": len(rows), "pages": pages}, complete=True,
        )
