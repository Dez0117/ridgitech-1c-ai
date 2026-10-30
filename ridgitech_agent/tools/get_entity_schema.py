from pydantic import Field

from ..models import Model


class Arguments(Model):
    entity_name: str = Field(min_length=1, max_length=200)


def execute(tools, args: Arguments):
    return tools.catalog.schema(args.entity_name, tools.context).model_dump(mode="json")
