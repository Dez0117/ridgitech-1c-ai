from pydantic import Field

from ._models import FilterValue, PeriodQuery


class Arguments(PeriodQuery):
    direction_id: str = Field(min_length=1, max_length=200)
    filters: dict[str, FilterValue] = Field(default_factory=dict)


async def execute(runtime, args: Arguments):
    return await runtime.business("get_cash_receipts_by_direction", args.model_dump(mode="json"))
