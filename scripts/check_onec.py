"""Сквозная проверка HTTP, инструментов и графа на реальной 1С без внешней LLM."""

import argparse
import asyncio
from datetime import date
from decimal import Decimal
import json
import os
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch
from dotenv import load_dotenv

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from agent.graph import AnalyticsAgent
from backend.integrations.onec_data_backend import OneCDataBackend
from backend.integrations.agent_service import AgentService
from backend.config import settings
from tools import BackendError


class CheckPlanner(BaseChatModel):
    """Возвращает заранее подготовленный план для проверки выполнения графа."""

    plan: dict

    @property
    def _llm_type(self):
        return "local-check"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        message = AIMessage(content="", tool_calls=[{"id": "check-plan", "name": "submit_query_plan", "args": {"plan": self.plan}, "type": "tool_call"}])
        return ChatResult(generations=[ChatGeneration(message=message)])


async def check(args):
    backend = OneCDataBackend(args.url, os.environ.get("ONEC_DATA_TOKEN", ""))
    try:
        info = await backend.bootstrap()
        context = await backend.context(uuid4().hex, allow_demo_metrics=args.allow_demo)
        definition = (await backend.call("get_metric_definition", {"metric_id": info.metric["metric_id"]}, context=context, timeout=60)).data
        directions = (await backend.call("resolve_dimension", {"dimension": "direction", "query": args.direction}, context=context, timeout=60)).data
        if len(directions) != 1:
            raise ValueError("Направление отсутствует или неоднозначно")
        organizations = [row for row in info.organizations if not args.organization or row["id"] == args.organization]
        if not organizations:
            raise ValueError("Доступная организация не найдена")
        results = []
        for organization in organizations:
            parameters = {
                "metric_id": definition["metric_id"], "organization_id": organization["id"],
                "direction_id": directions[0]["id"],
                "period": {"start": args.start, "end_exclusive": args.end_exclusive}, "filters": {},
            }
            direct = await backend.call("get_cash_receipts_by_direction", parameters, context=context, timeout=60)
            plan = {
                "metric_id": definition["metric_id"], "organization_id": organization["id"],
                "semantic_version": definition["semantic_version"], "catalog_version": definition["catalog_version"],
                "steps": [{"step_id": "receipts", "tool": "get_cash_receipts_by_direction", "arguments": parameters}],
            }
            result = await AnalyticsAgent(CheckPlanner(plan=plan), backend).arun(
                "Контрольный расчёт поступлений", context, current_date=date.today(),
                selected_organization_id=organization["id"],
            )
            if result.status != "completed" or not result.results or result.results[0]["data"] != direct.data:
                raise ValueError("Сквозная проверка графа не пройдена: " + result.message)
            with patch.object(settings, "analytics_backend", "onec"), patch.object(settings, "onec_data_url", args.url), patch.object(settings, "onec_data_token", os.environ.get("ONEC_DATA_TOKEN", "")), patch.object(settings, "onec_allow_demo_metrics", args.allow_demo), patch("backend.integrations.agent_service.create_qwen_model", return_value=CheckPlanner(plan=plan)):
                integrated = await AgentService().run("Контрольный расчёт поступлений", organization_id=organization["id"], history="")
            if integrated.status != "completed" or integrated.results[0]["data"] != direct.data:
                raise ValueError("Проверка подключения AgentService не пройдена: " + integrated.message)
            results.append({"organization": organization, "data": direct.data, "stats": direct.stats, "sources": direct.sources, "warnings": direct.warnings, "agent_status": result.status, "backend_integration_status": integrated.status})
        values = [Decimal(row["data"]["total"]) for row in results if row["data"]["total"] is not None]
        total = str(sum(values, Decimal(0))) if values else None
        print(json.dumps({"status": "ok", "planner": "Контрольный план, без вызова внешней LLM", "direction": directions[0], "period": {"start": args.start, "end_exclusive": args.end_exclusive}, "results": results, "total_across_checked_organizations": total}, ensure_ascii=False, indent=2))
    finally:
        await backend.close()


if __name__ == "__main__":
    load_dotenv(Path.cwd() / ".env", override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8011")
    parser.add_argument("--start", default="2025-01-01")
    parser.add_argument("--end-exclusive", default="2025-02-01")
    parser.add_argument("--direction", default="Разработка ПО")
    parser.add_argument("--organization")
    parser.add_argument("--allow-demo", action="store_true", help="Явно разрешить демонстрационную методику")
    try:
        asyncio.run(check(parser.parse_args()))
    except BackendError as exc:
        message = f"{exc.code}: {exc}"
        if exc.details:
            message += "\nДиагностика: " + json.dumps(exc.details, ensure_ascii=False)
        if exc.code == "API_UNAVAILABLE":
            message += (
                "\nВ отдельном терминале из корня проекта запустите сервис:\n"
                ".venv/bin/python -m services.onec_data\n"
                "Введите пароль OData и дождитесь сообщения Uvicorn running.\n"
                "Оставьте этот терминал открытым и повторите проверку во втором терминале."
            )
        parser.exit(1, message + "\n")
    except ValueError as exc:
        parser.exit(1, str(exc) + "\n")
