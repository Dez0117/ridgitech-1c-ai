from pydantic import Field

from ._models import Arguments as BaseArguments


class Arguments(BaseArguments):
    entity_name: str = Field(min_length=1, max_length=200)


async def execute(runtime, args: Arguments):
    result = await runtime.read("get_entity_schema", args.model_dump(mode="json"))
    if not isinstance(result["data"], dict) or result["data"].get("entity_name") != args.entity_name:
        raise ValueError("API returned a different entity schema")
    return result
