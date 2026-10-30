from pydantic import Field

from ._models import FilterValue, PeriodQuery


class Arguments(PeriodQuery):
    group_by: list[str] = Field(default_factory=list, max_length=3)
    filters: dict[str, FilterValue] = Field(default_factory=dict)


async def execute(runtime, args: Arguments):
    return await runtime.business("get_sales_turnovers", args.model_dump(mode="json"))
