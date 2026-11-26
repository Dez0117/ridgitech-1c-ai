import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import random
import ssl
import time
import xml.etree.ElementTree as ET
from decimal import Decimal
from urllib.parse import quote, urlencode

import httpx

from tools import BackendError
from .config import DataSettings
from .models import CATALOGS, MOVEMENTS
from .observability import current_trace, log_event


@dataclass
class DataPage:
    rows: list[dict]
    number: int
    truncated: bool = False


class ODataReader:
    """Ограниченное чтение опубликованных наборов через GET."""

    def __init__(self, settings: DataSettings, *, transport=None):
        self.settings = settings
        self.http = httpx.AsyncClient(
            auth=httpx.BasicAuth(settings.odata_username, settings.odata_password.get_secret_value()),
            timeout=settings.odata_timeout_seconds, follow_redirects=False, transport=transport,
            limits=httpx.Limits(max_connections=settings.odata_max_connections,
                               max_keepalive_connections=settings.odata_max_connections),
        )
        self.lock = asyncio.Lock()
        self.last_call = 0.0
        self.semaphore = asyncio.Semaphore(settings.odata_max_connections)

    async def close(self):
        await self.http.aclose()

    def timeout(self, remaining):
        def value(kind):
            configured = getattr(self.settings, f"odata_{kind}_timeout_seconds") or self.settings.odata_timeout_seconds
            return max(0.001, min(configured, remaining))
        return httpx.Timeout(connect=value("connect"), read=value("read"), write=value("write"), pool=value("pool"))

    async def throttle(self):
        async with self.lock:
            delay = 1 / self.settings.odata_requests_per_second - (time.monotonic() - self.last_call)
            if delay > 0:
                await asyncio.sleep(delay)
            self.last_call = time.monotonic()

    @asynccontextmanager
    async def connection_slot(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        configured = self.settings.odata_pool_timeout_seconds or self.settings.odata_timeout_seconds
        try:
            async with asyncio.timeout(min(configured, remaining)):
                await self.semaphore.acquire()
        except TimeoutError:
            if time.monotonic() >= deadline:
                raise
            raise httpx.PoolTimeout("Истекло время ожидания свободного соединения.") from None
        try:
            yield
        finally:
            self.semaphore.release()

    @staticmethod
    def certificate_error(exc):
        seen = set()
        while exc is not None and id(exc) not in seen:
            if isinstance(exc, ssl.SSLCertVerificationError):
                return True
            seen.add(id(exc))
            exc = exc.__cause__ or exc.__context__
        return False

    def retry_delay(self, attempt, response):
        base = self.settings.odata_retry_backoff_seconds * 2 ** (attempt - 1)
        delay = base + random.uniform(0, base / 4)
        if response is not None and response.headers.get("Retry-After"):
            value = response.headers["Retry-After"]
            try:
                seconds = float(value)
            except ValueError:
                try:
                    point = parsedate_to_datetime(value)
                    seconds = (point - datetime.now(timezone.utc)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    seconds = 0
            if seconds >= 0 and seconds < float("inf"):
                delay = max(delay, seconds)
        return delay

    async def get(self, path, parameters=None, *, page=None):
        if path not in {"$metadata", MOVEMENTS, *CATALOGS.values()}:
            raise BackendError("ACCESS_DENIED", "Источник отсутствует в разрешённом списке.")
        url = self.settings.odata_base_url + "/" + quote(path, safe="$")
        if parameters:
            # Пробелы кодируются как %20: этот экземпляр 1С некорректно обрабатывает знак +.
            url += "?" + urlencode(parameters, quote_via=quote)
        trace = current_trace.get()
        deadline = min(time.monotonic() + 60, trace.deadline) if trace else time.monotonic() + 60
        state = {"attempts": 0, "stage": "queue"}
        try:
            async with asyncio.timeout(max(0, deadline - time.monotonic())):
                return await self.request(url, path, page, deadline, state)
        except TimeoutError:
            log_event("odata_deadline", entity=path, page=page, **state)
            raise BackendError("TIMEOUT", "Истекло общее время обращения к 1С.", True,
                               details={"stage": "deadline", "error_type": "DeadlineExceeded", "attempts": state["attempts"]}) from None

    async def request(self, url, path, page, deadline, state):
        trace = current_trace.get()
        for attempt in range(1, self.settings.odata_max_retries + 2):
            started = time.monotonic()
            sent_at = None
            response = None
            phases = {}
            phase_starts = {}
            state["stage"] = "queue"

            async def network_trace(name, _info):
                phase = ("connect" if "connect_tcp" in name else "tls" if "start_tls" in name
                         else "read" if "receive_response_headers" in name else None)
                if phase:
                    if name.endswith(".started"):
                        state["stage"] = phase
                        phase_starts[phase] = time.monotonic()
                    elif phase in phase_starts:
                        phases[f"{phase}_ms"] = round((time.monotonic() - phase_starts[phase]) * 1000, 3)

            try:
                async with self.connection_slot(deadline):
                    await self.throttle()
                    state["attempts"] = attempt
                    sent_at = time.monotonic()
                    if trace:
                        trace.metrics["upstream_requests"] += 1
                        trace.metrics["upstream_retries"] += int(attempt > 1)
                    response = await self.http.get(url, timeout=self.timeout(deadline - sent_at), extensions={"trace": network_trace})
                if response.is_success:
                    log_event("odata_request", entity=path, page=page, attempt=attempt, status=response.status_code,
                              outcome="ok", elapsed_ms=round((time.monotonic() - started) * 1000, 3), **phases)
                    return response
                status = response.status_code
                retry = status in {429, 502, 503, 504}
                error = BackendError("ODATA_AUTH" if status in {401, 403} else "ODATA_ERROR", "1С отклонила запрос к данным.", retry,
                                     details={"stage": "http", "upstream_status": status, "attempts": attempt})
            except httpx.TimeoutException as exc:
                kind = type(exc).__name__
                stage = (state["stage"] if state["stage"] in {"connect", "tls"} else "connect") if isinstance(exc, httpx.ConnectTimeout) else {
                    "ReadTimeout": "read", "WriteTimeout": "write", "PoolTimeout": "pool"}.get(kind, "network")
                retry = isinstance(exc, httpx.ConnectTimeout)
                error = BackendError("TIMEOUT", "1С не ответила за отведённое время.", True,
                                     details={"stage": stage, "error_type": kind, "attempts": state["attempts"]})
            except httpx.RequestError as exc:
                certificate = self.certificate_error(exc)
                retry = isinstance(exc, httpx.ConnectError) and not certificate
                error = BackendError("API_UNAVAILABLE", "Не удалось подключиться к 1С.", retry,
                                     details={"stage": "tls" if certificate else "network", "error_type": type(exc).__name__, "attempts": attempt})
            except asyncio.CancelledError:
                log_event("odata_request", entity=path, page=page, attempt=attempt, outcome="cancelled", stage=state["stage"],
                          elapsed_ms=round((time.monotonic() - started) * 1000, 3))
                raise
            finally:
                if trace and sent_at is not None:
                    trace.metrics["upstream_ms"] += round((time.monotonic() - sent_at) * 1000, 3)
            log_event("odata_request", entity=path, page=page, attempt=attempt, outcome="error",
                      error_code=error.code, elapsed_ms=round((time.monotonic() - started) * 1000, 3), **error.details, **phases)
            if not retry or attempt > self.settings.odata_max_retries:
                raise error
            delay = self.retry_delay(attempt, response)
            if delay >= deadline - time.monotonic():
                raise error
            log_event("odata_retry", entity=path, page=page, attempt=attempt, delay_seconds=round(delay, 3))
            await asyncio.sleep(delay)

    async def discover_metadata(self):
        response = await self.get("$metadata")
        if response.headers.get("dataserviceversion", "").split(";")[0] != "3.0":
            raise BackendError("INVALID_SCHEMA", "Ожидался OData версии 3.0.")
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError:
            raise BackendError("INVALID_SCHEMA", "Не удалось разобрать метаданные 1С.") from None
        types = {}
        sets = {}
        for item in root.iter():
            tag = item.tag.rsplit("}", 1)[-1]
            if tag == "EntityType":
                types[item.attrib["Name"]] = {
                    "fields": {p.attrib["Name"]: p.attrib["Type"] for p in item if p.tag.endswith("}Property")},
                    "keys": [p.attrib["Name"] for p in item.iter() if p.tag.endswith("}PropertyRef")],
                    "navigation": {p.attrib["Name"]: {key: value for key, value in p.attrib.items() if key != "Name"}
                                   for p in item if p.tag.endswith("}NavigationProperty")},
                }
            elif tag == "EntitySet":
                sets[item.attrib["Name"]] = item.attrib["EntityType"].split(".")[-1]
        catalog = {name: types[typ] for name, typ in sets.items() if typ in types}
        return catalog, hashlib.sha256(response.content).hexdigest()[:16]

    @staticmethod
    def validate_metadata(catalog):
        required = {"Period", "Active", "Организация_Key", "НаправлениеДеятельности_Key", "ВидДвиженияДС", "Сумма"}
        mandatory = {MOVEMENTS, CATALOGS["organization"], CATALOGS["direction"]}
        if not mandatory <= catalog.keys() or not required <= catalog[MOVEMENTS]["fields"].keys() or not catalog[MOVEMENTS]["keys"]:
            raise BackendError("INVALID_SCHEMA", "В публикации 1С отсутствуют необходимые источники или поля.")
        expected = {"Period": "Edm.DateTime", "Active": "Edm.Boolean", "Организация_Key": "Edm.Guid",
                    "НаправлениеДеятельности_Key": "Edm.Guid", "ВидДвиженияДС": "Edm.String"}
        if any(catalog[MOVEMENTS]["fields"].get(name) != kind for name, kind in expected.items()) or catalog[MOVEMENTS]["fields"]["Сумма"] not in {"Edm.Decimal", "Edm.Double", "Edm.Single"}:
            raise BackendError("INVALID_SCHEMA", "Типы полей регистра не соответствуют методике поступлений.")
        for entity in (CATALOGS["organization"], CATALOGS["direction"]):
            if any(catalog[entity]["fields"].get(name) != kind for name, kind in {"Ref_Key": "Edm.Guid", "Description": "Edm.String", "DeletionMark": "Edm.Boolean"}.items()):
                raise BackendError("INVALID_SCHEMA", "В обязательном справочнике отсутствуют нужные поля или типы.")

    async def metadata(self):
        catalog, version = await self.discover_metadata()
        exposed = {name: entity for name, entity in catalog.items() if name in {MOVEMENTS, *CATALOGS.values()}}
        self.validate_metadata(exposed)
        return exposed, version

    async def rows(self, entity, *, select, order_by, predicate=None, max_rows=None):
        result, pages = [], 0
        async for page in self.iter_pages(entity, select=select, order_by=order_by, predicate=predicate, max_rows=max_rows):
            result.extend(page.rows)
            pages = page.number
        return result, pages

    async def iter_pages(self, entity, *, select, order_by, predicate=None, max_rows=None, truncate=False, page_size=None):
        limit = self.settings.odata_max_rows if max_rows is None else min(self.settings.odata_max_rows, max_rows)
        if type(limit) is not int or limit < 1:
            raise BackendError("ROW_LIMIT", "Исчерпан бюджет строк плана.")
        size = self.settings.odata_page_size if page_size is None else min(page_size, self.settings.odata_page_size)
        if type(size) is not int or size < 1:
            raise BackendError("INVALID_ARGUMENTS", "Некорректный размер страницы.")
        count = 0
        seen = set()
        pages = 0
        while True:
            top = min(size, limit - count + 1)
            params = {"$format": "json", "$select": select, "$orderby": order_by, "$top": top, "$skip": count}
            if predicate:
                params["$filter"] = predicate
            response = await self.get(entity, params, page=pages + 1)
            pages += 1
            try:
                payload = json.loads(response.content, parse_float=Decimal)
                batch = payload["value"]
                if not isinstance(batch, list) or any(not isinstance(item, dict) for item in batch) or len(batch) > top:
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                raise BackendError("INVALID_API_RESPONSE", "1С вернула некорректную страницу данных.") from None
            truncated = count + len(batch) > limit
            if truncated and not truncate:
                raise BackendError("ROW_LIMIT", "Превышен лимит выгрузки; сузьте период.")
            if payload.get("odata.nextLink") or payload.get("@odata.nextLink"):
                raise BackendError("INCOMPLETE_DATA", "1С потребовала неподдерживаемый способ продолжения выборки.")
            # Повтор страницы не должен приводить к повторному учёту денежных движений.
            fingerprint = hashlib.sha256(response.content).digest()
            if batch and fingerprint in seen:
                raise BackendError("INCOMPLETE_DATA", "При выгрузке 1С повторила страницу данных.")
            seen.add(fingerprint)
            kept = batch[:limit - count] if truncated else batch
            count += len(kept)
            yield DataPage(kept, pages, truncated)
            if truncated:
                return
            if len(batch) < top:
                return
