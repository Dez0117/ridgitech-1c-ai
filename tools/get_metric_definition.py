from pydantic import Field

from ._models import Arguments as BaseArguments


class Arguments(BaseArguments):
    metric_id: str = Field(min_length=1, max_length=200)


async def execute(runtime, args: Arguments):
    return await runtime.read("get_metric_definition", args.model_dump(mode="json"))
