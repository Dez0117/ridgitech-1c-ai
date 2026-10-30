import re
from pathlib import Path

from .models import AgentError, CatalogData, ExecutionContext


def words(text):
    return set(re.findall(r"[\w]+", text.casefold().replace("ё", "е")))


class Catalog:
    def __init__(self, path: Path):
        self.data = CatalogData.model_validate_json(path.read_text(encoding="utf-8"))
        self.entities = {e.entity_name: e for e in self.data.entities}
        self.metrics = {m.metric_id: m for m in self.data.metrics}
        if len(self.entities) != len(self.data.entities) or len(self.metrics) != len(self.data.metrics):
            raise ValueError("Duplicate catalog identifiers")
        for metric in self.metrics.values():
            entity = self.entities[metric.entity_name]
            required = [
                (metric.amount_field, "Edm.Decimal"),
                (metric.date_field, "Edm.DateTime"),
                (metric.organization_field, "Edm.String"),
                (entity.key_field, "Edm.String"),
                *((field, "Edm.String") for field in metric.dimensions.values()),
            ]
            if any(entity.fields.get(f) != t for f, t in required):
                raise ValueError("Metric fields do not match the catalog")
            for condition in metric.mandatory_filters:
                kind = entity.fields.get(condition.field)
                expected = "Edm.Boolean" if type(condition.value) is bool else "Edm.String"
                if kind != expected or condition.operator != "eq":
                    raise ValueError("Unsupported mandatory filter in this prototype")

    def check_context(self, context: ExecutionContext):
        if self.data.base_id != context.base_id:
            raise AgentError("ACCESS_DENIED", "Каталог не относится к выбранной базе.")

    def metric(self, metric_id, context):
        self.check_context(context)
        if metric_id not in context.allowed_metric_ids:
            raise AgentError("ACCESS_DENIED", "Метрика недоступна в текущем контексте.")
        if metric_id not in self.metrics:
            raise AgentError("METRIC_NOT_FOUND", "Метрика отсутствует в каталоге.")
        return self.metrics[metric_id]

    def search(self, query, context):
        self.check_context(context)
        query_words = words(query)
        matches = []
        for metric in self.metrics.values():
            if metric.metric_id not in context.allowed_metric_ids:
                continue
            # shortcut: exact tokens only; evaluate stemming/search on the agreed question set.
            score = len(query_words & words(" ".join([metric.name, *metric.synonyms])))
            if score:
                matches.append({"metric_id": metric.metric_id, "name": metric.name,
                                "description": metric.description, "approval": metric.approval,
                                "score": score})
        return sorted(matches, key=lambda m: (-m["score"], m["metric_id"]))[:5]

    def schema(self, entity_name, context):
        self.check_context(context)
        visible = {m.entity_name for m in self.metrics.values()
                   if m.metric_id in context.allowed_metric_ids}
        if entity_name not in visible or entity_name not in self.entities:
            raise AgentError("SOURCE_NOT_AVAILABLE", "Источник отсутствует в доступном каталоге.")
        return self.entities[entity_name]
