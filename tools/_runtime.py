import asyncio
import copy
import json
import time
from dataclasses import dataclass, replace
from typing import Protocol

from pydantic import ValidationError

from ._models import BackendResult, MetricPolicy


@dataclass(frozen=True)
class ToolContext:
    request_id: str
    base_id: str
    allowed_metric_ids: frozenset[str]
    allowed_organization_ids: frozenset[str]
    allowed_entity_names: frozenset[str]
    timeout_seconds: float = 60.0
    max_calls: int = 24
    allow_demo_metrics: bool = False
    remaining_rows: int | None = None

    def __post_init__(self):
        if not self.request_id or not self.base_id:
            raise ValueError("Request and base IDs are required")
        if not 0 < self.timeout_seconds <= 60 or not 1 <= self.max_calls <= 100:
            raise ValueError("Invalid execution limits")
        if self.remaining_rows is not None and (type(self.remaining_rows) is not int or self.remaining_rows < 1):
            raise ValueError("Остаток бюджета строк должен быть положительным целым числом")


class BackendError(Exception):
    """Безопасная ошибка адаптера без учётных данных."""

    def __init__(self, code, message, retryable=False, *, details=None):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable
        self.details = details or {}


class AnalyticsBackend(Protocol):
    async def call(self, operation: str, arguments: dict, *, context: ToolContext, timeout: float) -> BackendResult:
        """Вызвать настроенный API аналитики и привести ответ к общему контракту.

        Применить доверенную область доступа, остаток бюджета строк и тайм-аут.
        Сохранить точные десятичные строки. Преобразовать HTTP-ошибки в BackendError
        без адресов, заголовков и учётных данных.
        """
        ...


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class Runtime:
    def __init__(self, backend, context, modules):
        self.backend, self.context, self.modules = backend, context, modules
        self.deadline = time.monotonic() + context.timeout_seconds
        self.calls = 0
        self.plan = None
        self.plan_id = None
        self.steps = {}
        self.results = {}
        self.step_locks = {}
        self.plan_lock = asyncio.Lock()
        self.row_budget_lock = asyncio.Lock()
        self.plan_row_limit = None
        self.plan_rows_used = 0

    def remaining(self):
        value = self.deadline - time.monotonic()
        if value <= 0:
            raise BackendError("TIMEOUT", "Истекло время выполнения запроса.", True)
        return value

    def validate(self, module, arguments):
        return module.Arguments.model_validate_json(canonical(arguments))

    def check_scope(self, arguments):
        scope = self.context
        for field, allowed in [
            ("metric_id", scope.allowed_metric_ids),
            ("organization_id", scope.allowed_organization_ids),
            ("entity_name", scope.allowed_entity_names),
        ]:
            if field in arguments and arguments[field] not in allowed:
                raise BackendError("ACCESS_DENIED", "Параметр недоступен в текущем контексте.")

    async def read(self, operation, arguments, *, context=None):
        self.check_scope(arguments)
        async with asyncio.timeout(self.remaining()):
            response = await self.backend.call(
                operation, copy.deepcopy(arguments), context=context or self.context, timeout=self.remaining())
        try:
            result = response if isinstance(response, BackendResult) else BackendResult.model_validate(response)
            canonical(result.model_dump(mode="json"))
        except (ValidationError, ValueError, TypeError):
            raise BackendError("INVALID_API_RESPONSE", "API вернул ответ, не соответствующий контракту.")
        if operation == "get_metric_definition":
            try:
                policy = MetricPolicy.model_validate(result.data)
            except ValidationError:
                raise BackendError("INVALID_DEFINITION", "API вернул некорректное определение метрики.")
            if policy.metric_id != arguments["metric_id"]:
                raise BackendError("INVALID_DEFINITION", "API вернул определение другой метрики.")
        return result.model_dump(mode="json")

    async def business(self, operation, arguments):
        if self.plan is None:
            raise BackendError("PLAN_REQUIRED", "Сначала передайте план в submit_query_plan.")
        matching = [step for step in self.steps.values()
                    if step["tool"] == operation and step["arguments"] == arguments]
        if not matching:
            raise BackendError("PLAN_MISMATCH", "Вызов не соответствует принятому плану.")
        step = matching[0]
        step_id = step["step_id"]
        async with self.step_locks[step_id]:
            if step_id in self.results:
                return copy.deepcopy(self.results[step_id])
            if any(dep not in self.results for dep in step["depends_on"]):
                raise BackendError("DEPENDENCY_PENDING", "Сначала выполните зависимости шага.")
            if self.plan_row_limit is None:
                result = await self.read_business(operation, arguments, self.context)
            else:
                # Шаги одного плана получают бюджет по очереди, включая параллельные вызовы графа.
                async with self.row_budget_lock:
                    remaining = self.plan_row_limit - self.plan_rows_used
                    if remaining <= 0:
                        raise BackendError("ROW_LIMIT", "Исчерпан общий бюджет строк плана; сузьте период.")
                    try:
                        result = await self.read_business(operation, arguments, replace(self.context, remaining_rows=remaining))
                        rows = result["stats"].get("rows")
                        if type(rows) is not int or rows < 0:
                            raise BackendError("INVALID_API_RESPONSE", "API не сообщил корректное число обработанных строк.")
                        if rows > remaining:
                            raise BackendError("ROW_LIMIT", "Превышен общий бюджет строк плана.")
                    except BaseException:
                        # После ошибки или отмены неизвестен фактический расход выгрузки; бюджет закрывается.
                        self.plan_rows_used = self.plan_row_limit
                        raise
                    self.plan_rows_used += rows
            self.results[step_id] = copy.deepcopy(result)
            return result

    async def read_business(self, operation, arguments, context):
        result = await self.read(operation, arguments, context=context)
        if result["complete"] is not True:
            raise BackendError("INCOMPLETE_DATA", "Выборка неполная; сузьте период или фильтры.")
        if not result["sources"]:
            raise BackendError("MISSING_SOURCES", "API не указал источники результата.")
        data = result["data"]
        if not data or (isinstance(data, dict) and data.get("rows") == []):
            result["warnings"].append("Выборка пуста; отсутствие данных не подтверждает нулевой итог.")
        return result

    async def invoke(self, name, arguments):
        try:
            self.remaining()
            if self.calls >= self.context.max_calls:
                raise BackendError("TOOL_BUDGET", "Исчерпан бюджет вызовов инструментов.")
            self.calls += 1
            args = self.validate(self.modules[name], arguments)
            self.check_scope(args.model_dump(mode="json"))
            async with asyncio.timeout(self.remaining()):
                result = await self.modules[name].execute(self, args)
            return {"ok": True, "sources": [], "warnings": [], "stats": {}, **result}
        except ValidationError as exc:
            return {"ok": False, "error": {"code": "INVALID_ARGUMENTS", "message": "Проверьте типы и обязательные параметры.",
                "retryable": False, "fields": [".".join(map(str, e["loc"])) for e in exc.errors()]}}
        except BackendError as exc:
            return {"ok": False, "error": {"code": exc.code, "message": exc.message, "retryable": exc.retryable,
                    **({"details": exc.details} if exc.details else {})}}
        except TimeoutError:
            return {"ok": False, "error": {"code": "TIMEOUT", "message": "Превышено время ожидания API.", "retryable": True}}
        except OSError:
            return {"ok": False, "error": {"code": "API_UNAVAILABLE", "message": "API данных недоступен.", "retryable": True}}
        except (TypeError, ValueError):
            return {"ok": False, "error": {"code": "INVALID_DATA", "message": "Получены некорректные данные.", "retryable": False}}
