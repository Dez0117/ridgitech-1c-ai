import asyncio
from contextlib import aclosing
import json
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from uuid import UUID, uuid4

from pydantic import ValidationError

from tools import BackendError, BackendResult
from tools import get_cash_receipts_by_direction, get_entity_schema, get_metric_definition, resolve_dimension, search_metrics
from .client import ODataReader
from .models import CATALOGS, ENTITIES, METRIC_ID, MOVEMENTS
from .observability import current_trace, log_event, request_scope
from .metadata_state import MetadataState, schema_diff
from .query import Condition, EntityQuery, Ordering, compile_query


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
        self.metadata_lock = asyncio.Lock()
        self.cached_metadata = None
        self.metadata_cached_at = 0.0
        self.metadata_error = None
        self.metadata_state = MetadataState(self.settings)
        self.observed_metadata = self.metadata_state.load()

    async def metadata(self, *, force_refresh=False):
        async with self.metadata_lock:
            if not force_refresh and self.metadata_error is None and self.cached_metadata is not None and time.monotonic() - self.metadata_cached_at < self.settings.onec_metadata_ttl_seconds:
                return self.cached_metadata
            discovered, version = await self.reader.discover_metadata()
            exposed = {name: entity for name, entity in discovered.items() if name in ENTITIES}
            validation_error = None
            try:
                self.reader.validate_metadata(exposed)
            except BackendError as exc:
                validation_error = exc
            accepted = validation_error is None
            previous = self.observed_metadata
            if previous is None or previous["version"] != version or previous["accepted"] != accepted:
                event = {"timestamp": datetime.now(timezone.utc).isoformat(),
                         "event": "metadata_changed" if previous else "metadata_initialized",
                         "previous_version": previous["version"] if previous else None, "catalog_version": version,
                         "accepted": accepted, "diff": schema_diff(previous["schema"], discovered) if previous else None}
                try:
                    self.metadata_state.record(discovered, version, accepted, event)
                except BackendError as exc:
                    self.metadata_error = exc
                    raise
                self.observed_metadata = {"schema": discovered, "version": version, "accepted": accepted}
                log_event(event["event"], **{key: value for key, value in event.items() if key not in {"event", "timestamp"}})
            self.metadata_error = validation_error
            if validation_error:
                log_event("metadata_rejected", catalog_version=version, error_code=validation_error.code)
                raise validation_error
            result = exposed, version
            self.cached_metadata, self.metadata_cached_at = result, time.monotonic()
            log_event("metadata_refreshed", catalog_version=result[1])
            return result

    async def refresh_metadata_loop(self):
        while True:
            interval = self.settings.onec_metadata_refresh_seconds
            if interval <= 0:
                return
            await asyncio.sleep(interval)
            with request_scope("metadata-refresh-" + uuid4().hex, 60):
                try:
                    async with asyncio.timeout(60):
                        await self.snapshot(force_refresh=True)
                except BackendError as exc:
                    log_event("metadata_refresh_failed", error_code=exc.code, **exc.details)
                except TimeoutError:
                    log_event("metadata_refresh_failed", error_code="TIMEOUT", stage="deadline")
                except Exception as exc:
                    log_event("metadata_refresh_failed", error_type=type(exc).__name__)

    async def snapshot(self, *, force_refresh=False):
        schema, version = await self.metadata(force_refresh=force_refresh)
        async with self.catalog_lock:
            if not force_refresh and self.cached is not None and self.cached["version"] == version and time.monotonic() - self.cached_at < self.settings.onec_catalog_ttl_seconds:
                return self.cached
            dimensions = {}
            for kind in ("organization", "direction"):
                dimensions[kind] = await self.load_dimension(kind, schema)
            allowed = {str(UUID(key.strip())) for key in self.settings.onec_organization_ids.split(",") if key.strip()}
            if allowed:
                dimensions["organization"] = [row for row in dimensions["organization"] if row["id"] in allowed]
            self.cached = {"schema": schema, "version": version, "dimensions": dimensions}
            self.cached_at = time.monotonic()
            return self.cached

    async def load_dimension(self, kind, schema):
        query = compile_query(EntityQuery(entity_name=CATALOGS[kind], select=["Ref_Key", "Description", "DeletionMark"],
                              filters=[Condition(field="DeletionMark", value=False)], order_by=[Ordering(field="Ref_Key")]), schema)
        rows, _ = await self.reader.rows(query.entity_name, **query.reader_arguments())
        return [self.dimension_row(row) for row in rows]

    @staticmethod
    def dimension_row(row):
        try:
            identifier = str(UUID(row["Ref_Key"]))
            name = row["Description"]
            if not isinstance(name, str) or not name.strip() or row["DeletionMark"] is not False:
                raise ValueError
            return {"id": identifier, "name": name}
        except (KeyError, TypeError, ValueError):
            raise BackendError("INVALID_API_RESPONSE", "Запись справочника 1С не прошла проверку.") from None

    async def search_counterparties(self, query, snapshot, scope):
        target = normalized(query)
        filters = [Condition(field="DeletionMark", value=False)]
        alternatives = []
        try:
            condition = Condition(field="Ref_Key", value=str(UUID(query)))
            filters.append(condition)
        except ValueError:
            condition = None
            # Фильтр даёт надмножество совпадений е/ё без неподдерживаемых вложенных функций 1С.
            chunks = [part for part in target.split("е") if part]
            if chunks:
                filters.append(Condition(field="Description", operator="contains", value=max(chunks, key=len)))
            else:
                alternatives = [Condition(field="Description", operator="contains", value=letter) for letter in ("е", "ё")]
        limit = min(self.settings.onec_dimension_search_limit, self.settings.odata_max_rows)
        plan = EntityQuery(entity_name=CATALOGS["counterparty"], select=["Ref_Key", "Description", "DeletionMark"],
                           filters=filters, any_of=alternatives, limit=self.settings.odata_max_rows)
        compiled = compile_query(plan, snapshot["schema"], allowed_entities=frozenset(scope.allowed_entity_names))
        candidates, truncated, pages, seen, scanned = [], False, 0, set(), 0
        async with aclosing(self.reader.iter_pages(compiled.entity_name, **compiled.reader_arguments(), truncate=True, page_size=limit + 1)) as source:
            async for page in source:
                pages, scanned = page.number, scanned + len(page.rows)
                for row in page.rows:
                    candidate = self.dimension_row(row)
                    if candidate["id"] in seen:
                        raise BackendError("INVALID_API_RESPONSE", "Ответ справочника содержит повторяющиеся идентификаторы.")
                    seen.add(candidate["id"])
                    matches = candidate["id"] == condition.value if condition else target in normalized(candidate["name"])
                    if condition and not matches:
                        raise BackendError("INVALID_API_RESPONSE", "Ответ справочника не соответствует UUID поиска.")
                    if matches:
                        candidates.append(candidate)
                    if len(candidates) > limit:
                        truncated = True
                        break
                truncated = truncated or page.truncated
                if truncated:
                    break
        candidates = candidates[:limit]
        warnings = ["Поиск неполный: уточните название или UUID контрагента; показанный кандидат может быть не единственным."] if truncated else []
        return BackendResult(data=candidates, warnings=warnings,
                             stats={"rows": len(candidates), "pages": pages, "scanned_rows": scanned, "truncated": int(truncated)}, complete=not truncated)

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
            return BackendResult(data={"entity_name": entity, **snapshot["schema"][entity], "expand_paths": [],
                "catalog_version": snapshot["version"], "query_plan_schema": EntityQuery.model_json_schema(),
                "query_execution": "Структурированный план проверяется слоем данных; расчёты доступны через согласованные инструменты метрик."}, complete=True)
        if operation == "resolve_dimension":
            if CATALOGS[args["dimension"]] not in scope.allowed_entity_names:
                raise BackendError("ACCESS_DENIED", "Справочник недоступен.")
            query = args["query"].strip()
            if not query:
                raise BackendError("INVALID_ARGUMENTS", "Укажите название или UUID для поиска.")
            if args["dimension"] == "counterparty":
                return await self.search_counterparties(query, snapshot, scope)
            else:
                candidates = snapshot["dimensions"][args["dimension"]]
            if args["dimension"] == "organization":
                candidates = [row for row in candidates if row["id"] in scope.allowed_organization_ids]
            query = normalized(query)
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
        keys = snapshot["schema"][MOVEMENTS]["keys"]
        fields = list(dict.fromkeys(["Period", "Active", "Организация_Key", "НаправлениеДеятельности_Key", "ВидДвиженияДС", "Сумма", *keys]))
        plan = EntityQuery(entity_name=MOVEMENTS, select=fields, limit=max_rows, sum_field="Сумма", filters=[
            Condition(field="Period", operator="ge", value=start.isoformat()),
            Condition(field="Period", operator="lt", value=end.isoformat()),
            Condition(field="Организация_Key", value=organization), Condition(field="НаправлениеДеятельности_Key", value=direction),
            Condition(field="Active", value=True), Condition(field="ВидДвиженияДС", value="Поступление"),
        ])
        compiled = compile_query(plan, snapshot["schema"])
        total = Decimal(0)
        identities = set()
        count, pages = 0, 0
        with localcontext() as decimal_context:
            decimal_context.prec = 50
            async for page in self.reader.iter_pages(compiled.entity_name, **compiled.reader_arguments()):
                pages = page.number
                for row in page.rows:
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
                        count += 1
                    except (KeyError, TypeError, ValueError, InvalidOperation):
                        raise BackendError("INVALID_API_RESPONSE", "Данные 1С не прошли проверку периода, фильтров или сумм.") from None
            value = format(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f") if count else None
        sources = [{
            "kind": "odata", "base_id": self.settings.onec_base_id, "entity": MOVEMENTS,
            "organization_id": organization, "direction_id": direction,
            "period": args["period"], "mandatory_filters": self.definition(snapshot)["required_filters"],
            "semantic_version": "cash-receipts-v1", "catalog_version": snapshot["version"],
            "aggregation": "Сумма поля Сумма по всем полученным активным движениям поступлений.",
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
        }]
        warnings = [NOTICE, "Даты трактуются как даты учёта 1С в Europe/Moscow; согласованность страниц при изменении базы не гарантируется."]
        if not count:
            warnings.append("Выборка пуста; отсутствие данных не подтверждает нулевой итог.")
        trace = current_trace.get()
        stats = {"rows": count, "pages": pages}
        if trace:
            stats.update(trace.metrics)
            stats["duration_ms"] = round((time.monotonic() - trace.started) * 1000, 3)
        return BackendResult(
            data={"rows": [{"organization_id": organization, "direction_id": direction, "value": value}] if count else [], "total": value, "unit": "ед. учёта"},
            sources=sources, warnings=warnings, stats=stats, complete=True,
        )
