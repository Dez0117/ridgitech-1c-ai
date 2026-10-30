import asyncio
import hashlib

from ._models import Arguments as BaseArguments, QueryPlan
from ._runtime import BackendError, canonical


class Arguments(BaseArguments):
    plan: QueryPlan


async def execute(runtime, args: Arguments):
    plan = args.plan
    payload = plan.model_dump(mode="json")
    async with runtime.plan_lock:
        if runtime.plan is not None:
            if runtime.plan == payload:
                return {"data": {"status": "accepted", "plan_id": runtime.plan_id}}
            raise BackendError("PLAN_ALREADY_ACCEPTED", "В этом запросе уже принят другой план.")
        runtime.check_scope(payload)
        response = await runtime.read("get_metric_definition", {"metric_id": plan.metric_id})
        definition = response["data"]
        if not isinstance(definition, dict) or definition.get("metric_id") != plan.metric_id:
            raise BackendError("INVALID_DEFINITION", "API вернул определение другой метрики.")
        if definition.get("approval") != "approved":
            raise BackendError("UNAPPROVED_METRIC", "Методология метрики требует согласования.")
        if (plan.semantic_version, plan.catalog_version) != (
                definition.get("semantic_version"), definition.get("catalog_version")):
            raise BackendError("STALE_PLAN", "Обновите определение метрики и версию каталога.")
        signatures = set()
        steps = {}
        for step in plan.steps:
            normalized = runtime.validate(runtime.modules[step.tool], step.arguments).model_dump(mode="json")
            runtime.check_scope(normalized)
            if (normalized["metric_id"], normalized["organization_id"]) != (plan.metric_id, plan.organization_id):
                raise BackendError("PLAN_MISMATCH", "Метрика и организация шага отличаются от плана.")
            if step.tool not in definition.get("supported_tools", []):
                raise BackendError("UNSUPPORTED_OPERATION", "Операция не разрешена определением метрики.")
            time_kind = "as_of" if step.tool == "get_customer_balances" else "period"
            if definition.get("time_kind") != time_kind:
                raise BackendError("INVALID_PLAN", "Параметры времени не соответствуют определению метрики.")
            groups = normalized.get("group_by", [])
            if len(set(groups)) != len(groups) or not set(groups) <= set(definition.get("allowed_group_by", [])):
                raise BackendError("INVALID_PLAN", "Недопустимый разрез метрики.")
            filters = normalized.get("filters", {})
            if not set(filters) <= set(definition.get("allowed_filters", [])):
                raise BackendError("INVALID_PLAN", "Недопустимый фильтр метрики.")
            required = definition.get("required_filters", {})
            if any(key in filters and (type(filters[key]) is not type(value) or filters[key] != value)
                   for key, value in required.items()):
                raise BackendError("INVALID_PLAN", "Обязательные фильтры метрики нельзя переопределять.")
            signature = canonical({"tool": step.tool, "arguments": normalized})
            if signature in signatures:
                raise BackendError("INVALID_PLAN", "План содержит повторяющиеся расчётные шаги.")
            signatures.add(signature)
            steps[step.step_id] = {**step.model_dump(mode="json"), "arguments": normalized}
        identity = {"request_id": runtime.context.request_id, "base_id": runtime.context.base_id, "plan": payload}
        runtime.plan_id = hashlib.sha256(canonical(identity).encode()).hexdigest()[:24]
        runtime.plan = payload
        runtime.steps = steps
        runtime.step_locks = {step_id: asyncio.Lock() for step_id in steps}
        return {"data": {"status": "accepted", "plan_id": runtime.plan_id}}
