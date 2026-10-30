from pydantic import Field

from ..models import Model


class Arguments(Model):
    metric_id: str = Field(min_length=1, max_length=200)


def execute(tools, args: Arguments):
    metric = tools.catalog.metric(args.metric_id, tools.context)
    return {
        **metric.model_dump(mode="json"),
        "semantic_version": tools.catalog.data.semantic_version,
        "catalog_version": tools.catalog.data.catalog_version,
    }
