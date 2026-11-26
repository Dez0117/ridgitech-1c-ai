from dataclasses import replace
import asyncio
import json
import re
import time
import unittest
from unittest.mock import patch

import httpx
from pydantic import SecretStr

from backend.integrations.onec_data_backend import OneCDataBackend
from services.onec_data.client import ODataReader
from services.onec_data.config import DataSettings
from services.onec_data.main import create_app
from services.onec_data.models import CATALOGS, ENTITIES, METRIC_ID, MOVEMENTS
from services.onec_data.service import OneCDataService
from tools import BackendError, ToolContext, create_tools


ORG = "00000000-0000-0000-0000-000000000001"
DIRECTION = "00000000-0000-0000-0000-000000000002"
OTHER = "00000000-0000-0000-0000-000000000003"
XML_FIELDS = {"Ref_Key": "Edm.Guid", "Description": "Edm.String", "DeletionMark": "Edm.Boolean"}
MOVEMENT_FIELDS = {"Recorder": "Edm.String", "Recorder_Type": "Edm.String", "LineNumber": "Edm.Int64", "Period": "Edm.DateTime", "Active": "Edm.Boolean", "Организация_Key": "Edm.Guid", "НаправлениеДеятельности_Key": "Edm.Guid", "ВидДвиженияДС": "Edm.String", "Сумма": "Edm.Double"}


def metadata_xml(entities=ENTITIES):
    types, sets = [], []
    for entity in sorted(entities):
        fields = MOVEMENT_FIELDS if entity == MOVEMENTS else XML_FIELDS
        keys = ["Recorder", "Recorder_Type", "LineNumber"] if entity == MOVEMENTS else ["Ref_Key"]
        types.append(f'<EntityType Name="{entity}"><Key>' + "".join(f'<PropertyRef Name="{key}"/>' for key in keys) + '</Key>' + "".join(f'<Property Name="{name}" Type="{kind}"/>' for name, kind in fields.items()) + '</EntityType>')
        sets.append(f'<EntitySet Name="{entity}" EntityType="StandardODATA.{entity}"/>')
    return ('<Schema xmlns="http://schemas.microsoft.com/ado/2009/11/edm">' + "".join(types) + '<EntityContainer>' + "".join(sets) + '</EntityContainer></Schema>').encode()


def row(line=1, amount="0.10", **changes):
    return {"Recorder": OTHER, "Recorder_Type": "StandardODATA.Document_ПоступлениеНаРасчетныйСчет", "LineNumber": line, "Period": "2025-01-15T12:00:00", "Active": True, "Организация_Key": ORG, "НаправлениеДеятельности_Key": DIRECTION, "ВидДвиженияДС": "Поступление", "Сумма": amount, **changes}


class OneCDataTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.rows = [row(1, "0.10"), row(2, "0.20")]
        self.rows_by_period = {}
        self.counterparties = [{"Ref_Key": OTHER, "Description": "Контрагент"}]
        self.metadata_entities = ENTITIES
        self.requests = []
        settings = DataSettings(odata_base_url="https://onec.invalid/odata", odata_username="reader", odata_password=SecretStr("test-password"), odata_page_size=1, odata_requests_per_second=50)
        self.reader = ODataReader(settings, transport=httpx.MockTransport(self.upstream))
        self.service = OneCDataService(self.reader)
        self.app = create_app(self.service)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.backend = OneCDataBackend("http://data.invalid", transport=httpx.ASGITransport(app=self.app))
        self.context = ToolContext("test", "itmo", frozenset({METRIC_ID}), frozenset({ORG}), ENTITIES, allow_demo_metrics=True)
        self.args = {"metric_id": METRIC_ID, "organization_id": ORG, "direction_id": DIRECTION, "period": {"start": "2025-01-01", "end_exclusive": "2025-02-01"}, "filters": {}}

    def upstream(self, request):
        self.requests.append(request)
        self.assertEqual(request.method, "GET")
        if request.url.path.endswith("$metadata"):
            return httpx.Response(200, content=metadata_xml(self.metadata_entities), headers={"DataServiceVersion": "3.0"})
        if request.url.path.endswith(MOVEMENTS):
            self.assertNotIn(b"+", request.url.query)
            predicate = request.url.params["$filter"]
            self.assertIn("Period lt datetime'", predicate)
            self.assertIn("Active eq true", predicate)
            start = predicate.split("Period ge datetime'", 1)[1][:10]
            data = self.rows_by_period.get(start, self.rows)
        else:
            ids = {CATALOGS["organization"]: ORG, CATALOGS["direction"]: DIRECTION, CATALOGS["counterparty"]: OTHER}
            entity = request.url.path.split("/")[-1]
            data = self.counterparties if entity == CATALOGS["counterparty"] else [{"Ref_Key": ids[entity], "Description": "Разработка ПО" if entity == CATALOGS["direction"] else "Организация"}]
            if entity == CATALOGS["counterparty"]:
                predicate = request.url.params["$filter"]
                found = re.search(r"substringof\('((?:[^']|'')*)',Description\)", predicate)
                if found:
                    query = found.group(1).replace("''", "'")
                    data = [item for item in data if query in item["Description"].lower()]
                identifier = re.search(r"Ref_Key eq guid'([^']+)'", predicate)
                if identifier:
                    data = [item for item in data if item["Ref_Key"] == identifier.group(1)]
            data = [{"DeletionMark": False, **item} for item in data]
        skip = int(request.url.params["$skip"])
        top = int(request.url.params["$top"])
        return httpx.Response(200, json={"value": data[skip:skip + top]})

    async def asyncTearDown(self):
        await self.backend.close()
        await self.lifespan.__aexit__(None, None, None)
        await self.reader.close()

    async def invoke(self, args=None, context=None):
        return await self.backend.call("get_cash_receipts_by_direction", args or self.args, context=context or self.context, timeout=30)

    async def accepted_two_period_plan(self):
        info = await self.backend.bootstrap()
        second = {**self.args, "period": {"start": "2025-02-01", "end_exclusive": "2025-03-01"}}
        plan = {
            "metric_id": METRIC_ID, "organization_id": ORG,
            "semantic_version": info.metric["semantic_version"], "catalog_version": info.catalog_version,
            "steps": [
                {"step_id": "first", "tool": "get_cash_receipts_by_direction", "arguments": self.args},
                {"step_id": "second", "tool": "get_cash_receipts_by_direction", "arguments": second},
            ],
        }
        tools = {tool.name: tool for tool in create_tools(self.backend, self.context)}
        accepted = await tools["submit_query_plan"].ainvoke({"plan": plan})
        self.assertTrue(accepted["ok"], accepted)
        return tools["get_cash_receipts_by_direction"], second

    async def test_http_adapter_paginates_and_returns_exact_decimal(self):
        result = await self.invoke()
        self.assertEqual(result.data["total"], "0.30")
        self.assertTrue(result.complete)
        self.assertEqual(result.stats["rows"], 2)
        self.assertEqual(result.stats["pages"], 3)
        self.assertEqual(result.sources[0]["entity"], MOVEMENTS)
        self.assertNotEqual(result.data["unit"], "RUB")

    async def test_discovery_bootstrap_and_schema_use_real_metadata(self):
        info = await self.backend.bootstrap()
        self.assertEqual(info.organizations[0]["id"], ORG)
        for operation, args in (("search_metrics", {"query": "поступления"}), ("resolve_dimension", {"dimension": "direction", "query": "Разработка ПО"}), ("get_entity_schema", {"entity_name": MOVEMENTS}), ("get_metric_definition", {"metric_id": METRIC_ID})):
            result = await self.backend.call(operation, args, context=self.context, timeout=30)
            self.assertTrue(result.data)

    async def test_large_counterparty_catalog_is_loaded_only_on_request(self):
        self.service.settings.odata_max_rows = 2
        self.counterparties = [{"Ref_Key": f"00000000-0000-0000-0000-{number:012d}", "Description": f"Контрагент {number}"} for number in range(3, 6)]
        await self.backend.bootstrap()
        self.assertEqual((await self.invoke()).data["total"], "0.30")
        self.assertFalse(any(request.url.path.endswith(CATALOGS["counterparty"]) for request in self.requests))
        result = await self.backend.call("resolve_dimension", {"dimension": "counterparty", "query": "Контрагент"}, context=self.context, timeout=30)
        self.assertEqual(len(result.data), 2)
        self.assertEqual(result.stats["truncated"], 1)
        self.assertTrue(result.warnings)
        self.assertTrue(any(request.url.path.endswith(CATALOGS["counterparty"]) for request in self.requests))
        self.assertEqual((await self.invoke()).data["total"], "0.30")

    async def test_unpublished_counterparties_do_not_block_bootstrap(self):
        self.metadata_entities = ENTITIES - {CATALOGS["counterparty"]}
        info = await self.backend.bootstrap()
        self.assertNotIn(CATALOGS["counterparty"], info.allowed_entity_names)
        self.assertEqual((await self.invoke()).data["total"], "0.30")

    async def test_sequential_steps_receive_remaining_plan_budget(self):
        self.service.settings.odata_max_rows = 2
        self.rows = [row()]
        self.rows_by_period["2025-02-01"] = [row(3, Period="2025-02-15T12:00:00"), row(4, Period="2025-02-16T12:00:00")]
        tool, second = await self.accepted_two_period_plan()
        with patch.object(self.reader, "iter_pages", wraps=self.reader.iter_pages) as reads:
            first_result = await tool.ainvoke(self.args)
            second_result = await tool.ainvoke(second)
        self.assertTrue(first_result["ok"])
        self.assertEqual(second_result["error"]["code"], "ROW_LIMIT")
        self.assertNotIn("data", second_result)
        self.assertEqual([call.kwargs["max_rows"] for call in reads.call_args_list], [2, 1])

    async def test_parallel_steps_cannot_multiply_plan_budget(self):
        self.service.settings.odata_max_rows = 2
        self.rows_by_period["2025-02-01"] = [row(3, Period="2025-02-15T12:00:00"), row(4, Period="2025-02-16T12:00:00")]
        tool, second = await self.accepted_two_period_plan()
        with patch.object(self.reader, "iter_pages", wraps=self.reader.iter_pages) as reads:
            results = await asyncio.gather(tool.ainvoke(self.args), tool.ainvoke(second))
        self.assertEqual(sum(result["ok"] for result in results), 1)
        self.assertEqual(next(result for result in results if not result["ok"])["error"]["code"], "ROW_LIMIT")
        self.assertEqual(reads.call_count, 1)
        self.assertEqual(reads.call_args.kwargs["max_rows"], 2)

    async def test_parallel_steps_and_duplicate_calls_share_budget(self):
        self.service.settings.odata_max_rows = 3
        self.rows = [row()]
        self.rows_by_period["2025-02-01"] = [row(3, Period="2025-02-15T12:00:00"), row(4, Period="2025-02-16T12:00:00")]
        tool, second = await self.accepted_two_period_plan()
        with patch.object(self.reader, "iter_pages", wraps=self.reader.iter_pages) as reads:
            first_result, duplicate, second_result = await asyncio.gather(tool.ainvoke(self.args), tool.ainvoke(self.args), tool.ainvoke(second))
        self.assertTrue(all(result["ok"] for result in (first_result, duplicate, second_result)))
        self.assertEqual(first_result, duplicate)
        self.assertEqual(first_result["stats"]["rows"] + second_result["stats"]["rows"], 3)
        self.assertEqual([call.kwargs["max_rows"] for call in reads.call_args_list], [3, 2])

    async def test_catalog_refresh_does_not_reload_fresh_metadata(self):
        await self.backend.bootstrap()
        self.requests.clear()
        self.service.cached_at = time.monotonic() - self.service.settings.onec_catalog_ttl_seconds - 1
        await self.backend.bootstrap()
        self.assertFalse(any(item.url.path.endswith("$metadata") for item in self.requests))
        self.assertTrue(any(item.url.path.endswith(CATALOGS["organization"]) for item in self.requests))

    async def test_schema_refresh_and_forced_refresh_reload_catalogs(self):
        await self.backend.bootstrap()
        self.requests.clear()
        self.service.metadata_cached_at = time.monotonic() - self.service.settings.onec_metadata_ttl_seconds - 1
        self.metadata_entities = ENTITIES - {CATALOGS["counterparty"]}
        info = await self.backend.bootstrap()
        self.assertNotIn(CATALOGS["counterparty"], info.allowed_entity_names)
        self.assertTrue(any(item.url.path.endswith(CATALOGS["organization"]) for item in self.requests))
        self.requests.clear()
        response = await self.backend.http.get("v1/bootstrap?refresh=true")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any(item.url.path.endswith("$metadata") for item in self.requests))

    async def test_concurrent_bootstrap_refresh_is_shared(self):
        await asyncio.gather(*(self.backend.bootstrap() for _ in range(3)))
        self.assertEqual(sum(item.url.path.endswith("$metadata") for item in self.requests), 1)

    async def test_failed_catalog_refresh_does_not_publish_stale_data(self):
        await self.backend.bootstrap()
        self.service.cached_at = time.monotonic() - self.service.settings.onec_catalog_ttl_seconds - 1
        with patch.object(self.service, "load_dimension", side_effect=BackendError("TIMEOUT", "Тайм-аут.", True)):
            with self.assertRaises(BackendError) as caught:
                await self.backend.bootstrap()
        self.assertEqual(caught.exception.code, "TIMEOUT")

    async def test_changed_field_type_is_rejected_before_calculation(self):
        original = self.reader.get
        async def changed_schema(entity, *args, **kwargs):
            if entity == "$metadata":
                content = metadata_xml().replace(b'Name="Active" Type="Edm.Boolean"', b'Name="Active" Type="Edm.String"')
                return httpx.Response(200, content=content, headers={"DataServiceVersion": "3.0"})
            return await original(entity, *args, **kwargs)
        with patch.object(self.reader, "get", changed_schema):
            with self.assertRaises(BackendError) as caught:
                await self.invoke()
        self.assertEqual(caught.exception.code, "INVALID_SCHEMA")
        self.assertFalse(any(item.url.path.endswith(MOVEMENTS) for item in self.requests))

    async def test_counterparty_search_pushes_filter_and_uuid_to_odata(self):
        self.counterparties = [{"Ref_Key": OTHER, "Description": "ООО О'Брайен"}, {"Ref_Key": ORG, "Description": "Другой"}]
        for query in ("о'бра", OTHER):
            result = await self.backend.call("resolve_dimension", {"dimension": "counterparty", "query": query}, context=self.context, timeout=30)
            self.assertEqual([item["id"] for item in result.data], [OTHER])
            self.assertEqual(result.stats["truncated"], 0)
        predicates = [item.url.params["$filter"] for item in self.requests if item.url.path.endswith(CATALOGS["counterparty"])]
        self.assertTrue(any("о''бра" in predicate for predicate in predicates))
        self.assertTrue(any("Ref_Key eq guid'" in predicate for predicate in predicates))

    async def test_late_page_failure_does_not_return_partial_sum(self):
        await self.backend.bootstrap()
        original = self.reader.get
        async def failed_page(entity, parameters=None, **kwargs):
            if entity == MOVEMENTS and parameters["$skip"] == 1:
                raise BackendError("TIMEOUT", "Время чтения истекло.", True,
                                   details={"stage": "read", "error_type": "ReadTimeout", "attempts": 1})
            return await original(entity, parameters, **kwargs)
        with patch.object(self.reader, "get", failed_page):
            with self.assertRaises(BackendError) as error:
                await self.invoke()
        self.assertEqual(error.exception.code, "TIMEOUT")
        self.assertEqual(error.exception.details["error_type"], "ReadTimeout")
        self.assertEqual(error.exception.details["request_id"], "test")

    async def test_demo_permission_is_checked_by_plan_and_service(self):
        context = replace(self.context, allow_demo_metrics=False)
        with self.assertRaises(BackendError) as error:
            await self.invoke(context=context)
        self.assertEqual(error.exception.code, "UNAPPROVED_METRIC")
        info = await self.backend.bootstrap()
        plan = {"metric_id": METRIC_ID, "organization_id": ORG, "semantic_version": info.metric["semantic_version"], "catalog_version": info.catalog_version, "steps": [{"step_id": "cash", "tool": "get_cash_receipts_by_direction", "arguments": self.args}]}
        denied = {tool.name: tool for tool in create_tools(self.backend, context)}
        self.assertEqual((await denied["submit_query_plan"].ainvoke({"plan": plan}))["error"]["code"], "UNAPPROVED_METRIC")
        allowed = {tool.name: tool for tool in create_tools(self.backend, self.context)}
        self.assertTrue((await allowed["submit_query_plan"].ainvoke({"plan": plan}))["ok"])
        result = await allowed["get_cash_receipts_by_direction"].ainvoke(self.args)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["total"], "0.30")

    async def test_scope_and_unknown_arguments_are_rejected(self):
        for args, context in (({**self.args, "organization_id": OTHER}, self.context), (self.args, replace(self.context, base_id="another")), ({**self.args, "url": "https://other.invalid"}, self.context)):
            with self.assertRaises(BackendError):
                await self.invoke(args=args, context=context)
        self.assertFalse(any(request.url.path.endswith(MOVEMENTS) for request in self.requests))

    async def test_response_outside_period_or_scope_is_not_counted(self):
        for change in ({"Period": "2025-02-01T00:00:00"}, {"Active": False}, {"Организация_Key": OTHER}, {"Сумма": None}, {"Сумма": "NaN"}):
            self.rows = [row(**change)]
            with self.assertRaises(BackendError) as error:
                await self.invoke()
            self.assertEqual(error.exception.code, "INVALID_API_RESPONSE")

    async def test_row_limit_never_returns_partial_money(self):
        self.service.settings.odata_max_rows = 1
        with self.assertRaises(BackendError) as error:
            await self.invoke()
        self.assertEqual(error.exception.code, "ROW_LIMIT")

    async def test_empty_selection_is_not_zero(self):
        self.rows = []
        result = await self.invoke()
        self.assertEqual(result.data["rows"], [])
        self.assertIsNone(result.data["total"])
        self.assertTrue(result.warnings)

    async def test_duplicate_record_keys_do_not_double_count(self):
        self.rows = [row(), row()]
        with self.assertRaises(BackendError) as error:
            await self.invoke()
        self.assertIn(error.exception.code, {"INVALID_API_RESPONSE", "INCOMPLETE_DATA"})

    async def test_next_link_does_not_silently_truncate_data(self):
        await self.backend.bootstrap()
        async def next_page(*args, **kwargs):
            return httpx.Response(200, json={"value": [row()], "odata.nextLink": "https://other.invalid/private"})
        with patch.object(self.reader, "get", next_page):
            with self.assertRaises(BackendError) as error:
                await self.invoke()
            self.assertEqual(error.exception.code, "INCOMPLETE_DATA")

    async def test_required_filters_and_period_limit_are_enforced(self):
        for changes in ({"filters": {"active": False}}, {"filters": {"currency": "RUB"}}, {"period": {"start": "2023-01-01", "end_exclusive": "2025-02-01"}}):
            with self.assertRaises(BackendError) as error:
                await self.invoke(args={**self.args, **changes})
            self.assertEqual(error.exception.code, "INVALID_ARGUMENTS")

    async def test_draft_cannot_be_enabled_by_demo_flag(self):
        original = self.service.definition
        with patch.object(self.service, "definition", side_effect=lambda snapshot: {**original(snapshot), "approval": "draft"}):
            with self.assertRaises(BackendError) as error:
                await self.invoke()
            self.assertEqual(error.exception.code, "UNAPPROVED_METRIC")
            info = await self.backend.bootstrap()
            plan = {"metric_id": METRIC_ID, "organization_id": ORG, "semantic_version": info.metric["semantic_version"], "catalog_version": info.catalog_version, "steps": [{"step_id": "cash", "tool": "get_cash_receipts_by_direction", "arguments": self.args}]}
            tools = {tool.name: tool for tool in create_tools(self.backend, self.context)}
            self.assertEqual((await tools["submit_query_plan"].ainvoke({"plan": plan}))["error"]["code"], "UNAPPROVED_METRIC")

    async def test_http_token_is_required_when_configured(self):
        self.service.settings.onec_data_token = SecretStr("internal-secret")
        with self.assertRaises(BackendError):
            await self.backend.bootstrap()

    async def test_upstream_error_is_sanitized(self):
        await self.backend.bootstrap()
        async def failed_get(*args, **kwargs):
            raise BackendError("ODATA_AUTH", "Проверьте доступ.")
        with patch.object(self.reader, "get", failed_get):
            with self.assertRaises(BackendError) as error:
                await self.invoke()
            self.assertEqual(error.exception.code, "ODATA_AUTH")
            self.assertNotIn("test-password", str(error.exception))
