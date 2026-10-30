from typing import Literal

from pydantic import Field

from ._models import Arguments as BaseArguments


class Arguments(BaseArguments):
    dimension: Literal["organization", "counterparty", "direction"]
    query: str = Field(min_length=1, max_length=200)


async def execute(runtime, args: Arguments):
    result = await runtime.read("resolve_dimension", args.model_dump(mode="json"))
    if not isinstance(result["data"], list):
        raise ValueError("Expected dimension candidates")
    if args.dimension == "organization":
        result["data"] = [item for item in result["data"]
                          if isinstance(item, dict) and item.get("id") in runtime.context.allowed_organization_ids]
    return result
