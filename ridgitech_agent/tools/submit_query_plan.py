import hashlib
import json
from datetime import date, datetime, time

from ..models import AgentError, ExecutionPlan, Filter, Model, QueryPlan, ReadStep


class Arguments(Model):
    plan: QueryPlan


def midnight(value: date):
    return datetime.combine(value, time()).isoformat()


def execute(tools, args: Arguments):
    plan = args.plan
    metric = tools.catalog.metric(plan.metric_id, tools.context)
    entity = tools.catalog.schema(metric.entity_name, tools.context)
    if (plan.semantic_version, plan.catalog_version) != (tools.catalog.data.semantic_version, tools.catalog.data.catalog_version):
        raise AgentError("STALE_PLAN", "Изменились версии каталога или метрики; получите определение заново.")
    if metric.approval != "approved" and not (metric.approval == "demo" and tools.context.demo_mode):
        raise AgentError("UNAPPROVED_METRIC", "Методология метрики не подтверждена.")
    if plan.organization_id not in tools.context.allowed_organization_ids:
        raise AgentError("ACCESS_DENIED", "Организация недоступна в текущем контексте.")
    if len(set(plan.group_by)) != len(plan.group_by) or any(d not in metric.dimensions for d in plan.group_by):
        raise AgentError("INVALID_PLAN", "Недопустимые или повторяющиеся разрезы.")
    if tools.accepted is not None:
        if tools.accepted.query == plan:
            return {"plan_id": tools.accepted.plan_id, "status": "accepted"}
        raise AgentError("PLAN_ALREADY_ACCEPTED", "В этом запросе уже принят другой план.")
    filters = [
        Filter(field=metric.organization_field, operator="eq", value=plan.organization_id),
        Filter(field=metric.date_field, operator="ge", value=midnight(plan.period.start)),
        Filter(field=metric.date_field, operator="lt", value=midnight(plan.period.end_exclusive)),
        *metric.mandatory_filters,
    ]
    group_fields = {name: metric.dimensions[name] for name in plan.group_by}
    select = sorted({entity.key_field, metric.amount_field, *(f.field for f in filters), *group_fields.values()})
    identity = json.dumps({"query": plan.model_dump(mode="json"), "base_id": tools.context.base_id,
                           "request_id": tools.context.request_id}, sort_keys=True)
    tools.accepted = ExecutionPlan(
        plan_id=hashlib.sha256(identity.encode()).hexdigest()[:24],
        request_id=tools.context.request_id, base_id=tools.context.base_id, query=plan,
        read=ReadStep(entity_name=metric.entity_name, select=select, filters=filters, order_by=entity.key_field),
        amount_field=metric.amount_field, group_fields=group_fields, aggregation=metric.aggregation,
        unit=metric.unit, decimal_places=metric.decimal_places,
    )
    return {"plan_id": tools.accepted.plan_id, "status": "accepted"}
