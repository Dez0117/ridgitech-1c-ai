import copy
import json
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from agent.evaluation import EvalCase, Expected, evaluate, grade, read_cases
from agent.models import AgentResult, AgentSettings
from backend.integrations.onec_data_backend import OneCDataBackend
from scripts.build_onec_golden import oracle
from agent.onec_snapshot import SnapshotUpstream


ORG = "00000000-0000-0000-0000-000000000001"
DIR = "00000000-0000-0000-0000-000000000002"
METRIC = "cash_receipts_by_direction"
ARGS = {"metric_id": METRIC, "organization_id": ORG, "direction_id": DIR,
        "period": {"start": "2025-01-01", "end_exclusive": "2025-02-01"}}
PLAN = {"metric_id": METRIC, "organization_id": ORG, "semantic_version": "demo-v1", "catalog_version": "catalog-v1",
        "steps": [{"step_id": "cash", "tool": "get_cash_receipts_by_direction", "arguments": ARGS}]}


class Planner(BaseChatModel):
    @property
    def _llm_type(self):
        return "test-plan-only"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[
            {"id": "p", "name": "submit_query_plan", "args": {"plan": PLAN}, "type": "tool_call"}]))])


def test_oracle_filters_and_half_open_dates_are_independent():
    row = {"Организация_Key": ORG, "НаправлениеДеятельности_Key": DIR, "Period": "2025-01-31T23:59:59",
           "Active": True, "ВидДвиженияДС": "Поступление", "Сумма": "0.10"}
    records = [row, {**row, "Сумма": "0.20"}, {**row, "Active": False, "Сумма": "9999"},
               {**row, "ВидДвиженияДС": "Выбытие", "Сумма": "9999"}, {**row, "Организация_Key": "other"},
               {**row, "Period": "2025-02-01T00:00:00", "Сумма": "9999"}]
    assert oracle(records, ORG, DIR, "2025-01-01", "2025-02-01") == ("0.30", 2)
    assert oracle(records, ORG, DIR, "2024-01-01", "2024-02-01") == (None, 0)


def test_grader_rejects_reused_steps_wrong_units_and_provenance():
    result = AgentResult(status="completed", request_id="x", message="0.30", results=[
        {"tool": "get_cash_receipts_by_direction", "arguments": ARGS,
         "data": {"total": "0.30", "rows": [], "unit": "RUB"}, "stats": {"rows": 2},
         "complete": True, "sources": [{"kind": "odata", "entity": "register", "catalog_version": "v1",
             "semantic_version": "v1", **ARGS, "mandatory_filters": {"active": True, "movement_kind": "Поступление"}}]}])
    wanted = Expected(status="completed", arguments=[{"arguments": ARGS}, {"arguments": ARGS}],
                      units=["ед. учёта"], source_rows=[2], source_kind="odata")
    checks = grade(result, wanted)["checks"]
    assert checks["arguments_0"] and not checks["arguments_1"]
    assert checks["provenance"] and not checks["units"]
    changed = copy.deepcopy(result)
    changed.results[0]["sources"][0]["direction_id"] = "other"
    assert not grade(changed, wanted)["checks"]["provenance"]


def test_grader_checks_clarification_topic_and_no_execution():
    wanted = Expected(status="needs_clarification", no_calculations=True, message_patterns=["направлен"])
    result = AgentResult(status="needs_clarification", request_id="x", message="За какой период?")
    assert not grade(result, wanted)["passed"]
    result.message = "По какому направлению?"
    assert grade(result, wanted)["passed"]
    result.trace = [{"event": "calculation"}]
    assert not grade(result, wanted)["passed"]


def test_golden_set_is_diverse_and_has_exact_labels():
    cases = read_cases("evals/onec/golden-v1.jsonl")
    assert len(cases) == 37
    assert len({c.category for c in cases}) >= 10
    calculations = [c for c in cases if c.expect.status == "completed"]
    assert len(calculations) == 22
    assert all(c.expect.arguments and c.expect.units and c.expect.source_rows and c.expect.source_kind for c in calculations)
    assert next(c for c in cases if c.id == "cash-canonical").expect.totals == ["5988658.00"]
    assert len(read_cases("evals/onec/smoke-v1.jsonl")) == 12


def test_snapshot_rejects_writes_and_queries_outside_capture():
    import hashlib
    with TemporaryDirectory() as folder:
        metadata = b"<metadata/>"
        snapshot = {"metadata_sha256": hashlib.sha256(metadata).hexdigest(), "periods": [["2025-01-01", "2025-02-01"]],
                    "dimensions": {"organization": [{"id": ORG, "name": "Test"}]}, "records": []}
        (Path(folder) / "metadata.xml").write_bytes(metadata)
        (Path(folder) / "snapshot.json").write_text(json.dumps(snapshot))
        upstream = SnapshotUpstream(folder)
        with pytest.raises(ValueError):
            upstream(httpx.Request("DELETE", "https://snapshot.invalid/odata/register"))
        predicate = ("Period ge datetime'2024-01-01T00:00:00' and Period lt datetime'2024-02-01T00:00:00' "
                     f"and Организация_Key eq guid'{ORG}' and НаправлениеДеятельности_Key eq guid'{DIR}' "
                     "and Active eq true and ВидДвиженияДС eq 'Поступление'")
        from services.onec_data.models import MOVEMENTS
        with pytest.raises(ValueError):
            upstream(httpx.Request("GET", "https://snapshot.invalid/odata/" + MOVEMENTS, params={"$filter": predicate}))
        (Path(folder) / "metadata.xml").write_bytes(b"tampered")
        with pytest.raises(ValueError):
            SnapshotUpstream(folder)


def test_transport_failure_is_not_a_successful_policy_refusal():
    wanted = Expected(status="failed", no_calculations=True)
    result = AgentResult(status="failed", request_id="x", message="Ошибка модели",
                         error={"code": "MODEL_API_ERROR"})
    assert not grade(result, wanted)["passed"]
    result.error = {"code": "UNSUPPORTED_METRIC"}
    result.message = "Изменять данные нельзя."
    assert grade(result, wanted)["passed"]


def test_real_eval_routes_through_http_adapter_and_closes_clients():
    definition = {"metric_id": METRIC, "name": "Поступления", "approval": "demo", "semantic_version": "demo-v1",
                  "catalog_version": "catalog-v1", "supported_tools": ["get_cash_receipts_by_direction"],
                  "time_kind": "period", "allowed_group_by": [], "allowed_filters": [], "required_filters": {}}
    bootstrap = {"base_id": "itmo", "allowed_metric_ids": [METRIC], "allowed_entity_names": ["register"],
                 "organizations": [{"id": ORG, "name": "Организация"}], "directions": [{"id": DIR, "name": "Разработка ПО"}],
                 "catalog_version": "catalog-v1", "metric": definition}
    requests, clients = [], []
    def route(request):
        requests.append(request)
        if request.url.path == "/v1/bootstrap":
            return httpx.Response(200, json=bootstrap)
        body = json.loads(request.content)
        data = definition if body["operation"] == "get_metric_definition" else {"total": "0.30", "rows": [{"value": "0.30"}], "unit": "ед. учёта"}
        return httpx.Response(200, json={"data": data, "complete": True, "stats": {"rows": 2},
                                        "sources": [{"kind": "odata", "entity": "register"}]})
    def create(url, token):
        client = OneCDataBackend(url, token, transport=httpx.MockTransport(route))
        clients.append(client)
        return client
    case = EvalCase(id="http", question="Поступления за январь по разработке", category="calculation",
                    current_date=date(2026, 11, 16), selected_organization_id=ORG,
                    expect=Expected(status="completed", totals=["0.30"], units=["ед. учёта"]))
    import asyncio
    with TemporaryDirectory() as folder, patch("backend.integrations.onec_data_backend.OneCDataBackend", side_effect=create):
        summary = asyncio.run(evaluate(Planner(), [case], settings=AgentSettings(), output=Path(folder),
            provider_config={"model": "test-only"}, backend="onec", allow_demo_metrics=True))
        assert summary["passed"] == 1, (Path(folder) / "results.jsonl").read_text()
        manifest = json.loads((Path(folder) / "manifest.json").read_text())
        assert manifest["backend"] == "onec_live" and manifest["dataset_hash_kind"] == "catalog_only_not_rows"
        assert summary["by_category"]["calculation"]["passed"] == 1
        assert len(json.loads((Path(folder) / "results.jsonl").read_text())["backend_calls"]) == 2
    assert all(client.http.is_closed for client in clients)
    assert any(request.url.path == "/v1/call" for request in requests)
