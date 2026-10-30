import json
import time
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from ridgitech_agent.agent import Agent, ModelTurn, ToolCall
from ridgitech_agent.catalog import Catalog
from ridgitech_agent.demo import FIXTURES, ScriptedDemoModel, demo_context
from ridgitech_agent.executor import DemoAdapter, Page, odata_parameters
from ridgitech_agent.models import AgentError, Limits, QueryPlan
from ridgitech_agent.ollama import OllamaModel
from ridgitech_agent.tools import Tools, tool_schemas


PLAN = {
    "metric_id": "demo_sales", "semantic_version": "demo-1", "catalog_version": "demo-1",
    "organization_id": "demo-org", "period": {"start": "2025-01-01", "end_exclusive": "2025-02-01"},
    "group_by": [],
}


class ConstantModel:
    def __init__(self, turn):
        self.turn = turn

    def complete(self, messages, tools, *, timeout):
        return self.turn


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog(FIXTURES / "catalog.json")
        self.adapter = DemoAdapter(FIXTURES / "rows.json")
        self.context = demo_context(limits=Limits(page_size=2))
        self.tools = Tools(self.catalog, self.context)

    def run_scenario(self, scenario="success", model=None, context=None):
        return Agent(self.catalog, self.adapter, model or ScriptedDemoModel(scenario)).run(
            "Тестовый вопрос", "", context or self.context)

    def submit(self, plan=None):
        return self.tools.dispatch("submit_query_plan", {"plan": plan or PLAN})

    def test_success_filters_and_pagination(self):
        result = self.run_scenario()
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.data, [{"value": "4000.00", "unit": "RUB"}])
        self.assertEqual((result.stats["rows"], result.stats["pages"]), (3, 2))
        self.assertEqual(result.query_plan.query.organization_id, "demo-org")
        self.assertIn("ДЕМОНСТРАЦИЯ", result.warnings[0])
        self.assertIn("datetime'2025-02-01T00:00:00'", result.trace[-1]["odata_parameters_preview"]["$filter"])

    def test_grouped(self):
        result = self.run_scenario("grouped")
        self.assertEqual(result.data, [
            {"direction": "Поддержка", "value": "2500.20", "unit": "RUB"},
            {"direction": "Разработка ПО", "value": "1499.80", "unit": "RUB"}])

    def test_clarification_and_unsupported_do_not_read(self):
        with patch.object(self.adapter, "read_page", side_effect=AssertionError("must not read")):
            self.assertEqual(self.run_scenario("clarification").status, "needs_clarification")
            self.assertEqual(self.run_scenario("unsupported").error.code, "UNSUPPORTED_METRIC")

    def test_row_limit_does_not_return_partial_sum(self):
        result = self.run_scenario(context=demo_context(limits=Limits(max_rows=2, page_size=2)))
        self.assertEqual(result.error.code, "ROW_LIMIT")
        self.assertEqual(result.data, [])
        self.assertNotIn("3500.30", result.message)
        self.assertIsNotNone(result.query_plan)

    def test_exact_limit_is_complete_if_source_confirms_end(self):
        result = self.run_scenario(context=demo_context(limits=Limits(max_rows=3, page_size=2)))
        self.assertEqual(result.status, "completed")

    def test_page_limit(self):
        result = self.run_scenario(context=demo_context(limits=Limits(page_size=1, max_pages=1)))
        self.assertEqual(result.error.code, "PAGE_LIMIT")
        self.assertEqual(result.data, [])

    def test_empty_is_not_silent_zero(self):
        self.adapter.rows = []
        result = self.run_scenario()
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.data, [])
        self.assertIn("Выборка пуста", result.warnings[-1])
        self.assertNotIn("0.00", result.message)

    def test_zero_has_warning(self):
        self.adapter.rows = [self.adapter.rows[0], {**self.adapter.rows[1], "Amount": "-1000.10"}]
        result = self.run_scenario()
        self.assertEqual(result.data[0]["value"], "0.00")
        self.assertIn("нулевой итог", result.warnings[-1])

    def test_decimal_is_exact_and_rounding_occurs_after_sum(self):
        self.adapter.rows = [{**row, "Amount": "0.005"} for row in self.adapter.rows[:3]]
        self.assertEqual(self.run_scenario().data[0]["value"], "0.02")

    def test_invalid_decimal_never_returns_sum(self):
        for amount in [0.1, "NaN", "Infinity", None, "1e30"]:
            with self.subTest(amount=amount):
                self.adapter.rows[0]["Amount"] = amount
                result = self.run_scenario()
                self.assertEqual(result.error.code, "INVALID_DATA")
                self.assertEqual(result.data, [])

    def test_invalid_page_and_duplicate_rows(self):
        with patch.object(self.adapter, "read_page", return_value=Page([], True)):
            self.assertEqual(self.run_scenario().error.code, "INCOMPLETE_DATA")
        self.adapter.rows[1]["Id"] = "001"
        self.assertEqual(self.run_scenario().error.code, "INCOMPLETE_DATA")

    def test_invalid_date_and_missing_field_do_not_return_sum(self):
        self.adapter.rows[0]["Date"] = "2025-01-99T10:00:00"
        self.assertEqual(self.run_scenario().error.code, "INVALID_DATA")
        with patch.object(self.adapter, "read_page", return_value=Page([{"Id": "001"}], False)):
            self.assertEqual(self.run_scenario().error.code, "INVALID_DATA")

    def test_adapter_must_not_bypass_filters(self):
        row = {**self.adapter.rows[0], "Organization": "other-org"}
        with patch.object(self.adapter, "read_page", return_value=Page([row], False)):
            result = self.run_scenario()
            self.assertEqual(result.error.code, "INVALID_DATA")
            self.assertEqual(result.data, [])

    def test_argument_validation_and_unknown_tool(self):
        for arguments in [{"query": 12}, {"query": "sales", "base_id": "other"}, {}, {"query": ""}]:
            self.assertEqual(self.tools.dispatch("search_metrics", arguments)["error"]["code"], "INVALID_ARGUMENTS")
        self.assertEqual(self.tools.dispatch("execute_python", {"code": "..."})["error"]["code"], "UNKNOWN_TOOL")

    def test_cannot_override_source_filters_or_limits(self):
        for field, value in [("source", "http://other"), ("filters", []), ("max_rows", 999999), ("aggregation", "count")]:
            with self.subTest(field=field):
                reply = self.submit({**PLAN, field: value})
                self.assertEqual(reply["error"]["code"], "INVALID_ARGUMENTS")
                self.assertIsNone(self.tools.accepted)

    def test_period_validation(self):
        for period in [{"start": "today", "end_exclusive": "tomorrow"},
                       {"start": "2025-02-01", "end_exclusive": "2025-01-01"},
                       {"start": "2025-01-01T00:00:00", "end_exclusive": "2025-02-01"}]:
            self.assertEqual(self.submit({**PLAN, "period": period})["error"]["code"], "INVALID_ARGUMENTS")

    def test_versions_dimensions_and_access(self):
        for change, code in [({"semantic_version": "old"}, "STALE_PLAN"),
                             ({"catalog_version": "old"}, "STALE_PLAN"),
                             ({"organization_id": "other-org"}, "ACCESS_DENIED"),
                             ({"group_by": ["currency"]}, "INVALID_PLAN"),
                             ({"group_by": ["direction", "direction"]}, "INVALID_PLAN")]:
            self.assertEqual(self.submit({**PLAN, **change})["error"]["code"], code)

    def test_scope_hides_metrics_and_schema(self):
        context = self.context.model_copy(update={"allowed_metric_ids": []})
        tools = Tools(self.catalog, context)
        self.assertEqual(tools.dispatch("search_metrics", {"query": "продажи"})["result"], [])
        self.assertFalse(tools.dispatch("get_entity_schema", {"entity_name": "DemoSales"})["ok"])
        self.assertFalse(tools.dispatch("get_metric_definition", {"metric_id": "demo_sales"})["ok"])
        other = self.context.model_copy(update={"base_id": "other"})
        self.assertEqual(self.run_scenario(context=other).error.code, "ACCESS_DENIED")

    def test_demo_metric_requires_explicit_demo_context(self):
        tools = Tools(self.catalog, self.context.model_copy(update={"demo_mode": False}))
        self.assertEqual(tools.dispatch("submit_query_plan", {"plan": PLAN})["error"]["code"], "UNAPPROVED_METRIC")

    def test_same_plan_is_idempotent_and_different_plan_is_rejected(self):
        first = self.submit()
        self.assertEqual(self.submit(), first)
        self.assertEqual(self.submit({**PLAN, "group_by": ["direction"]})["error"]["code"], "PLAN_ALREADY_ACCEPTED")

    def test_compilation_escapes_string_literals(self):
        context = self.context.model_copy(update={"allowed_organization_ids": ["O'Brien"]})
        tools = Tools(self.catalog, context)
        self.assertTrue(tools.dispatch("submit_query_plan", {"plan": {**PLAN, "organization_id": "O'Brien"}})["ok"])
        parameters = odata_parameters(tools.accepted.read, self.catalog.entities["DemoSales"], top=10, skip=0)
        self.assertIn("Organization eq 'O''Brien'", parameters["$filter"])
        self.assertNotIn("$apply", parameters)

    def test_no_llm_numeric_answer_is_accepted(self):
        result = self.run_scenario(model=ConstantModel(ModelTurn(content='{"status":"completed","message":"42 RUB"}')))
        self.assertEqual(result.error.code, "MODEL_PROTOCOL")
        self.assertEqual(result.data, [])

    def test_model_retry_budget_and_tool_budget(self):
        model = ConstantModel(ModelTurn(tool_calls=[ToolCall(name="submit_query_plan", arguments={"plan": {**PLAN, "catalog_version": "bad"}})]))
        context = self.context.model_copy(update={"limits": Limits(max_model_turns=2)})
        self.assertEqual(self.run_scenario(model=model, context=context).error.code, "MODEL_BUDGET")
        model = ConstantModel(ModelTurn(tool_calls=[ToolCall(name="search_metrics", arguments={"query": "продажи"})] * 2))
        context = self.context.model_copy(update={"limits": Limits(max_tool_calls=1)})
        self.assertEqual(self.run_scenario(model=model, context=context).error.code, "TOOL_BUDGET")

    def test_plan_can_be_corrected_after_validation_error(self):
        class CorrectingModel:
            def complete(inner, messages, tools, *, timeout):
                failed = any(m["role"] == "tool" for m in messages)
                plan = PLAN if failed else {**PLAN, "catalog_version": "bad"}
                return ModelTurn(tool_calls=[ToolCall(name="submit_query_plan", arguments={"plan": plan})])
        result = self.run_scenario(model=CorrectingModel())
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.trace[0]["result"]["error"]["code"], "STALE_PLAN")

    def test_batch_with_duplicate_submit_executes_only_once(self):
        model = ConstantModel(ModelTurn(tool_calls=[ToolCall(name="submit_query_plan", arguments={"plan": PLAN})] * 2))
        with patch.object(self.adapter, "read_page", wraps=self.adapter.read_page) as read:
            result = self.run_scenario(model=model)
            self.assertEqual(result.status, "completed")
            self.assertEqual(read.call_count, 2)  # two pages, not two plan executions

    def test_deadline_includes_model_time(self):
        class SlowModel:
            def complete(inner, messages, tools, *, timeout):
                self.assertLessEqual(timeout, 0.01)
                time.sleep(0.02)
                return ModelTurn(content='{"status":"needs_clarification","message":"?"}')
        context = self.context.model_copy(update={"limits": Limits(timeout_seconds=0.01)})
        self.assertEqual(self.run_scenario(model=SlowModel(), context=context).error.code, "TIMEOUT")

    def test_upstream_errors_are_structured(self):
        with patch.object(self.adapter, "read_page", side_effect=OSError("private endpoint details")):
            result = self.run_scenario()
            self.assertEqual(result.error.code, "UPSTREAM_UNAVAILABLE")
            self.assertNotIn("private endpoint", result.model_dump_json())

    def test_catalog_field_mismatch_is_rejected_at_load(self):
        data = json.loads((FIXTURES / "catalog.json").read_text())
        data["metrics"][0]["amount_field"] = "Missing"
        with TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                Catalog(path)

    def test_catalog_rejects_conflicting_field_roles(self):
        data = json.loads((FIXTURES / "catalog.json").read_text())
        data["metrics"][0]["amount_field"] = "Organization"
        with TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                Catalog(path)

    def test_schema_is_json_serializable_and_rejects_extra_fields(self):
        schemas = tool_schemas()
        json.dumps(schemas)
        self.assertEqual(len(schemas), 4)
        self.assertFalse(schemas[-1]["function"]["parameters"]["$defs"]["QueryPlan"]["additionalProperties"])
        with self.assertRaises(ValidationError):
            QueryPlan.model_validate_json(json.dumps({**PLAN, "base_id": "other"}))


class OllamaTests(unittest.TestCase):
    def test_http_contract_and_tool_parsing(self):
        payload = {"done": True, "message": {"content": "", "tool_calls": [{"function": {
            "name": "search_metrics", "arguments": {"query": "продажи"}}}]}}
        with patch("ridgitech_agent.ollama.urlopen") as open_request:
            open_request.return_value.__enter__.return_value.read.return_value = json.dumps(payload).encode()
            result = OllamaModel("test-model").complete([{"role": "user", "content": "hi"}], tool_schemas(), timeout=1)
            request = open_request.call_args.args[0]
            self.assertEqual(request.full_url, "http://127.0.0.1:11434/api/chat")
            self.assertFalse(json.loads(request.data)["stream"])
            self.assertEqual(result.tool_calls[0].name, "search_metrics")
            self.assertEqual(open_request.call_args.kwargs["timeout"], 1)

    def test_malformed_response_is_structured(self):
        for payload in [b"not-json", b"{}", b'{"done":true,"message":{}}', b"[]"]:
            with self.subTest(payload=payload), patch("ridgitech_agent.ollama.urlopen") as open_request:
                open_request.return_value.__enter__.return_value.read.return_value = payload
                with self.assertRaises(AgentError) as error:
                    OllamaModel("test-model").complete([], [], timeout=1)
                self.assertEqual(error.exception.code, "MODEL_PROTOCOL")


if __name__ == "__main__":
    unittest.main()
