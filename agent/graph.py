import asyncio
import hashlib
import json
import re
import time
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langsmith import tracing_context
from pydantic import ValidationError

from tools import ToolContext, create_tools
from tools._models import QueryPlan
from tools._runtime import canonical
from .models import AgentResult, AgentSettings, TerminalMessage


DEFAULT_PROMPT = Path(__file__).parent / "prompts" / "system.md"
PLANNING = {"search_metrics", "get_metric_definition", "get_entity_schema", "resolve_dimension", "submit_query_plan"}


class State(MessagesState):
    plan: dict | None
    results: dict
    batch: list[str]
    trace: list[dict]
    model_calls: int
    usage: dict
    cost_usd: str | None
    cost_reports: int
    error: dict | None


def text_content(message):
    if isinstance(message.content, str):
        return message.content
    return "".join(block.get("text", "") for block in message.content
                   if isinstance(block, dict) and block.get("type") == "text")


def error(code, message, **details):
    return {"code": code, "message": message, **details}


def tool_responses(messages):
    responses = []
    for message in reversed(messages):
        if not isinstance(message, ToolMessage):
            break
        try:
            payload = json.loads(text_content(message))
        except (ValueError, TypeError):
            payload = {"ok": False, "error": error("UNKNOWN_TOOL", "Недопустимый вызов инструмента.")}
        responses.append((message, payload))
    return list(reversed(responses))


def render_results(plan, records):
    comparisons = []
    sections = []
    for record in records:
        data = record["data"]
        if not isinstance(data, dict) or not isinstance(data.get("unit"), str) or not data["unit"]:
            raise ValueError("Отсутствует единица результата.")
        if not isinstance(data.get("rows"), list) or any(not isinstance(row, dict) for row in data["rows"]):
            raise ValueError("Некорректная таблица результата.")
        amounts = [row.get("value") for row in data["rows"]]
        if data.get("total") is not None:
            amounts.append(data["total"])
        elif data["rows"]:
            raise ValueError("Отсутствует итог непустой выборки.")
        if any(type(value) is not str or not re.fullmatch(r"-?\d{1,28}(\.\d{1,6})?", value, flags=re.ASCII)
               for value in amounts):
            raise ValueError("Денежные значения должны быть точными десятичными строками.")
    units = {record["data"].get("unit") for record in records if isinstance(record["data"], dict)} - {None}
    if len(units) > 1:
        raise ValueError("Единицы результатов различаются; сравнение невозможно.")
    for record in records:
        data = record["data"]
        arguments = record["arguments"]
        period = arguments.get("period")
        when = (f"[{period['start']}, {period['end_exclusive']})" if period else arguments.get("as_of", ""))
        title = {"get_sales_turnovers": "Обороты продаж", "get_top_counterparties": "Топ контрагентов",
                 "get_cash_receipts_by_direction": "Поступления денег", "get_customer_balances": "Задолженность покупателей"}[record["tool"]]
        label = f"{title}; организация {plan['organization_id']}; {when}"
        if isinstance(data, dict) and data.get("rows") == []:
            sections.append(label + ": данных нет.")
        elif isinstance(data, dict) and record["tool"] == "get_top_counterparties":
            rows = data.get("rows", [])
            sections.append(label + ":\n" + "\n".join(
                f"{i}. {row.get('counterparty_name', row.get('counterparty_id', ''))}: {row['value']} {data['unit']}"
                for i, row in enumerate(rows, 1)))
        elif isinstance(data, dict) and data.get("total") is not None:
            sections.append(label + f": {data['total']} {data.get('unit', '')}.")
        else:
            sections.append(label + ": результат в таблице.")
    if len(records) == 2 and records[0]["tool"] == records[1]["tool"]:
        first, second = (record["data"] for record in records)
        if all(isinstance(d, dict) and d.get("total") is not None for d in [first, second]):
            with localcontext() as ctx:
                ctx.prec = 50
                left, right = Decimal(first["total"]), Decimal(second["total"])
                if not left.is_finite() or not right.is_finite():
                    raise ValueError("Некорректное денежное значение.")
                delta = (left - right).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                percent = ((delta / right * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                           if right else None)
                comparisons.append({"first_step": records[0]["step_id"], "second_step": records[1]["step_id"],
                                    "delta": str(delta), "percent": str(percent) if percent is not None else None,
                                    "unit": first.get("unit")})
                sections.append(f"Первый результат минус второй: {delta} {first.get('unit', '')}"
                                + (f" ({percent}%)." if percent is not None else "; процент к нулевой базе не определён."))
    return "\n".join(sections), comparisons


class AnalyticsAgent:
    def __init__(self, model: BaseChatModel, backend, *, settings=None, prompt=None):
        self.model, self.backend = model, backend
        self.settings = settings or AgentSettings()
        self.prompt = prompt if prompt is not None else DEFAULT_PROMPT.read_text(encoding="utf-8")

    async def arun(self, question: str, context: ToolContext, *, current_date: date,
                   timezone="Europe/Moscow", selected_organization_id=None, history="",
                   on_event: Callable[[str], None] | None = None) -> AgentResult:
        started = time.monotonic()
        deadline = started + context.timeout_seconds
        tools = create_tools(self.backend, context)
        planning_tools = [tool for tool in tools if tool.name in PLANNING]
        calculation_tools = [tool for tool in tools if tool.name not in PLANNING]
        schemas = [convert_to_openai_tool(tool) for tool in tools]
        prompt_hash = hashlib.sha256(self.prompt.encode()).hexdigest()
        tools_hash = hashlib.sha256(canonical(schemas).encode()).hexdigest()

        def result_failure(code, message):
            return AgentResult(status="failed", request_id=context.request_id, message=message,
                error=error(code, message), prompt_hash=prompt_hash, tools_hash=tools_hash,
                elapsed_seconds=time.monotonic() - started)

        if not isinstance(question, str) or not question.strip() or len(question) > 10000:
            return result_failure("INVALID_QUESTION", "Нужен непустой вопрос длиной до 10000 символов.")
        if not isinstance(history, str) or len(history) > 20000:
            return result_failure("INVALID_CONTEXT", "История диалога слишком велика.")
        if selected_organization_id is not None and selected_organization_id not in context.allowed_organization_ids:
            return result_failure("ACCESS_DENIED", "Выбранная организация недоступна.")
        ZoneInfo(timezone)
        model = self.model.bind_tools(planning_tools)

        def remaining():
            value = deadline - time.monotonic()
            if value <= 0:
                raise TimeoutError
            return value

        def emit(stage):
            if on_event:
                on_event(stage)

        async def call_model(state):
            emit("planning")
            if state["model_calls"] >= self.settings.max_model_turns:
                return {"error": error("MODEL_BUDGET", "Не удалось составить план за отведённое число попыток.")}
            try:
                async with asyncio.timeout(remaining()):
                    response = await model.ainvoke(state["messages"])
            except TimeoutError:
                return {"error": error("TIMEOUT", "Превышено общее время обработки."), "model_calls": state["model_calls"] + 1}
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                return {"error": error("MODEL_API_ERROR", "Ошибка вызова модели; проверьте доступ и лимиты API.",
                                       kind=type(exc).__name__, status_code=status), "model_calls": state["model_calls"] + 1}
            if not isinstance(response, AIMessage):
                return {"error": error("MODEL_PROTOCOL", "Модель вернула некорректное сообщение."),
                        "model_calls": state["model_calls"] + 1}
            usage = dict(state["usage"])
            for key in ["input_tokens", "output_tokens", "total_tokens"]:
                usage[key] = usage.get(key, 0) + (response.usage_metadata or {}).get(key, 0)
            cost = state["cost_usd"]
            cost_reports = state["cost_reports"]
            reported_usage = response.response_metadata.get("token_usage") or response.response_metadata.get("usage") or {}
            reported_cost = response.response_metadata.get("cost", reported_usage.get("cost"))
            if reported_cost is not None:
                cost = str(Decimal(cost or "0") + Decimal(str(reported_cost)))
                cost_reports += 1
            trace = state["trace"] + [{"event": "model", "text": text_content(response),
                "tool_calls": response.tool_calls, "usage": response.usage_metadata,
                "metadata": {k: response.response_metadata[k] for k in
                    ["id", "model_name", "model_provider", "provider", "system_fingerprint", "finish_reason"] if k in response.response_metadata}}]
            update = {"messages": [response], "model_calls": state["model_calls"] + 1,
                      "usage": usage, "cost_usd": cost, "cost_reports": cost_reports, "trace": trace}
            if response.invalid_tool_calls:
                update["error"] = error("MODEL_PROTOCOL", "Модель вернула некорректный вызов инструмента.")
            elif response.response_metadata.get("finish_reason") in {"length", "max_tokens"}:
                update["error"] = error("MODEL_OUTPUT_LIMIT", "Модель исчерпала лимит выходных токенов.")
            return update

        async def inspect_plan(state):
            emit("validating_plan")
            trace = list(state["trace"])
            accepted = state["plan"]
            failure = None
            for message, payload in tool_responses(state["messages"]):
                trace.append({"event": "tool", "name": message.name, "call_id": message.tool_call_id, "result": payload})
                if message.name == "submit_query_plan" and payload.get("ok"):
                    accepted = QueryPlan.model_validate_json(canonical(payload["data"]["plan"])).model_dump(mode="json")
                elif payload.get("error", {}).get("code") in {
                    "TIMEOUT", "TOOL_BUDGET", "API_UNAVAILABLE", "INVALID_API_RESPONSE", "INVALID_DEFINITION",
                    "UNAPPROVED_METRIC", "ACCESS_DENIED",
                }:
                    failure = payload["error"]
            return {"trace": trace, "plan": accepted, "error": failure}

        async def prepare_execution(state):
            emit("executing")
            done = set(state["results"])
            ready = [step for step in state["plan"]["steps"] if step["step_id"] not in done
                     and set(step["depends_on"]) <= done][:self.settings.max_parallel_steps]
            if not ready:
                return {"error": error("INVALID_PLAN", "В плане не осталось доступных для исполнения шагов.")}
            return {"batch": [step["step_id"] for step in ready], "messages": [AIMessage(content="", tool_calls=[
                {"name": step["tool"], "args": step["arguments"], "id": "exec-" + step["step_id"], "type": "tool_call"}
                for step in ready])]}

        async def inspect_execution(state):
            emit("checking_result")
            records, trace = dict(state["results"]), list(state["trace"])
            steps = {step["step_id"]: step for step in state["plan"]["steps"]}
            failure = None
            responses = tool_responses(state["messages"])
            if len(responses) != len(state["batch"]):
                return {"error": error("INCOMPLETE_DATA", "Не получены результаты всех шагов.")}
            for message, payload in responses:
                step_id = message.tool_call_id.removeprefix("exec-")
                trace.append({"event": "calculation", "step_id": step_id, "name": message.name, "result": payload})
                if payload.get("ok") is not True:
                    failure = payload.get("error") or error("TOOL_ERROR", "Не удалось выполнить расчёт.")
                else:
                    step = steps[step_id]
                    records[step_id] = {"step_id": step_id, "tool": step["tool"], "arguments": step["arguments"], **payload}
            return {"results": records, "trace": trace, "error": failure}

        builder = StateGraph(State)
        builder.add_node("planner", call_model)
        builder.add_node("planning_tools", ToolNode(planning_tools))
        builder.add_node("inspect_plan", inspect_plan)
        builder.add_node("prepare_execution", prepare_execution)
        builder.add_node("calculation_tools", ToolNode(calculation_tools))
        builder.add_node("inspect_execution", inspect_execution)
        builder.add_edge(START, "planner")
        builder.add_conditional_edges("planner", lambda s: END if s["error"] or not s["messages"][-1].tool_calls else "planning_tools")
        builder.add_edge("planning_tools", "inspect_plan")
        builder.add_conditional_edges("inspect_plan", lambda s: END if s["error"] else "prepare_execution" if s["plan"] else "planner")
        builder.add_conditional_edges("prepare_execution", lambda s: END if s["error"] else "calculation_tools")
        builder.add_edge("calculation_tools", "inspect_execution")
        builder.add_conditional_edges("inspect_execution", lambda s: END if s["error"] or len(s["results"]) == len(s["plan"]["steps"]) else "prepare_execution")
        graph = builder.compile()

        trusted = {"base_id": context.base_id, "current_date": current_date.isoformat(), "timezone": timezone,
                   "selected_organization_id": selected_organization_id}
        system = self.prompt + "\nКонтекст backend:\n" + canonical(trusted)
        system += "\nРасчётные операции для шагов плана:\n" + canonical([schema for schema in schemas if schema["function"]["name"] not in PLANNING])
        initial = {"messages": [SystemMessage(content=system), HumanMessage(content=canonical({"question": question, "history": history}))],
                   "plan": None, "results": {}, "batch": [], "trace": [], "model_calls": 0, "usage": {},
                   "cost_usd": None, "cost_reports": 0, "error": None}
        with tracing_context(enabled=False):
            state = initial
            try:
                async with asyncio.timeout(remaining()):
                    async for snapshot in graph.astream(initial,
                            {"recursion_limit": self.settings.max_model_turns * 3 + 30}, stream_mode="values"):
                        state = snapshot
            except TimeoutError:
                state = {**state, "error": error("TIMEOUT", "Превышено общее время обработки; сузьте запрос.")}
        records = [state["results"][step["step_id"]] for step in (state["plan"] or {}).get("steps", [])
                   if step["step_id"] in state["results"]]
        common = {"request_id": context.request_id, "plan": state["plan"], "trace": state["trace"],
                  "usage": state["usage"],
                  "cost_usd": state["cost_usd"] if state["cost_reports"] == state["model_calls"] else None,
                  "model_calls": state["model_calls"],
                  "elapsed_seconds": time.monotonic() - started, "prompt_hash": prompt_hash, "tools_hash": tools_hash}
        if state["error"]:
            return AgentResult(status="failed", message=state["error"]["message"], error=state["error"], **common)
        if state["plan"]:
            try:
                message, comparisons = render_results(state["plan"], records)
            except (ValueError, InvalidOperation, KeyError, TypeError):
                problem = error("INVALID_RESULT", "Результат не прошёл проверку единиц или числовых значений.")
                return AgentResult(status="failed", message=problem["message"], error=problem, **common)
            sources = {canonical(source): source for record in records for source in record.get("sources", [])}
            warnings = list(dict.fromkeys(w for record in records for w in record.get("warnings", [])))
            return AgentResult(status="completed", message=message, results=records, comparisons=comparisons,
                               sources=list(sources.values()), warnings=warnings, **common)
        try:
            terminal = TerminalMessage.model_validate_json(text_content(state["messages"][-1]))
        except ValidationError:
            problem = error("MODEL_PROTOCOL", "Ожидался план или структурированный уточняющий вопрос.")
            return AgentResult(status="failed", message=problem["message"], error=problem, **common)
        unsupported = terminal.status == "unsupported"
        return AgentResult(status="failed" if unsupported else "needs_clarification", message=terminal.message,
            error=error("UNSUPPORTED_METRIC", terminal.message) if unsupported else None, **common)
