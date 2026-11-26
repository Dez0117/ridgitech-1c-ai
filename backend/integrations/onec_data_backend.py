import httpx
import re
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from tools import BackendError, BackendResult, ToolContext


class Bootstrap(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    base_id: str
    allowed_metric_ids: list[str]
    allowed_entity_names: list[str]
    organizations: list[dict[str, str]]
    directions: list[dict[str, str]]
    catalog_version: str
    metric: dict

    @model_validator(mode="after")
    def valid_scope(self):
        from uuid import UUID

        if not self.base_id or not self.catalog_version or not self.allowed_metric_ids or self.metric.get("metric_id") not in self.allowed_metric_ids:
            raise ValueError("Некорректная область доступа")
        for row in [*self.organizations, *self.directions]:
            if not row.get("name") or not row.get("id"):
                raise ValueError("Не заполнены данные справочника")
            UUID(row["id"])
        return self


class OneCDataBackend:
    """HTTP-адаптер внутреннего сервиса данных к контракту агента."""

    def __init__(self, url: str, token: str = "", *, transport=None):
        self.calls = []
        self.bootstrap_info = None
        self.http = httpx.AsyncClient(
            base_url=url.rstrip("/") + "/", follow_redirects=False,
            headers={"X-Internal-Token": token} if token else {}, transport=transport,
        )

    async def close(self):
        await self.http.aclose()

    @staticmethod
    def error_details(error, request_id, response):
        raw = error.get("details", {})
        if not isinstance(raw, dict):
            return {}
        details = {}
        if isinstance(raw.get("stage"), str) and raw["stage"] in {"connect", "tls", "read", "write", "pool", "network", "http", "deadline", "queue"}:
            details["stage"] = raw["stage"]
        if isinstance(raw.get("error_type"), str) and raw["error_type"] in {"ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout", "TimeoutException", "ConnectError", "ReadError", "WriteError", "RemoteProtocolError", "LocalProtocolError", "ProxyError", "NetworkError", "RequestError", "DeadlineExceeded"}:
            details["error_type"] = raw["error_type"]
        for key, low, high in (("attempts", 0, 4), ("upstream_status", 100, 599)):
            if type(raw.get(key)) is int and low <= raw[key] <= high:
                details[key] = raw[key]
        identifier = request_id if isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", request_id) else response.headers.get("X-Request-ID", "")
        if identifier and re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", identifier):
            details["request_id"] = identifier
        return details

    async def request(self, method, path, *, timeout, payload=None, request_id=None):
        identifier = request_id or (payload or {}).get("context", {}).get("request_id")
        headers = {"X-Request-ID": identifier} if isinstance(identifier, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", identifier) else {}
        try:
            response = await self.http.request(method, path, json=payload, timeout=timeout, headers=headers)
        except httpx.TimeoutException:
            raise BackendError("TIMEOUT", "Истекло время ожидания сервиса данных.", True) from None
        except httpx.RequestError:
            raise BackendError("API_UNAVAILABLE", "Сервис данных 1С недоступен.", True) from None
        try:
            data = response.json()
        except ValueError:
            raise BackendError("INVALID_API_RESPONSE", "Сервис данных вернул некорректный ответ.") from None
        if response.is_error:
            error = data.get("error", {}) if isinstance(data, dict) else {}
            if not isinstance(error, dict):
                error = {}
            codes = {"ACCESS_DENIED", "INVALID_ARGUMENTS", "UNAPPROVED_METRIC", "METRIC_NOT_FOUND", "ROW_LIMIT", "INCOMPLETE_DATA", "ODATA_AUTH", "ODATA_ERROR", "INVALID_SCHEMA", "INVALID_API_RESPONSE", "TIMEOUT", "API_UNAVAILABLE"}
            code = error.get("code")
            if not isinstance(code, str):
                code = None
            # Текст произвольной HTTP-ошибки не попадает в ответы и журнал агента.
            messages = {
                "ACCESS_DENIED": "Источник или организация недоступны.",
                "UNAPPROVED_METRIC": "Методика требует согласования или явного демонстрационного режима.",
                "ROW_LIMIT": "Превышен лимит данных; сузьте период.",
                "INCOMPLETE_DATA": "Получена неполная выборка.",
                "ODATA_AUTH": "Проверьте доступ сервисной учётной записи 1С.",
                "TIMEOUT": "Истекло время обращения к данным 1С.",
            }
            raise BackendError(code if code in codes else "API_UNAVAILABLE", messages.get(code, "Сервис данных 1С отклонил запрос."),
                               error.get("retryable") is True if code in codes else response.status_code >= 500,
                               details=self.error_details(error, identifier, response))
        return data

    async def bootstrap(self, timeout=60, *, request_id=None):
        data = await self.request("GET", "v1/bootstrap", timeout=timeout, request_id=request_id)
        try:
            info = Bootstrap.model_validate(data)
            self.bootstrap_info = info.model_dump(mode="json")
            return info
        except ValidationError:
            raise BackendError("INVALID_API_RESPONSE", "Некорректный каталог сервиса данных.") from None

    async def context(self, request_id, *, timeout_seconds=60, allow_demo_metrics=False):
        info = await self.bootstrap(timeout_seconds, request_id=request_id)
        return ToolContext(
            request_id=request_id, base_id=info.base_id,
            allowed_metric_ids=frozenset(info.allowed_metric_ids),
            allowed_organization_ids=frozenset(row["id"] for row in info.organizations),
            allowed_entity_names=frozenset(info.allowed_entity_names),
            timeout_seconds=timeout_seconds, allow_demo_metrics=allow_demo_metrics,
        )

    async def call(self, operation, arguments, *, context, timeout):
        if operation not in {"search_metrics", "get_metric_definition", "get_entity_schema", "resolve_dimension", "get_cash_receipts_by_direction"}:
            raise BackendError("UNSUPPORTED_OPERATION", "Операция ещё не подключена к реальной 1С.")
        self.calls.append({"operation": operation, "arguments": arguments})
        payload = {
            "operation": operation, "arguments": arguments, "timeout": min(timeout, 60),
            "max_rows": context.remaining_rows,
            "context": {
                "request_id": context.request_id, "base_id": context.base_id,
                "allowed_metric_ids": sorted(context.allowed_metric_ids),
                "allowed_organization_ids": sorted(context.allowed_organization_ids),
                "allowed_entity_names": sorted(context.allowed_entity_names),
                "allow_demo_metrics": context.allow_demo_metrics,
            },
        }
        data = await self.request("POST", "v1/call", timeout=timeout, payload=payload)
        try:
            return BackendResult.model_validate(data)
        except ValidationError:
            raise BackendError("INVALID_API_RESPONSE", "Результат 1С не соответствует контракту агента.") from None
