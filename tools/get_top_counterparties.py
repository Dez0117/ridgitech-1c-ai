from pydantic import Field

from ._models import FilterValue, PeriodQuery


class Arguments(PeriodQuery):
    limit: int = Field(ge=1, le=100)
    filters: dict[str, FilterValue] = Field(default_factory=dict)


async def execute(runtime, args: Arguments):
    return await runtime.business("get_top_counterparties", args.model_dump(mode="json"))
