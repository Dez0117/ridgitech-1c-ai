import asyncio
import copy
import json
import time
from dataclasses import dataclass
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

    def __post_init__(self):
        if not self.request_id or not self.base_id:
            raise ValueError("Request and base IDs are required")
        if not 0 < self.timeout_seconds <= 60 or not 1 <= self.max_calls <= 100:
            raise ValueError("Invalid execution limits")


class BackendError(Exception):
    """A safe error translated by the backend adapter; never include credentials."""

    def __init__(self, code, message, retryable=False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable


class AnalyticsBackend(Protocol):
    async def call(self, operation: str, arguments: dict, *, context: ToolContext, timeout: float) -> BackendResult:
        """Call the configured analytics API and normalize its response.

        Apply the trusted access scope, preserve decimal strings and respect timeout.
        Map HTTP errors to BackendError without URLs, headers or credentials.
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

    async def read(self, operation, arguments):
        self.check_scope(arguments)
        async with asyncio.timeout(self.remaining()):
            response = await self.backend.call(
                operation, copy.deepcopy(arguments), context=self.context, timeout=self.remaining())
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
            result = await self.read(operation, arguments)
            if result["complete"] is not True:
                raise BackendError("INCOMPLETE_DATA", "Выборка неполная; сузьте период или фильтры.")
            if not result["sources"]:
                raise BackendError("MISSING_SOURCES", "API не указал источники результата.")
            data = result["data"]
            if not data or (isinstance(data, dict) and data.get("rows") == []):
                result["warnings"].append("Выборка пуста; отсутствие данных не подтверждает нулевой итог.")
            self.results[step_id] = copy.deepcopy(result)
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
            return {"ok": False, "error": {"code": exc.code, "message": exc.message, "retryable": exc.retryable}}
        except TimeoutError:
            return {"ok": False, "error": {"code": "TIMEOUT", "message": "Превышено время ожидания API.", "retryable": True}}
        except OSError:
            return {"ok": False, "error": {"code": "API_UNAVAILABLE", "message": "API данных недоступен.", "retryable": True}}
        except (TypeError, ValueError):
            return {"ok": False, "error": {"code": "INVALID_DATA", "message": "Получены некорректные данные.", "retryable": False}}
