import asyncio
import hashlib
import json
import time
import xml.etree.ElementTree as ET
from decimal import Decimal
from urllib.parse import quote, urlencode

import httpx

from tools import BackendError
from .config import DataSettings
from .models import CATALOGS, MOVEMENTS


class ODataReader:
    """Ограниченное чтение опубликованных наборов через GET."""

    def __init__(self, settings: DataSettings, *, transport=None):
        self.settings = settings
        self.http = httpx.AsyncClient(
            auth=httpx.BasicAuth(settings.odata_username, settings.odata_password.get_secret_value()),
            timeout=settings.odata_timeout_seconds, follow_redirects=False, transport=transport,
        )
        self.lock = asyncio.Lock()
        self.last_call = 0.0

    async def close(self):
        await self.http.aclose()

    async def get(self, path, parameters=None):
        if path not in {"$metadata", MOVEMENTS, *CATALOGS.values()}:
            raise BackendError("ACCESS_DENIED", "Источник отсутствует в разрешённом списке.")
        url = self.settings.odata_base_url + "/" + quote(path, safe="$")
        if parameters:
            # Пробелы кодируются как %20: этот экземпляр 1С некорректно обрабатывает знак +.
            url += "?" + urlencode(parameters, quote_via=quote)
        async with self.lock:
            delay = 1 / self.settings.odata_requests_per_second - (time.monotonic() - self.last_call)
            if delay > 0:
                await asyncio.sleep(delay)
            self.last_call = time.monotonic()
        try:
            response = await self.http.get(url)
            response.raise_for_status()
            return response
        except httpx.TimeoutException:
            raise BackendError("TIMEOUT", "1С не ответила за отведённое время.", True) from None
        except httpx.HTTPStatusError as exc:
            code = "ODATA_AUTH" if exc.response.status_code in {401, 403} else "ODATA_ERROR"
            raise BackendError(code, "1С отклонила запрос к данным.", exc.response.status_code >= 500) from None
        except httpx.RequestError:
            raise BackendError("API_UNAVAILABLE", "Не удалось подключиться к 1С.", True) from None

    async def metadata(self):
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
                }
            elif tag == "EntitySet":
                sets[item.attrib["Name"]] = item.attrib["EntityType"].split(".")[-1]
        catalog = {name: types[typ] for name, typ in sets.items() if name in {MOVEMENTS, *CATALOGS.values()} and typ in types}
        required = {"Period", "Active", "Организация_Key", "НаправлениеДеятельности_Key", "ВидДвиженияДС", "Сумма"}
        mandatory = {MOVEMENTS, CATALOGS["organization"], CATALOGS["direction"]}
        if not mandatory <= catalog.keys() or not required <= catalog[MOVEMENTS]["fields"].keys() or not catalog[MOVEMENTS]["keys"]:
            raise BackendError("INVALID_SCHEMA", "В публикации 1С отсутствуют необходимые источники или поля.")
        return catalog, hashlib.sha256(response.content).hexdigest()[:16]

    async def rows(self, entity, *, select, order_by, predicate=None, max_rows=None):
        limit = self.settings.odata_max_rows if max_rows is None else min(self.settings.odata_max_rows, max_rows)
        if type(limit) is not int or limit < 1:
            raise BackendError("ROW_LIMIT", "Исчерпан бюджет строк плана.")
        rows = []
        seen = set()
        pages = 0
        while True:
            top = min(self.settings.odata_page_size, limit - len(rows) + 1)
            params = {"$format": "json", "$select": select, "$orderby": order_by, "$top": top, "$skip": len(rows)}
            if predicate:
                params["$filter"] = predicate
            response = await self.get(entity, params)
            pages += 1
            try:
                payload = json.loads(response.content, parse_float=Decimal)
                batch = payload["value"]
                if not isinstance(batch, list) or any(not isinstance(item, dict) for item in batch) or len(batch) > top:
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                raise BackendError("INVALID_API_RESPONSE", "1С вернула некорректную страницу данных.") from None
            if len(rows) + len(batch) > limit:
                raise BackendError("ROW_LIMIT", "Превышен лимит выгрузки; сузьте период.")
            if payload.get("odata.nextLink") or payload.get("@odata.nextLink"):
                raise BackendError("INCOMPLETE_DATA", "1С потребовала неподдерживаемый способ продолжения выборки.")
            # Повтор страницы не должен приводить к повторному учёту денежных движений.
            fingerprint = response.content
            if batch and fingerprint in seen:
                raise BackendError("INCOMPLETE_DATA", "При выгрузке 1С повторила страницу данных.")
            seen.add(fingerprint)
            rows.extend(batch)
            if len(batch) < top:
                return rows, pages
