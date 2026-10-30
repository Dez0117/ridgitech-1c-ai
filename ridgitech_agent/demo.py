import argparse
import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from .agent import Agent, ModelTurn, ToolCall
from .catalog import Catalog
from .executor import DemoAdapter
from .models import ExecutionContext, Limits


FIXTURES = Path(__file__).parent / "fixtures"


class ScriptedDemoModel:
    """Fixed scenarios for reproducible checks. This is a model stub, not an LLM."""

    def __init__(self, scenario):
        self.scenario = scenario

    def complete(self, messages, tools, *, timeout):
        results = [m for m in messages if m["role"] == "tool"]
        if not results:
            return ModelTurn(tool_calls=[ToolCall(name="search_metrics", arguments={
                "query": "прибыль" if self.scenario == "unsupported" else "продажи"})])
        if self.scenario == "unsupported":
            return ModelTurn(content=json.dumps({"status": "unsupported", "message": "Прибыль пока не поддерживается в тестовом каталоге."}))
        if self.scenario == "clarification":
            return ModelTurn(content=json.dumps({"status": "needs_clarification", "message": "По какой организации посчитать продажи за январь 2025?"}))
        if len(results) == 1:
            return ModelTurn(tool_calls=[ToolCall(name="get_metric_definition", arguments={"metric_id": "demo_sales"}),
                                         ToolCall(name="get_entity_schema", arguments={"entity_name": "DemoSales"})])
        return ModelTurn(tool_calls=[ToolCall(name="submit_query_plan", arguments={"plan": {
            "metric_id": "demo_sales", "semantic_version": "demo-1", "catalog_version": "demo-1",
            "organization_id": "demo-org", "period": {"start": "2025-01-01", "end_exclusive": "2025-02-01"},
            "group_by": ["direction"] if self.scenario == "grouped" else []}})])


def demo_context(**overrides):
    return ExecutionContext(request_id=str(uuid4()), base_id="demo", allowed_metric_ids=["demo_sales"],
        allowed_organization_ids=["demo-org"], current_date=datetime.now(ZoneInfo("Europe/Moscow")).date(),
        timezone="Europe/Moscow", demo_mode=True, **overrides)


def main():
    parser = argparse.ArgumentParser(description="Прототип агента на синтетических данных; без подключения к 1С.")
    parser.add_argument("--scenario", choices=["success", "grouped", "clarification", "unsupported", "limit"], default="success")
    parser.add_argument("--ollama-model", help="Вместо сценарной заглушки вызвать локальную LLM с поддержкой tools")
    parser.add_argument("--question", default="Посчитай тестовую сумму продаж за январь 2025 по организации demo-org")
    args = parser.parse_args()
    if args.ollama_model:
        from .ollama import OllamaModel
        model = OllamaModel(args.ollama_model)
    else:
        model = ScriptedDemoModel(args.scenario)
    limits = Limits(max_rows=2 if args.scenario == "limit" else 1000, page_size=2)
    agent = Agent(Catalog(FIXTURES / "catalog.json"), DemoAdapter(FIXTURES / "rows.json"), model)
    print(agent.run(args.question, "", demo_context(limits=limits)).model_dump_json(indent=2))


if __name__ == "__main__":
    main()
