"""Explicit tool registry. Each tool owns its arguments, handler and instructions."""

import json
from pathlib import Path
from typing import Any, Dict

from pydantic import ValidationError

from ..models import AgentError
from . import get_entity_schema, get_metric_definition, search_metrics, submit_query_plan


TOOL_MODULES = {
    "search_metrics": search_metrics,
    "get_metric_definition": get_metric_definition,
    "get_entity_schema": get_entity_schema,
    "submit_query_plan": submit_query_plan,
}
INSTRUCTIONS = {
    name: Path(module.__file__).with_suffix(".md").read_text(encoding="utf-8")
    for name, module in TOOL_MODULES.items()
}


def tool_schemas():
    return [
        {"type": "function", "function": {
            "name": name,
            "description": INSTRUCTIONS[name],
            "parameters": module.Arguments.model_json_schema(),
        }}
        for name, module in TOOL_MODULES.items()
    ]


class Tools:
    """Create once per request. Backend supplies the catalog and trusted context."""

    def __init__(self, catalog, context):
        catalog.check_context(context)
        self.catalog = catalog
        self.context = context
        self.accepted = None

    def dispatch(self, name: str, arguments: Dict[str, Any]):
        if name not in TOOL_MODULES:
            return {"ok": False, "error": {"code": "UNKNOWN_TOOL", "message": "Неизвестный tool."}}
        module = TOOL_MODULES[name]
        try:
            args = module.Arguments.model_validate_json(json.dumps(arguments, allow_nan=False))
            return {"ok": True, "result": module.execute(self, args)}
        except ValidationError as exc:
            return {"ok": False, "error": {
                "code": "INVALID_ARGUMENTS", "message": "Аргументы не соответствуют схеме.",
                "details": [{"field": ".".join(map(str, e["loc"])), "reason": e["type"]}
                            for e in exc.errors()],
            }}
        except AgentError as exc:
            return {"ok": False, "error": {"code": exc.code, "message": exc.message}}
        except (TypeError, ValueError):
            return {"ok": False, "error": {"code": "INVALID_ARGUMENTS", "message": "Ожидается корректный JSON."}}


__all__ = ["Tools", "tool_schemas"]
