from pydantic import Field

from ._models import Arguments as BaseArguments


class Arguments(BaseArguments):
    query: str = Field(min_length=1, max_length=2000)


async def execute(runtime, args: Arguments):
    result = await runtime.read("search_metrics", args.model_dump(mode="json"))
    if not isinstance(result["data"], list):
        raise ValueError("Expected metric candidates")
    result["data"] = [item for item in result["data"]
                      if isinstance(item, dict) and item.get("metric_id") in runtime.context.allowed_metric_ids][:5]
    return result
