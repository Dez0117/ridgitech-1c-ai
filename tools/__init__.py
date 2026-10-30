"""Request-scoped LangChain tools with Markdown instructions and typed arguments."""

from pathlib import Path

from langchain_core.tools import BaseTool, StructuredTool

from . import (
    get_cash_receipts_by_direction, get_customer_balances, get_entity_schema,
    get_metric_definition, get_sales_turnovers, get_top_counterparties,
    resolve_dimension, search_metrics, submit_query_plan,
)
from ._models import BackendResult
from ._runtime import AnalyticsBackend, BackendError, Runtime, ToolContext


MODULES = {
    "search_metrics": search_metrics,
    "get_metric_definition": get_metric_definition,
    "get_entity_schema": get_entity_schema,
    "resolve_dimension": resolve_dimension,
    "submit_query_plan": submit_query_plan,
    "get_sales_turnovers": get_sales_turnovers,
    "get_top_counterparties": get_top_counterparties,
    "get_cash_receipts_by_direction": get_cash_receipts_by_direction,
    "get_customer_balances": get_customer_balances,
}


def create_tools(backend: AnalyticsBackend, context: ToolContext) -> list[BaseTool]:
    """Create a fresh registry per request, then pass it to bind_tools / ToolNode."""
    runtime = Runtime(backend, context, MODULES)

    def bind(name):
        async def call(**arguments):
            return await runtime.invoke(name, arguments)
        return call

    return [StructuredTool.from_function(
        name=name,
        description=Path(module.__file__).with_suffix(".md").read_text(encoding="utf-8"),
        args_schema=module.Arguments.model_json_schema(),
        coroutine=bind(name),
    ) for name, module in MODULES.items()]


__all__ = ["AnalyticsBackend", "BackendError", "BackendResult", "ToolContext", "create_tools"]
