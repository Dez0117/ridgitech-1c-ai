import json
import time
from pathlib import Path
from typing import List, Literal, Protocol

from pydantic import Field, ValidationError

from .executor import execute, remaining
from .models import AgentError, AgentResult, ErrorInfo, Model
from .tools import Tools, tool_schemas


SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "system.md").read_text(encoding="utf-8")


class ToolCall(Model):
    name: str
    arguments: dict


class ModelTurn(Model):
    content: str = ""
    tool_calls: List[ToolCall] = Field(default_factory=list)


class TerminalMessage(Model):
    status: Literal["needs_clarification", "unsupported"]
    message: str = Field(min_length=1, max_length=2000)


class ChatModel(Protocol):
    def complete(self, messages: list, tools: list, *, timeout: float) -> ModelTurn:
        ...


class Agent:
    def __init__(self, catalog, adapter, model: ChatModel):
        self.catalog = catalog
        self.adapter = adapter
        self.model = model

    def run(self, question, conversation_context, execution_context) -> AgentResult:
        """Backend supplies history as text and constructs trusted execution_context.

        This synchronous method belongs in a worker/thread, not an async event loop.
        Model and data adapters must honour the remaining timeout.
        """
        context = execution_context
        started = time.monotonic()
        deadline = started + context.limits.timeout_seconds
        trace = []
        tools = None
        calls = 0
        demo_warning = ["ДЕМОНСТРАЦИЯ: синтетические данные, не отчётность Риджитех."] if context.demo_mode else []
        try:
            if not isinstance(question, str) or not question.strip() or len(question) > 10000:
                raise AgentError("INVALID_QUESTION", "Нужен непустой вопрос длиной до 10000 символов.")
            if not isinstance(conversation_context, str) or len(conversation_context) > 20000:
                raise AgentError("INVALID_CONTEXT", "История должна быть текстом длиной до 20000 символов.")
            tools = Tools(self.catalog, context)
            if self.adapter.base_id != context.base_id:
                raise AgentError("ACCESS_DENIED", "Адаптер не относится к выбранной базе.")
            visible_context = context.model_dump(mode="json", exclude={"limits", "request_id"})
            messages = [{"role": "system", "content": SYSTEM_PROMPT + "\nКонтекст backend:\n" + json.dumps(visible_context, ensure_ascii=False)},
                        {"role": "user", "content": json.dumps({"history": conversation_context, "question": question}, ensure_ascii=False)}]
            for turn in range(context.limits.max_model_turns):
                response = self.model.complete(messages, tool_schemas(), timeout=remaining(deadline))
                remaining(deadline)
                if response.tool_calls:
                    if calls + len(response.tool_calls) > context.limits.max_tool_calls:
                        raise AgentError("TOOL_BUDGET", "Исчерпан бюджет вызовов инструментов.")
                    messages.append({"role": "assistant", "content": response.content, "tool_calls": [
                        {"type": "function", "function": c.model_dump()} for c in response.tool_calls]})
                    for call in response.tool_calls:
                        remaining(deadline)
                        result = tools.dispatch(call.name, call.arguments)
                        calls += 1
                        trace.append({"tool": call.name, "arguments": call.arguments, "result": result})
                        messages.append({"role": "tool", "tool_name": call.name,
                                         "content": json.dumps(result, ensure_ascii=False)})
                        if tools.accepted:
                            plan = tools.accepted
                            schema = self.catalog.schema(plan.read.entity_name, context)
                            result = execute(plan, context, self.adapter, schema, deadline)
                            remaining(deadline)
                            trace.append({"step": "read_and_sum", "entity": plan.read.entity_name,
                                          "rows": result["row_count"], "pages": result["pages"],
                                          "odata_parameters_preview": result["request_preview"]})
                            amount = result["total"]
                            period = plan.query.period
                            message = (f"{amount} {plan.unit}" if amount is not None else "Данных за период нет")
                            message += (f"; организация {plan.query.organization_id}; период "
                                        f"[{period.start}, {period.end_exclusive}); источник {plan.read.entity_name}.")
                            return AgentResult(status="completed", request_id=context.request_id, message=message,
                                query_plan=plan, data=result["data"], sources=[plan.read.entity_name],
                                warnings=demo_warning + result["warnings"], trace=trace,
                                stats={"rows": result["row_count"], "pages": result["pages"], "tool_calls": calls,
                                       "model_turns": turn + 1, "elapsed_seconds": time.monotonic() - started})
                else:
                    try:
                        terminal = TerminalMessage.model_validate_json(response.content)
                    except ValidationError:
                        raise AgentError("MODEL_PROTOCOL", "Модель не вернула допустимый план или уточнение.")
                    unsupported = terminal.status == "unsupported"
                    return AgentResult(status="failed" if unsupported else "needs_clarification",
                        request_id=context.request_id, message=terminal.message, trace=trace, warnings=demo_warning,
                        error=ErrorInfo(code="UNSUPPORTED_METRIC", message=terminal.message) if unsupported else None,
                        stats={"tool_calls": calls, "model_turns": turn + 1, "elapsed_seconds": time.monotonic() - started})
            raise AgentError("MODEL_BUDGET", "Не удалось составить допустимый план за отведённое число попыток.")
        except TimeoutError:
            error = AgentError("TIMEOUT", "Превышено время ожидания модели или источника; повторите запрос позже.")
        except OSError:
            error = AgentError("UPSTREAM_UNAVAILABLE", "Модель или источник недоступны; повторите запрос позже.")
        except AgentError as exc:
            error = exc
        return AgentResult(status="failed", request_id=context.request_id, message=error.message,
            query_plan=tools.accepted if tools else None, warnings=demo_warning, trace=trace,
            error=ErrorInfo(code=error.code, message=error.message),
            stats={"tool_calls": calls, "elapsed_seconds": time.monotonic() - started})
