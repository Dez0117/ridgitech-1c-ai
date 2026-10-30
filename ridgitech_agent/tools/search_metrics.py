from pydantic import Field

from ..models import Model


class Arguments(Model):
    query: str = Field(min_length=1, max_length=2000)


def execute(tools, args: Arguments):
    return tools.catalog.search(args.query, tools.context)
