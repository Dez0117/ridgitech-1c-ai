import asyncio
import copy
import json
import unittest

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

from tools import BackendResult, ToolContext, create_tools


PERIOD = {"start": "2025-01-01", "end_exclusive": "2025-02-01"}
SALES = {"metric_id": "sales", "organization_id": "org-1", "period": PERIOD}
BUSINESS = {
    "get_sales_turnovers": SALES,
    "get_top_counterparties": {**SALES, "limit": 5},
    "get_cash_receipts_by_direction": {**SALES, "metric_id": "cash", "direction_id": "direction-1"},
    "get_customer_balances": {"metric_id": "balances", "organization_id": "org-1", "as_of": "2025-02-01T00:00:00+03:00"},
}


def definition(metric_id):
    supported = {
        "sales": ["get_sales_turnovers", "get_top_counterparties"],
        "cash": ["get_cash_receipts_by_direction"],
        "balances": ["get_customer_balances"],
    }
    return {"metric_id": metric_id, "approval": "approved", "semantic_version": "v1",
            "catalog_version": "v1", "supported_tools": supported[metric_id],
            "time_kind": "as_of" if metric_id == "balances" else "period",
            "allowed_group_by": ["direction"], "allowed_filters": ["posted", "currency"],
            "required_filters": {"posted": True, "currency": "RUB"}}


def plan(operation="get_sales_turnovers", arguments=None):
    arguments = copy.deepcopy(arguments or BUSINESS[operation])
    return {"metric_id": arguments["metric_id"], "organization_id": arguments["organization_id"],
            "semantic_version": "v1", "catalog_version": "v1", "steps": [
                {"step_id": "calculate", "tool": operation, "arguments": arguments}]}


class FakeBackend:
    def __init__(self):
        self.calls = []
        self.definitions = {name: definition(name) for name in ["sales", "cash", "balances"]}
        self.complete = True
        self.sources = ["control-source"]
        self.data = {"value": "1234.56", "unit": "RUB"}
        self.delay = 0

    async def call(self, operation, arguments, *, context, timeout):
        self.calls.append((operation, copy.deepcopy(arguments), context, timeout))
        await asyncio.sleep(self.delay)
        if operation == "search_metrics":
            return BackendResult(data=[{"metric_id": "sales"}, {"metric_id": "secret"}])
        if operation == "get_metric_definition":
            return BackendResult(data=self.definitions[arguments["metric_id"]])
        if operation == "get_entity_schema":
            return BackendResult(data={"entity_name": arguments["entity_name"], "fields": {"amount": "Edm.Decimal"}})
        if operation == "resolve_dimension":
            return BackendResult(data=[{"id": "org-1", "name": "Доступная"}, {"id": "org-2", "name": "Другая"}])
        return BackendResult(data=self.data, sources=self.sources, complete=self.complete)


class ToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.context = ToolContext(request_id="request-1", base_id="base-1",
            allowed_metric_ids=frozenset({"sales", "cash", "balances"}),
            allowed_organization_ids=frozenset({"org-1"}), allowed_entity_names=frozenset({"Sales"}))
        self.tools = {tool.name: tool for tool in create_tools(self.backend, self.context)}

    async def call(self, name, arguments):
        return await self.tools[name].ainvoke(copy.deepcopy(arguments))

    async def accept(self, payload=None):
        return await self.call("submit_query_plan", {"plan": payload or plan()})

    async def test_real_langchain_tools_and_complete_schemas(self):
        self.assertEqual(len(self.tools), 9)
        for name, tool in self.tools.items():
            self.assertIsInstance(tool, BaseTool)
            self.assertTrue(tool.description.startswith("# " + name))
            self.assertFalse(tool.args_schema["additionalProperties"])
            schema = json.dumps(tool.args_schema)
            self.assertNotIn("base_id", schema)
            self.assertNotIn("api_key", schema)
        self.assertIn("limit", self.tools["get_top_counterparties"].args_schema["required"])

    async def test_metadata_tools_execute_and_filter_scope(self):
        self.assertEqual((await self.call("search_metrics", {"query": "Продажи"}))["data"], [{"metric_id": "sales"}])
        self.assertEqual((await self.call("get_metric_definition", {"metric_id": "sales"}))["data"]["metric_id"], "sales")
        self.assertEqual((await self.call("get_entity_schema", {"entity_name": "Sales"}))["data"]["entity_name"], "Sales")
        resolved = await self.call("resolve_dimension", {"dimension": "organization", "query": "Организация"})
        self.assertEqual([row["id"] for row in resolved["data"]], ["org-1"])
        self.assertTrue(all(call[2] == self.context and 0 < call[3] <= 60 for call in self.backend.calls))

    async def test_extra_fields_and_wrong_types_are_rejected(self):
        for args in [{"query": 12}, {"query": "sales", "base_url": "http://other"}, {"query": ""}]:
            self.assertEqual((await self.call("search_metrics", args))["error"]["code"], "INVALID_ARGUMENTS")
        for limit in [True, "5", 0, 101]:
            args = {**BUSINESS["get_top_counterparties"], "limit": limit}
            self.assertEqual((await self.call("get_top_counterparties", args))["error"]["code"], "INVALID_ARGUMENTS")
        self.assertEqual(self.backend.calls, [])

    async def test_access_is_checked_before_backend(self):
        for name, args in [("get_metric_definition", {"metric_id": "secret"}),
                           ("get_entity_schema", {"entity_name": "Secret"}),
                           ("get_sales_turnovers", {**SALES, "organization_id": "org-2"})]:
            self.assertEqual((await self.call(name, args))["error"]["code"], "ACCESS_DENIED")
        self.assertEqual(self.backend.calls, [])

    async def test_all_business_tools_execute_accepted_steps(self):
        for operation, arguments in BUSINESS.items():
            with self.subTest(operation=operation):
                self.tools = {tool.name: tool for tool in create_tools(self.backend, self.context)}
                self.assertTrue((await self.accept(plan(operation, arguments)))["ok"])
                result = await self.call(operation, arguments)
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["data"]["value"], "1234.56")
                self.assertEqual(result["sources"], ["control-source"])

    async def test_business_requires_plan_and_exact_arguments(self):
        self.assertEqual((await self.call("get_sales_turnovers", SALES))["error"]["code"], "PLAN_REQUIRED")
        await self.accept()
        changed = {**SALES, "period": {"start": "2024-01-01", "end_exclusive": "2024-02-01"}}
        self.assertEqual((await self.call("get_sales_turnovers", changed))["error"]["code"], "PLAN_MISMATCH")
        self.assertFalse(any(call[0] == "get_sales_turnovers" for call in self.backend.calls))

    async def test_dates_and_timezone_are_validated(self):
        for period in [{"start": "today", "end_exclusive": "tomorrow"},
                       {"start": "2025-02-01", "end_exclusive": "2025-01-01"}]:
            result = await self.call("get_sales_turnovers", {**SALES, "period": period})
            self.assertEqual(result["error"]["code"], "INVALID_ARGUMENTS")
        args = {**BUSINESS["get_customer_balances"], "as_of": "2025-02-01T00:00:00"}
        self.assertEqual((await self.call("get_customer_balances", args))["error"]["code"], "INVALID_ARGUMENTS")

    async def test_plan_versions_and_approval(self):
        self.assertEqual((await self.accept({**plan(), "catalog_version": "old"}))["error"]["code"], "STALE_PLAN")
        self.backend.definitions["sales"]["approval"] = "draft"
        self.assertEqual((await self.accept())["error"]["code"], "UNAPPROVED_METRIC")

    async def test_malformed_policy_cannot_be_used_for_calculation(self):
        self.backend.definitions["sales"]["supported_tools"] = "get_sales_turnovers"
        self.assertEqual((await self.accept())["error"]["code"], "INVALID_DEFINITION")
        self.assertEqual((await self.call("get_sales_turnovers", SALES))["error"]["code"], "PLAN_REQUIRED")

    async def test_plan_cannot_override_methodology(self):
        cases = [({"group_by": ["currency"]}, "INVALID_PLAN"),
                 ({"group_by": ["direction", "direction"]}, "INVALID_PLAN"),
                 ({"filters": {"posted": False}}, "INVALID_PLAN"),
                 ({"filters": {"currency": "USD"}}, "INVALID_PLAN"),
                 ({"filters": {"arbitrary": "x"}}, "INVALID_PLAN")]
        for change, code in cases:
            self.assertEqual((await self.accept(plan(arguments={**SALES, **change})))["error"]["code"], code)
        self.backend.definitions["sales"]["supported_tools"] = []
        self.assertEqual((await self.accept())["error"]["code"], "UNSUPPORTED_OPERATION")

    async def test_plan_rejects_wrong_metric_time_kind_and_scope(self):
        self.backend.definitions["sales"]["time_kind"] = "as_of"
        self.assertEqual((await self.accept())["error"]["code"], "INVALID_PLAN")
        self.backend.definitions["sales"]["time_kind"] = "period"
        payload = plan()
        payload["steps"][0]["arguments"]["metric_id"] = "cash"
        self.assertEqual((await self.accept(payload))["error"]["code"], "PLAN_MISMATCH")

    async def test_cycles_unknown_dependencies_and_duplicate_steps(self):
        for dependency in ["calculate", "missing"]:
            payload = plan()
            payload["steps"][0]["depends_on"] = [dependency]
            self.assertEqual((await self.accept(payload))["error"]["code"], "INVALID_ARGUMENTS")
        payload = plan()
        payload["steps"].append({**copy.deepcopy(payload["steps"][0]), "step_id": "duplicate"})
        self.assertEqual((await self.accept(payload))["error"]["code"], "INVALID_PLAN")

    async def test_dependency_order(self):
        later = {**SALES, "period": {"start": "2025-02-01", "end_exclusive": "2025-03-01"}}
        payload = plan()
        payload["steps"].append({"step_id": "later", "tool": "get_sales_turnovers", "arguments": later, "depends_on": ["calculate"]})
        self.assertTrue((await self.accept(payload))["ok"])
        self.assertEqual((await self.call("get_sales_turnovers", later))["error"]["code"], "DEPENDENCY_PENDING")
        self.assertTrue((await self.call("get_sales_turnovers", SALES))["ok"])
        self.assertTrue((await self.call("get_sales_turnovers", later))["ok"])

    async def test_plan_and_step_are_idempotent_including_concurrent_calls(self):
        first = await self.accept()
        self.assertEqual(await self.accept(), first)
        self.backend.delay = 0.01
        results = await asyncio.gather(self.call("get_sales_turnovers", SALES), self.call("get_sales_turnovers", SALES))
        self.assertEqual(results[0], results[1])
        self.assertEqual(sum(call[0] == "get_sales_turnovers" for call in self.backend.calls), 1)
        changed = plan(arguments={**SALES, "group_by": ["direction"]})
        self.assertEqual((await self.accept(changed))["error"]["code"], "PLAN_ALREADY_ACCEPTED")

    async def test_incomplete_or_unsourced_results_do_not_return_partial_value(self):
        await self.accept()
        self.backend.complete = False
        result = await self.call("get_sales_turnovers", SALES)
        self.assertEqual(result["error"]["code"], "INCOMPLETE_DATA")
        self.assertNotIn("1234.56", json.dumps(result))
        self.backend.complete, self.backend.sources = True, []
        self.assertEqual((await self.call("get_sales_turnovers", SALES))["error"]["code"], "MISSING_SOURCES")

    async def test_empty_selection_has_warning(self):
        await self.accept()
        self.backend.data = []
        result = await self.call("get_sales_turnovers", SALES)
        self.assertTrue(result["ok"])
        self.assertIn("Выборка пуста", result["warnings"][0])

    async def test_deadline_and_call_budget(self):
        context = ToolContext(**{**self.context.__dict__, "max_calls": 1})
        self.tools = {tool.name: tool for tool in create_tools(self.backend, context)}
        await self.call("search_metrics", {"query": "sales"})
        self.assertEqual((await self.call("search_metrics", {"query": "sales"}))["error"]["code"], "TOOL_BUDGET")
        context = ToolContext(**{**self.context.__dict__, "timeout_seconds": 0.01})
        self.tools = {tool.name: tool for tool in create_tools(self.backend, context)}
        self.backend.delay = 0.03
        self.assertEqual((await self.call("search_metrics", {"query": "sales"}))["error"]["code"], "TIMEOUT")

    async def test_backend_errors_are_structured(self):
        async def unavailable(*args, **kwargs):
            raise OSError("private endpoint and credentials")
        self.backend.call = unavailable
        result = await self.call("search_metrics", {"query": "sales"})
        self.assertEqual(result["error"]["code"], "API_UNAVAILABLE")
        self.assertNotIn("private endpoint", json.dumps(result))
        async def malformed(*args, **kwargs):
            return {"data": 42}
        self.backend.call = malformed
        self.assertEqual((await self.call("search_metrics", {"query": "sales"}))["error"]["code"], "INVALID_API_RESPONSE")

    async def test_request_state_is_isolated(self):
        await self.accept()
        fresh = {tool.name: tool for tool in create_tools(self.backend, self.context)}
        self.assertEqual((await fresh["get_sales_turnovers"].ainvoke(SALES))["error"]["code"], "PLAN_REQUIRED")

    async def test_native_langgraph_toolnode(self):
        builder = StateGraph(MessagesState)
        builder.add_node("tools", ToolNode(list(self.tools.values())))
        builder.add_edge(START, "tools")
        builder.add_edge("tools", END)
        graph = builder.compile()
        state = await graph.ainvoke({"messages": [AIMessage(content="", tool_calls=[
            {"id": "call-1", "name": "search_metrics", "args": {"query": "sales"}, "type": "tool_call"}])]})
        message = state["messages"][-1]
        self.assertIsInstance(message, ToolMessage)
        self.assertEqual(message.tool_call_id, "call-1")
        self.assertEqual(json.loads(message.content)["data"], [{"metric_id": "sales"}])


if __name__ == "__main__":
    unittest.main()
