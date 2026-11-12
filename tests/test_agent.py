import asyncio
import copy
import json
import os
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr

from agent import AnalyticsAgent, AgentSettings
from agent.evaluation import Expected, compare_runs, evaluate, grade, read_cases
from agent.fixture_backend import FixtureBackend
from agent.providers import create_qwen_model


TODAY = date(2026, 11, 11)
PERIOD = {"start": "2025-01-01", "end_exclusive": "2025-02-01"}


def sales_plan(*, period=None, group_by=None, two_periods=False, dependent=False):
    arguments = {"metric_id": "demo_sales", "organization_id": "demo-org", "period": period or PERIOD}
    if group_by:
        arguments["group_by"] = group_by
    steps = [{"step_id": "first", "tool": "get_sales_turnovers", "arguments": arguments}]
    if two_periods:
        steps.append({"step_id": "second", "tool": "get_sales_turnovers",
                      "arguments": {**arguments, "period": {"start": "2024-01-01", "end_exclusive": "2024-02-01"}},
                      "depends_on": ["first"] if dependent else []})
    return {"metric_id": "demo_sales", "organization_id": "demo-org", "semantic_version": "synthetic-v1",
            "catalog_version": "synthetic-v1", "steps": steps}


def tool_call(name, arguments, call_id="call-1"):
    return AIMessage(content="", tool_calls=[{"id": call_id, "name": name, "args": arguments, "type": "tool_call"}])


def submit(payload=None):
    return tool_call("submit_query_plan", {"plan": payload or sales_plan()})


class ScriptedModel(BaseChatModel):
    """Test transport, with no language understanding or network calls."""
    responses: list[AIMessage]
    delay: float = 0
    _cursor: int = PrivateAttr(default=0)
    _bound_names: list[str] = PrivateAttr(default_factory=list)

    @property
    def _llm_type(self):
        return "scripted-test-model"

    def bind_tools(self, tools, **kwargs):
        self._bound_names = [tool.name for tool in tools]
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        index = min(self._cursor, len(self.responses) - 1)
        self._cursor += 1
        return ChatResult(generations=[ChatGeneration(message=self.responses[index].model_copy(deep=True))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        await asyncio.sleep(self.delay)
        return self._generate(messages, stop, run_manager, **kwargs)


class AgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.backend = FixtureBackend()

    async def run_agent(self, replies, *, context=None, settings=None, **kwargs):
        self.model = ScriptedModel(responses=replies)
        return await AnalyticsAgent(self.model, self.backend, settings=settings).arun(
            "Контрольный вопрос", context or self.backend.context("test-request"), current_date=TODAY,
            selected_organization_id="demo-org", **kwargs)

    async def test_graph_executes_plan_without_llm_numeric_answer(self):
        result = await self.run_agent([submit()])
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.results[0]["data"]["total"], "4000.00")
        self.assertEqual(result.results[0]["stats"], {"rows": 3, "pages": 2})
        self.assertEqual(result.model_calls, 1)
        self.assertEqual(set(self.model._bound_names), {"search_metrics", "get_metric_definition", "get_entity_schema",
                                                       "resolve_dimension", "submit_query_plan"})
        self.assertIn("Синтетические", result.warnings[0])
        self.assertEqual(result.sources[0]["entity"], "DemoSales")

    async def test_discovery_tools_and_validation_repair(self):
        bad = {**sales_plan(), "catalog_version": "old"}
        result = await self.run_agent([
            tool_call("search_metrics", {"query": "продажи"}),
            tool_call("get_metric_definition", {"metric_id": "demo_sales"}),
            tool_call("get_entity_schema", {"entity_name": "DemoSales"}),
            submit(bad), submit(),
        ])
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.model_calls, 5)
        errors = [event["result"].get("error", {}).get("code") for event in result.trace if event["event"] == "tool"]
        self.assertIn("STALE_PLAN", errors)

    async def test_two_periods_execute_and_compare_exactly(self):
        for dependent in [False, True]:
            with self.subTest(dependent=dependent):
                self.backend = FixtureBackend()
                result = await self.run_agent([submit(sales_plan(two_periods=True, dependent=dependent))])
                self.assertEqual([record["data"]["total"] for record in result.results], ["4000.00", "3000.00"])
                self.assertEqual(result.comparisons[0]["delta"], "1000.00")
                self.assertEqual(result.comparisons[0]["percent"], "33.33")

    async def test_table_top_receipts_and_balances(self):
        scenarios = [
            ("demo_sales", "get_top_counterparties", {"period": PERIOD, "limit": 1}, "4000.00"),
            ("demo_cash", "get_cash_receipts_by_direction", {"period": PERIOD, "direction_id": "demo-dev"}, "6000.30"),
            ("demo_balances", "get_customer_balances", {"as_of": "2026-11-11T00:00:00+03:00"}, "2100.00"),
        ]
        for metric, operation, parameters, total in scenarios:
            with self.subTest(operation=operation):
                self.backend = FixtureBackend()
                payload = {**sales_plan(), "metric_id": metric, "steps": [{"step_id": "value", "tool": operation,
                    "arguments": {"metric_id": metric, "organization_id": "demo-org", **parameters}}]}
                result = await self.run_agent([submit(payload)])
                self.assertEqual(result.status, "completed", result.message)
                self.assertEqual(result.results[0]["data"]["total"], total)
                if operation == "get_top_counterparties":
                    self.assertEqual(result.results[0]["data"]["rows"][0]["value"], "2500.20")
                    self.assertEqual(len(result.results[0]["data"]["rows"]), 1)

    async def test_clarification_and_unsupported_do_not_calculate(self):
        for status, final_status in [("needs_clarification", "needs_clarification"), ("unsupported", "failed")]:
            result = await self.run_agent([AIMessage(content=json.dumps({"status": status, "message": "Уточните показатель."}))])
            self.assertEqual(result.status, final_status)
            self.assertEqual(result.results, [])
        self.assertEqual(self.backend.calls, [])

    async def test_freeform_number_is_rejected(self):
        result = await self.run_agent([AIMessage(content="Продажи составили 999999 рублей.")])
        self.assertEqual(result.error["code"], "MODEL_PROTOCOL")
        self.assertEqual(result.results, [])

    async def test_row_limit_does_not_expose_partial_result(self):
        self.backend = FixtureBackend(max_rows=2)
        result = await self.run_agent([submit()])
        self.assertEqual(result.error["code"], "ROW_LIMIT")
        self.assertEqual(result.results, [])
        self.assertNotIn("4000.00", result.message)

    async def test_invalid_monetary_result_is_rejected(self):
        original = self.backend.call
        async def broken(operation, *args, **kwargs):
            result = await original(operation, *args, **kwargs)
            if operation == "get_sales_turnovers":
                result.data["total"] = 4000.0
            return result
        self.backend.call = broken
        result = await self.run_agent([submit()])
        self.assertEqual(result.error["code"], "INVALID_RESULT")
        self.assertEqual(result.results, [])

    async def test_empty_is_not_silent_zero(self):
        payload = sales_plan(period={"start": "2023-01-01", "end_exclusive": "2023-02-01"})
        result = await self.run_agent([submit(payload)])
        self.assertEqual(result.status, "completed")
        self.assertIsNone(result.results[0]["data"]["total"])
        self.assertTrue(any("Выборка пуста" in warning for warning in result.warnings))
        self.assertNotIn("0.00", result.message)

    async def test_budgets_and_scope(self):
        response = tool_call("search_metrics", {"query": "продажи"})
        result = await self.run_agent([response], settings=AgentSettings(max_model_turns=2))
        self.assertEqual(result.error["code"], "MODEL_BUDGET")
        result = await self.run_agent([response], context=self.backend.context("budget", max_calls=1))
        self.assertEqual(result.error["code"], "TOOL_BUDGET")
        model = ScriptedModel(responses=[submit()])
        result = await AnalyticsAgent(model, self.backend).arun("Вопрос", self.backend.context("scope"),
            current_date=TODAY, selected_organization_id="other-org")
        self.assertEqual(result.error["code"], "ACCESS_DENIED")
        self.assertEqual(model._cursor, 0)

    async def test_truncated_response_preserves_usage(self):
        message = AIMessage(content="{", response_metadata={"finish_reason": "length", "cost": 0.001},
                            usage_metadata={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30})
        result = await self.run_agent([message])
        self.assertEqual(result.error["code"], "MODEL_OUTPUT_LIMIT")
        self.assertEqual(result.usage["total_tokens"], 30)
        self.assertEqual(result.cost_usd, "0.001")

    async def test_model_timeout_and_safe_error(self):
        model = ScriptedModel(responses=[submit()], delay=0.03)
        result = await AnalyticsAgent(model, self.backend).arun("Вопрос", self.backend.context("timeout", timeout_seconds=0.01), current_date=TODAY)
        self.assertEqual(result.error["code"], "TIMEOUT")
        class Unavailable(ScriptedModel):
            async def _agenerate(self, *args, **kwargs):
                raise RuntimeError("private key and endpoint")
        result = await AnalyticsAgent(Unavailable(responses=[submit()]), self.backend).arun("Вопрос", self.backend.context("error"), current_date=TODAY)
        self.assertEqual(result.error["code"], "MODEL_API_ERROR")
        self.assertNotIn("private key", result.model_dump_json())
        self.assertEqual(result.model_calls, 1)

    async def test_openrouter_request_and_native_response_usage(self):
        raw = {"id": "gen-test", "model": "qwen/qwen3.8-27b", "provider": "TestProvider",
            "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content":
                '{"status":"needs_clarification","message":"За какой период?"}'}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 24, "total_tokens": 36, "cost": 0.00003}}
        send = AsyncMock(return_value=raw)
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "unit-test-key"}):
            model = create_qwen_model()
        model.client = SimpleNamespace(chat=SimpleNamespace(send_async=send))
        result = await AnalyticsAgent(model, self.backend).arun("Продажи", self.backend.context("provider"), current_date=TODAY)
        self.assertEqual(result.status, "needs_clarification")
        self.assertEqual(result.usage["total_tokens"], 36)
        self.assertEqual(result.cost_usd, "0.00003")
        payload = send.call_args.kwargs
        self.assertEqual(payload["model"], "qwen/qwen3.8-27b")
        self.assertEqual(payload["reasoning"], {"effort": "none"})
        self.assertEqual(payload["provider"]["require_parameters"], True)
        self.assertEqual(len(payload["tools"]), 5)
        self.assertNotIn("unit-test-key", result.model_dump_json())

    async def test_openrouter_transport_timeout_and_no_hidden_retries(self):
        requests = []
        async def respond(request):
            requests.append(request)
            return httpx.Response(503, json={"error": {"code": 503, "message": "unavailable"}})
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "unit-test-key"}):
            model = create_qwen_model()
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            model.client.sdk_configuration.async_client = client
            result = await AnalyticsAgent(model, self.backend).arun(
                "Продажи", self.backend.context("transport", timeout_seconds=5), current_date=TODAY)
        self.assertEqual(result.error["code"], "MODEL_API_ERROR")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].extensions["timeout"]["read"], 60)

    async def test_prompt_receives_fixed_dates_and_history(self):
        class Inspecting(ScriptedModel):
            def _generate(inner, messages, *args, **kwargs):
                self.assertIn('"current_date": "2026-11-11"', messages[0].content)
                self.assertIn("Europe/Moscow", messages[0].content)
                self.assertIn("предыдущий", messages[1].content)
                return super()._generate(messages, *args, **kwargs)
        model = Inspecting(responses=[submit()])
        result = await AnalyticsAgent(model, self.backend).arun("Январь 2025", self.backend.context("history"),
            current_date=TODAY, history="предыдущий вопрос: продажи")
        self.assertEqual(result.status, "completed")


class EvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixture_historical_balance_and_decimal_input(self):
        backend = FixtureBackend()
        arguments = {"metric_id": "demo_balances", "organization_id": "demo-org", "as_of": "2025-01-31T00:00:00+03:00"}
        result = await backend.call("get_customer_balances", arguments, context=backend.context("balance"), timeout=5)
        self.assertEqual(result.data["total"], "700.00")
        with TemporaryDirectory() as directory:
            data = copy.deepcopy(backend.data)
            data["sales"][0]["amount"] = 0.1
            path = Path(directory) / "bad.json"
            path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                FixtureBackend(path)

    async def test_dataset_and_grading_detect_wrong_period_and_total(self):
        cases = read_cases("evals/cases.jsonl")
        self.assertEqual(len(cases), 14)
        backend = FixtureBackend()
        result = await AnalyticsAgent(ScriptedModel(responses=[submit()]), backend).arun(
            "Продажи", backend.context("grade"), current_date=TODAY)
        self.assertTrue(grade(result, cases[0].expect)["passed"])
        changed = Expected(status="completed", totals=["999.99"], arguments=[{"arguments": {
            "period": {"start": "2024-01-01", "end_exclusive": "2024-02-01"}}}])
        score = grade(result, changed)
        self.assertFalse(score["passed"])
        self.assertFalse(score["checks"]["arguments_0"])

    async def test_run_artifacts_and_comparison(self):
        case = read_cases("evals/cases.jsonl")[0]
        with TemporaryDirectory() as directory:
            before, after = Path(directory) / "before", Path(directory) / "after"
            config = {"name": "scripted-test-model", "model": "test-only"}
            good = ScriptedModel(responses=[submit()])
            bad = ScriptedModel(responses=[AIMessage(content="42")])
            first = await evaluate(bad, [case], settings=AgentSettings(), output=before, provider_config=config)
            second = await evaluate(good, [case], settings=AgentSettings(), output=after, provider_config=config)
            self.assertEqual((first["passed"], second["passed"]), (0, 1))
            self.assertTrue(second["complete"])
            for name in ["manifest.json", "summary.json", "results.jsonl", "prompt.md", "tools.json", "cases.jsonl"]:
                self.assertTrue((after / name).is_file())
            comparison = compare_runs(before, after)
            self.assertEqual(comparison["improved"], ["sales-month#1"])
            self.assertEqual(comparison["regressed"], [])
            with self.assertRaises(ValueError):
                await evaluate(good, [case], settings=AgentSettings(), output=after, provider_config=config)
            record = json.loads((after / "results.jsonl").read_text())
            record["input"]["current_date"] = "2026-10-30"
            (after / "results.jsonl").write_text(json.dumps(record) + "\n")
            with self.assertRaises(ValueError):
                compare_runs(before, after)


if __name__ == "__main__":
    unittest.main()
